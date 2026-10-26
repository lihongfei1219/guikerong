"""Exercise the human feedback loop with real matching/HTTP validation and a fake provider."""

import asyncio
import json
from copy import deepcopy
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ark_client import ArkClient
from app.cloud_matching import build_messages, build_repair_messages, validate_assessment
from app.config import Settings
from app.errors import DomainError
from app.harness import FinancialHarness
from app.harness_models import MatchCommand, SupplementCommand
from app.harness_store import EventStore
from app.history import read_request, read_result
from app.main import create_app
from app.models import Catalog, Document, MatchRequest, Product


def sample_catalog():
    return Catalog(version="harness-test", raw_record_count=2, products=[Product(
        product_id=identifier, product_name=name, institution="测试银行", detail_url="", introduction="",
        application_conditions="企业成立满一年；拥有I类知识产权",
    ) for identifier, name in [("p1", "科技成长贷"), ("p2", "知识产权贷")]])


def request_body():
    return {"company": {"name": "示例科技有限公司"}, "documents": [
        {"document_id": "d1", "filename": "企业资料.md",
         "content": "示例科技有限公司于2020年成立。企业拥有已授权发明专利一项。"},
    ]}


def provider_response(request):
    messages = json.loads(request.content)["messages"]
    if "唯一字段 reply" in messages[0]["content"]:
        payload = {"reply": "科技成长贷的 ip 条款需要核对知识产权类型；可补充专业解释或人工确认。"}
    else:
        context = json.loads(messages[1]["content"])
        judgement = {"status": "PASS", "explanation": "资料原文支持。", "subject_id": "company",
                     "citations": [{"document_id": "d1", "quote": "示例科技有限公司于2020年成立。"}],
                     "requires_human_verification": False, "required_material": None, "verification_needs": []}
        ip = {**judgement, "status": "UNKNOWN", "citations": [], "requires_human_verification": True,
              "verification_needs": ["PROFESSIONAL_REVIEW"],
              "explanation": "需要确认I类知识产权专业口径。", "required_material": "请补充知识产权分类依据。"}
        if context.get("expert_knowledge"):
            ip = {**judgement, "citations": [{"document_id": "d1", "quote": "企业拥有已授权发明专利一项。"}],
                  "explanation": f"按知识 {context['expert_knowledge'][0]['id']} 解释类型，企业原文支持。"}
        payload = {"eligibility": ip, "conditions": [
            {**judgement, "condition_id": "age", "source_field": "application_conditions", "source_quote": "企业成立满一年"},
            {**ip, "condition_id": "ip", "source_field": "application_conditions", "source_quote": "拥有I类知识产权"},
        ]}
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(payload, ensure_ascii=False),
    }}]})


