import json
from copy import deepcopy

import pytest

from guikerong_finance.db.events import EventStore
from guikerong_finance.service.harness import FinancialHarness
from tests.test_document_edit import edit
from tests.test_harness import create, environment, match, sample_catalog
from tests.test_supplements import supplement

__all__ = ["environment"]


def delete(client, session, document_id):
    return client.post(
        f"/api/v1/sessions/{session['id']}/documents/delete",
        json={"expected_version": session["version"], "document_id": document_id},
    )


def test_delete_scoped_supplement_preserves_history_and_updates_only_affected_product(environment):
    client, settings, calls, _ = environment
    first = match(client, supplement(client, create(client)).json())
    original_request = deepcopy(first["request"])
    original_result = deepcopy(first["run"]["result"])
    before = EventStore(settings.harness_db_path).read(first["id"])
    response = delete(client, first, "s1")
    assert response.status_code == 200, response.text
    changed = response.json()
    assert [d["document_id"] for d in changed["request"]["documents"]] == ["d1"]
    assert {p["product_id"]: p["stale"] for p in changed["effective_matches"]} == {"p1": True, "p2": False}
    assert {p["product_id"]: p["can_review"] for p in changed["effective_matches"]} == {"p1": False, "p2": True}
    assert changed["run"]["result"] == original_result
    assert EventStore(settings.harness_db_path).read(first["id"])[: len(before)] == before
    historical = client.get(f"/api/v1/sessions/{first['id']}/runs/{first['run']['id']}").json()
    assert historical["snapshot"]["request"] == original_request
    restarted = FinancialHarness(settings, EventStore(settings.harness_db_path)).session(first["id"], sample_catalog())
    assert restarted["request"] == changed["request"]
    rerun = match(client, changed, ["p1"])
    assert not rerun["stale"]
    context = json.loads(calls[-1]["messages"][1]["content"])
    assert "human_supplements" not in context
    assert [d["document_id"] for d in context["documents"]] == ["d1"]
    appended = supplement(client, rerun, identifier="s2").json()
    assert [d["document_id"] for d in appended["request"]["documents"]] == ["d1", "s2"]
    assert delete(client, appended, "s1").status_code == 404
    assert edit(client, appended, "不能修改已删除文件", "s1").status_code == 404
    assert delete(client, first, "s1").status_code == 409


def test_delete_original_file_keeps_remaining_supplement_and_invalidates_all_products(environment):
    client, _, _, _ = environment
    first = match(client, supplement(client, create(client), metadata={"verification": "CONFIRMED"}).json())
    original_supplement = deepcopy(first["request"]["documents"][-1])
    response = delete(client, first, "d1")
    assert response.status_code == 200, response.text
    changed = response.json()
    assert changed["request"]["documents"] == [original_supplement]
    assert changed["result_counts"] == {"total": 2, "current": 0, "stale": 2}
    restored = client.get(f"/api/v1/sessions/{first['id']}").json()
    assert restored["request"] == changed["request"]


@pytest.mark.parametrize(("document_id", "status"), [("missing", 404), ("d1", 400), ("", 400)])
def test_invalid_delete_does_not_change_session(environment, document_id, status):
    client, settings, _, _ = environment
    session = create(client)
    before = EventStore(settings.harness_db_path).read(session["id"])
    assert delete(client, session, document_id).status_code == status
    assert EventStore(settings.harness_db_path).read(session["id"]) == before
    restored = client.get(f"/api/v1/sessions/{session['id']}").json()
    assert restored["request"] == session["request"]
