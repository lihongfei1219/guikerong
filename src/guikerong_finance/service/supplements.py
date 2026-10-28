"""Scope human evidence and validate adoption feedback."""

import hashlib
import json

from guikerong_finance.models.domain import SupplementFeedback


def original_contains(text, quote):
    """Textarea line endings may differ; preserve every other original character."""
    return quote.replace("\r\n", "\n").replace("\r", "\n") in text.replace("\r\n", "\n").replace("\r", "\n")


def scope_status(document, product):
    info = document.human_supplement
    if not info:
        return "ACTIVE"
    if info.product_id and info.product_id != product.product_id:
        return "OUT_OF_SCOPE"
    if info.product_id and info.application_conditions != product.application_conditions:
        return "CONDITIONS_CHANGED"
    return "ACTIVE"


def applicable_documents(request, product):
    return [doc for doc in request.documents if scope_status(doc, product) == "ACTIVE"]


def input_fingerprint(request, product):
    payload = {
        "company": request.company.model_dump(mode="json"),
        "documents": [
            d.model_dump(
                mode="json",
                exclude={"human_supplement"} if d.human_supplement and d.human_supplement.legacy else set(),
            )
            for d in applicable_documents(request, product)
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def clause_in_scope(info, condition):
    quote = info.condition_quote
    return (
        not quote
        or original_contains(condition.source_quote, quote)
        or original_contains(quote, condition.source_quote)
    )


def feedback_for(assessment, request, product):
    """Return complete feedback plus reasons requiring professional recheck.

    A reported adoption must actually cite that supplement in the claimed conditions.
    No missing or malformed feedback is silently presented as successful adoption.
    """
    supplied = {}
    duplicates = set()
    warnings = []
    for item in assessment.supplement_feedback:
        if item.document_id in supplied:
            duplicates.add(item.document_id)
            warnings.append("模型重复报告同一人工补充的处理结果。")
        supplied[item.document_id] = item
    documents = {d.document_id: d for d in request.documents if d.human_supplement}
    if set(supplied) - set(documents):
        warnings.append("模型报告了不存在的人工补充。")
    conditions = {c.condition_id: c for c in assessment.conditions}
    feedback = []
    for document_id, doc in documents.items():
        scope = scope_status(doc, product)
        if scope != "ACTIVE":
            feedback.append(
                SupplementFeedback(
                    document_id=document_id,
                    status=scope,
                    reason=(
                        "该补充仅适用于其他产品，未发送给本产品模型。"
                        if scope == "OUT_OF_SCOPE"
                        else "补充绑定的申请条件已变化，本轮未采用，请核对后重新补充。"
                    ),
                )
            )
            continue
        item = supplied.get(document_id)
        cited_by = {
            c.condition_id for c in assessment.conditions if any(ref.document_id == document_id for ref in c.citations)
        }
        overall_cited = any(ref.document_id == document_id for ref in assessment.eligibility.citations)
        invalid = not item or document_id in duplicates or bool(set(item.condition_ids) - set(conditions))
        if item and item.status == "ADOPTED":
            invalid |= (
                not item.condition_ids
                or not item.impact
                or not set(item.condition_ids).issubset(cited_by)
                or any(
                    not clause_in_scope(doc.human_supplement, conditions[cid])
                    for cid in item.condition_ids
                    if cid in conditions
                )
                or doc.human_supplement.verification != "CONFIRMED"
            )
        elif item and item.status == "NOT_USED" and (cited_by or overall_cited):
            invalid = True
        if item and item.status in {"OUT_OF_SCOPE", "CONDITIONS_CHANGED"}:
            invalid = True
        if invalid or (item and item.status == "NOT_REPORTED"):
            item = SupplementFeedback(
                document_id=document_id,
                status="NOT_REPORTED",
                reason="模型未提供可核对的采纳反馈，请重新匹配或人工核对；不代表补充事实无效。",
            )
            warnings.append("人工补充的采纳反馈缺失或与条款引用不一致。")
        feedback.append(item)
    return feedback, list(dict.fromkeys(warnings))
