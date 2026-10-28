"""Real Ark client over an in-memory transport: no network or provider credentials."""

import asyncio
import json
from copy import deepcopy
from datetime import date, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from support import match_company, post_match, read_company_directory

from guikerong_finance.application import create_app
from guikerong_finance.config import ROOT, Settings
from guikerong_finance.errors import DomainError
from guikerong_finance.models.domain import Catalog, MatchRequest, Product
from guikerong_finance.service.ark_client import ArkClient
from guikerong_finance.service.matching import SHANGHAI, match_company_async


def settings(**updates):
    return Settings(
        **{
            "matching_mode": "ark",
            "ark_api_key": "test-ark-key",
            "ark_model": "test-model",
            "ark_max_retries": 0,
            **updates,
        }
    )


def request():
    return MatchRequest.model_validate(
        {
            "company": {"name": "模型接口测试有限公司"},
            "documents": [
                {
                    "document_id": "d1",
                    "filename": "随意格式.md",
                    "source_date": "2026-01-01",
                    "content": "  模型接口测试有限公司\r\n2020年初成立，主营半导体材料研发生产。\r\n",
                },
                {
                    "document_id": "d2",
                    "filename": "补充.md",
                    "content": "# 新增数据类型\n值：无需固定字段也能保留这段内容。\n来源：企业说明\n",
                },
            ],
        }
    )


def catalog(**updates):
    return Catalog(
        version="test-cloud",
        raw_record_count=1,
        products=[
            Product(
                **{
                    "product_id": "p1",
                    "product_name": "测试研发贷",
                    "institution": "测试机构",
                    "detail_url": "https://example.test/p1",
                    "introduction": "面向半导体材料研发企业的经营周转融资",
                    "application_conditions": "成立满一年且从事半导体材料研发生产",
                    "raw_fields": {
                        "产品名称": "测试研发贷",
                        "申请条件": "成立满一年且从事半导体材料研发生产",
                        "额度": "最高 1000 万元",
                        "利率": "按照实际审批执行",
                    },
                    "availability_status": "ACTIVE",
                    **updates,
                }
            )
        ],
    )


def assessment(body, conditions=("age", "business")):  # noqa: ARG001 - fixture/parameter intentionally exercises setup and cases
    def judgement(quote):
        return {
            "status": "PASS",
            "explanation": "企业资料原文支持此项判断。",
            "subject_id": "company",
            "citations": [{"document_id": "d1", "quote": quote}],
            "requires_human_verification": False,
            "required_material": None,
        }

    return {
        "eligibility": judgement("2020年初成立，主营半导体材料研发生产。"),
        "conditions": [
            {
                **judgement("2020年初成立，主营半导体材料研发生产。"),
                "condition_id": identifier,
                "source_field": "application_conditions",
                "source_quote": "成立满一年",
            }
            for identifier in conditions
        ],
    }


def local_product_id(products, context):
    return next(
        product.product_id
        for product in products.products
        if product.product_name == context["product"]["product_name"]
    )


def install_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "guikerong_finance.service.matching.ArkClient", lambda config: ArkClient(config, transport=transport)
    )


def completion(payload):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(payload, ensure_ascii=False)},
                }
            ]
        },
    )


@pytest.mark.parametrize("overall_status", ["UNKNOWN", "FAIL"])
def test_complete_passing_conditions_determine_overall_status(monkeypatch, overall_status):
    body, calls = request(), []
    products = catalog(application_conditions="成立满一年；从事半导体材料研发生产")
    answer = assessment(body)
    answer["conditions"][1]["source_quote"] = "从事半导体材料研发生产"
    answer["eligibility"].update(
        status=overall_status,
        requires_human_verification=True,
        explanation="旧的矛盾理由。",
        required_material="旧的补件要求。",
        verification_needs=["MATERIAL_CONFIRMATION"],
        citations=[],
    )

    def handler(http_request):
        calls.append(http_request)
        return completion(answer)

    install_transport(monkeypatch, handler)
    result = match_company(body, products, settings())
    assert len(calls) == 1
    product = result.matches[0]
    assert product.decision == "FULLY_SATISFIED"
    assert product.eligibility.status == "PASS"
    assert all(c.status == "PASS" for c in product.eligibility.condition_results)
    assert product.eligibility.evidence_ids
    assert not product.verification_needs
    assert not product.required_actions
    assert "旧的" not in product.eligibility.explanation
    assert product.model_assessment.eligibility.required_material is None
    assert not product.model_assessment.eligibility.requires_human_verification
    assert answer["eligibility"]["status"] == overall_status


def test_overall_reconciliation_does_not_bypass_clause_evidence(monkeypatch):
    body = request()
    answer = assessment(body, conditions=("age",))
    answer["conditions"][0]["citations"] = []
    answer["eligibility"].update(status="UNKNOWN", requires_human_verification=True)
    install_transport(monkeypatch, lambda _: completion(answer))
    result = match_company(body, catalog(application_conditions="成立满一年"), settings())
    assert result.matches[0].eligibility.status == "UNKNOWN"
    assert result.matches[0].eligibility.condition_results[0].status == "UNKNOWN"


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_discontinuous_policy_quote_repairs_without_waiving_account_requirement(monkeypatch, repair_succeeds):
    body, calls = request(), []
    first, account, last = "依法合规从事经营活动", "在农合机构开立结算账户", "借款用途符合国家产业政策"
    products = catalog(application_conditions=f"{first}，{account}，{last}")
    answer = assessment(body)
    answer["eligibility"].update(status="UNKNOWN", requires_human_verification=True)
    policy = {
        **answer["conditions"][0],
        "assessment_basis": "PRELIMINARY_POLICY",
        "citations": [],
        "source_quote": first + "，" + last,
    }
    concrete = {
        **answer["conditions"][1],
        "status": "UNKNOWN",
        "requires_human_verification": True,
        "source_quote": account,
        "citations": [],
        "verification_needs": ["MATERIAL_CONFIRMATION"],
    }
    answer["conditions"] = [policy, concrete]
    corrected = deepcopy(answer)
    corrected["conditions"] = [
        {**policy, "source_quote": first},
        concrete,
        {**policy, "condition_id": "use", "source_quote": last},
    ]

    def handler(http_request):
        context = json.loads(json.loads(http_request.content)["messages"][-1]["content"])
        calls.append(context)
        return completion(corrected if len(calls) == 2 and repair_succeeds else answer)

    install_transport(monkeypatch, handler)
    result = match_company(body, products, settings())
    assert len(calls) == 2
    assert calls[1]["validation_feedback"]["reason"] == "INVALID_POLICY_SOURCE_QUOTE"
    assert calls[1]["product"] == calls[0]["product"]
    if repair_succeeds:
        item = result.matches[0]
        assert [c.status for c in item.eligibility.condition_results] == ["PASS", "UNKNOWN", "PASS"]
        assert all(c.source_quote_verified for c in item.eligibility.condition_results)
        assert item.eligibility.status == "UNKNOWN"
        assert not result.failed_products
    else:
        assert not result.matches
        assert result.failed_products[0].reason == "INVALID_POLICY_SOURCE_QUOTE"


