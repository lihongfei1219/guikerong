"""Read old persisted snapshots without rewriting audit events or keeping retired APIs."""

from copy import deepcopy

from app.models import MatchRequest, MatchResponse, ProductDecision, Status, VerificationNeed
from app.product_status import classify, required_actions


def read_request(data):
    snapshot = deepcopy(data)
    for document in snapshot.get("documents", []):
        supplement = document.get("human_supplement")
        if supplement:
            for key in ("author", "source", "fact_date", "kind", "corrects_document_id", "corrects_quote"):
                supplement.pop(key, None)
    return MatchRequest.model_validate({key: value for key, value in snapshot.items()
                                       if key not in {"top_n", "include_diagnostics"}})


def read_result(data):
    snapshot = deepcopy(data)
    for key in ("recommendations", "pending_verification", "not_matched", "diagnostics_included"):
        snapshot.pop(key, None)
    for product in snapshot["matches"]:
        product.pop("recommendation_reasons", None)
        overall = product["eligibility"]
        for judgement in [overall, *overall["condition_results"]]:
            if judgement["status"] == "UNKNOWN" and not judgement.get("verification_needs"):
                judgement["verification_needs"] = [VerificationNeed.PROFESSIONAL_REVIEW.value]
                warning = "历史结果未记录待核验类型，暂交专业人员确认原因；可重新匹配获取分类。"
                warnings = product.setdefault("validation_warnings", [])
                if warning not in warnings:
                    warnings.append(warning)
        product["decision"] = {"PASS": "FULLY_SATISFIED", "UNKNOWN": "PENDING_VERIFICATION",
                               "FAIL": "NOT_ELIGIBLE"}[overall["status"]]
        if overall["status"] == Status.UNKNOWN and not product.get("verification_needs"):
            product["verification_needs"] = overall["verification_needs"]
    summary = snapshot["summary"]
    for key in ("recommended_total", "returned_count"):
        summary.pop(key, None)
    for key, decision in (("fully_satisfied_count", ProductDecision.FULLY_SATISFIED),
                          ("pending_count", ProductDecision.PENDING_VERIFICATION),
                          ("not_matched_count", ProductDecision.NOT_ELIGIBLE)):
        summary[key] = sum(product["decision"] == decision for product in snapshot["matches"])
    result = MatchResponse.model_validate(snapshot)
    for product in result.matches:
        product.decision, product.verification_needs = classify(product.eligibility)
        product.required_actions = required_actions(product.eligibility)
    return result
