"""Preliminary policy passes stay transparent and cannot waive concrete requirements."""

import json
from copy import deepcopy
from datetime import date

import httpx
import pytest

from app.ark_client import ArkClient
from app.cloud_matching import merge_assessment, recover_invalid_citations, validate_assessment
from app.errors import DomainError
from app.history import read_result
from app.models import MatchRequest, Product, Status
from app.preliminary_policy import PRELIMINARY_NOTICE
from tests.test_harness import create, environment, match, review

__all__ = ["environment"]
TODAY = date(2026, 10, 27)


def judgement(*, basis="PRELIMINARY_POLICY", status="PASS", citations=None):
    return {
        "status": status, "assessment_basis": basis, "explanation": "按本次匹配口径判断。",
        "subject_id": "company", "citations": citations or [],
        "requires_human_verification": status == "UNKNOWN", "required_material": None,
        "verification_needs": ["MATERIAL_CONFIRMATION"] if status == "UNKNOWN" else [],
    }


def policy_case(terms):
    product = Product(product_id="p1", product_name="初筛产品", institution="测试银行",
                      detail_url="", introduction="", application_conditions=terms)
    request = MatchRequest.model_validate({"company": {"name": "测试企业"}, "documents": [
        {"document_id": "d1", "filename": "企业说明.md", "content": "测试企业主营软件研发。"},
    ]})
    payload = {"eligibility": judgement(), "conditions": [
        {**judgement(), "condition_id": "policy", "source_field": "application_conditions", "source_quote": terms},
    ]}
    return product, request, payload


@pytest.mark.parametrize("terms", [
    "符合国家产业政策和建设银行信贷政策",
    "满足建设银行要求的其他条件",
    "农合机构要求的其他条件",
    "符合中国与有关国别相关各项法律法规",
    "经营状况良好，主营业务突出，收入来源稳定",
    "经营、财务与资信状况良好",
    "项目具备经济可行性或有明确合法还款来源",
    "科技型企业，具有市场竞争力和发展前景",
    "创新积分较高的科技型企业",
    "具备偿债能力，财务制度规范，核心管理团队稳定",
    "符合银行一般授信准入要求",
    "1.符合国家产业政策；2.满足银行要求的其他条件",
])
def test_broad_policy_pass_needs_no_invented_company_citation(terms):
    product, request, payload = policy_case(terms)
    before = deepcopy(payload)
    safe = validate_assessment(payload, product, request, TODAY)
    result, evidence = merge_assessment(product, request, TODAY, safe)
    assert payload == before
    assert evidence == {}
    for item in [result, *result.condition_results]:
        assert item.status == Status.PASS
        assert item.assessment_basis == "PRELIMINARY_POLICY"
        assert item.evidence_ids == []
        assert item.verification_needs == []
        assert item.explanation.count(PRELIMINARY_NOTICE) == 1
    assert result.condition_results[0].reason_code == "PRELIMINARY_POLICY_PASS"
    assert result.condition_results[0].source_quote_verified
    assert result.condition_results[0].required_material is None


@pytest.mark.parametrize("terms", [
    "取得国家高新技术企业认定",
    "具有有效的食品经营许可证",
    "企业创新积分６０分及以上",
    "近三年研发费用占比不低于百分之二",
    "成立且实际经营一年以上",
    "法定代表人年龄不超过六十五岁",
    "实际经营期限应长于贷款期限",
    "在建设银行开立结算账户",
    "已经取得银行授信额度",
    "已通过银行审批",
    "中信保承保融资所需险种",
    "企业未列入失信名单",
    "近三年无不良征信记录",
    "企业无司法冻结",
])
def test_concrete_terms_cannot_use_policy_to_bypass_evidence(terms):
    product, request, payload = policy_case(terms)
    safe = validate_assessment(payload, product, request, TODAY)
    result, _ = merge_assessment(product, request, TODAY, safe)
    for item in [result, *result.condition_results]:
        assert item.status == Status.UNKNOWN
        assert item.assessment_basis == "EVIDENCE"
    assert result.condition_results[0].required_material