@pytest.mark.parametrize("initial_status", ["UNKNOWN", "FAIL"])
@pytest.mark.parametrize("repaired_status", ["PASS", "UNKNOWN"])
def test_all_pass_overall_conflict_is_rechecked_once(monkeypatch, initial_status, repaired_status):
    body, calls = request(), []
    initial = assessment(body)
    initial["eligibility"].update(
        status=initial_status,
        explanation="整体与逐条矛盾的旧理由。",
        requires_human_verification=initial_status == "UNKNOWN",
    )
    corrected = assessment(body)
    if repaired_status == "UNKNOWN":
        for item in (corrected["eligibility"], corrected["conditions"][0]):
            item.update(
                status="UNKNOWN",
                explanation="具体经营事实仍不明确。",
                requires_human_verification=True,
                verification_needs=["MATERIAL_CONFIRMATION"],
                required_material="确认实际经营事实。",
            )

    def handler(http_request):
        context = json.loads(json.loads(http_request.content)["messages"][-1]["content"])
        calls.append(context)
        return completion(initial if len(calls) == 1 else corrected)

    install_transport(monkeypatch, handler)
    result = match_company(body, catalog(), settings())
    assert len(calls) == 2
    assert calls[1]["validation_feedback"] == {"reason": "INCONSISTENT_OVERALL_STATUS", "section": "eligibility"}
    assert calls[1]["documents"] == calls[0]["documents"]
    assert calls[1]["product"] == calls[0]["product"]
    assert "整体与逐条矛盾的旧理由" not in json.dumps(calls[1], ensure_ascii=False)
    assert not result.failed_products
    product = result.matches[0]
    assert product.eligibility.status == repaired_status
    assert product.eligibility.condition_results[0].status == repaired_status
    assert (product.decision == "FULLY_SATISFIED") == (repaired_status == "PASS")


def test_persistent_all_pass_conflict_is_model_failure_not_company_missing_documents(monkeypatch):
    body, calls = request(), []
    answer = assessment(body)
    answer["eligibility"].update(status="UNKNOWN", requires_human_verification=True)

    def handler(http_request):
        calls.append(http_request)
        return completion(answer)

    install_transport(monkeypatch, handler)
    result = match_company(body, catalog(), settings())
    assert len(calls) == 2
    assert not result.matches
    assert result.failed_products[0].reason == "INCONSISTENT_OVERALL_STATUS"
    assert "不代表企业缺少材料" in result.failed_products[0].message


def test_api_sends_raw_documents_and_returns_supported_reasons(monkeypatch):
    body, calls = request(), []

    def handler(http_request):
        envelope = json.loads(http_request.content)
        calls.append(envelope)
        assert http_request.headers["authorization"] == "Bearer test-ark-key"
        assert http_request.url.path == "/api/v3/chat/completions"
        context = json.loads(envelope["messages"][-1]["content"])
        assert context["documents"] == [doc.model_dump(mode="json") for doc in body.documents]
        assert context["product"] == {
            "product_name": "测试研发贷",
            "application_conditions": "成立满一年且从事半导体材料研发生产",
        }
        assert "required_condition_ids" not in context
        assert set(context["response_example"]) == {"eligibility", "conditions"}
        return completion(assessment(body))

    install_transport(monkeypatch, handler)
    with TestClient(create_app(settings(), catalog())) as client:
        health = client.get("/health").json()
        assert health["matching_mode"] == "ark"
        assert health["model_connectivity_checked"] is False
        response = post_match(client, json=body.model_dump(mode="json"))
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(calls) == 1
    assert calls[0]["model"] == "test-model"
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert result["versions"]["matching_mode"] == "ark"
    assert "extractor" not in result["versions"]
    assert result["summary"]["fully_satisfied_count"] == 1
    assert (
        next(p for p in result["matches"] if p["decision"] == "FULLY_SATISFIED")["model_assessment"]["eligibility"][
            "status"
        ]
        == "PASS"
    )
    assert "relevance" not in next(p for p in result["matches"] if p["decision"] == "FULLY_SATISFIED")
    assert result["evidence"]


def test_numeric_condition_is_judged_by_model_without_fixed_field_parser(monkeypatch):
    body = request()
    answer = assessment(body)
    answer["conditions"][0].update(status="FAIL", explanation="模型认为成立年限不符。")
    answer["eligibility"].update(status="FAIL", explanation="成立年限不符合申请要求。")
    install_transport(monkeypatch, lambda _: completion(answer))
    result = match_company(body, catalog(), settings(), now=datetime(2026, 10, 22, tzinfo=SHANGHAI))
    assert result.summary.not_matched_count == 1
    assert result.summary.fully_satisfied_count == 0
    assert result.matches[0].match_score == 50
    assert result.matches[0].eligibility.status == "FAIL"
    age = next(c for c in result.matches[0].eligibility.condition_results if c.condition_id == "age")
    assert age.status == "FAIL"


def test_three_of_five_conditions_scores_sixty_even_when_overall_eligibility_fails(monkeypatch):
    body = request()
    clauses = ["成立满一年", "从事半导体材料研发生产", "拥有自主知识产权", "经营正常", "有出口订单"]
    answer = assessment(body, tuple(f"c{i}" for i in range(5)))
    statuses = ["PASS", "PASS", "PASS", "FAIL", "UNKNOWN"]
    for condition, clause, status in zip(answer["conditions"], clauses, statuses, strict=True):
        condition.update(source_quote=clause, status=status, explanation=f"条件“{clause}”的模型判断为{status}。")
    answer["eligibility"].update(status="FAIL", explanation="存在未通过的必要条件，因此整体资格不符合。")
    install_transport(monkeypatch, lambda _: completion(answer))
    result = match_company(body, catalog(application_conditions="；".join(clauses)), settings())
    item = result.matches[0]
    assert item.match_score == 60
    assert item.eligibility.status == "FAIL"
    assert item.match_breakdown.model_dump() == {
        "total_conditions": 5,
        "matched_conditions": 3,
        "unmatched_conditions": 1,
        "unknown_conditions": 1,
        "points_per_condition": 20.0,
        "method": "equal_weight",
    }
    assert [condition.score_contribution for condition in item.eligibility.condition_results] == [20, 20, 20, 0, 0]
    assert [condition.score_deduction for condition in item.eligibility.condition_results] == [0, 0, 0, 20, 20]
    assert [condition.status for condition in item.eligibility.condition_results] == statuses
    assert item.decision == "NOT_ELIGIBLE"
    assert result.summary.fully_satisfied_count == 0
    assert result.summary.not_matched_count == 1


