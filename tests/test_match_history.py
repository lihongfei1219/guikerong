"""Cumulative views must retain provenance, scope reviews and expose immutable history."""

import json
from copy import deepcopy
from datetime import date

import pytest

from app.errors import DomainError
from app.harness import FinancialHarness
from app.harness_store import EventStore
from tests.test_harness import create, draft, environment, match, publish, review, sample_catalog

__all__ = ["environment"]


def products(session):
    return {p["product_id"]: p for p in session["effective_matches"]}


def refresh(client, session):
    response = client.get(f"/api/v1/sessions/{session['id']}")
    assert response.status_code == 200
    return response.json()


def test_targeted_match_retains_other_products_reviews_and_source_evidence(environment):
    client, settings, calls, _ = environment
    initial = match(client, create(client))
    first_run = initial["run"]["id"]
    reviewed = review(client, initial, product_id="p2", condition="__eligibility__").json()
    second = match(client, reviewed, ["p1"])
    saved = refresh(client, second)
    assert len(calls) == 3  # History reads do not invoke the provider.
    assert saved["result_counts"] == {"total": 2, "current": 2, "stale": 0}
    p1, p2 = products(saved)["p1"], products(saved)["p2"]
    assert p1["run_id"] == second["run"]["id"] and p1["history_count"] == 2
    assert p2["run_id"] == first_run and p2["history_count"] == 1
    assert p2["human_reviews"]["__eligibility__"]["status"] == "PASS"
    assert p2["evidence"] == {key: initial["run"]["result"]["evidence"][key] for key in p2["evidence"]}
    # The older global run is still the latest evaluation for p2, so it remains reviewable.
    assert review(client, saved, product_id="p2", run_id=first_run).status_code == 200
    assert review(client, refresh(client, saved), run_id=first_run).status_code == 409
    restarted = FinancialHarness(settings, EventStore(settings.harness_db_path)).session(saved["id"], sample_catalog())
    assert len(restarted["effective_matches"]) == 2
    assert restarted["messages"][0]["run_id"] == first_run


def test_history_snapshots_and_reviews_are_read_only_and_not_inherited(environment):
    client, settings, calls, _ = environment
    first = match(client, create(client))
    reviewed = review(client, first, condition="__eligibility__").json()
    item = publish(client, draft(client))
    second = match(client, reviewed, ["p1"])
    assert not products(second)["p1"]["human_reviews"]
    base = f"/api/v1/sessions/{first['id']}"
    store = EventStore(settings.harness_db_path)
    before = deepcopy(store.read(first["id"]))
    history = client.get(f"{base}/products/p1/history").json()
    assert [r["id"] for r in history["runs"]] == [second["run"]["id"], first["run"]["id"]]
    old = client.get(f"{base}/runs/{first['run']['id']}").json()
    new = client.get(f"{base}/runs/{second['run']['id']}").json()
    assert old["read_only"] and all(not p["can_review"] for p in old["effective_matches"])
    assert old["snapshot"]["knowledge"]["p1"] == []
    assert new["snapshot"]["knowledge"]["p1"][0]["id"] == item["id"]
    assert products(old)["p1"]["human_reviews"]["__eligibility__"]["status"] == "PASS"
    assert old["result"]["matches"][0]["eligibility"]["status"] == "UNKNOWN"
    assert store.read(first["id"]) == before and len(calls) == 3
    assert client.get(f"{base}/runs/missing").status_code == 404
    other = create(client)
    assert client.get(f"/api/v1/sessions/{other['id']}/runs/{first['run']['id']}").status_code == 404


def test_document_refresh_is_per_product_and_chat_receives_entire_overview(environment):
    client, _, calls, _ = environment
    first = match(client, create(client))
    changed = client.post(f"/api/v1/sessions/{first['id']}/documents", json={
        "expected_version": first["version"], "documents": [
            {"document_id": "d2", "filename": "补充.md", "content": "补充：新材料已核对。"}],
    }).json()
    assert changed["result_counts"]["stale"] == 2
    second = match(client, changed, ["p1"])
    assert second["result_counts"] == {"total": 2, "current": 1, "stale": 1}
    assert products(second)["p2"]["stale"] and not products(second)["p2"]["can_review"]
    assert review(client, second, product_id="p2", run_id=first["run"]["id"]).status_code == 409
    response = client.post(f"/api/v1/sessions/{second['id']}/messages", json={
        "expected_version": second["version"], "message": "请汇总全部产品"})
    assert response.status_code == 200
    context = json.loads(calls[-1]["messages"][1]["content"])
    assert len(context["current_result"]) == 2
    assert products({"effective_matches": context["current_result"]})["p2"]["stale"]
    old = client.get(f"/api/v1/sessions/{first['id']}/runs/{first['run']['id']}").json()
    assert len(old["snapshot"]["request"]["documents"]) == 1