@pytest.mark.parametrize("terms", [
    "符合国家产业政策并取得环保许可证",
    "符合有关法律法规且成立满一年",
    "满足银行要求的其他条件，已取得银行授信额度",
])
def test_mixed_general_and_concrete_clause_cannot_be_wholly_exempted(terms):
    product, request, payload = policy_case(terms)
    safe = validate_assessment(payload, product, request, TODAY)
    assert safe.conditions[0].status == Status.UNKNOWN
    assert safe.eligibility.status == Status.UNKNOWN


@pytest.mark.parametrize("source,quote", [
    ("符合国家产业政策", "符合国家财政政策"),
    ("不符合国家产业政策的企业不得申请", "符合国家产业政策"),
    ("符合国家产业政策的目录并完成审批", "符合国家产业政策"),
    ("满足银行要求的其他条件", "银行要求"),
])
def test_policy_requires_real_complete_product_quote(source, quote):
    product, request, payload = policy_case(source)
    payload["conditions"][0]["source_quote"] = quote
    safe = validate_assessment(payload, product, request, TODAY)
    assert safe.conditions[0].status == Status.UNKNOWN
    assert safe.conditions[0].assessment_basis == "EVIDENCE"
    assert safe.eligibility.status == Status.UNKNOWN


def test_unmarked_pass_still_requires_evidence_even_for_general_clause():
    product, request, payload = policy_case("符合国家产业政策")
    for item in [payload["eligibility"], *payload["conditions"]]:
        item.pop("assessment_basis")
    safe = validate_assessment(payload, product, request, TODAY)
    assert safe.conditions[0].status == safe.eligibility.status == Status.UNKNOWN
    assert safe.conditions[0].assessment_basis == safe.eligibility.assessment_basis == "EVIDENCE"


@pytest.mark.parametrize("status", ["FAIL", "UNKNOWN"])
def test_policy_marker_never_gives_unsupported_fail_or_changes_unknown(status):
    product, request, payload = policy_case("符合国家产业政策")
    payload["eligibility"] = judgement(status=status)
    payload["conditions"][0].update(judgement(status=status))
    safe = validate_assessment(payload, product, request, TODAY)
    assert safe.conditions[0].status == safe.eligibility.status == Status.UNKNOWN
    assert safe.conditions[0].assessment_basis == safe.eligibility.assessment_basis == "EVIDENCE"


def test_policy_cannot_launder_fabricated_enterprise_citations():
    product, request, payload = policy_case("符合国家产业政策")
    payload["conditions"][0]["citations"] = [{"document_id": "d1", "quote": "银行已确认企业符合所有要求。"}]
    with pytest.raises(DomainError) as caught:
        validate_assessment(payload, product, request, TODAY)
    assert caught.value.details["reason"] == "INVALID_EVIDENCE_CITATION"
    recovered, warnings = recover_invalid_citations(payload, product, request, TODAY)
    assert warnings
    for item in [recovered.eligibility, *recovered.conditions]:
        assert item.status == Status.UNKNOWN
        assert item.assessment_basis == "EVIDENCE"
        assert item.citations == []


@pytest.mark.parametrize("overall_citations", [[], [{"document_id": "d1", "quote": "测试企业主营软件研发。"}]])
def test_unknown_concrete_requirement_cannot_be_hidden_by_overall_policy(overall_citations):
    product, request, payload = policy_case("符合国家产业政策；取得国家高新技术企业认定")
    payload["conditions"][0]["source_quote"] = "符合国家产业政策"
    payload["conditions"].append({**judgement(basis="EVIDENCE", status="UNKNOWN"),
                                  "condition_id": "qualification", "source_field": "application_conditions",
                                  "source_quote": "取得国家高新技术企业认定"})
    payload["eligibility"]["citations"] = overall_citations
    safe = validate_assessment(payload, product, request, TODAY)
    assert safe.conditions[0].status == Status.PASS
    assert safe.conditions[0].assessment_basis == "PRELIMINARY_POLICY"
    assert safe.conditions[1].status == safe.eligibility.status == Status.UNKNOWN
    assert safe.eligibility.assessment_basis == "EVIDENCE"