def test_all_matches_sort_by_overall_status_and_keep_distinct_evidence(monkeypatch):
    body, products = request(), catalog()
    body.documents.append(
        body.documents[0].model_copy(
            update={
                "document_id": "d3",
                "filename": "待核验来源.md",
                "content": "仅用于零分产品的资料线索。",
            }
        )
    )
    products.products = [
        products.products[0].model_copy(update={"product_id": identifier, "product_name": identifier})
        for identifier in ("lower", "higher", "zero")
    ]

    def handler(http_request):
        context = json.loads(json.loads(http_request.content)["messages"][-1]["content"])
        identifier = context["product"]["product_name"]
        answer = assessment(body)
        document = {"higher": body.documents[0], "lower": body.documents[1], "zero": body.documents[2]}[identifier]
        for judgement in [answer["eligibility"], *answer["conditions"]]:
            judgement["citations"] = [{"document_id": document.document_id, "quote": document.content}]
        if identifier == "lower":
            answer["conditions"][0]["status"] = answer["eligibility"]["status"] = "FAIL"
        elif identifier == "zero":
            for judgement in [answer["eligibility"], *answer["conditions"]]:
                judgement.update(status="UNKNOWN", explanation="现有资料仅提供线索，条件尚待核验。")
        return completion(answer)

    install_transport(monkeypatch, handler)
    result = match_company(body, products, settings())
    assert [item.product_id for item in result.matches] == ["higher", "zero", "lower"]
    assert [item.match_score for item in result.matches] == [100, 0, 50]
    assert [item.product_id for item in [p for p in result.matches if p.decision == "FULLY_SATISFIED"]] == ["higher"]
    assert [p.product_id for p in result.matches if p.decision == "PENDING_VERIFICATION"] == ["zero"]
    assert {evidence.document_id for evidence in result.evidence.values()} == {"d1", "d2", "d3"}
    assert all(
        identifier in result.evidence
        for item in result.matches
        for condition in item.eligibility.condition_results
        for identifier in condition.evidence_ids
    )
    assert result.matches[-1].eligibility.explanation


def test_model_request_contains_only_full_conditions_and_name_regardless_of_other_product_fields(monkeypatch):
    body, calls = request(), []
    original_conditions = "  1.成立满一年\r\n2.从事半导体材料研发生产\r\n3.具有持续经营能力  "
    first = catalog(product_id="LOCAL_ONLY_ID_ONE", application_conditions=original_conditions)
    changed = catalog(
        product_id="LOCAL_ONLY_ID_TWO",
        application_conditions=original_conditions,
        institution="LOCAL_ONLY_BANK",
        detail_url="https://local-only.example/product",
        introduction="LOCAL_ONLY_INTRO" * 10000,
        applicable_customers="LOCAL_ONLY_CUSTOMERS",
        raw_fields={
            "申请条件": "LOCAL_ONLY_RAW_CONTRADICTION",
            "产品名称": "LOCAL_ONLY_RAW_NAME",
            "贷款额度": "LOCAL_ONLY_AMOUNT",
        },
        availability_status="SNAPSHOT",
    )

    def handler(http_request):
        envelope = json.loads(http_request.content)
        calls.append(envelope)
        context = json.loads(envelope["messages"][-1]["content"])
        assert context["product"] == {"product_name": "测试研发贷", "application_conditions": original_conditions}
        assert context["documents"] == [doc.model_dump(mode="json") for doc in body.documents]
        assert "LOCAL_ONLY" not in json.dumps(envelope, ensure_ascii=False)
        assert set(context["response_example"]) == {"eligibility", "conditions"}
        answer = assessment(body, ("age", "business", "operations"))
        answer["conditions"][1]["source_quote"] = "从事半导体材料研发生产"
        answer["conditions"][2]["source_quote"] = "具有持续经营能力"
        return completion(answer)

    install_transport(monkeypatch, handler)
    now = datetime(2026, 10, 22, tzinfo=SHANGHAI)
    for products in (first, changed):
        result = match_company(body, products, settings(ark_max_prompt_characters=20000), now=now)
        assert result.summary.fully_satisfied_count == 1
        assert (
            next(p for p in result.matches if p.decision == "FULLY_SATISFIED").product_id
            == products.products[0].product_id
        )
    assert calls[0] == calls[1]


def test_same_name_products_are_bound_to_their_local_coroutines_without_sending_ids(monkeypatch):
    body, products = request(), catalog()
    products.products.append(
        products.products[0].model_copy(
            update={
                "product_id": "p2",
                "application_conditions": "从事半导体材料研发生产",
            }
        )
    )

    def handler(http_request):
        envelope = json.loads(http_request.content)
        context = json.loads(envelope["messages"][-1]["content"])
        assert context["product"]["product_name"] == "测试研发贷"
        assert "product_id" not in json.dumps(envelope, ensure_ascii=False)
        answer = assessment(body, ("business",))
        answer["conditions"][0]["source_quote"] = "从事半导体材料研发生产"
        if context["product"]["application_conditions"] == "从事半导体材料研发生产":
            answer["eligibility"].update(status="FAIL", explanation="离线模拟此产品的模型整体不通过结论。")
            answer["conditions"][0].update(status="FAIL", explanation="离线模拟此申请条件不通过。")
        return completion(answer)

    install_transport(monkeypatch, handler)
    result = match_company(body, products, settings())
    assert [item.product_id for item in [p for p in result.matches if p.decision == "FULLY_SATISFIED"]] == ["p1"]
    assert [item.product_id for item in [p for p in result.matches if p.decision == "NOT_ELIGIBLE"]] == ["p2"]


def test_native_deepseek_completes_matching_with_raw_inputs_and_provider_metadata(monkeypatch):
    body, calls = request(), []

    def handler(http_request):
        envelope = json.loads(http_request.content)
        calls.append(envelope)
        assert str(http_request.url) == "https://api.deepseek.com/chat/completions"
        assert envelope["model"] == "deepseek-flash"
        assert envelope["max_tokens"] == 4096
        assert "max_completion_tokens" not in envelope
        context = json.loads(envelope["messages"][-1]["content"])
        assert context["documents"] == [doc.model_dump(mode="json") for doc in body.documents]
        return completion(assessment(body))

    install_transport(monkeypatch, handler)
    config = settings(matching_mode="deepseek", ark_model="deepseek-flash", ark_base_url="https://api.deepseek.com")
    with TestClient(create_app(config, catalog())) as client:
        health = client.get("/health").json()
        assert health["model_provider"] == health["matching_mode"] == "deepseek"
        response = post_match(client, json=body.model_dump(mode="json"))
    assert response.status_code == 200
    assert response.json()["versions"]["model"] == "deepseek-flash"
    assert response.json()["versions"]["matching_mode"] == "deepseek"
    assert response.json()["summary"]["fully_satisfied_count"] == 1
    assert len(calls) == 1


def test_model_pass_is_fully_satisfied_for_snapshot_product(monkeypatch):
    body = request()
    install_transport(monkeypatch, lambda _: completion(assessment(body)))
    result = match_company(body, catalog(availability_status="SNAPSHOT"), settings())
    assert result.summary.fully_satisfied_count == 1
    assert result.summary.pending_count == 0
    assert next(p for p in result.matches if p.decision == "FULLY_SATISFIED").required_actions == []