def test_knowledge_freshness_scopes_draft_publish_retire_and_expiry(environment, monkeypatch):
    client, _, _, _ = environment
    first = match(client, create(client))
    item = draft(client, valid_until="2099-01-01")
    assert not refresh(client, first)["stale"]
    item = publish(client, item)
    changed = refresh(client, first)
    assert products(changed)["p1"]["stale"] and not products(changed)["p2"]["stale"]
    assert review(client, changed).status_code == 409
    second = match(client, changed, ["p1"])
    assert not second["stale"]
    monkeypatch.setattr("app.harness.today", lambda: date(2099, 1, 2))
    assert products(refresh(client, second))["p1"]["stale"]
    monkeypatch.setattr("app.harness.today", lambda: date(2026, 10, 26))
    publish(client, item, "RETIRED")
    assert products(refresh(client, second))["p1"]["stale"]
    third = match(client, refresh(client, second), ["p1"])
    publish(client, draft(client, product_id=None, condition_quote=""))
    assert refresh(client, third)["result_counts"]["stale"] == 2


@pytest.mark.parametrize("failure", ["global", "partial", "interrupted"])
def test_latest_failure_preserves_success_and_history(environment, failure):
    client, _, _, application = environment
    first = match(client, create(client))
    harness = application.state.harness
    if failure == "global":
        async def fail(*args, **kwargs):
            raise DomainError("TEST_FAILURE", "模拟服务失败", 502)
        harness.matcher = fail
        response = client.post(f"/api/v1/sessions/{first['id']}/match", json={
            "expected_version": first["version"], "product_ids": ["p1"]})
        assert response.status_code == 502
    else:
        old_request = next(e["data"] for e in harness.store.read(first["id"]) if e["kind"] == "match/requested")
        snapshot = deepcopy(old_request)
        snapshot["id"] = "run_failed"
        snapshot["catalog"]["products"] = snapshot["catalog"]["products"][:1]
        events = [("match/requested", snapshot)]
        if failure == "partial":
            result = deepcopy(first["run"]["result"])
            result["matches"] = []
            result["failed_products"] = [{"product_id": "p1", "product_name": "科技成长贷",
                                          "code": "INVALID", "message": "模拟无效结果"}]
            events.append(("match/completed", {**first["run"], "id": "run_failed", "result": result}))
        harness.store.append(first["id"], first["version"], events)
    saved = refresh(client, first)
    assert len(saved["effective_matches"]) == 2
    product = products(saved)["p1"]
    assert product["run_id"] == first["run"]["id"] and not product["can_review"]
    assert product["latest_attempt"]["status"] == ("INCOMPLETE" if failure == "interrupted" else "FAILED")
    assert products(saved)["p2"]["can_review"]
    assert review(client, saved, run_id=first["run"]["id"]).status_code == 409


def test_catalog_change_only_invalidates_affected_product(environment):
    client, _, _, application = environment
    first = match(client, create(client))
    catalog = sample_catalog()
    catalog.products[0].application_conditions += "；须新增银行确认"
    result = application.state.harness.session(first["id"], catalog)
    assert products(result)["p1"]["stale"] and not products(result)["p2"]["stale"]


def test_full_36_then_one_retains_36_after_reload(environment):
    client, _, calls, application = environment
    catalog = sample_catalog()
    catalog.products = [catalog.products[0].model_copy(update={"product_id": f"p{i}",
                        "product_name": f"测试产品{i}"}) for i in range(1, 37)]
    catalog.raw_record_count = 36
    application.state.catalog = catalog
    first = match(client, create(client))
    second = match(client, first, ["p17"])
    restored = refresh(client, second)
    assert len(restored["effective_matches"]) == 36 and len(calls) == 37
    assert sum(p["run_id"] == first["run"]["id"] for p in restored["effective_matches"]) == 35
    assert products(restored)["p17"]["history_count"] == 2


def test_evidence_with_same_id_in_different_runs_never_crosses_products(environment):
    client, _, _, application = environment
    first = match(client, create(client))
    harness = application.state.harness
    original = deepcopy(first["run"]["result"])
    # Deliberately reuse an evidence ID with a different quote in the next run.
    evidence_id = original["matches"][0]["eligibility"]["condition_results"][0]["evidence_ids"][0]
    next_result = deepcopy(original)
    next_result["matches"] = [p for p in next_result["matches"] if p["product_id"] == "p1"]
    next_result["evidence"][evidence_id]["quote"] = "下一轮独有引文"
    requested = next(e["data"] for e in harness.store.read(first["id"]) if e["kind"] == "match/requested")
    requested = deepcopy(requested)
    requested["id"] = "run_collision"
    requested["catalog"]["products"] = requested["catalog"]["products"][:1]
    harness.store.append(first["id"], first["version"], [
        ("match/requested", requested),
        ("match/completed", {**first["run"], "id": "run_collision", "result": next_result}),
    ])
    saved = products(refresh(client, first))
    assert saved["p1"]["evidence"][evidence_id]["quote"] == "下一轮独有引文"
    assert saved["p2"]["evidence"][evidence_id]["quote"] == original["evidence"][evidence_id]["quote"]
