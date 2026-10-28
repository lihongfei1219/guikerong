"""Transparent condition coverage, independent of overall application approval."""

from decimal import ROUND_HALF_UP, Decimal

from guikerong_finance.models.domain import EvaluationResult, MatchBreakdown, Status


def calculate_match_score(result: EvaluationResult) -> tuple[float, MatchBreakdown]:
    """Score validated conditions equally and annotate each one's contribution.

    FAIL and UNKNOWN both earn zero, but remain separate in counts and explanations.
    No unverified verdict or evidence can be turned into a passing condition here.
    Repeated calculation overwrites annotations rather than accumulating deductions.
    """
    conditions = result.condition_results
    total = len(conditions)
    matched = sum(condition.status == Status.PASS for condition in conditions)
    unmatched = sum(condition.status == Status.FAIL for condition in conditions)
    unknown = sum(condition.status == Status.UNKNOWN for condition in conditions)
    weight = 100.0 / total if total else 0.0
    for condition in conditions:
        condition.score_contribution = weight if condition.status == Status.PASS else 0.0
        condition.score_deduction = 0.0 if condition.status == Status.PASS else weight
    score = float((Decimal(100) * matched / total).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)) if total else 0.0
    return score, MatchBreakdown(
        total_conditions=total,
        matched_conditions=matched,
        unmatched_conditions=unmatched,
        unknown_conditions=unknown,
        points_per_condition=weight,
    )