@pytest.mark.parametrize(
    ("upstream_status", "code", "retryable"),
    [
        (401, "ARK_AUTH_ERROR", False),
        (400, "ARK_REQUEST_REJECTED", False),
        (429, "ARK_RATE_LIMITED", True),
    ],
)
def test_provider_error_is_explicit_private_and_has_no_partial_result(monkeypatch, upstream_status, code, retryable):
    install_transport(monkeypatch, lambda _: httpx.Response(upstream_status, text="private provider diagnostics"))
    with TestClient(create_app(settings(), catalog())) as client:
        response = post_match(client, json=request().model_dump(mode="json"))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["retryable"] is retryable
    assert "private provider diagnostics" not in response.text
    assert "test-ark-key" not in response.text
    assert "recommendations" not in response.json()


def test_all_prompts_are_checked_before_the_first_provider_call(monkeypatch):
    from guikerong_finance.service.cloud_matching import build_messages

    calls, body, products = [], request(), catalog()
    first = products.products[0]
    limit = sum(len(message["content"]) for message in build_messages(first, body, date(2026, 10, 22))) + 50
    products.products.append(first.model_copy(update={"product_id": "p2", "application_conditions": "大" * limit}))
    install_transport(monkeypatch, lambda _: calls.append(1))
    with pytest.raises(DomainError) as exc:
        match_company(body, products, settings(ark_max_prompt_characters=limit))
    assert exc.value.code == "CLOUD_CONTEXT_TOO_LARGE"
    assert calls == []


def test_each_product_is_an_independent_coroutine_with_a_bounded_concurrency(monkeypatch):
    body, products = request(), catalog()
    base = products.products[0]
    products.products = [
        base.model_copy(
            update={
                "product_id": f"p{index}",
                "product_name": f"产品{index}",
                "application_conditions": f"唯一条件-{index}",
            }
        )
        for index in range(7)
    ]
    active = peak = 0
    calls, task_ids = [], set()

    async def handler(http_request):
        nonlocal active, peak
        context = json.loads(json.loads(http_request.content)["messages"][-1]["content"])
        identifier = local_product_id(products, context)
        index = identifier[1:]
        assert "required_condition_ids" not in context
        assert "rules" not in context["product"]
        assert context["product"]["application_conditions"] == f"唯一条件-{index}"
        assert context["documents"] == [doc.model_dump(mode="json") for doc in body.documents]
        calls.append(identifier)
        task_ids.add(id(asyncio.current_task()))
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.02)
            answer = assessment(body, (f"model-condition-{index}",))
            answer["conditions"][0]["source_quote"] = f"唯一条件-{index}"
            return completion(answer)
        finally:
            active -= 1

    install_transport(monkeypatch, handler)
    result = match_company(body, products, settings(ark_concurrency=2))
    assert len(calls) == len(set(calls)) == len(task_ids) == 7
    assert peak == 2
    assert active == 0
    assert result.summary.fully_satisfied_count == 7


def test_overall_deadline_cancels_inflight_call_and_returns_no_partial_result(monkeypatch):
    started, cancelled = [], []

    async def handler(_):
        started.append(True)
        try:
            await asyncio.sleep(5)
        finally:
            cancelled.append(True)

    install_transport(monkeypatch, handler)
    with pytest.raises(DomainError) as exc:
        asyncio.run(match_company_async(request(), catalog(), settings(timeout_seconds=0.1)))
    assert exc.value.code == "MATCH_TIMEOUT"
    assert started == cancelled == [True]


def test_failure_cancels_other_products_and_semaphore_waiters(monkeypatch):
    products = catalog()
    products.products = [
        products.products[0].model_copy(update={"product_id": f"p{i}", "product_name": f"产品{i}"}) for i in range(5)
    ]
    entered, cancelled = [], []

    async def handler(http_request):
        context = json.loads(json.loads(http_request.content)["messages"][-1]["content"])
        identifier = local_product_id(products, context)
        entered.append(identifier)
        if identifier == "p0":
            await asyncio.sleep(0.02)
            return httpx.Response(401, text="private rejection")
        try:
            await asyncio.sleep(5)
        finally:
            cancelled.append(identifier)

    install_transport(monkeypatch, handler)
    with pytest.raises(DomainError) as exc:
        match_company(request(), products, settings(ark_concurrency=2))
    assert exc.value.code == "ARK_AUTH_ERROR"
    assert len(entered) < 5
    assert set(cancelled) == set(entered) - {"p0"}


@pytest.mark.parametrize("product_count", [1, 3])
def test_all_invalid_model_responses_return_explicit_failed_products(monkeypatch, product_count):
    answer = assessment(request())
    answer["conditions"] = []
    products = catalog()
    products.products = [
        products.products[0].model_copy(update={"product_id": f"p{i}", "product_name": f"产品{i}"})
        for i in range(product_count)
    ]
    install_transport(monkeypatch, lambda _: completion(answer))
    with TestClient(create_app(settings(), products)) as client:
        response = post_match(client, json=request().model_dump(mode="json"))
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "PARTIAL"
    assert result["summary"]["failed_count"] == product_count
    assert result["summary"]["evaluated_count"] == 0
    assert result["summary"]["pending_count"] == result["summary"]["not_matched_count"] == 0
    assert [p for p in result["matches"] if p["decision"] == "FULLY_SATISFIED"] == []
    assert result["matches"] == []
    assert len(result["failed_products"]) == product_count
    assert {failure["code"] for failure in result["failed_products"]} == {"ARK_INVALID_RESPONSE"}
    assert {failure["reason"] for failure in result["failed_products"]} == {"UNUSABLE_MODEL_RESPONSE"}
    assert result["evidence"] == {}


def test_only_invalid_product_is_corrected_with_all_original_documents(monkeypatch):
    body, products, contexts = request(), catalog(), []
    products.products.append(
        products.products[0].model_copy(update={"product_id": "p2", "product_name": "第二测试产品"})
    )

    def handler(http_request):
        context = json.loads(json.loads(http_request.content)["messages"][-1]["content"])
        contexts.append(context)
        identifier = local_product_id(products, context)
        assert context["documents"] == [doc.model_dump(mode="json") for doc in body.documents]
        answer = assessment(body)
        if identifier == "p1" and sum(local_product_id(products, c) == "p1" for c in contexts) == 1:
            answer["conditions"][0]["citations"][0]["quote"] = "不存在的改写引文"
        return completion(answer)

    install_transport(monkeypatch, handler)
    result = match_company(body, products, settings())
    assert result.summary.fully_satisfied_count == 2
    assert [local_product_id(products, c) for c in contexts].count("p1") == 2
    assert [local_product_id(products, c) for c in contexts].count("p2") == 1
    repair = next(c for c in contexts if "validation_feedback" in c)
    assert "previous_response" not in repair
    assert "不存在的改写引文" not in json.dumps(repair, ensure_ascii=False)
    assert repair["validation_feedback"]["reason"] == "INVALID_EVIDENCE_CITATION"
    assert "required_condition_ids" not in repair
    assert set(repair["product"]) == {"product_name", "application_conditions"}
    assert "product_id" not in json.dumps(repair, ensure_ascii=False)
    assert "第二测试产品" not in json.dumps(repair, ensure_ascii=False)


