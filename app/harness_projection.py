"""会话及评估的只读投影：从事件还原展示状态，不回写历史记录。"""

import hashlib
import json
from copy import deepcopy

from app.continuation import comparisons_for
from app.errors import DomainError
from app.history import read_knowledge, read_request, read_result
from app.models import HumanSupplement, Status, VerificationNeed
from app.product_status import classify, needs_for, product_sort_key, required_actions
from app.scoring import calculate_match_score
from app.supplements import input_fingerprint


def replay_session(identifier: str, events: list[dict]) -> dict:
    """仅从事件重建会话，兼容旧补充来源；不访问数据库或模型。"""
    if not events or events[0]["kind"] != "session/created":
        raise DomainError("SESSION_NOT_FOUND", "会话不存在。", 404)
    request = read_request(events[0]["data"])
    state = {"id": identifier, "version": events[-1]["version"], "messages": [],
             "input_revision": 1, "run": None, "reviews": [], "events": events}
    previous = None
    for event in events[1:]:
        data = event["data"]
        if event["kind"] == "session/continued":
            state["origin"] = data
        elif event["kind"] == "baseline/imported":
            state["run"] = data
        elif event["kind"] == "documents/added":
            previous_docs = {d.document_id: d for d in request.documents}
            request = read_request(data)
            for doc in request.documents:
                if doc.human_supplement:
                    continue
                old = previous_docs.get(doc.document_id)
                if old:
                    doc.human_supplement = old.human_supplement
                else:
                    # 旧补充的来源由事件推导，不能根据文件名猜测，也不回写旧事件。
                    doc.human_supplement = HumanSupplement(
                        recorded_at=event["created_at"], legacy=True,
                    )
            state["input_revision"] = event["version"]
        elif event["kind"] in {"documents/updated", "documents/deleted"}:
            request = read_request(data)
            state["input_revision"] = event["version"]
        elif event["kind"] in {"user/message", "assistant/message"}:
            message = {"role": event["kind"].split("/")[0], "content": data["content"]}
            if data.get("run_id"):
                message["run_id"] = data["run_id"]
            elif event["kind"] == "assistant/message" and previous and previous["kind"] == "match/completed":
                message["run_id"] = previous["data"]["id"]
            if data.get("context_label"):
                message["context_label"] = data["context_label"]
            state["messages"].append(message)
        elif event["kind"] == "match/completed":
            state["run"] = data
        elif event["kind"] == "review/recorded":
            state["reviews"].append({**data, "created_at": event["created_at"]})
        previous = event
    state["request"] = request.model_dump(mode="json")
    return state


def effective_matches(state: dict) -> list[dict]:
    """在模型结果副本上按顺序叠加人工复核，原始评估始终保留。"""
    if not state["run"]:
        return []
    result = read_result(state["run"]["result"])
    rendered = []
    for product in result.matches:
        reviews = [r for r in state["reviews"] if r["product_id"] == product.product_id
                   and r["run_id"] == state["run"]["id"]]
        human = {}
        last_condition_review = -1
        last_overall_review = -1
        for index, review in enumerate(reviews):
            if review["status"] == "UNKNOWN" and not review.get("verification_needs"):
                product.validation_warnings.append("历史人工复核未记录待核验类型，请重新确认核验原因。")
            human[review["condition_id"]] = review
            if review["condition_id"] == "__eligibility__":
                last_overall_review = index
                _apply_review(product.eligibility, review)
            else:
                last_condition_review = index
                for condition in product.eligibility.condition_results:
                    if condition.condition_id == review["condition_id"]:
                        _apply_review(condition, review)
                        condition.reason_code = "HUMAN_REVIEW"
                        condition.required_material = review["reason"] if review["status"] == "UNKNOWN" else None
        overall_review_needed = last_condition_review > last_overall_review
        # 单条复核晚于整体复核时，必须重新确认整体资格，不能机械合并条款结论。
        if overall_review_needed:
            product.eligibility.status = Status.UNKNOWN
            product.eligibility.assessment_basis = "EVIDENCE"
            product.eligibility.verification_needs = [VerificationNeed.PROFESSIONAL_REVIEW]
            product.eligibility.explanation = "条款已有人工修改，需重新综合确认整体资格；不能由单条结论自动推断。"
            product.eligibility.evidence_ids = []
        product.match_score, product.match_breakdown = calculate_match_score(product.eligibility)
        product.decision, product.verification_needs = classify(product.eligibility)
        product.required_actions = required_actions(product.eligibility)
        value = product.model_dump(mode="json")
        value["human_reviews"] = human
        value["overall_review_needed"] = overall_review_needed
        if overall_review_needed:
            value["human_reviews"].pop("__eligibility__", None)
        rendered.append(value)
    return sorted(rendered, key=product_sort_key)


