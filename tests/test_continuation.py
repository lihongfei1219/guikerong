"""Continue a product with saved facts without losing scope, evidence or history."""

import asyncio
import json
from copy import deepcopy
from datetime import date

import httpx
import pytest

from app.ark_client import ArkClient
from app.cloud_matching import MATCHING_PROMPT_VERSION, build_messages, build_repair_messages
from app.continuation import comparison_for
from app.errors import DomainError
from app.harness_models import MatchCommand, ProductSupplementsCommand
from app.harness_projection import runs_from
from app.harness_store import EventStore
from app.history import read_request
from tests.test_harness import create, environment, match, provider_response, review, sample_catalog

__all__ = ["environment"]


def products(state):
    return {p["product_id"]: p for p in state["effective_matches"]}


def save(client, state, entries=None, product_id="p1", **updates):
    return client.post(f"/api/v1/sessions/{state['id']}/products/{product_id}/supplements", json={
        "expected_version": state["version"], "base_run_id": products(state)[product_id]["run_id"],
        "entries": entries or [{"condition_id": "ip", "content": "企业拥有I类知识产权。", "verification": "CONFIRMED"}],
        **updates,
    })


def test_atomic_batch_scope_versions_and_immutable_history(environment):
    client, settings, calls, _ = environment
    first = match(client, create(client))
    store = EventStore(settings.harness_db_path)
    before = store.read(first["id"])
    entries = [{"condition_id": "ip", "content": "知识产权已核实。", "verification": "CONFIRMED"},
               {"condition_id": "age", "content": "企业自2020年实际经营。"}]
    failed = save(client, first, [*entries, {"condition_id": "missing", "content": "不能保存"}])
    assert failed.status_code == 400 and store.read(first["id"]) == before
    assert save(client, first, expected_version=1).status_code == 409
    assert save(client, first, base_run_id="other-run").status_code == 409
    response = save(client, first, entries)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert len(calls) == 2  # Saving never calls the model.
    assert len(saved["request"]["documents"]) == 3
    assert products(saved)["p1"]["stale"] and not products(saved)["p2"]["stale"]
    for doc, quote in zip(saved["request"]["documents"][1:], ["拥有I类知识产权", "企业成立满一年"], strict=True):
        assert doc["human_supplement"]["product_id"] == "p1"
        assert doc["human_supplement"]["condition_quote"] == quote
    assert saved["request"]["documents"][-1]["human_supplement"]["verification"] == "UNVERIFIED"
    assert store.read(first["id"])[:len(before)] == before
    old = client.get(f"/api/v1/sessions/{first['id']}/runs/{first['run']['id']}").json()
    assert old["snapshot"]["request"] == first["request"]


@pytest.mark.parametrize("entry", [
    {"condition_id": "ip", "content": "  "},
    {"condition_id": "ip", "content": "事实", "verification": "YES"},
    {"condition_id": "ip", "content": "事实", "document_id": "d1"},
    {"condition_id": "ip", "content": "事实", "product_id": "p2"},
])
def test_invalid_batch_leaves_events_unchanged(environment, entry):
    client, settings, _, _ = environment
    first = match(client, create(client))
    store = EventStore(settings.harness_db_path)
    before = store.read(first["id"])
    assert save(client, first, [entry]).status_code == 400
    assert store.read(first["id"]) == before


def test_edit_keeps_id_scope_and_old_snapshot_and_rejects_other_product(environment):
    client, _, calls, _ = environment
    first = match(client, create(client))
    saved = save(client, first).json()
    second = match(client, saved, ["p1"])
    doc = saved["request"]["documents"][-1]
    edit = {"condition_id": "ip", "document_id": doc["document_id"], "content": "修订后的知识产权事实。",
            "verification": "UNVERIFIED"}
    assert save(client, second, [edit], product_id="p2").status_code == 400
    assert save(client, second, [{**edit, "condition_id": "age"}]).status_code == 400
    assert save(client, second, [edit, edit]).status_code == 400
    updated = save(client, second, [edit]).json()
    current = updated["request"]["documents"][-1]
    assert len(updated["request"]["documents"]) == 2
    assert current["document_id"] == doc["document_id"] and current["content"] == edit["content"]
    assert current["human_supplement"]["verification"] == "UNVERIFIED"
    old = client.get(f"/api/v1/sessions/{first['id']}/runs/{second['run']['id']}").json()
    assert old["snapshot"]["request"]["documents"][-1] == doc
    third = match(client, updated, ["p1"])
    context = json.loads(calls[-1]["messages"][1]["content"])
    assert context["continuation"]["updated_document_ids"] == [doc["document_id"]]
    assert context["continuation"]["added_document_ids"] == []
    assert context["human_supplements"][0]["content"] == edit["content"]
    assert products(third)["p2"]["run_id"] == first["run"]["id"]


