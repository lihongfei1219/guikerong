import asyncio
import sqlite3

import pytest

from app.errors import DomainError
from app.harness import FinancialHarness
from app.harness_models import MatchCommand
from app.harness_store import EventStore
from app.history import read_result
from tests.test_harness import create, environment, match, request_body, sample_catalog
from tests.test_history_continue import branch

__all__ = ["environment"]


def delete(client, session, **updates):
    return client.request("DELETE", f"/api/v1/sessions/{session['id']}",
                          json={"expected_version": session["version"], **updates})


def test_delete_removes_all_session_and_trace_rows_but_keeps_other_data(environment):
    client, settings, _, _ = environment
    victim = match(client, create(client))
    response = client.post(f"/api/v1/sessions/{victim['id']}/messages", json={
        "expected_version": victim["version"], "message": "说明匹配结果"})
    victim = response.json()
    other = match(client, create(client))
    store = EventStore(settings.harness_db_path)
    store.append("knowledge", 0, [("knowledge/drafted", {"id": "keep-knowledge", "content": "保留"})])
    other_events, knowledge = store.read(other["id"]), store.read("knowledge")
    run_id = victim["run"]["id"]
    assert store.read(run_id)
    response = delete(client, victim)
    assert response.status_code == 200, response.text
    assert response.json()["deleted_model_requests"] == 2
    reopened = EventStore(settings.harness_db_path)
    assert reopened.read(victim["id"]) == reopened.read(run_id) == []
    assert reopened.read(other["id"]) == other_events
    assert reopened.read("knowledge") == knowledge
    assert reopened.read(other["run"]["id"])
    assert all(s["id"] != victim["id"] for s in reopened.sessions())
    for suffix in ("", "/events", f"/runs/{run_id}", "/products/p1/history"):
        assert client.get(f"/api/v1/sessions/{victim['id']}{suffix}").status_code == 404
    assert delete(client, victim).status_code == 404


def test_version_conflict_and_reserved_stream_cannot_delete_data(environment):
    client, settings, _, _ = environment
    victim = match(client, create(client))
    store = EventStore(settings.harness_db_path)
    before = store.read(victim["id"])
    assert delete(client, victim, expected_version=1).status_code == 409
    assert store.read(victim["id"]) == before
    store.append("knowledge", 0, [("knowledge/drafted", {"content": "保留"})])
    assert client.request("DELETE", "/api/v1/sessions/knowledge", json={"expected_version": 1}).status_code == 404
    assert store.read("knowledge")


def test_deleting_source_keeps_independent_branch_and_marks_origin_missing(environment):
    client, settings, _, _ = environment
    source = match(client, create(client))
    continued = branch(client, source, source["run"]["id"]).json()
    store = EventStore(settings.harness_db_path)
    copied_events = store.read(continued["id"])
    assert delete(client, source).status_code == 200
    survivor = client.get(f"/api/v1/sessions/{continued['id']}").json()
    assert survivor["request"] == continued["request"]
    assert survivor["origin"]["deleted"] is True
    assert store.read(continued["id"]) == copied_events
    assert survivor["effective_matches"]


def test_deleting_branch_keeps_source_trace(environment):
    client, settings, _, _ = environment
    source = match(client, create(client))
    continued = branch(client, source, source["run"]["id"]).json()
    continued = match(client, continued)
    store = EventStore(settings.harness_db_path)
    original_trace = store.read(source["run"]["id"])
    assert delete(client, continued).status_code == 200
    assert store.read(source["run"]["id"]) == original_trace
    assert store.read(continued["run"]["id"]) == []
    assert client.get(f"/api/v1/sessions/{source['id']}").status_code == 200


@pytest.mark.parametrize("late_trace", [True, False])
def test_inflight_model_cannot_recreate_deleted_session_or_trace(environment, late_trace):
    client, settings, _, _ = environment
    reference = match(client, create(client))
    state = create(client)
    store = EventStore(settings.harness_db_path)
    run_ids = []

    async def delayed_model(*args, on_model_request, **kwargs):
        on_model_request({"product_id": "p1", "messages": [{"content": "private trace"}]})
        run_id = next(e["data"]["id"] for e in store.read(state["id"]) if e["kind"] == "match/requested")
        run_ids.append(run_id)
        store.delete_session(state["id"], state["version"] + 1)
        if late_trace:
            on_model_request({"product_id": "p2", "messages": [{"content": "late trace"}]})
        return read_result(reference["run"]["result"])

    harness = FinancialHarness(settings, store, matcher=delayed_model)
    with pytest.raises(DomainError):
        asyncio.run(harness.match(state["id"], MatchCommand(expected_version=state["version"]), sample_catalog()))
    assert store.read(state["id"]) == []
    assert all(store.read(run_id) == [] for run_id in run_ids)


def test_delete_is_atomic_when_session_delete_fails(environment):
    client, settings, _, _ = environment
    victim = match(client, create(client))
    store = EventStore(settings.harness_db_path)
    events, trace = store.read(victim["id"]), store.read(victim["run"]["id"])
    with sqlite3.connect(settings.harness_db_path) as connection:
        connection.execute("CREATE TRIGGER reject_session_delete BEFORE DELETE ON events "
                           "WHEN OLD.kind='session/created' BEGIN SELECT RAISE(ABORT,'simulated failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.delete_session(victim["id"], victim["version"])
    assert store.read(victim["id"]) == events
    assert store.read(victim["run"]["id"]) == trace


def test_catalog_failure_does_not_block_session_deletion(tmp_path):
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    settings = Settings(harness_db_path=tmp_path / "events.sqlite3", catalog_db_path=tmp_path / "missing.sqlite3")
    with TestClient(create_app(settings)) as client:
        victim = client.post("/api/v1/sessions", json=request_body()).json()
        assert delete(client, victim).status_code == 200
