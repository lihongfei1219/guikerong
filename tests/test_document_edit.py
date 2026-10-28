import json
from copy import deepcopy

import pytest

from guikerong_finance.db.events import EventStore
from tests.test_harness import create, environment, match
from tests.test_supplements import supplement

__all__ = ["environment"]


def edit(client, session, content, document_id="d1"):
    return client.post(
        f"/api/v1/sessions/{session['id']}/documents/edit",
        json={"expected_version": session["version"], "document_id": document_id, "content": content},
    )


def test_edit_replaces_full_content_preserves_history_and_uses_new_model_input(environment):
    client, settings, calls, _ = environment
    first = match(client, create(client))
    original = deepcopy(first["request"]["documents"][0])
    before = EventStore(settings.harness_db_path).read(first["id"])
    content = "  # 修改后的文件\n\n示例科技有限公司于2020年成立。\n企业拥有已授权发明专利两项。\n"
    response = edit(client, first, content)
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["request"]["documents"] == [{**original, "content": content}]
    assert updated["result_counts"] == {"total": 2, "current": 0, "stale": 2}
    assert all(not p["can_review"] for p in updated["effective_matches"])
    assert EventStore(settings.harness_db_path).read(first["id"])[: len(before)] == before
    assert client.get(f"/api/v1/sessions/{first['id']}").json()["request"] == updated["request"]
    historical = client.get(f"/api/v1/sessions/{first['id']}/runs/{first['run']['id']}").json()
    assert historical["snapshot"]["request"]["documents"] == [original]
    rerun = match(client, updated)
    assert not rerun["stale"]
    for call in calls[-2:]:
        context = json.loads(call["messages"][1]["content"])
        assert context["documents"][0]["content"] == content
        assert "human_supplements" not in context
    assert edit(client, first, "过期提交").status_code == 409
    restored = client.get(f"/api/v1/sessions/{first['id']}").json()
    assert restored["request"]["documents"][0]["content"] == content


def test_edit_scoped_supplement_keeps_scope_and_does_not_add_document(environment):
    client, _, _, _ = environment
    first = match(client, supplement(client, create(client)).json())
    old = first["request"]["documents"][-1]
    changed = edit(client, first, "本次贷款期限为2年。", "s1").json()
    assert len(changed["request"]["documents"]) == 2
    assert changed["request"]["documents"][-1] == {**old, "content": "本次贷款期限为2年。"}
    assert {p["product_id"]: p["stale"] for p in changed["effective_matches"]} == {"p1": True, "p2": False}
    unchanged = edit(client, changed, "本次贷款期限为2年。", "s1").json()
    assert unchanged["version"] == changed["version"]
    # Appending another document must not restore an earlier version of the edited file.
    appended = supplement(client, changed, identifier="s2").json()
    assert appended["request"]["documents"][1]["content"] == "本次贷款期限为2年。"


@pytest.mark.parametrize(
    ("content", "document_id", "status"),
    [
        ("", "d1", 400),
        (" \n\t", "d1", 400),
        ("内容", "missing", 404),
        ("x" * (1024 * 1024 + 1), "d1", 413),
    ],
    ids=["empty", "whitespace", "missing-file", "oversized"],
)
def test_invalid_edit_does_not_change_saved_file(environment, content, document_id, status):
    client, _, _, _ = environment
    session = create(client)
    assert edit(client, session, content, document_id).status_code == status
    restored = client.get(f"/api/v1/sessions/{session['id']}").json()
    assert restored["version"] == session["version"]
    assert restored["request"] == session["request"]
