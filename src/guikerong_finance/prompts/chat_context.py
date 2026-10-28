"""Bounded chat projections; stored evidence, documents and conversations stay intact."""

import json
from copy import deepcopy

from guikerong_finance.errors import DomainError
from guikerong_finance.prompts.loader import prompt_text
from guikerong_finance.prompts.matching_context import MATCHING_INTERPRETATION_POLICY
from guikerong_finance.service.history import read_request
from guikerong_finance.service.supplements import applicable_documents, original_contains

CHAT_SYSTEM = prompt_text("chat_system")

CHAT_SYSTEM += MATCHING_INTERPRETATION_POLICY + "\n上述口径仅用于解释；本次仍只返回 reply，不改写已保存的匹配结果。"


def _fields(value, keys):
    return {key: deepcopy(value[key]) for key in keys if key in value and value[key] not in (None, "", [])}


def _knowledge_context(knowledge, product_id):
    items: dict = {}
    for pid, entries in knowledge.items():
        if product_id and pid != product_id:
            continue
        for entry in entries:
            item = items.setdefault(
                entry["id"],
                {
                    **_fields(entry, ("id", "title", "content", "product_id", "condition_quote")),
                    "applicable_products": [],
                },
            )
            item["applicable_products"].append({"product_id": pid, **_fields(entry, ("matched_condition",))})
    return list(items.values())


def build_chat_context(
    request, results, knowledge, *, product=None, condition_quote="", historical_run_id=None, overview=False
):
    """Retain complete source documents and every condition; never mutate saved results."""
    documents = deepcopy(request)
    if product is not None:
        applicable = {d.document_id for d in applicable_documents(read_request(request), product)}
        documents["documents"] = [d for d in documents["documents"] if d["document_id"] in applicable]
    original_documents = {d["document_id"]: d["content"] for d in documents["documents"]}
    evidence: dict = {}
    evidence_keys: dict = {}
    products = []
    for result in results:
        if product is not None and result["product_id"] != product.product_id:
            continue
        value = _fields(
            result,
            (
                "product_id",
                "product_name",
                "institution",
                "application_conditions",
                "decision",
                "run_id",
                "evaluated_at",
                "stale",
                "stale_reasons",
                "validation_warnings",
            ),
        )
        value["latest_attempt"] = _fields(
            result.get("latest_attempt") or {}, ("status", "run_id", "message", "error_code")
        )
        keys: tuple[str, ...] = ("status", "assessment_basis", "verification_needs")
        if not overview:
            keys += ("explanation", "logic_expression", "subject_id", "reason_code")

        def judgement(source, *, condition=False, keys=keys, result=result):
            compact = _fields(source, keys)
            if condition:
                compact.update(
                    _fields(source, ("condition_id", "source_quote", "source_quote_verified", "required_material"))
                )
            if not overview:
                references = []
                for identifier in source.get("evidence_ids", []):
                    saved = result.get("evidence", {}).get(identifier)
                    if not saved:
                        continue
                    ref = _fields(saved, ("document_id", "line_start", "line_end", "subject_id"))
                    ref.update(evidence_id=identifier, run_id=result.get("run_id"))
                    # References belong to the saved run. If that passage is no longer in
                    # current documents, retain the old quote explicitly as historical evidence.
                    if not original_contains(original_documents.get(saved["document_id"], ""), saved.get("quote", "")):
                        ref["historical_quote"] = saved.get("quote", "")
                    else:
                        # Editing a document may move an unchanged passage to different
                        # lines. Keep its exact quote once, rather than pointing at new text
                        # using the saved run's old line numbers.
                        ref["quote"] = saved.get("quote", "")
                    signature = json.dumps(ref, ensure_ascii=False, sort_keys=True)
                    key = evidence_keys.get(signature)
                    if key is None:
                        key = f"ref{len(evidence) + 1}"
                        evidence_keys[signature] = key
                        evidence[key] = ref
                    references.append(key)
                if references:
                    compact["evidence_ids"] = references
            return compact

        eligibility = result["eligibility"]
        value["eligibility"] = judgement(eligibility)
        value["eligibility"]["condition_results"] = [
            judgement(c, condition=True) for c in eligibility["condition_results"]
        ]
        if result.get("human_reviews"):
            # Distinguish human decisions even when detailed reasons are omitted for overview.
            fields: tuple[str, ...] = ("reviewer", "status", "created_at", "verification_needs")
            if not overview:
                fields += ("reason", "evidence")
            value["human_reviews"] = {cid: _fields(review, fields) for cid, review in result["human_reviews"].items()}
        if not overview:
            value.update(_fields(result, ("match_score", "match_breakdown")))
            feedback = (result.get("model_assessment") or {}).get("supplement_feedback")
            if feedback:
                value["supplement_feedback"] = deepcopy(feedback)
            continuation = result.get("continuation")
            if continuation:
                value["continuation"] = _fields(
                    continuation,
                    (
                        "base_run_id",
                        "before",
                        "after",
                        "unpaired_previous_count",
                        "added_document_ids",
                        "updated_document_ids",
                        "removed_document_ids",
                    ),
                )
                value["continuation"]["changes"] = [
                    _fields(c, ("condition_id", "before", "after")) for c in continuation.get("changes", [])
                ]
        products.append(value)
    context = {
        "company_documents": documents,
        "current_result": products,
        "knowledge": _knowledge_context(knowledge, product.product_id if product else None),
        "context_projection": {
            "result_detail": "overview" if overview else "detailed",
            "note": (
                "企业资料和知识正文完整保留；全部产品和条款均保留原文、状态、核验类型及补件要求。"
                "为适应预算，本次未提供详细判断理由和证据引用，不得声称已读取完整历史解释；"
                "如需某产品的详细解释，请用户在输入框选择该产品后提问。"
                if overview
                else "企业资料和知识正文完整保留；匹配结果移除重复模型回答和界面字段。"
                "evidence 的行号属于原评估轮次，不能当作修订后资料的当前行号；以 quote 核对当前正文。"
                "当前正文已不存在的旧引用单独标为 historical_quote。"
            ),
        },
    }
    if evidence:
        context["evidence"] = evidence
    if historical_run_id:
        context["historical_run_id"] = historical_run_id
    else:
        context["result_stale"] = any(p.get("stale", False) for p in products)
    if product is not None:
        context["focus"] = {
            "product_id": product.product_id,
            "product_name": product.product_name,
            "condition_quote": condition_quote,
        }
    return context