def test_failed_citation_correction_only_scores_the_remaining_verified_conditions(monkeypatch):
    body, calls = request(), []
    answer = assessment(body)
    answer["conditions"][0]["citations"][0]["quote"] = "不存在的改写引文"

    def handler(_):
        calls.append(True)
        return completion(answer)

    install_transport(monkeypatch, handler)
    with TestClient(create_app(settings(), catalog())) as client:
        response = post_match(client, json=body.model_dump(mode="json"))
    assert len(calls) == 2
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "COMPLETED"
    assert result["summary"]["evaluated_count"] == result["summary"]["pending_count"] == 1
    assert result["summary"]["failed_count"] == result["summary"]["fully_satisfied_count"] == 0
    assert result["failed_products"] == []
    pending = result["matches"][0]
    assert pending["product_id"] == "p1"
    assert pending["validation_warnings"]
    assert pending["eligibility"]["status"] == "UNKNOWN"
    assert pending["match_score"] == 50
    assert pending["match_breakdown"]["matched_conditions"] == pending["match_breakdown"]["unknown_conditions"] == 1
    assert pending["eligibility"]["condition_results"][0]["score_contribution"] == 0
    assert pending["eligibility"]["condition_results"][1]["score_contribution"] == 50
    assert pending["model_assessment"]["conditions"][0]["citations"] == []
    assert pending["required_actions"]
    assert "不存在的改写引文" not in response.text


@pytest.mark.parametrize("failure_kind", ["bad_citation", "invalid_schema", "invalid_product_citation"])
def test_one_bad_response_preserves_the_other_33_products_and_finishes_progress(
    monkeypatch,
    failure_kind,
):
    events: list
    body, products, calls, events, cancelled = request(), catalog(), [], [], []
    products.products = [
        products.products[0].model_copy(update={"product_id": f"p{i}", "product_name": f"产品{i}"}) for i in range(34)
    ]

    async def handler(http_request):
        context = json.loads(json.loads(http_request.content)["messages"][-1]["content"])
        identifier = local_product_id(products, context)
        calls.append(identifier)
        try:
            await asyncio.sleep(0.02 if identifier == "p33" else 0.001)
        except asyncio.CancelledError:
            cancelled.append(identifier)
            raise
        answer = assessment(body)
        if identifier == "p32":
            if failure_kind == "bad_citation":
                answer["conditions"][0]["citations"][0]["quote"] = "PRIVATE_INVALID_CITATION"
            elif failure_kind == "invalid_schema":
                answer["conditions"] = []
            else:
                answer["conditions"][0]["source_quote"] = "PRIVATE_INVALID_PRODUCT_CITATION"
        return completion(answer)

    install_transport(monkeypatch, handler)
    result = asyncio.run(match_company_async(body, products, settings(), on_progress=events.append))
    recovered = failure_kind != "invalid_schema"
    assert result.status == ("COMPLETED" if recovered else "PARTIAL")
    assert result.summary.fully_satisfied_count == (34 if failure_kind == "invalid_product_citation" else 33)
    assert result.summary.evaluated_count == (34 if recovered else 33)
    assert result.summary.pending_count == int(failure_kind == "bad_citation")
    assert result.summary.failed_count == int(not recovered)
    assert result.summary.not_matched_count == 0
    assert len(result.failed_products) == int(not recovered)
    assert [p.product_id for p in result.matches if p.decision == "PENDING_VERIFICATION"] == (
        ["p32"] if failure_kind == "bad_citation" else []
    )
    assert len(result.matches) == (34 if recovered else 33)
    assert calls.count("p32") == (1 if failure_kind == "invalid_product_citation" else 2)
    assert len(calls) == (34 if failure_kind == "invalid_product_citation" else 35)
    assert set(calls) == {f"p{i}" for i in range(34)}
    assert cancelled == []
    terminal = [event for event in events if event.get("status") in {"completed", "failed"}]
    assert [event["completed"] for event in terminal] == list(range(1, 35))
    assert {event["total"] for event in terminal} == {34}
    assert len([event for event in terminal if event["status"] == "failed"]) == int(not recovered)
    assert next(event for event in terminal if event["product_id"] == "p33")["status"] == "completed"
    assert not any(event.get("status") == "cancelled" for event in events)
    assert events[-1]["phase"] == "finalizing"
    if failure_kind == "bad_citation":
        pending = result.matches[-1]
        assert pending.product_id == "p32"
        assert pending.match_score == 50
        assert pending.validation_warnings
        assert pending.model_assessment is not None
        assert pending.model_assessment.conditions[0].citations == []
        assert pending.model_assessment is not None
        assert pending.model_assessment.conditions[1].citations
    if not recovered:
        failure = result.failed_products[0]
        assert failure.product_id == "p32"
        assert failure.code == "ARK_INVALID_RESPONSE"
        assert "match_score" not in failure.model_dump()
        assert (
            failure.reason
            == {"invalid_schema": "UNUSABLE_MODEL_RESPONSE", "invalid_product_citation": "INVALID_PRODUCT_CITATION"}[
                failure_kind
            ]
        )
    if failure_kind == "invalid_product_citation":
        item = next(item for item in result.matches if item.product_id == "p32")
        assert item.match_score == 100
        assert item.validation_warnings
        assert item.eligibility.condition_results[0].source_quote_verified is False
    else:
        assert "PRIVATE_INVALID" not in result.model_dump_json()
    assert "PRIVATE_INVALID" not in json.dumps(events)
    assert all(any(evidence.quote in doc.content for doc in body.documents) for evidence in result.evidence.values())


def test_corrected_unknown_condition_receives_no_points_but_keeps_other_verified_points(monkeypatch):
    body, calls = request(), []

    def handler(_):
        calls.append(True)
        answer = assessment(body)
        condition = answer["conditions"][0]
        if len(calls) == 1:
            condition["citations"][0]["quote"] = "不存在的改写引文"
        else:
            condition.update(status="UNKNOWN", citations=[], explanation="没有找到可支持判断的连续原文。")
            answer["eligibility"].update(status="UNKNOWN", citations=[], explanation="申请资格需要补充资料判断。")
        return completion(answer)

    install_transport(monkeypatch, handler)
    result = match_company(body, catalog(), settings())
    assert len(calls) == 2
    assert result.summary.fully_satisfied_count == 0
    assert result.summary.pending_count == 1
    assert result.matches[0].match_score == 50
    assert result.matches[0].eligibility.condition_results[0].status == "UNKNOWN"
    assert result.matches[0].eligibility.condition_results[0].score_contribution == 0