def _apply_review(judgement, review):
    """统一覆盖条款或整体的公共字段，人工依据单独展示，不冒充模型原文引用。"""
    judgement.status = Status(review["status"])
    judgement.assessment_basis = "EVIDENCE"
    judgement.verification_needs = needs_for(review["status"], review.get("verification_needs"))
    judgement.explanation = review["reason"]
    judgement.evidence_ids = []


def fingerprint(items):
    content = json.dumps(sorted((read_knowledge(item) for item in items), key=lambda item: item["id"]),
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content.encode()).hexdigest()


def runs_from(events):
    """按发生顺序聚合各轮输入和结果；导入基线先复制，避免后续聚合修改事件。"""
    runs = {}
    for event in events:
        data = event["data"]
        kind = event["kind"]
        if kind == "baseline/imported":
            runs[data["id"]] = deepcopy(data)
        elif kind == "match/requested":
            snapshot = {**data, "request": read_request(data["request"]).model_dump(mode="json")}
            if "knowledge" in snapshot:
                snapshot["knowledge"] = {pid: [read_knowledge(item) for item in items]
                                         for pid, items in snapshot["knowledge"].items()}
            runs[data["id"]] = {"id": data["id"], "snapshot": snapshot, "status": "INCOMPLETE",
                                "created_at": event["created_at"], "result": None}
        elif kind == "match/completed":
            run = runs.setdefault(data["id"], {"id": data["id"], "snapshot": {},
                                               "created_at": event["created_at"]})
            run.update(data, status="COMPLETED", completed_at=event["created_at"])
        elif kind == "turn/failed" and data["id"] in runs:
            runs[data["id"]].update(status="FAILED", error_code=data["code"])
    return runs


def attempts_for(run):
    attempts = {p["product_id"]: {"product_id": p["product_id"], "product_name": p["product_name"],
                                  "run_id": run["id"], "status": run["status"],
                                  "created_at": run["created_at"], "error_code": run.get("error_code")}
                for p in run["snapshot"].get("catalog", {}).get("products", [])}
    result = run.get("result") or {}
    for group, status in (("matches", "SUCCEEDED"), ("failed_products", "FAILED"),
                          ("unavailable_products", "UNAVAILABLE")):
        for product in result.get(group, []):
            attempts[product["product_id"]] = {
                "product_id": product["product_id"], "product_name": product["product_name"],
                "run_id": run["id"], "status": status, "created_at": run["created_at"],
                "message": product.get("message") or product.get("reason"),
            }
    return attempts


def summary(run):
    return {"id": run["id"], "created_at": run["created_at"], "status": run["status"],
            "attempts": list(attempts_for(run).values())}


