"""投影可脱离数据库重放，不能修改原始事件或导入基线。"""

from copy import deepcopy

from guikerong_finance.service.harness_projection import project, replay_session, run_detail, runs_from
from tests.test_harness import create, environment, match, review, sample_catalog

__all__ = ["environment"]


def test_replay_and_review_projection_leave_saved_events_unchanged(environment):
    client, _, calls, application = environment
    matched = match(client, create(client))
    reviewed = review(client, matched, condition="__eligibility__").json()
    events = application.state.harness.store.read(reviewed["id"])
    original = deepcopy(events)
    call_count = len(calls)

    for _ in range(2):
        state = replay_session(reviewed["id"], events)
        project(state, sample_catalog(), {})
        assert state["effective_matches"] == reviewed["effective_matches"]
        detail = run_detail(state, matched["run"]["id"])
        assert detail["result"] == matched["run"]["result"]
        assert detail["effective_matches"][0]["human_reviews"]
        assert events == original
    assert len(calls) == call_count


def test_aggregating_imported_baseline_does_not_modify_its_event():
    baseline = {"id": "run_imported", "status": "COMPLETED", "snapshot": {}, "result": {}}
    events = [
        {"kind": "baseline/imported", "data": baseline},
        {"kind": "turn/failed", "data": {"id": "run_imported", "code": "TURN_INTERRUPTED"}},
    ]
    original = deepcopy(events)

    first = runs_from(events)
    assert first["run_imported"]["status"] == "FAILED"
    assert events == original
    assert runs_from(events) == first