def test_lf_quote_of_crlf_source_is_accepted_without_a_correction_call(monkeypatch):
    body, calls = request(), []
    answer = assessment(body)
    answer["eligibility"]["citations"][0]["quote"] = body.documents[0].content.replace("\r\n", "\n")

    def handler(_):
        calls.append(True)
        return completion(answer)

    install_transport(monkeypatch, handler)
    result = match_company(body, catalog(), settings())
    assert len(calls) == 1
    assert result.summary.fully_satisfied_count == 1
    assert any(evidence.quote == body.documents[0].content for evidence in result.evidence.values())


def test_repair_prompt_over_budget_is_not_truncated_or_sent(monkeypatch):
    from guikerong_finance.service.cloud_matching import build_messages

    body, products, calls = request(), catalog(), []
    limit = sum(len(m["content"]) for m in build_messages(products.products[0], body, date(2026, 10, 22))) + 10
    answer = assessment(body)
    answer["conditions"] = []

    def handler(_):
        calls.append(True)
        return completion(answer)

    install_transport(monkeypatch, handler)
    result = match_company(body, products, settings(ark_max_prompt_characters=limit))
    assert len(calls) == 1
    assert result.status == "PARTIAL"
    assert result.summary.evaluated_count == 0
    assert result.summary.failed_count == 1
    assert result.failed_products[0].product_id == "p1"
    assert result.failed_products[0].code == "CLOUD_CONTEXT_TOO_LARGE"


def test_overall_deadline_also_cancels_a_correction_call(monkeypatch):
    body, entered, cancelled = request(), [], []
    answer = assessment(body)
    answer["conditions"] = []

    async def handler(_):
        entered.append(True)
        if len(entered) == 1:
            return completion(answer)
        try:
            await asyncio.sleep(5)
        finally:
            cancelled.append(True)

    install_transport(monkeypatch, handler)
    with pytest.raises(DomainError) as exc:
        match_company(body, catalog(), settings(timeout_seconds=0.2))
    assert exc.value.code == "MATCH_TIMEOUT"
    assert len(entered) == 2
    assert cancelled == [True]


@pytest.mark.parametrize("status", ["PASS", "FAIL"])
def test_no_citation_loses_only_its_condition_points_without_aborting_other_products(monkeypatch, status):
    body, products, calls = request(), catalog(), []
    products.products.append(
        products.products[0].model_copy(update={"product_id": "p2", "product_name": "第二测试产品"})
    )

    def handler(http_request):
        context = json.loads(json.loads(http_request.content)["messages"][-1]["content"])
        identifier = local_product_id(products, context)
        calls.append(identifier)
        answer = assessment(body)
        if identifier == "p1":
            answer["conditions"][0].update(status=status, citations=[], explanation="没有资质证明。")
            answer["eligibility"].update(status=status, citations=[], explanation="没有资质证明。")
        return completion(answer)

    install_transport(monkeypatch, handler)
    result = match_company(body, products, settings())
    assert len(calls) == 2
    assert result.summary.fully_satisfied_count == 1
    assert result.summary.pending_count == 1
    assert result.summary.not_matched_count == 0
    assert [item.product_id for item in result.matches] == ["p2", "p1"]
    assert [item.match_score for item in result.matches] == [100, 50]
    condition = result.matches[1].eligibility.condition_results[0]
    assert condition.status == "UNKNOWN"
    assert condition.required_material
    assert result.matches[1].required_actions


def test_progress_counts_real_completions_and_admitted_requests_only(monkeypatch):
    body, products, events = request(), catalog(), []
    base = products.products[0]
    products.products = [base.model_copy(update={"product_id": f"p{i}", "product_name": f"产品{i}"}) for i in range(4)]
    products.products.append(base.model_copy(update={"product_id": "broken", "availability_status": "UNAVAILABLE"}))
    states, seen = {}, set()

    def progress(event):
        events.append(event)
        if event["type"] == "product":
            states[event["product_id"]] = event["status"]
            assert sum(status in {"running", "repairing"} for status in states.values()) <= 2

    async def handler(http_request):
        context = json.loads(json.loads(http_request.content)["messages"][-1]["content"])
        identifier = local_product_id(products, context)
        assert states[identifier] in {"running", "repairing"}
        answer = assessment(body)
        if identifier == "p0" and identifier not in seen:
            answer["conditions"][0]["citations"][0]["quote"] = "不在原文中的引文"
        seen.add(identifier)
        await asyncio.sleep(0.01)
        return completion(answer)

    install_transport(monkeypatch, handler)
    result = asyncio.run(match_company_async(body, products, settings(ark_concurrency=2), on_progress=progress))
    assert result.summary.evaluated_count == 4
    assert events[0]["type"] == "started"
    assert events[0]["total"] == 4
    assert events[0]["products"][-1]["status"] == "skipped"
    assert [event["phase"] for event in events if event["type"] == "phase"] == [
        "preparing",
        "matching",
        "finalizing",
    ]
    completed_events = [event for event in events if event.get("status") == "completed"]
    assert [event["completed"] for event in completed_events] == [1, 2, 3, 4]
    assert {event["total"] for event in completed_events} == {4}
    assert {event["eligibility_status"] for event in completed_events} == {"PASS"}
    assert {event["match_score"] for event in completed_events} == {100}
    assert {event["matched_conditions"] for event in completed_events} == {2}
    assert {event["total_conditions"] for event in completed_events} == {2}
    p0 = [event["status"] for event in events if event.get("product_id") == "p0"]
    assert p0 == ["running", "validating", "queued", "repairing", "validating", "completed"]
    serialized = json.dumps(events, ensure_ascii=False)
    for private in [body.company.name, body.documents[0].content, "test-ark-key", "不在原文中的引文"]:
        assert private not in serialized


@pytest.mark.parametrize("overall_status", ["PASS", "UNKNOWN", "FAIL"])
def test_progress_exposes_condition_score_regardless_of_overall_verdict(monkeypatch, overall_status):
    events: list
    body, events = request(), []
    products = catalog(application_conditions="成立满一年且从事半导体材料研发生产")
    answer = assessment(body)
    answer["conditions"][0].update(status="UNKNOWN", citations=[], explanation="年限资料不足，需结合另一分支判断。")
    answer["conditions"][1]["source_quote"] = "从事半导体材料研发生产"
    answer["eligibility"].update(status=overall_status, explanation="专家根据全部产品原文给出的整体结论。")
    install_transport(monkeypatch, lambda _: completion(answer))
    result = asyncio.run(match_company_async(body, products, settings(), on_progress=events.append))
    completed = next(event for event in events if event.get("status") == "completed")
    assert completed["eligibility_status"] == overall_status
    assert completed["match_score"] == 50
    assert completed["matched_conditions"] == 1
    assert completed["total_conditions"] == 2
    assert "50" in completed["message"]
    assert "项判断待核验" not in completed["message"]
    assert result.summary.fully_satisfied_count == int(overall_status == "PASS")
    assert result.summary.pending_count == int(overall_status == "UNKNOWN")
    assert result.summary.not_matched_count == int(overall_status == "FAIL")
    assert result.matches[0].match_score == 50


