"""Product qualification is an overall decision, never inferred from a partial score."""

from app.models import EvaluationResult, ProductDecision, RequiredAction, Status, VerificationNeed


def needs_for(status, needs):
    if status != Status.UNKNOWN:
        return []
    return sorted({VerificationNeed(need) for need in needs or [VerificationNeed.PROFESSIONAL_REVIEW]})


def classify(result: EvaluationResult):
    decision = {Status.PASS: ProductDecision.FULLY_SATISFIED, Status.UNKNOWN: ProductDecision.PENDING_VERIFICATION,
                Status.FAIL: ProductDecision.NOT_ELIGIBLE}[result.status]
    if result.status != Status.UNKNOWN:
        return decision, []
    needs = list(result.verification_needs)
    for condition in result.condition_results:
        needs.extend(needs_for(condition.status, condition.verification_needs))
    return decision, needs_for(result.status, needs)


def required_actions(result: EvaluationResult):
    actions = [RequiredAction(
        condition_id=c.condition_id, field_id=c.field_id, subject_id=c.subject_id, reason_code=c.reason_code,
        material=c.required_material or c.explanation, verification_needs=needs_for(c.status, c.verification_needs),
    ) for c in result.condition_results if c.status == Status.UNKNOWN]
    if result.status == Status.UNKNOWN:
        actions.append(RequiredAction(condition_id="__eligibility__", subject_id="company",
                                      reason_code="OVERALL_REVIEW", material=result.explanation,
                                      verification_needs=classify(result)[1]))
    return actions


def product_sort_key(item):
    decision = item["decision"] if isinstance(item, dict) else item.decision
    score = item["match_score"] if isinstance(item, dict) else item.match_score
    return ({ProductDecision.FULLY_SATISFIED: 0, ProductDecision.PENDING_VERIFICATION: 1,
             ProductDecision.NOT_ELIGIBLE: 2}[decision], -score)
