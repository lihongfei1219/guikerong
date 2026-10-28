"""Chat scope and saved facts have separate, explicit semantics."""

import json

import pytest

from app.harness_store import EventStore
from tests.test_continuation import save
from tests.test_harness import create, environment, match

__all__ = ["environment"]


def ask(client, session, **kwargs):
    return client.post(f"/api/v1/sessions/{session['id']}/messages", json={
        "expected_version": session["version"], "message": "本次拟贷款两年，这能满足条件吗？", **kwargs})


def test_chat_focus_is_explicit_and_question_does_not_become_matching_material(environment):
    client, _, calls, _ = environment
    first = match(client, create(client))
    response = ask(client, first, product_id="p1", condition_quote="拥有I类知识产权")
    assert response.status_code == 200, response.text
    after = response.json()
    context = json.loads(calls[-1]["messages"][1]["content"])
    assert context["focus"] == {"product_id": "p1", "product_name": "科技成长贷", "condition_quote": "拥有I类知识产权"}
    assert after["request"] == first["request"]
    assert after["effective_matches"] == first["effective_matches"]
    assert after["messages"][-2]["role"] == "user"
    assert after["messages"][-2]["context_label"] == "提问 · 科技成长贷"
    match(client, after, ["p1"])
    assert "本次拟贷款两年" not in calls[-1]["messages"][1]["content"]


def test_history_question_receives_only_selected_snapshot_not_later_documents_or_chat(environment):
    client, _, calls, _ = environment
    first = match(client, create(client))
    later = save(client, first, [{"condition_id": "ip", "content": "后续新增信息，仅属于最新资料。"}]).json()
    response = ask(client, later, run_id=first["run"]["id"], product_id="p1")
    assert response.status_code == 200, response.text
    messages = calls[-1]["messages"]
    context = json.loads(messages[1]["content"])
    assert context["historical_run_id"] == first["run"]["id"]
    assert context["company_documents"] == first["request"]
    assert len(messages) == 3
    assert "后续新增信息" not in json.dumps(messages, ensure_ascii=False)
    assert response.json()["request"] == later["request"]
    assert response.json()["messages"][-1]["context_label"].startswith("历史轮次提问")


@pytest.mark.parametrize("scope", [
    {"product_id": "missing"}, {"condition_quote": "拥有I类知识产权"},
    {"product_id": "p1", "condition_quote": "不存在的条款"}, {"run_id": "missing"},
])
def test_invalid_chat_scope_does_not_write_or_call_provider(environment, scope):
    client, settings, calls, _ = environment
    first = match(client, create(client))
    store = EventStore(settings.harness_db_path)
    events = store.read(first["id"])
    count = len(calls)
    assert ask(client, first, **scope).status_code in {400, 404}
    assert store.read(first["id"]) == events
    assert len(calls) == count


def test_supplement_text_is_saved_in_conversation_and_survives_reload(environment):
    client, _, _, _ = environment
    first = match(client, create(client))
    response = save(client, first, [{"condition_id": "ip", "content": "企业已核实拥有I类知识产权。", "verification": "CONFIRMED"}])
    assert response.status_code == 200
    saved = response.json()
    assert saved["messages"][-2] == {"role": "user", "content": "企业已核实拥有I类知识产权。",
                                    "context_label": "补充事实 · 科技成长贷"}
    assert client.get(f"/api/v1/sessions/{saved['id']}").json()["messages"] == saved["messages"]