def test_progress_failure_cancels_other_products_without_claiming_completion(monkeypatch):
    events: list
    products, events = catalog(), []
    products.products = [
        products.products[0].model_copy(update={"product_id": f"p{i}", "product_name": f"产品{i}"}) for i in range(4)
    ]

    async def handler(http_request):
        context = json.loads(json.loads(http_request.content)["messages"][-1]["content"])
        if local_product_id(products, context) == "p0":
            await asyncio.sleep(0.01)
            return httpx.Response(401)
        await asyncio.sleep(5)
        return None

    install_transport(monkeypatch, handler)
    with pytest.raises(DomainError) as exc:
        asyncio.run(match_company_async(request(), products, settings(ark_concurrency=2), on_progress=events.append))
    assert exc.value.code == "ARK_AUTH_ERROR"
    statuses = {event["product_id"]: event["status"] for event in events if event["type"] == "product"}
    assert statuses == {"p0": "failed", "p1": "cancelled", "p2": "cancelled", "p3": "cancelled"}
    assert all(event["completed"] == 0 for event in events if event["type"] == "product")
    assert not any(event.get("phase") == "finalizing" for event in events)


@pytest.mark.parametrize(
    ("company_name", "document_count"),
    [
        ("广西贺州市桂东电子科技有限公司", 28),
        ("广西汇发科技开发有限公司", 20),
    ],
)
@pytest.mark.parametrize("expert_status", ["UNKNOWN", "PASS"])
def test_real_company_documents_are_sent_completely_for_every_available_product(
    monkeypatch,
    company_name,
    document_count,
    expert_status,
):
    from guikerong_finance.service.catalog import load_catalog

    directory = ROOT / "公司" / company_name
    body = read_company_directory(directory, name=company_name)
    expected_documents = [doc.model_dump(mode="json") for doc in body.documents]
    assert len(expected_documents) == document_count
    for document in expected_documents:
        assert document["content"] == (directory / document["filename"]).read_bytes().decode("utf-8-sig")
    calls = []
    config = settings()
    products = load_catalog(config.csv_path)
    expected_products = {
        (product.product_name, product.application_conditions): product for product in products.products
    }

    def handler(http_request):
        envelope = json.loads(http_request.content)
        context = json.loads(envelope["messages"][-1]["content"])
        product_key = (context["product"]["product_name"], context["product"]["application_conditions"])
        calls.append(product_key)
        assert context["documents"] == expected_documents
        assert all(document["content"] for document in context["documents"])
        product = expected_products[product_key]
        assert "required_condition_ids" not in context
        assert context["product"] == {
            "product_name": product.product_name,
            "application_conditions": product.application_conditions,
        }
        assert "product_id" not in json.dumps(envelope, ensure_ascii=False)
        assert product.product_id not in json.dumps(envelope, ensure_ascii=False)
        # Synthetic verdicts verify orchestration and absence of legacy gates, not real credit eligibility.
        answer = context["response_example"]
        if expert_status == "PASS":
            for item in [answer["eligibility"], *answer["conditions"]]:
                item.update(
                    status="PASS",
                    explanation="离线模拟金融专家整体判定通过。",
                    subject_id="company",
                    citations=[
                        {"document_id": body.documents[0].document_id, "quote": body.documents[0].content[:100]}
                    ],
                    requires_human_verification=False,
                    required_material=None,
                )
        return completion(answer)

    install_transport(monkeypatch, handler)
    result = match_company(body, products, config)
    assert len(calls) == len(set(calls)) == 36
    assert result.summary.catalog_total == 36
    assert result.summary.unavailable_count == 0
    if expert_status == "PASS":
        assert result.summary.fully_satisfied_count == 36
        assert result.summary.pending_count == 0
        assert all(
            product.model_assessment is not None
            for product in [p for p in result.matches if p.decision == "FULLY_SATISFIED"]
        )
        assert all(
            product.required_actions == []
            for product in [p for p in result.matches if p.decision == "FULLY_SATISFIED"]
        )
    else:
        assert result.summary.pending_count == 36
        assert [p for p in result.matches if p.decision == "FULLY_SATISFIED"] == []
        assert all(
            product.model_assessment is not None
            for product in [p for p in result.matches if p.decision == "PENDING_VERIFICATION"]
        )


@pytest.mark.parametrize("name", ["科企订单贷", "善科贷", "桂盛·专利贷"])
def test_real_product_raw_input_and_tolerant_model_output(monkeypatch, name):
    from guikerong_finance.service.catalog import load_catalog

    body = request()
    source = load_catalog(settings().csv_path)
    product = next(item for item in source.products if item.product_name == name)
    # Include formatting around real clauses to verify neither input nor result
    # silently strips original source whitespace.
    product = product.model_copy(update={"application_conditions": " \r\n" + product.application_conditions + "\r\n "})
    selected = Catalog(products=[product], version=source.version, raw_record_count=1)
    calls = []
    model_description = "模型重新整理的条件描述：企业应正常经营。"

    def handler(http_request):
        context = json.loads(json.loads(http_request.content)["messages"][-1]["content"])
        calls.append(context)
        assert context["product"] == {"product_name": name, "application_conditions": product.application_conditions}
        assert context["documents"] == [doc.model_dump(mode="json") for doc in body.documents]
        # Missing overall verdict, extra fields, Chinese status, string bool and
        # a single citation object must not discard useful per-condition results.
        return completion(
            {
                "conditions": [
                    {
                        "status": "符合",
                        "reason": "模拟模型说明。",
                        "requires_human_verification": "false",
                        "source_quote": model_description,
                        "citations": {
                            "document_id": body.documents[0].document_id,
                            "quote": body.documents[0].content,
                        },
                        "extra_field": "ignored",
                    }
                ],
                "extra_summary": "ignored",
            }
        )

    install_transport(monkeypatch, handler)
    result = match_company(body, selected, settings())
    assert len(calls) == 1
    assert result.status == "COMPLETED"
    assert result.failed_products == []
    item = result.matches[0]
    assert item.match_score == 100
    assert item.application_conditions == product.application_conditions
    assert item.eligibility.status == "UNKNOWN"  # No invented overall verdict.
    assert item.eligibility.condition_results[0].source_quote == model_description
    assert item.eligibility.condition_results[0].source_quote_verified is False
    assert item.validation_warnings
    assert {e.quote for e in result.evidence.values()} == {body.documents[0].content}