def _complete_turns(history, product_name):
    """Keep complete question/answer pairs; saved facts already live in source documents."""
    turns, pending = [], None
    for message in history:
        if message["role"] == "user":
            pending = message
        elif message["role"] == "assistant" and pending is not None:
            label = pending.get("context_label", "")
            if (
                not message.get("run_id")
                and not label.startswith(("补充事实", "历史轮次提问"))
                and message.get("context_label", "") == label
                and (not product_name or " · " not in label or label == f"提问 · {product_name}")
            ):
                pair = [{"role": m["role"], "content": m["content"]} for m in (pending, message)]
                if label:
                    pair[0]["content"] = f"[历史提问范围：{label}]\n{pair[0]['content']}"
                turns.append(pair)
            pending = None
    return turns


def build_chat_messages(system, context, history, message, budget):
    """Use the newest complete turns that fit; the new question is never shortened."""
    context = deepcopy(context)
    turns = _complete_turns(history, context.get("focus", {}).get("product_name"))
    selected: list = []

    def messages_for(chosen):
        retained = sum(len(turn) for turn in chosen)
        context["conversation_window"] = {
            "retained_messages": retained,
            "omitted_messages": len(history) - retained,
            "note": "仅携带预算内最近完整问答；较早、未完成、其他范围或已保存为资料的消息可能省略，"
            "原会话记录仍完整保存。",
        }
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False, separators=(",", ":"))},
            *[m for turn in chosen for m in turn],
            {"role": "user", "content": message},
        ]

    messages = messages_for(selected)
    if sum(len(m["content"]) for m in messages) > budget:
        raise DomainError(
            "CLOUD_CONTEXT_TOO_LARGE",
            "当前提问的资料范围超过模型预算，请在输入框选择具体产品后重试；"
            "若已选择产品，请在企业资料中精简重复材料。原会话和记录均保留。",
            422,
        )
    for turn in reversed(turns):
        candidate = messages_for([turn, *selected])
        if sum(len(m["content"]) for m in candidate) > budget:
            break
        selected.insert(0, turn)
        messages = candidate
    return messages