def project(state, catalog=None, knowledge=None):
    """按产品选取最近成功结果，并结合资料、知识和最新尝试计算过期及复核状态。"""
    runs = runs_from(state["events"])
    all_reviews = state["reviews"]
    latest = {}
    attempts = {}
    counts = {}
    for run in runs.values():
        run_attempts = attempts_for(run)
        attempts.update(run_attempts)
        for pid in run_attempts:
            counts[pid] = counts.get(pid, 0) + 1
        if run.get("result"):
            comparisons = comparisons_for(run)
            for product in effective_matches({"run": run, "reviews": all_reviews}):
                product.update(run_id=run["id"], evaluated_at=run.get("completed_at", run["created_at"]),
                               input_revision=run.get("input_revision"), continuation=comparisons.get(product["product_id"]))
                evidence_ids = set(product["eligibility"]["evidence_ids"])
                for condition in product["eligibility"]["condition_results"]:
                    evidence_ids.update(condition["evidence_ids"])
                product["evidence"] = {key: value for key, value in run["result"].get("evidence", {}).items()
                                       if key in evidence_ids}
                latest[product["product_id"]] = product
    current = {p.product_id: p for p in catalog.products} if catalog is not None else None
    current_request = read_request(state["request"]) if current is not None and latest else None
    for pid, product in latest.items():
        run = runs[product["run_id"]]
        snapshot = run["snapshot"]
        reasons = []
        saved_fingerprint = snapshot.get("input_fingerprints", {}).get(pid)
        if snapshot.get("request") and current is not None and pid in current:
            changed_input = (input_fingerprint(read_request(snapshot["request"]), current[pid]) !=
                             input_fingerprint(current_request, current[pid]))
        elif saved_fingerprint and current is not None and pid in current:
            changed_input = saved_fingerprint != input_fingerprint(current_request, current[pid])
        else:
            changed_input = product["input_revision"] != state["input_revision"]
        if changed_input:
            reasons.append("企业资料已更新")
        if current is not None:
            if pid not in current or current[pid].application_conditions != product["application_conditions"]:
                reasons.append("产品已移除或申请条件已更新")
            if "knowledge" not in snapshot:
                reasons.append("历史知识快照不完整")
            elif fingerprint(snapshot["knowledge"].get(pid, [])) != fingerprint((knowledge or {}).get(pid, [])):
                reasons.append("适用专业知识已更新或停用")
        product.update(stale=bool(reasons), stale_reasons=reasons, history_count=counts.get(pid, 1),
                       knowledge_fingerprint=fingerprint(snapshot.get("knowledge", {}).get(pid, [])),
                       latest_attempt=attempts.get(pid))
        product["can_review"] = not reasons and attempts.get(pid, {}).get("status") == "SUCCEEDED"
    state["effective_matches"] = sorted(latest.values(), key=product_sort_key)
    state["reviews"] = [r for r in all_reviews if r["product_id"] in latest
                        and latest[r["product_id"]]["run_id"] == r["run_id"]]
    state["stale"] = any(p["stale"] for p in latest.values())
    state["runs"] = [summary(run) for run in reversed(list(runs.values()))]
    state["product_attempts"] = list(attempts.values())
    state["result_counts"] = {"total": len(latest), "current": sum(not p["stale"] for p in latest.values()),
                              "stale": sum(p["stale"] for p in latest.values())}


def run_detail(state, run_id):
    """读取指定轮次的冻结输入及复核结果，不混入后续材料。"""
    runs = runs_from(state["events"])
    if run_id not in runs:
        raise DomainError("RUN_NOT_FOUND", "匹配轮次不存在。", 404)
    run = runs[run_id]
    reviews = [{**e["data"], "created_at": e["created_at"]} for e in state["events"]
               if e["kind"] == "review/recorded" and e["data"]["run_id"] == run_id]
    products = effective_matches({"run": run, "reviews": reviews}) if run.get("result") else []
    comparisons = comparisons_for(run)
    for product in products:
        product.update(run_id=run_id, can_review=False, evidence=run["result"].get("evidence", {}),
                       evaluated_at=run.get("completed_at", run["created_at"]),
                       continuation=comparisons.get(product["product_id"]))
    return {**summary(run), "snapshot": run["snapshot"], "result": run.get("result"),
            "effective_matches": products, "reviews": reviews, "read_only": True,
            "can_continue": bool(run["snapshot"].get("request") and (run.get("result") or {}).get("matches"))}