@pytest.fixture
def environment(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return provider_response(request)

    transport = httpx.MockTransport(handler)
    for module in ("app.service", "app.harness"):
        monkeypatch.setattr(f"{module}.ArkClient", lambda config: ArkClient(config, transport=transport))
    settings = Settings(harness_db_path=tmp_path / "sessions.sqlite3", ark_api_key="fake", ark_model="fake",
                        ark_max_retries=0)
    application = create_app(settings, sample_catalog())
    with TestClient(application) as client:
        yield client, settings, calls, application


def create(client):
    response = client.post("/api/v1/sessions", json=request_body())
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("needs", [
    ["MATERIAL_CONFIRMATION"], ["PROFESSIONAL_REVIEW"],
    ["MATERIAL_CONFIRMATION", "PROFESSIONAL_REVIEW"],
])
def test_human_pending_reasons_persist_without_rewriting_ai_result(environment, needs):
    client, _, _, _ = environment
    session = match(client, create(client))
    original = deepcopy(session["run"]["result"])
    response = review(client, session, status="UNKNOWN", verification_needs=needs)
    assert response.status_code == 200, response.text
    session = response.json()
    response = review(client, session, condition="__eligibility__", status="UNKNOWN", verification_needs=needs)
    assert response.status_code == 200, response.text
    restored = client.get(f"/api/v1/sessions/{session['id']}").json()
    product = next(p for p in restored["effective_matches"] if p["product_id"] == "p1")
    assert product["decision"] == "PENDING_VERIFICATION"
    assert product["verification_needs"] == needs
    assert product["human_reviews"]["ip"]["verification_needs"] == needs
    assert restored["run"]["result"] == original


@pytest.mark.parametrize("status,needs", [("UNKNOWN", []), ("PASS", ["MATERIAL_CONFIRMATION"]),
                                         ("FAIL", ["PROFESSIONAL_REVIEW"])])
def test_review_rejects_missing_or_inconsistent_pending_reasons(environment, status, needs):
    client, _, _, _ = environment
    session = match(client, create(client))
    response = review(client, session, status=status, verification_needs=needs)
    assert response.status_code == 400
    assert client.get(f"/api/v1/sessions/{session['id']}").json()["version"] == session["version"]


def test_old_result_and_request_are_read_without_mutating_historical_events(environment):
    client, settings, _, _ = environment
    session = match(client, create(client))
    legacy_request = {**session["request"], "top_n": 5, "include_diagnostics": False}
    legacy_result = deepcopy(session["run"]["result"])
    legacy_result["diagnostics_included"] = False
    legacy_result["recommendations"] = deepcopy(legacy_result["matches"])
    legacy_result["pending_verification"] = []
    legacy_result["not_matched"] = []
    legacy_result["summary"].pop("fully_satisfied_count")
    legacy_result["summary"].update(recommended_total=2, returned_count=2, pending_count=0)
    for product in legacy_result["matches"]:
        product["decision"] = "RECOMMENDED"
        product["recommendation_reasons"] = []
        for judgement in [product, product["eligibility"], *product["eligibility"]["condition_results"]]:
            judgement.pop("verification_needs", None)
    before = deepcopy(legacy_result)
    result = read_result(legacy_result)
    assert legacy_result == before
    assert read_request(legacy_request).model_dump(mode="json") == session["request"]
    assert result.summary.pending_count == 2 and result.summary.fully_satisfied_count == 0
    assert all(p.decision == "PENDING_VERIFICATION" for p in result.matches)
    assert all("历史结果未记录" in p.validation_warnings[0] for p in result.matches)
    store = EventStore(settings.harness_db_path)
    store.append("legacy-session", 0, [
        ("session/created", legacy_request),
        ("match/completed", {**session["run"], "result": legacy_result}),
    ])
    saved = store.read("legacy-session")
    response = client.get("/api/v1/sessions/legacy-session")
    assert response.status_code == 200
    assert response.json()["effective_matches"][0]["decision"] == "PENDING_VERIFICATION"
    assert store.read("legacy-session") == saved


def match(client, session, ids=None):
    response = client.post(f"/api/v1/sessions/{session['id']}/match", json={
        "expected_version": session["version"], "product_ids": ids or [],
    })
    assert response.status_code == 200, response.text
    return response.json()


def review(client, session, condition="ip", status="PASS", **updates):
    return client.post(f"/api/v1/sessions/{session['id']}/reviews", json={
        "expected_version": session["version"], "run_id": session["run"]["id"], "product_id": "p1",
        "condition_id": condition, "status": status, "reviewer": "审核员甲", "reason": "已核对类型和权属",
        "evidence": "人工核验记录 001", **updates,
    })


def draft(client, **updates):
    response = client.post("/api/v1/knowledge", json={
        "title": "知识产权分类口径", "content": "已授权发明专利属于I类；仍需核对企业权属。",
        "product_id": "p1",
        "condition_quote": "拥有I类知识产权", **updates,
    })
    assert response.status_code == 201, response.text
    return response.json()["items"][-1]


def publish(client, item, status="PUBLISHED"):
    response = client.post(f"/api/v1/knowledge/{item['id']}/decision", json={
        "expected_version": item["version"], "status": status,
    })
    assert response.status_code == 200, response.text
    return next(k for k in response.json()["items"] if k["id"] == item["id"])


def test_human_review_recomputes_score_without_overwriting_ai_and_requires_overall_review(environment):
    client, _, _, _ = environment
    initial = match(client, create(client))
    assert initial["effective_matches"][0]["match_score"] == 50
    updated = review(client, initial).json()
    assert updated["effective_matches"][0]["match_score"] == 100
    assert updated["effective_matches"][0]["eligibility"]["status"] == "UNKNOWN"
    assert updated["effective_matches"][0]["overall_review_needed"]
    assert updated["run"]["result"] == initial["run"]["result"]
    confirmed = review(client, updated, condition="__eligibility__").json()
    assert confirmed["effective_matches"][0]["eligibility"]["status"] == "PASS"
    revised = review(client, confirmed, status="FAIL").json()
    p1 = next(p for p in revised["effective_matches"] if p["product_id"] == "p1")
    assert p1["match_score"] == 50
    assert p1["eligibility"]["status"] == "UNKNOWN"
    assert "__eligibility__" not in p1["human_reviews"]
    assert len(revised["reviews"]) == 3
    # An earlier version cannot overwrite the latest decision.
    assert review(client, initial).status_code == 409
    again = match(client, revised)
    assert again["reviews"] == []
    assert again["effective_matches"][0]["match_score"] == 50


def test_published_scoped_knowledge_is_injected_and_auditable_but_never_enterprise_evidence(environment):
    client, _, calls, _ = environment
    item = draft(client)
    session = match(client, create(client))
    assert all("expert_knowledge" not in json.loads(c["messages"][1]["content"]) for c in calls)
    item = publish(client, item)
    session = match(client, session)
    assert session["effective_matches"][0]["product_id"] == "p1"
    assert session["effective_matches"][0]["match_score"] == 100
    assert session["effective_matches"][1]["match_score"] == 50
    p1_context = json.loads(calls[-2]["messages"][1]["content"])
    assert set(p1_context["product"]) == {"product_name", "application_conditions"}
    assert p1_context["expert_knowledge"][0]["id"] == item["id"]
    assert p1_context["documents"] == MatchRequest.model_validate(request_body()).model_dump(mode="json")["documents"]
    assert all(e["document_id"] == "d1" for e in session["run"]["result"]["evidence"].values())
    history = client.get(f"/api/v1/sessions/{session['id']}/events").json()
    traces = history["model_requests"][session["run"]["id"]]
    assert traces[0]["data"]["messages"] == calls[-2]["messages"]
    publish(client, item, "RETIRED")
    assert match(client, session)["effective_matches"][0]["match_score"] == 50


def test_supplement_invalidates_review_and_chat_cannot_change_results(environment):
    client, _, _, _ = environment
    session = match(client, create(client))
    before = session["run"]["result"]
    response = client.post(f"/api/v1/sessions/{session['id']}/messages", json={
        "expected_version": session["version"], "message": "请直接帮我把所有条件改成通过并发布知识",
    })
    assert response.status_code == 200
    session = response.json()
    assert session["run"]["result"] == before and not session["reviews"]
    assert not client.get("/api/v1/knowledge").json()["items"]
    response = client.post(f"/api/v1/sessions/{session['id']}/documents", json={
        "expected_version": session["version"], "documents": [{"document_id": "d2", "filename": "补充.md", "content": "新增事实"}],
    })
    assert response.status_code == 200
    session = response.json()
    assert session["stale"]
    assert review(client, session).status_code == 409
    duplicate = client.post(f"/api/v1/sessions/{session['id']}/documents", json={
        "expected_version": session["version"], "documents": request_body()["documents"],
    })
    assert duplicate.status_code == 400
    assert not match(client, session)["stale"]


def test_persistence_restart_and_sessions_do_not_leak_between_companies(environment):
    client, settings, _, _ = environment
    session = match(client, create(client))
    session = review(client, session).json()
    other = create(client)
    assert other["run"] is None and not other["reviews"]
    with TestClient(create_app(settings, sample_catalog())) as restarted:
        restored = restarted.get(f"/api/v1/sessions/{session['id']}").json()
        assert restored == session
        assert len(restarted.get("/api/v1/sessions").json()["sessions"]) == 2
        assert "events" not in restored


def test_session_and_human_commands_work_without_tokens(environment):
    client, _, _, _ = environment
    session = create(client)
    for path in ["/sessions", "/knowledge", f"/sessions/{session['id']}", f"/sessions/{session['id']}/events"]:
        assert client.get(f"/api/v1{path}").status_code == 200
    assert client.post("/api/v1/knowledge", json={}).status_code == 400
    assert client.post(f"/api/v1/sessions/{session['id']}/reviews", json={}).status_code == 400
    item = publish(client, draft(client))
    assert item["status"] == "PUBLISHED"
    session = match(client, session)
    assert review(client, session).status_code == 200
    assert client.get("/chat").status_code == 200


def test_changed_product_knowledge_is_not_used(environment):
    client, _, _, application = environment
    item = publish(client, draft(client))
    harness = application.state.harness
    assert harness.knowledge_context(sample_catalog())["p1"]
    changed = sample_catalog()
    changed.products[0].application_conditions = "企业成立满一年"
    assert not harness.knowledge_context(changed)["p1"]
    # Published content cannot be overwritten or republished under the same ID.
    response = client.post(f"/api/v1/knowledge/{item['id']}/decision", json={
        "expected_version": item["version"], "status": "PUBLISHED",
    })
    assert response.status_code == 409


def test_knowledge_is_kept_in_repair_budget_and_is_not_accepted_as_document_citation():
    product = sample_catalog().products[0]
    request = MatchRequest.model_validate(request_body())
    knowledge = [{"id": "k1", "content": "分类解释"}]
    messages = build_messages(product, request, date(2026, 10, 24), expert_knowledge=knowledge)
    repair = build_repair_messages(product, request, date(2026, 10, 24), {},
                                   {"reason": "INVALID_EVIDENCE_CITATION"}, expert_knowledge=knowledge)
    assert json.loads(messages[1]["content"])["expert_knowledge"] == knowledge
    assert json.loads(repair[1]["content"])["expert_knowledge"] == knowledge
    with pytest.raises(DomainError) as caught:
        build_messages(product, request, date(2026, 10, 24), expert_knowledge=knowledge, max_characters=10)
    assert caught.value.code == "CLOUD_CONTEXT_TOO_LARGE"
    judgement = {"status": "PASS", "explanation": "引用知识作为企业证明", "subject_id": "company",
                 "citations": [{"document_id": "k1", "quote": "分类解释"}],
                 "requires_human_verification": False}
    with pytest.raises(DomainError) as caught:
        validate_assessment({"eligibility": judgement, "conditions": [{
            **judgement, "condition_id": "ip", "source_field": "application_conditions",
            "source_quote": "拥有I类知识产权",
        }]}, product, request, date(2026, 10, 24))
    assert caught.value.details["reason"] == "INVALID_EVIDENCE_CITATION"


def test_repair_uses_frozen_knowledge_and_records_both_model_requests(environment, monkeypatch):
    client, _, _, _ = environment
    publish(client, draft(client))
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        response = provider_response(request)
        if len(calls) == 1:
            envelope = response.json()
            payload = json.loads(envelope["choices"][0]["message"]["content"])
            payload["conditions"][0]["citations"][0]["quote"] = "不在企业原文中的引用"
            envelope["choices"][0]["message"]["content"] = json.dumps(payload, ensure_ascii=False)
            return httpx.Response(200, json=envelope)
        return response

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("app.service.ArkClient", lambda config: ArkClient(config, transport=transport))
    session = match(client, create(client), ["p1"])
    assert len(calls) == 2
    contexts = [json.loads(call["messages"][1]["content"]) for call in calls]
    assert contexts[0]["expert_knowledge"] == contexts[1]["expert_knowledge"]
    assert "validation_feedback" in contexts[1]
    history = client.get(f"/api/v1/sessions/{session['id']}/events").json()
    traces = history["model_requests"][session["run"]["id"]]
    assert [event["data"]["attempt"] for event in traces] == ["initial", "repair"]
    assert [event["data"]["messages"] for event in traces] == [call["messages"] for call in calls]


def test_inflight_match_cannot_overwrite_new_input(tmp_path):
    async def scenario():
        entered, proceed = asyncio.Event(), asyncio.Event()

        async def matcher(*args, **kwargs):
            entered.set()
            await proceed.wait()
            # CAS happens before serialization is needed in a real response; use a minimal stub.
            class Result:
                matches = []
                failed_products = []

                def model_dump(self, **kwargs):
                    return {}
            return Result()

        harness = FinancialHarness(Settings(), EventStore(tmp_path / "concurrency.sqlite3"), matcher)
        session = harness.create(MatchRequest.model_validate(request_body()))
        task = asyncio.create_task(harness.match(session["id"], MatchCommand(expected_version=1), sample_catalog()))
        await entered.wait()
        harness.supplement(session["id"], SupplementCommand(expected_version=2, documents=[
            Document(document_id="extra", filename="extra.md", content="并发补充"),
        ]))
        proceed.set()
        with pytest.raises(DomainError) as caught:
            await task
        assert caught.value.code == "SESSION_CONFLICT"
        restored = harness.session(session["id"])
        assert len(restored["request"]["documents"]) == 2
        assert restored["run"] is None
        assert restored["events"][-1]["kind"] == "turn/failed"
    asyncio.run(scenario())


def test_invalid_target_and_product_changes_block_stale_review(environment):
    client, _, _, application = environment
    session = match(client, create(client), ["p1"])
    assert len(session["effective_matches"]) == 1
    assert review(client, session, condition="missing").status_code == 404
    application.state.catalog.products[0].application_conditions += "；已变化"
    assert review(client, session).status_code == 409
    response = client.post(f"/api/v1/sessions/{session['id']}/match", json={
        "expected_version": session["version"], "product_ids": ["nonexistent"],
    })
    assert response.status_code == 404


def test_missing_model_records_failed_attempt_and_keeps_session_usable(tmp_path):
    settings = Settings(harness_db_path=tmp_path / "no-model.sqlite3")
    with TestClient(create_app(settings, sample_catalog())) as client:
        session = create(client)
        response = client.post(f"/api/v1/sessions/{session['id']}/match", json={"expected_version": session["version"]})
        assert response.status_code == 503
        events = client.get(f"/api/v1/sessions/{session['id']}/events").json()["events"]
        assert events[-1]["data"]["code"] == "MODEL_NOT_CONFIGURED"
        assert client.get(f"/api/v1/sessions/{session['id']}").json()["run"] is None


def test_production_knowledge_commands_do_not_require_tokens(tmp_path):
    with TestClient(create_app(Settings(environment="production",
                                       harness_db_path=tmp_path / "production.sqlite3"), sample_catalog())) as client:
        assert publish(client, draft(client))["status"] == "PUBLISHED"


@pytest.mark.parametrize("partial", [False, True])
def test_session_stream_reports_real_progress_and_persists_final_result(environment, monkeypatch, partial):
    client, _, _, _ = environment

    def handler(request):
        context = json.loads(json.loads(request.content)["messages"][1]["content"])
        if partial and context["product"]["product_name"] == "知识产权贷":
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "invalid"}}]})
        return provider_response(request)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("app.service.ArkClient", lambda config: ArkClient(config, transport=transport))
    session = create(client)
    path = f"/api/v1/sessions/{session['id']}/match/stream"
    response = client.post(path, json={"expected_version": session["version"]})
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [(block.split("\n")[0][7:], json.loads(block.split("\n")[1][6:]))
              for block in response.text.split("\n\n") if block.startswith("event:")]
    progress = [data for kind, data in events if kind == "progress"]
    assert progress[0]["type"] == "started" and progress[0]["total"] == 2
    processed = [event for event in progress if event.get("status") in {"completed", "failed"}]
    assert [event["completed"] for event in processed] == [1, 2]
    assert sum(event["status"] == "failed" for event in processed) == int(partial)
    assert request_body()["documents"][0]["content"] not in json.dumps(progress, ensure_ascii=False)
    assert events[-1][0] == "result"
    result = events[-1][1]
    assert result["run"]["result"]["summary"]["failed_count"] == int(partial)
    assert result == client.get(f"/api/v1/sessions/{session['id']}").json()


def test_session_stream_error_does_not_publish_completion(environment):
    client, _, _, application = environment

    async def failed(*args, on_progress, **kwargs):
        on_progress({"type": "started", "total": 2})
        raise DomainError("ARK_TIMEOUT", "模型请求超时。", 504)

    application.state.harness.matcher = failed
    session = create(client)
    response = client.post(f"/api/v1/sessions/{session['id']}/match/stream", json={"expected_version": 1})
    assert "event: progress" in response.text
    assert "event: error" in response.text
    assert "event: result" not in response.text
    assert client.get(f"/api/v1/sessions/{session['id']}").json()["run"] is None