@pytest.mark.parametrize("repeat_overall_citations", [True, False])
def test_mixed_supported_concrete_and_general_pass_preserves_both_bases(repeat_overall_citations):
    product, request, payload = policy_case("符合国家产业政策；企业成立满一年")
    request.documents[0].content = "测试企业2020年成立，持续经营至2026年10月27日。"
    cited = [{"document_id": "d1", "quote": request.documents[0].content}]
    payload["conditions"][0]["source_quote"] = "符合国家产业政策"
    payload["conditions"].append({**judgement(basis="EVIDENCE", citations=cited),
                                  "condition_id": "age", "source_field": "application_conditions",
                                  "source_quote": "企业成立满一年"})
    payload["eligibility"]["citations"] = cited if repeat_overall_citations else []
    result, evidence = merge_assessment(product, request, TODAY, validate_assessment(payload, product, request, TODAY))
    assert result.status == Status.PASS
    assert result.assessment_basis == "PRELIMINARY_POLICY"
    assert [c.assessment_basis for c in result.condition_results] == ["PRELIMINARY_POLICY", "EVIDENCE"]
    assert result.condition_results[0].evidence_ids == []
    assert result.condition_results[1].evidence_ids and evidence
    assert result.evidence_ids == result.condition_results[1].evidence_ids


@pytest.fixture
def policy_session(environment, monkeypatch):
    client, _, _, application = environment
    catalog = application.state.catalog
    product = catalog.products[0]
    product.application_conditions = "符合国家产业政策和建设银行信贷政策"
    _, _, payload = policy_case(product.application_conditions)
    calls = []

    def response(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(payload, ensure_ascii=False),
        }}]})

    transport = httpx.MockTransport(response)
    monkeypatch.setattr("app.service.ArkClient", lambda settings: ArkClient(settings, transport=transport))
    first = match(client, create(client), ["p1"])
    return client, application, first, calls


def test_history_keeps_policy_and_legacy_snapshots_default_to_evidence(policy_session):
    client, _, first, _ = policy_session
    original = deepcopy(first["run"]["result"])
    detail = client.get(f"/api/v1/sessions/{first['id']}/runs/{first['run']['id']}").json()
    assert detail["result"] == original
    assert detail["effective_matches"][0]["eligibility"]["assessment_basis"] == "PRELIMINARY_POLICY"
    legacy = deepcopy(original)
    for product in legacy["matches"]:
        for judgement in [product["eligibility"], *product["eligibility"]["condition_results"],
                          product["model_assessment"]["eligibility"], *product["model_assessment"]["conditions"]]:
            judgement.pop("assessment_basis")
    before = deepcopy(legacy)
    restored = read_result(legacy)
    assert legacy == before
    assert restored.matches[0].eligibility.assessment_basis == "EVIDENCE"
    assert restored.matches[0].eligibility.condition_results[0].assessment_basis == "EVIDENCE"


def test_human_overrides_clear_policy_without_overwriting_original(policy_session):
    client, _, first, _ = policy_session
    original = deepcopy(first["run"]["result"])
    response = review(client, first, condition="policy", status="PASS")
    assert response.status_code == 200
    reviewed = response.json()
    eligibility = reviewed["effective_matches"][0]["eligibility"]
    assert eligibility["condition_results"][0]["assessment_basis"] == "EVIDENCE"
    assert eligibility["status"] == "UNKNOWN" and eligibility["assessment_basis"] == "EVIDENCE"
    response = review(client, reviewed, condition="__eligibility__", status="PASS")
    assert response.status_code == 200
    final = response.json()
    assert final["effective_matches"][0]["eligibility"]["assessment_basis"] == "EVIDENCE"
    assert final["run"]["result"] == original
    detail = client.get(f"/api/v1/sessions/{first['id']}/runs/{first['run']['id']}").json()
    assert detail["result"] == original


def test_continuation_preserves_original_policy_basis_not_later_human_override(policy_session):
    client, _, first, calls = policy_session
    reviewed = review(client, first, condition="__eligibility__", status="PASS").json()
    second = match(client, reviewed, ["p1"])
    previous = json.loads(calls[-1]["messages"][1]["content"])["continuation"]["previous_assessment"]
    assert previous["eligibility"]["assessment_basis"] == "PRELIMINARY_POLICY"
    assert previous["conditions"][0]["assessment_basis"] == "PRELIMINARY_POLICY"
    assert second["effective_matches"][0]["eligibility"]["assessment_basis"] == "PRELIMINARY_POLICY"