def test_each_product_uses_its_own_successful_baseline_and_current_documents(environment):
    client, settings, calls, _ = environment
    first = match(client, create(client))
    assert all("continuation" not in json.loads(call["messages"][1]["content"]) for call in calls)
    reviewed = review(client, first, condition="__eligibility__").json()
    saved = save(client, reviewed).json()
    second = match(client, saved, ["p1"])
    assert len(calls) == 3
    context = json.loads(calls[-1]["messages"][1]["content"])
    continuation = context["continuation"]
    assert continuation["base_run_id"] == first["run"]["id"]
    assert continuation["previous_prompt_version"] == MATCHING_PROMPT_VERSION
    assert continuation["current_prompt_version"] == MATCHING_PROMPT_VERSION
    assert continuation["policy_changed"] is False
    assert continuation["previous_assessment"]["eligibility"]["status"] == "UNKNOWN"  # No human override.
    assert continuation["focus_condition_quotes"] == ["拥有I类知识产权"]
    assert continuation["added_document_ids"] == [saved["request"]["documents"][-1]["document_id"]]
    assert context["documents"][0]["content"] == first["request"]["documents"][0]["content"]
    third = match(client, second)
    contexts = {json.loads(c["messages"][1]["content"])["product"]["product_name"]:
                json.loads(c["messages"][1]["content"]) for c in calls[-2:]}
    assert contexts["科技成长贷"]["continuation"]["base_run_id"] == second["run"]["id"]
    assert contexts["知识产权贷"]["continuation"]["base_run_id"] == first["run"]["id"]
    assert contexts["知识产权贷"]["continuation"]["added_document_ids"] == []
    assert "human_supplements" not in contexts["知识产权贷"]
    history = client.get(f"/api/v1/sessions/{first['id']}/runs/{second['run']['id']}").json()
    assert history["snapshot"]["continuations"]["p1"] == continuation
    assert history["effective_matches"][0]["continuation"]["base_run_id"] == first["run"]["id"]
    assert products(third)["p1"]["human_reviews"] == {}
    assert EventStore(settings.harness_db_path).read(first["id"])


def test_failure_keeps_saved_facts_and_retry_uses_last_success(environment, monkeypatch):
    client, _, calls, application = environment
    first = match(client, create(client))
    saved = save(client, first).json()
    original = application.state.harness.matcher

    async def broken(*args, **kwargs):
        raise DomainError("MATCH_TIMEOUT", "模拟超时", 504)

    monkeypatch.setattr(application.state.harness, "matcher", broken)
    response = client.post(f"/api/v1/sessions/{saved['id']}/match", json={
        "expected_version": saved["version"], "product_ids": ["p1"]})
    assert response.status_code == 504
    restored = client.get(f"/api/v1/sessions/{saved['id']}").json()
    assert restored["request"] == saved["request"]
    assert products(restored)["p1"]["run_id"] == first["run"]["id"]
    assert products(restored)["p1"]["latest_attempt"]["status"] == "FAILED"
    monkeypatch.setattr(application.state.harness, "matcher", original)
    final = match(client, restored, ["p1"])
    assert len(final["request"]["documents"]) == 2
    assert json.loads(calls[-1]["messages"][1]["content"])["continuation"]["base_run_id"] == first["run"]["id"]


def test_continuation_survives_repair_and_counts_toward_budget(environment):
    client, _, _, _ = environment
    first = match(client, create(client))
    second = match(client, save(client, first).json(), ["p1"])
    history = client.get(f"/api/v1/sessions/{first['id']}/runs/{second['run']['id']}").json()
    continuation = history["snapshot"]["continuations"]["p1"]
    product, request = sample_catalog().products[0], read_request(second["request"])
    arguments = {"continuation": continuation, "expert_knowledge": [{"id": "k1", "content": "解释"}]}
    initial = build_messages(product, request, date(2026, 10, 26), **arguments)
    repair = build_repair_messages(product, request, date(2026, 10, 26), {}, {"reason": "INVALID_EVIDENCE_CITATION"}, **arguments)
    assert json.loads(repair[1]["content"])["continuation"] == continuation
    assert "不是企业事实" in initial[0]["content"]
    plain = build_messages(product, request, date(2026, 10, 26))
    with pytest.raises(DomainError, match="预算"):
        build_messages(product, request, date(2026, 10, 26), continuation=continuation,
                       max_characters=sum(len(m["content"]) for m in plain))


