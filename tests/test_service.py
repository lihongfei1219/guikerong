import json
from datetime import datetime

import pytest
from support import match_company

from app.config import Settings
from app.errors import DomainError
from app.models import Catalog, CompanyInput, Document, MatchRequest, Product
from app.service import SHANGHAI, validate_input


def request():
    return MatchRequest(company=CompanyInput(name="测试科技有限公司"), documents=[
        Document(document_id="doc1", filename="任意格式.md", content="测试科技有限公司从事研发工作。")])


def product(product_id="p1", **updates):
    return Product(**{"product_id": product_id, "product_name": "测试产品", "institution": "测试机构",
                      "detail_url": "", "introduction": "科技研发企业周转融资",
                      "application_conditions": "成立满一年", **updates})


def match(products, payload=None, **config):
    return match_company(payload or request(), Catalog(products=products, version="test", raw_record_count=len(products)),
                         Settings(ark_api_key="offline", ark_model="offline", **config),
                         now=datetime(2026, 10, 22, 10, tzinfo=SHANGHAI))


@pytest.fixture
def model_answers(monkeypatch):
    """Return an explicit expert verdict independently from legacy rule trees."""
    outcomes = {}

    class FakeClient:
        def __init__(self, _):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def complete_json(self, messages, *, on_start=None):
            if on_start:
                on_start()
            context = json.loads(messages[-1]["content"])
            configured = {**outcomes, **outcomes.get("by_name", {}).get(context["product"]["product_name"], {})}
            answer = context["response_example"]
            assert set(context["product"]) == {"product_name", "application_conditions"}
            assert "required_condition_ids" not in context
            assert set(answer) == {"eligibility", "conditions"}
            answer["conditions"] = [{"condition_id": "age", "source_field": "application_conditions",
                                     "source_quote": configured.get("source_quote", "成立满一年")}]
            for item in [answer["eligibility"], *answer["conditions"]]:
                item.update(status="PASS", explanation="企业资料支持。", subject_id="company",
                            citations=[{"document_id": "doc1", "quote": "测试科技有限公司"}],
                            requires_human_verification=False, required_material=None)
            answer["eligibility"]["status"] = configured.get("eligibility", "PASS")
            answer["eligibility"]["explanation"] = configured.get("explanation", "专家根据完整产品信息确认符合条件。")
            if answer["eligibility"]["status"] == "UNKNOWN":
                answer["eligibility"]["required_material"] = "补充整体资格认定所需材料"
            for condition in configured.get("extra_conditions", []):
                answer["conditions"].append({**answer["conditions"][0], **condition})
            for item in answer["conditions"]:
                item["status"] = configured.get(item["condition_id"], "PASS")
                if item["status"] == "UNKNOWN":
                    item["required_material"] = "补充该条件对应材料"
            return answer

    monkeypatch.setattr("app.service.ArkClient", FakeClient)
    return outcomes


def test_all_unavailable_is_service_error(model_answers):
    with pytest.raises(DomainError) as error:
        match([product(availability_status="UNAVAILABLE")])
    assert error.value.code == "CATALOG_UNAVAILABLE"


@pytest.mark.parametrize("config,code", [
    ({"max_document_bytes": 1}, "INPUT_TOO_LARGE"),
    ({"max_total_document_bytes": 1}, "INPUT_TOO_LARGE"),
    ({"max_characters": 1}, "INPUT_COMPLEXITY_EXCEEDED"),
])
def test_oversize_and_complexity_are_explicit_errors(config, code):
    with pytest.raises(DomainError) as info:
        validate_input(request(), Settings(**config))
    assert info.value.code == code


def test_product_budget_rejects_before_model_calls(model_answers):
    with pytest.raises(DomainError) as error:
        match([product("p1"), product("p2")], ark_max_products=1)
    assert error.value.code == "CLOUD_PRODUCT_LIMIT"


def test_missing_credentials_are_an_explicit_configuration_error():
    with pytest.raises(DomainError) as error:
        match_company(request(), Catalog(products=[product()], version="test", raw_record_count=1), Settings())
    assert error.value.code == "MODEL_NOT_CONFIGURED"
    assert error.value.status_code == 503


@pytest.mark.parametrize("overall,decision", [
    ("PASS", "FULLY_SATISFIED"), ("UNKNOWN", "PENDING_VERIFICATION"), ("FAIL", "NOT_ELIGIBLE"),
])
@pytest.mark.parametrize("clause,score", [("PASS", 100), ("UNKNOWN", 0), ("FAIL", 0)])
def test_product_status_uses_overall_verdict_instead_of_partial_score(model_answers, overall, decision, clause, score):
    model_answers.update(eligibility=overall, age=clause)
    result = match([product()])
    item = result.matches[0]
    assert item.decision == decision
    assert item.match_score == score
    assert result.summary.fully_satisfied_count == int(overall == "PASS")
    assert result.summary.pending_count == int(overall == "UNKNOWN")
    assert result.summary.not_matched_count == int(overall == "FAIL")
    assert bool(item.verification_needs) == (overall == "UNKNOWN")
