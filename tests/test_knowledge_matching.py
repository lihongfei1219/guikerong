import json
from copy import deepcopy

import pytest

from guikerong_finance.service.knowledge_matching import match_condition, matching_knowledge
from tests.test_harness import create, draft, environment, match, publish, sample_catalog

__all__ = ["environment"]


@pytest.mark.parametrize(
    ("query", "conditions", "expected"),
    [
        ("知识产权", "企业成立满一年；拥有I类知识产权", "拥有I类知识产权"),
        ("Ｉ 类 知识产权！", "企业成立满一年；拥有Ⅰ类知识产权", "拥有Ⅰ类知识产权"),
        ("拥有一类知识产权", "企业成立满一年；拥有I类知识产权", "拥有I类知识产权"),
        ("拥有I类知识产全", "企业成立满一年；拥有I类知识产权", "拥有I类知识产权"),
        ("贷款期限", "企业成立满一年；拥有I类知识产权", None),
        ("!!!", "企业成立满一年", None),
        ("企", "企业成立满一年", None),
        ("知识产权", "", None),
    ],
)
def test_condition_matching_supports_keywords_and_approximate_text(query, conditions, expected):
    assert match_condition(query, conditions) == expected


def test_fuzzy_scoped_knowledge_carries_full_explanation_and_matching_original_clause(environment):
    client, _, calls, _ = environment
    explanation = "这里填写完整的分类解释。\n补充适用边界及判断方法，不改变申请条件。"
    item = publish(client, draft(client, condition_quote="拥有一类知识产权", content=explanation))
    session = match(client, create(client))
    contexts = [json.loads(call["messages"][1]["content"]) for call in calls]
    assert contexts[0]["expert_knowledge"][0]["id"] == item["id"]
    assert contexts[0]["expert_knowledge"][0]["content"] == explanation
    assert contexts[0]["expert_knowledge"][0]["condition_quote"] == "拥有一类知识产权"
    assert contexts[0]["expert_knowledge"][0]["matched_condition"] == "拥有I类知识产权"
    assert "expert_knowledge" not in contexts[1]
    history = client.get(f"/api/v1/sessions/{session['id']}/runs/{session['run']['id']}").json()
    assert history["snapshot"]["knowledge"]["p1"][0]["content"] == explanation


def test_general_keywords_route_only_to_matching_products_and_retirement_stops_injection(environment):
    client, _, _, application = environment
    item = publish(client, draft(client, product_id=None, condition_quote="知识产权"))
    catalog = sample_catalog()
    catalog.products[1].application_conditions = "企业成立满一年"
    harness = application.state.harness
    context = harness.knowledge_context(catalog)
    assert context["p1"][0]["id"] == item["id"]
    assert context["p2"] == []
    changed = catalog.model_copy(deep=True)
    changed.products[0].application_conditions += "；银行要求提供营业执照。"
    assert harness.knowledge_context(changed)["p1"]
    publish(client, item, "RETIRED")
    assert harness.knowledge_context(catalog) == {"p1": [], "p2": []}


def test_product_scope_and_matching_do_not_mutate_saved_knowledge():
    catalog = sample_catalog()
    item = {"product_id": "p1", "condition_quote": "知识产权", "content": "完整解释"}
    before = deepcopy(item)
    assert matching_knowledge(item, catalog.products[1]) is None
    matched = matching_knowledge(item, catalog.products[0])
    assert matched is not None
    assert matched["content"] == item["content"]
    assert item == before


@pytest.mark.parametrize(
    ("product_id", "query"), [("p1", "贷款期限"), (None, "贷款期限"), ("missing", "知识产权"), ("p1", "")]
)
def test_unmatched_or_invalid_scope_does_not_save_knowledge(environment, product_id, query):
    client, _, _, _ = environment
    response = client.post(
        "/api/v1/knowledge",
        json={
            "title": "解释",
            "content": "完整解释",
            "product_id": product_id,
            "condition_quote": query,
        },
    )
    assert response.status_code == 400
    assert client.get("/api/v1/knowledge").json()["items"] == []
