import json
from copy import deepcopy

import pytest

from guikerong_finance.db.events import EventStore
from tests.test_harness import create, draft, environment, match, publish

__all__ = ["environment"]

RETIRED = {
    "source": "旧来源",
    "author": "旧录入人",
    "valid_until": "2000-01-01",
    "reviewer": "旧审核人",
    "reason": "旧审核说明",
}


def test_knowledge_can_be_saved_published_and_used_without_retired_fields(environment):
    client, settings, calls, _ = environment
    item = publish(client, draft(client))
    assert RETIRED.keys().isdisjoint(item)
    session = match(client, create(client), ["p1"])
    context = json.loads(calls[-1]["messages"][1]["content"])
    assert context["expert_knowledge"][0]["id"] == item["id"]
    assert RETIRED.keys().isdisjoint(context["expert_knowledge"][0])
    event = EventStore(settings.harness_db_path).read("knowledge")[0]
    assert RETIRED.keys().isdisjoint(event["data"])
    assert not session["stale"]


@pytest.mark.parametrize("field", RETIRED)
def test_retired_fields_are_not_accepted_in_new_knowledge(environment, field):
    client, settings, _, _ = environment
    response = client.post(
        "/api/v1/knowledge",
        json={
            "title": "解释",
            "content": "专业术语的解释。",
            field: RETIRED[field],
        },
    )
    assert response.status_code == 400
    assert EventStore(settings.harness_db_path).read("knowledge") == []


def test_legacy_knowledge_and_history_ignore_removed_fields_without_rewriting_events(environment):
    client, settings, calls, _ = environment
    store = EventStore(settings.harness_db_path)
    store.append(
        "knowledge",
        0,
        [
            (
                "knowledge/drafted",
                {
                    "id": "legacy-knowledge",
                    "title": "历史解释",
                    "content": "发明专利属于I类。",
                    "product_id": None,
                    "condition_quote": "",
                    **RETIRED,
                },
            )
        ],
    )
    legacy = client.get("/api/v1/knowledge").json()["items"][0]
    assert RETIRED.keys().isdisjoint(legacy)
    store.append(
        "knowledge",
        1,
        [
            (
                "knowledge/decided",
                {
                    "id": legacy["id"],
                    "expected_version": legacy["version"],
                    "status": "PUBLISHED",
                    "reviewer": RETIRED["reviewer"],
                    "reason": RETIRED["reason"],
                },
            )
        ],
    )
    published = client.get("/api/v1/knowledge").json()["items"][0]
    assert RETIRED.keys().isdisjoint(published)
    saved_knowledge = store.read("knowledge")
    assert client.get("/api/v1/knowledge").json()["items"][0]["applicable"]
    session = match(client, create(client), ["p1"])
    context = json.loads(calls[-1]["messages"][1]["content"])
    assert RETIRED.keys().isdisjoint(context["expert_knowledge"][0])

    # Older evaluations may still have the retired metadata in their saved snapshot.
    events = deepcopy(store.read(session["id"]))
    for event in events:
        if event["kind"] == "match/requested":
            event["data"]["knowledge"]["p1"][0].update(RETIRED)
    store.append("legacy-session", 0, [(event["kind"], event["data"]) for event in events])
    saved_history = store.read("legacy-session")
    restored = client.get("/api/v1/sessions/legacy-session").json()
    assert not restored["stale"]
    history = client.get(f"/api/v1/sessions/legacy-session/runs/{session['run']['id']}").json()
    knowledge = history["snapshot"]["knowledge"]["p1"][0]
    assert knowledge["id"] == published["id"]
    assert RETIRED.keys().isdisjoint(knowledge)
    assert store.read("legacy-session") == saved_history
    assert store.read("knowledge") == saved_knowledge


@pytest.mark.parametrize("field", ["reviewer", "reason"])
def test_removed_decision_fields_are_rejected_without_changing_knowledge(environment, field):
    client, settings, _, _ = environment
    item = draft(client)
    store = EventStore(settings.harness_db_path)
    before = store.read("knowledge")
    response = client.post(
        f"/api/v1/knowledge/{item['id']}/decision",
        json={
            "expected_version": item["version"],
            "status": "PUBLISHED",
            field: RETIRED[field],
        },
    )
    assert response.status_code == 400
    assert store.read("knowledge") == before


def test_publish_and_retire_need_only_status_and_version(environment):
    client, settings, _, _ = environment
    item = draft(client)
    published = publish(client, item)
    assert published["status"] == "PUBLISHED"
    assert RETIRED.keys().isdisjoint(published)
    retired = publish(client, published, "RETIRED")
    assert retired["status"] == "RETIRED"
    assert RETIRED.keys().isdisjoint(retired)
    assert not client.get("/api/v1/knowledge").json()["items"][0]["applicable"]
    for event in EventStore(settings.harness_db_path).read("knowledge"):
        assert RETIRED.keys().isdisjoint(event["data"])