@pytest.mark.parametrize("previous_version", [None, "product-contextual-interpretation-4.1"])
def test_old_policy_is_explicit_on_initial_and_repair_without_changing_history(environment, previous_version):
    client, _, _, _ = environment
    first = match(client, create(client))
    from app.continuation import continuation_context

    history = client.get(f"/api/v1/sessions/{first['id']}/runs/{first['run']['id']}").json()
    history["result"]["versions"].pop("prompt", None)
    if previous_version:
        history["result"]["versions"]["prompt"] = previous_version
    original = deepcopy(history)
    request = read_request(first["request"])
    context = continuation_context({history["id"]: history}, request, sample_catalog())["p1"]
    assert context["previous_prompt_version"] == previous_version
    assert context["policy_changed"] is True
    assert context["added_document_ids"] == []
    initial = build_messages(sample_catalog().products[0], request, date(2026, 10, 27), continuation=context)
    repair = build_repair_messages(sample_catalog().products[0], request, date(2026, 10, 27), {}, {}, continuation=context)
    assert json.loads(initial[1]["content"])["continuation"] == context
    assert json.loads(repair[1]["content"])["continuation"] == context
    assert history == original


def test_deleted_documents_marked_and_contract_change_starts_fresh(environment):
    client, _, calls, application = environment
    first = match(client, create(client))
    saved = save(client, first).json()
    second = match(client, saved, ["p1"])
    doc_id = saved["request"]["documents"][-1]["document_id"]
    deleted = client.post(f"/api/v1/sessions/{first['id']}/documents/delete", json={
        "expected_version": second["version"], "document_id": doc_id}).json()
    third = match(client, deleted, ["p1"])
    context = json.loads(calls[-1]["messages"][1]["content"])
    assert context["continuation"]["removed_document_ids"] == [doc_id]
    assert "human_supplements" not in context
    changed = sample_catalog()
    changed.products[0].application_conditions += "；企业实际经营满两年"
    with pytest.raises(DomainError, match="申请条件已变化"):
        application.state.harness.product_supplements(first["id"], "p1", ProductSupplementsCommand(
            expected_version=third["version"], base_run_id=third["run"]["id"],
            entries=[{"condition_id": "ip", "content": "新信息"}]), changed)
    asyncio.run(application.state.harness.match(first["id"], MatchCommand(
        expected_version=third["version"], product_ids=["p1"]), changed))
    assert "continuation" not in json.loads(calls[-1]["messages"][1]["content"])


def test_verified_supplement_changes_result_with_citations_and_unchanged_other_product(environment, monkeypatch):
    client, _, calls, _ = environment
    first = match(client, create(client))
    saved = save(client, first).json()
    doc = saved["request"]["documents"][-1]

    def handler(request):
        calls.append(json.loads(request.content))
        response = provider_response(request).json()
        payload = json.loads(response["choices"][0]["message"]["content"])
        condition = payload["conditions"][1]
        condition.update(status="PASS", explanation="补充已明确I类知识产权，原待核验问题已解决。",
                         verification_needs=[], requires_human_verification=False, required_material=None,
                         citations=[{"document_id": doc["document_id"], "quote": doc["content"]}])
        payload["eligibility"] = {key: condition[key] for key in payload["eligibility"]}
        payload["supplement_feedback"] = [{"document_id": doc["document_id"], "status": "ADOPTED",
            "reason": "用户已核实，事实对应条款。", "impact": "知识产权条款由待核验变为符合。", "condition_ids": ["ip"]}]
        response["choices"][0]["message"]["content"] = json.dumps(payload, ensure_ascii=False)
        return httpx.Response(200, json=response)

    monkeypatch.setattr("app.service.ArkClient", lambda config: ArkClient(config, transport=httpx.MockTransport(handler)))
    final = match(client, saved, ["p1"])
    result = products(final)["p1"]
    assert result["decision"] == "FULLY_SATISFIED"
    assert result["model_assessment"]["supplement_feedback"][0]["status"] == "ADOPTED"
    assert result["continuation"]["before"] == "UNKNOWN" and result["continuation"]["after"] == "PASS"
    assert result["continuation"]["changes"][1]["before"] == "UNKNOWN"
    assert result["continuation"]["changes"][1]["after"] == "PASS"
    assert products(final)["p2"] == products(first)["p2"]


def test_comparison_uses_unique_verified_quotes_not_model_ids(environment):
    client, settings, _, _ = environment
    first = match(client, create(client))
    second = match(client, first, ["p1"])
    run = deepcopy(runs_from(EventStore(settings.harness_db_path).read(first["id"]))[second["run"]["id"]])
    current = run["result"]["matches"][0]["eligibility"]["condition_results"]
    current[0]["condition_id"] = "new-age-id"
    comparison = comparison_for(run, "p1")
    assert comparison["changes"][0]["before"] == "PASS"
    current[0]["source_quote"] = "模型重新划分的条款"
    assert comparison_for(run, "p1")["changes"][0]["before"] is None
    current[0]["source_quote"] = current[1]["source_quote"]
    assert all(c["before"] is None for c in comparison_for(run, "p1")["changes"])
