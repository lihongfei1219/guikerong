"""Starting from history copies that exact input and leaves the original stream unchanged."""

import json
from copy import deepcopy

from guikerong_finance.db.events import EventStore
from guikerong_finance.service.harness import FinancialHarness
from tests.test_continuation import products, save
from tests.test_harness import create, environment, match, review, sample_catalog

__all__ = ["environment"]


def branch(client, session, run_id, product_id=None):
    return client.post(f"/api/v1/sessions/{session['id']}/runs/{run_id}/continue", json={"product_id": product_id})


def test_continue_old_run_uses_old_materials_and_independent_events(environment):
    client, settings, calls, _ = environment
    first = match(client, create(client))
    later = match(client, save(client, first).json(), ["p1"])
    reviewed = review(client, later, condition="__eligibility__").json()
    store = EventStore(settings.harness_db_path)
    original_events = store.read(first["id"])
    response = branch(client, reviewed, first["run"]["id"], "p1")
    assert response.status_code == 201, response.text
    continued = response.json()
    assert len(calls) == 3  # Opening the continuation does not evaluate anything.
    assert continued["id"] != first["id"]
    assert continued["request"] == first["request"]
    assert continued["origin"]["run_id"] == first["run"]["id"]
    assert continued["origin"]["session_id"] == first["id"]
    assert products(continued)["p1"]["human_reviews"] == {}
    assert not continued["stale"]
    assert products(continued)["p1"]["evaluated_at"] == products(first)["p1"]["evaluated_at"]
    baseline_id = continued["run"]["id"]
    assert baseline_id != first["run"]["id"]
    new_saved = save(client, continued, [{"condition_id": "ip", "content": "只属于历史继续副本的新事实。"}]).json()
    new_result = match(client, new_saved, ["p1"])
    context = json.loads(calls[-1]["messages"][1]["content"])
    assert context["continuation"]["base_run_id"] == baseline_id
    assert context["human_supplements"][0]["content"] == "只属于历史继续副本的新事实。"
    assert len(new_result["request"]["documents"]) == 2
    assert store.read(first["id"]) == original_events
    assert client.get(f"/api/v1/sessions/{first['id']}").json()["request"] == reviewed["request"]
    restored = FinancialHarness(settings, store).session(continued["id"], sample_catalog())
    assert restored["origin"] == continued["origin"]
    assert restored["request"] == new_result["request"]
    listing = client.get("/api/v1/sessions").json()["sessions"]
    assert next(s for s in listing if s["id"] == continued["id"])["origin"] == continued["origin"]


def test_continue_keeps_snapshot_supplement_metadata_and_supports_repeated_branching(environment):
    client, _, _, _ = environment
    first = match(client, create(client))
    second = match(client, save(client, first).json(), ["p1"])
    continued = branch(client, second, second["run"]["id"]).json()
    assert continued["request"] == second["request"]
    assert continued["request"]["documents"][-1]["human_supplement"]["verification"] == "CONFIRMED"
    detail = client.get(f"/api/v1/sessions/{continued['id']}/runs/{continued['run']['id']}").json()
    assert detail["can_continue"]
    again = branch(client, continued, continued["run"]["id"]).json()
    assert again["request"] == second["request"]
    assert again["origin"]["session_id"] == continued["id"]
    assert len(again["effective_matches"]) == 1  # Only the selected run's products, not later/current overview.


def test_continue_validates_run_ownership_and_missing_snapshots(environment):
    client, settings, _, _ = environment
    first = match(client, create(client))
    unrelated = create(client)
    assert branch(client, unrelated, first["run"]["id"]).status_code == 404
    assert branch(client, first, "missing").status_code == 404
    assert branch(client, first, first["run"]["id"], "missing-product").status_code == 400
    store = EventStore(settings.harness_db_path)
    store.append("legacy", 0, [("session/created", first["request"]), ("match/completed", first["run"])])
    detail = client.get(f"/api/v1/sessions/legacy/runs/{first['run']['id']}").json()
    assert not detail["can_continue"]
    assert branch(client, {"id": "legacy"}, first["run"]["id"]).status_code == 400
    assert len(store.sessions()) == 3  # No empty session created on any error.


def test_history_human_overrides_remain_in_source_not_new_model_baseline(environment):
    client, _, _, _ = environment
    first = match(client, create(client))
    reviewed = review(client, first, condition="__eligibility__").json()
    before = deepcopy(products(reviewed)["p1"])
    continued = branch(client, reviewed, first["run"]["id"]).json()
    assert products(continued)["p1"]["eligibility"]["status"] == "UNKNOWN"
    assert products(continued)["p1"]["human_reviews"] == {}
    old = client.get(f"/api/v1/sessions/{first['id']}/runs/{first['run']['id']}").json()
    assert products(old)["p1"]["human_reviews"] == before["human_reviews"]
    assert products(old)["p1"]["eligibility"]["status"] == "PASS"
