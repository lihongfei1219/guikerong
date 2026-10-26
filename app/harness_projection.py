"""Read-only projections of saved evaluations; never rewrite audit events."""

import hashlib
import json

from app.errors import DomainError
from app.history import read_request
from app.product_status import product_sort_key
from app.supplements import input_fingerprint


def fingerprint(items):
    content = json.dumps(sorted(items, key=lambda item: item["id"]), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content.encode()).hexdigest()


def runs_from(events):
    runs = {}
    for event in events:
        data = event["data"]
        kind = event["kind"]
        if kind == "match/requested":
            snapshot = {**data, "request": read_request(data["request"]).model_dump(mode="json")}
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


def project(state, render, catalog=None, knowledge=None):
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
            for product in render({"run": run, "reviews": all_reviews}):
                product.update(run_id=run["id"], evaluated_at=run.get("completed_at", run["created_at"]),
                               input_revision=run.get("input_revision"))
                evidence_ids = set(product["eligibility"]["evidence_ids"])
                for condition in product["eligibility"]["condition_results"]:
                    evidence_ids.update(condition["evidence_ids"])
                product["evidence"] = {key: value for key, value in run["result"].get("evidence", {}).items()
                                       if key in evidence_ids}
                latest[product["product_id"]] = product
    current = {p.product_id: p for p in catalog.products} if catalog is not None else None
    for pid, product in latest.items():
        run = runs[product["run_id"]]
        snapshot = run["snapshot"]
        reasons = []
        saved_fingerprint = snapshot.get("input_fingerprints", {}).get(pid)
        if snapshot.get("request") and current is not None and pid in current:
            changed_input = (input_fingerprint(read_request(snapshot["request"]), current[pid]) !=
                             input_fingerprint(read_request(state["request"]), current[pid]))
        elif saved_fingerprint and current is not None and pid in current:
            changed_input = saved_fingerprint != input_fingerprint(read_request(state["request"]), current[pid])
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
                reasons.append("适用专业知识已更新、停用或过期")
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


def run_detail(state, run_id, render):
    runs = runs_from(state["events"])
    if run_id not in runs:
        raise DomainError("RUN_NOT_FOUND", "匹配轮次不存在。", 404)
    run = runs[run_id]
    reviews = [{**e["data"], "created_at": e["created_at"]} for e in state["events"]
               if e["kind"] == "review/recorded" and e["data"]["run_id"] == run_id]
    products = render({"run": run, "reviews": reviews}) if run.get("result") else []
    for product in products:
        product.update(run_id=run_id, can_review=False, evidence=run["result"].get("evidence", {}),
                       evaluated_at=run.get("completed_at", run["created_at"]))
    return {**summary(run), "snapshot": run["snapshot"], "result": run.get("result"),
            "effective_matches": products, "reviews": reviews, "read_only": True}
