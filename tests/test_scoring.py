"""Equal-weight scoring uses validated conditions, not the model's overall verdict."""

import pytest

from app.models import ConditionResult, EvaluationResult, Status
from app.scoring import calculate_match_score


def evaluation(statuses, overall="UNKNOWN"):
    return EvaluationResult(
        status=overall, logic_expression="模型综合判断", explanation="总体资格与条件匹配度分别展示。",
        condition_results=[ConditionResult(
            condition_id=f"c{index}", source_field="application_conditions", source_quote=f"条件{index}",
            subject_id="company", status=status, reason_code="MODEL_JUDGEMENT", explanation=f"该项为{status}。",
        ) for index, status in enumerate(statuses)],
    )


@pytest.mark.parametrize("statuses,expected_score,matched,unmatched,unknown", [
    (["PASS", "PASS", "PASS", "FAIL", "UNKNOWN"], 60.0, 3, 1, 1),
    (["PASS", "FAIL", "UNKNOWN"], 33.3, 1, 1, 1),
    (["PASS", "PASS", "UNKNOWN"], 66.7, 2, 0, 1),
    (["PASS", "PASS", "PASS"], 100.0, 3, 0, 0),
    (["FAIL", "FAIL"], 0.0, 0, 2, 0),
    (["UNKNOWN", "UNKNOWN"], 0.0, 0, 0, 2),
    ([], 0.0, 0, 0, 0),
])
def test_equal_weight_score_and_breakdown(statuses, expected_score, matched, unmatched, unknown):
    value = evaluation(statuses, overall="FAIL")
    score, breakdown = calculate_match_score(value)
    assert score == expected_score
    assert breakdown.total_conditions == len(statuses)
    assert breakdown.matched_conditions == matched
    assert breakdown.unmatched_conditions == unmatched
    assert breakdown.unknown_conditions == unknown
    assert breakdown.method == "equal_weight"
    points = 100 / len(statuses) if statuses else 0
    assert breakdown.points_per_condition == pytest.approx(points)
    for condition, status in zip(value.condition_results, statuses, strict=True):
        assert condition.score_contribution == pytest.approx(points if status == "PASS" else 0)
        assert condition.score_deduction == pytest.approx(0 if status == "PASS" else points)
    if statuses:
        assert sum(item.score_contribution + item.score_deduction for item in value.condition_results) == pytest.approx(100)
    assert value.status == "FAIL"


def test_score_rounds_the_ratio_once_with_half_up_precision():
    value = evaluation(["PASS"] + ["UNKNOWN"] * 15)
    score, breakdown = calculate_match_score(value)
    assert score == 6.3
    assert breakdown.points_per_condition == 6.25
    assert value.condition_results[0].score_contribution == 6.25
    assert value.condition_results[-1].score_deduction == 6.25


def test_recalculation_replaces_old_contributions_and_is_idempotent():
    value = evaluation(["PASS", "PASS", "PASS"])
    assert calculate_match_score(value)[0] == 100
    value.condition_results[0].status = Status.UNKNOWN
    score, breakdown = calculate_match_score(value)
    assert score == 66.7
    assert value.condition_results[0].score_contribution == 0
    assert value.condition_results[0].score_deduction == pytest.approx(100 / 3)
    snapshot = value.model_dump()
    assert calculate_match_score(value) == (score, breakdown)
    assert value.model_dump() == snapshot