def test_overlapping_sources_do_not_trigger_extra_calls_with_complete_real_documents(monkeypatch):
    """Replay the observed structural error; the stub does not prove model accuracy."""
    from guikerong_finance.service.catalog import load_catalog

    body = read_company_directory(
        ROOT / "公司" / "广西贺州市桂东电子科技有限公司", name="广西贺州市桂东电子科技有限公司"
    )
    source = load_catalog(settings().csv_path)
    product = next(p for p in source.products if p.product_name == "科企贷")
    products = Catalog(products=[product], version=source.version, raw_record_count=1)
    document = next(d for d in body.documents if d.filename.endswith("技术与研发字段.md"))
    # The three existing rows establish type/grant, applicant and legal status.
    rows = [
        line
        for line in document.content.splitlines()
        if any(
            f"| {field} |" in line
            for field in (
                "专利名称、类型、摘要和申请信息",
                "专利申请人、发明人和共同申请人",
                "专利法律状态和有效性",
            )
        )
    ]
    assert len(rows) == 3
    judgement = {
        "status": "PASS",
        "subject_id": "company",
        "explanation": "已授权发明专利符合I类路径，其余任选分支不影响结论。",
        "citations": [{"document_id": document.document_id, "quote": row} for row in rows],
        "requires_human_verification": False,
        "required_material": None,
    }
    good: dict = {
        "eligibility": judgement,
        "conditions": [
            {
                **judgement,
                "condition_id": "choice_group",
                "source_field": "application_conditions",
                "source_quote": product.application_conditions,
            }
        ],
    }
    bad = deepcopy(good)
    bad["conditions"].append(
        {
            **judgement,
            "condition_id": "unused_talent",
            "status": "UNKNOWN",
            "citations": [],
            "source_field": "application_conditions",
            "source_quote": "企业主/主要股东获得国家或地方政府颁发的高层次人才称号",
            "explanation": "未提供人才证明。",
            "requires_human_verification": True,
            "required_material": "补充人才证明。",
        }
    )
    calls = []

    def handler(http_request):
        context = json.loads(json.loads(http_request.content)["messages"][-1]["content"])
        assert context["documents"] == [d.model_dump(mode="json") for d in body.documents]
        assert context["product"] == {
            "product_name": product.product_name,
            "application_conditions": product.application_conditions,
        }
        calls.append(context)
        assert "validation_feedback" not in context
        return completion(bad)

    install_transport(monkeypatch, handler)
    result = match_company(body, products, settings())
    assert len(calls) == 1
    item = result.matches[0]
    assert result.status == "COMPLETED"
    assert item.match_score == 50
    assert item.match_breakdown.total_conditions == 2
    assert item.match_breakdown.matched_conditions == 1
    assert len(item.required_actions) == 1
    assert item.eligibility.status == "PASS"
    assert {e.quote for e in result.evidence.values()} == set(rows)
    assert {e.line_start for e in result.evidence.values()} == {12, 13, 14}


@pytest.mark.parametrize(
    "fact",
    [
        "本企业一项发明专利已公开，正在审查，尚未授权。",
        "本企业拥有专利117件，未说明类型、授权状态及逐件归属。",
        "本企业仅有一项已授权实用新型专利，没有其他知识产权或人才称号。",
        "关联公司拥有一项已授权发明专利，申请企业未取得该项权利。",
    ],
)
def test_patent_keywords_do_not_override_a_models_unconfirmed_condition(monkeypatch, fact):
    body = request()
    body.documents = [body.documents[0].model_copy(update={"content": fact})]
    terms = "拥有1项以上I类自主知识产权或2项以上II类自主知识产权；股东有人才称号；满足一项即可"
    products = catalog(application_conditions=terms)
    judgement = {
        "status": "UNKNOWN",
        "subject_id": "company",
        "explanation": "现有实例不足以确认至少一条路径达标。",
        "citations": [{"document_id": "d1", "quote": fact}],
        "requires_human_verification": True,
        "required_material": "核对合格知识产权或人才认定。",
    }
    answer = {
        "eligibility": judgement,
        "conditions": [
            {
                **judgement,
                "condition_id": "choice",
                "source_field": "application_conditions",
                "source_quote": terms,
            }
        ],
    }
    install_transport(monkeypatch, lambda _: completion(answer))
    item = match_company(body, products, settings()).matches[0]
    assert item.match_score == 0
    assert item.match_breakdown.unknown_conditions == 1
    assert item.eligibility.condition_results[0].status == "UNKNOWN"


@pytest.fixture(autouse=True)
def isolated_chat_store(tmp_path, monkeypatch):
    from guikerong_finance.db.events import EventStore

    monkeypatch.setattr(
        "guikerong_finance.application.EventStore", lambda _: EventStore(tmp_path / "integration.sqlite3")
    )


@pytest.mark.parametrize(
    ("terms", "fact", "status"),
    [
        ("高科技企业", "本企业主营软件研发，已向客户交付自研设备管理软件。", "PASS"),
        ("具有国家高新技术企业资质", "本企业主营软件研发，尚未提供高新技术企业认定信息。", "UNKNOWN"),
        ("国家高新技术企业", "企业名称为甲科技有限公司，实际业务情况未提供。", "UNKNOWN"),
        ("实际经营期限长于本次贷款期限", "本企业实际经营五年，本次申请贷款期限未确定。", "UNKNOWN"),
    ],
)
def test_contextual_assessments_keep_evidence_and_do_not_become_blanket_pass(monkeypatch, terms, fact, status):
    """Verify the request/result pipeline with fixtures, not live model reasoning accuracy."""
    from guikerong_finance.service.cloud_matching import MATCHING_INTERPRETATION_POLICY

    body = request()
    body.documents = [body.documents[0].model_copy(update={"content": fact})]
    products = catalog(application_conditions=terms)
    judgement = {
        "status": status,
        "subject_id": "company",
        "explanation": "研发及软件交付事实支持科技属性。"
        if status == "PASS"
        else "该必要条件所需的具体事实尚不完整。",
        "citations": [{"document_id": "d1", "quote": fact}],
        "requires_human_verification": status == "UNKNOWN",
        "required_material": "请确认原文所要求的具体资格或期限。" if status == "UNKNOWN" else None,
        "verification_needs": ["MATERIAL_CONFIRMATION"] if status == "UNKNOWN" else [],
    }
    answer = {
        "eligibility": judgement,
        "conditions": [
            {
                **judgement,
                "condition_id": "requirement",
                "source_field": "application_conditions",
                "source_quote": terms,
            }
        ],
    }

    def handler(http_request):
        messages = json.loads(http_request.content)["messages"]
        assert MATCHING_INTERPRETATION_POLICY in messages[0]["content"]
        context = json.loads(messages[-1]["content"])
        assert context["product"]["application_conditions"] == terms
        assert context["documents"] == [doc.model_dump(mode="json") for doc in body.documents]
        return completion(answer)

    install_transport(monkeypatch, handler)
    result = match_company(body, products, settings())
    item = result.matches[0]
    assert item.eligibility.status == status
    assert item.eligibility.condition_results[0].status == status
    assert {e.quote for e in result.evidence.values()} == {fact}
    assert result.versions["prompt"] == "product-preliminary-policy-4.6"
