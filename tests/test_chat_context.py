"""Chat context stays within budget without truncating source facts or mutating history."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from guikerong_finance.db.events import EventStore
from guikerong_finance.errors import DomainError
from guikerong_finance.prompts.chat_context import build_chat_context, build_chat_messages
from guikerong_finance.service.harness import CHAT_SYSTEM
from tests.test_chat_composer import ask
from tests.test_continuation import save
from tests.test_harness import create, draft, environment, match, publish, request_body, review

__all__ = ["environment"]


def size(messages):
    return sum(len(m["content"]) for m in messages)


def set_budget(environment, budget):
    _, settings, _, application = environment
    configured = replace(settings, ark_max_prompt_characters=budget)
    application.state.harness.settings = configured
    return configured


def test_projection_retains_every_judgement_and_deduplicates_evidence(environment):
    client, _, _, _ = environment
    state = match(client, create(client))
    before = deepcopy(state)
    context = build_chat_context(state["request"], state["effective_matches"], {})
    assert context["company_documents"] == state["request"]
    assert len(context["current_result"]) == len(state["effective_matches"])
    assert len(context["evidence"]) == 1  # The same source supports both products.
    for original, compact in zip(state["effective_matches"], context["current_result"], strict=True):
        assert "model_assessment" not in compact
        assert "evidence" not in compact
        assert compact["eligibility"]["explanation"] == original["eligibility"]["explanation"]
        for saved, condition in zip(
            original["eligibility"]["condition_results"], compact["eligibility"]["condition_results"], strict=True
        ):
            for key in ("condition_id", "source_quote", "status", "assessment_basis", "explanation"):
                assert condition[key] == saved[key]
    assert all("source_text" not in e and "quote" in e for e in context["evidence"].values())
    assert state == before


def test_projection_keeps_changed_document_evidence_as_historical_quote(environment):
    client, _, _, _ = environment
    state = match(client, create(client))
    state["request"]["documents"][0]["content"] = "修订后的正文，不包含旧引文。\r\n  "
    context = build_chat_context(state["request"], state["effective_matches"], {})
    assert context["company_documents"]["documents"][0]["content"] == "修订后的正文，不包含旧引文。\r\n  "
    assert next(iter(context["evidence"].values()))["historical_quote"] == "示例科技有限公司于2020年成立。"


def test_inserted_document_lines_keep_exact_quote_instead_of_pointing_at_new_text(environment):
    client, _, _, _ = environment
    state = match(client, create(client))
    state["request"]["documents"][0]["content"] = (
        "新增第一行。\n新增第二行。\n" + state["request"]["documents"][0]["content"]
    )
    context = build_chat_context(state["request"], state["effective_matches"], {})
    reference = next(iter(context["evidence"].values()))
    assert reference["line_start"] == 1  # Still describes the saved run, not current lines.
    assert reference["quote"] == "示例科技有限公司于2020年成立。"
    assert "historical_quote" not in reference
    assert "不能当作修订后资料的当前行号" in context["context_projection"]["note"]


def test_scoped_question_filters_other_product_documents_results_knowledge_and_history(environment):
    client, _, calls, _ = environment
    publish(client, draft(client, product_id="p1", content="第一产品的解释。"))
    publish(client, draft(client, product_id="p2", content="第二产品专属解释。"))
    shared = publish(client, draft(client, product_id=None, condition_quote="", content="共同专业解释。"))
    state = match(client, create(client))
    state = save(client, state, [{"condition_id": "ip", "content": "第一产品专属补充。"}]).json()
    state = save(client, state, [{"condition_id": "ip", "content": "第二产品专属补充。"}], product_id="p2").json()
    state = ask(client, state, product_id="p2", message="第二产品专属追问。").json()
    response = ask(client, state, product_id="p1")
    assert response.status_code == 200, response.text
    messages = calls[-1]["messages"]
    context = json.loads(messages[1]["content"])
    assert [p["product_id"] for p in context["current_result"]] == ["p1"]
    assert len(context["company_documents"]["documents"]) == 2
    assert [k["id"] for k in context["knowledge"]].count(shared["id"]) == 1
    assert all(p["product_id"] == "p1" for k in context["knowledge"] for p in k["applicable_products"])
    assert "第二产品专属" not in json.dumps(messages, ensure_ascii=False)
    assert response.json()["request"] == state["request"]


def test_historical_scope_uses_original_catalog_documents_and_knowledge(environment):
    client, _, calls, application = environment
    old = publish(client, draft(client, content="当时适用的专业解释。"))
    state = match(client, create(client))
    state = save(client, state, [{"condition_id": "ip", "content": "原轮次专属补充。"}]).json()
    first = match(client, state, ["p1"])
    publish(client, old, status="RETIRED")
    publish(client, draft(client, content="之后才发布的新专业解释。"))
    later = save(client, first, [{"condition_id": "ip", "content": "后续新增企业事实。"}]).json()
    application.state.catalog.products[0].application_conditions += "；新增条件"
    response = ask(client, later, run_id=first["run"]["id"], product_id="p1")
    assert response.status_code == 200, response.text
    messages = calls[-1]["messages"]
    context = json.loads(messages[1]["content"])
    assert context["company_documents"] == first["request"]
    assert len(messages) == 3
    serialized = json.dumps(messages, ensure_ascii=False)
    assert "当时适用的专业解释" in serialized
    assert "原轮次专属补充" in serialized
    assert "之后才发布的新专业解释" not in serialized
    assert "后续新增企业事实" not in serialized
    assert "新增条件" not in serialized


def test_overview_discloses_omitted_details_and_preserves_all_conditions_and_human_decisions(environment):
    client, _, _, _ = environment
    state = match(client, create(client))
    state = review(client, state, status="UNKNOWN", verification_needs=["MATERIAL_CONFIRMATION"]).json()
    context = build_chat_context(state["request"], state["effective_matches"], {}, overview=True)
    assert context["context_projection"]["result_detail"] == "overview"
    assert "未提供详细判断理由" in context["context_projection"]["note"]
    assert "evidence" not in context
    for original, compact in zip(state["effective_matches"], context["current_result"], strict=True):
        assert "explanation" not in compact["eligibility"]
        for saved, condition in zip(
            original["eligibility"]["condition_results"], compact["eligibility"]["condition_results"], strict=True
        ):
            for key in ("source_quote", "status", "assessment_basis", "source_quote_verified"):
                assert condition[key] == saved[key]
            assert condition.get("required_material") == saved["required_material"]
            assert condition.get("verification_needs", []) == saved["verification_needs"]
    p1 = next(p for p in context["current_result"] if p["product_id"] == "p1")
    assert p1["human_reviews"]["ip"]["reviewer"] == "审核员甲"
    assert p1["human_reviews"]["ip"]["status"] == "UNKNOWN"


def test_history_window_keeps_latest_complete_turn_and_entire_new_question():
    context = build_chat_context(request_body(), [], {})
    recent = [
        {"role": "user", "content": "最近的问题", "context_label": "提问"},
        {"role": "assistant", "content": "最近的完整回答", "context_label": "提问"},
    ]
    older = [{"role": "user", "content": "老问题" * 3000}, {"role": "assistant", "content": "老回答" * 3000}]
    question = "这次的问题必须原样保留。\n" * 20
    exact = size(build_chat_messages("system", context, recent, question, 100000))
    messages = build_chat_messages("system", context, [*older, *recent], question, exact)
    assert size(messages) == exact
    assert len(messages) == 5
    assert messages[-1]["content"] == question
    assert messages[-3]["content"].endswith("最近的问题")
    assert messages[-2]["content"] == "最近的完整回答"
    assert json.loads(messages[1]["content"])["conversation_window"]["omitted_messages"] == 2
    assert "老问题" not in json.dumps(messages, ensure_ascii=False)


def test_history_window_never_keeps_partial_answer_or_skips_large_newer_turn_for_older_one():
    context = build_chat_context(request_body(), [], {})
    history = [
        {"role": "assistant", "content": "无对应问题的状态通知"},
        {"role": "user", "content": "很早的问题"},
        {"role": "assistant", "content": "很早的回答"},
        {"role": "user", "content": "最近的问题"},
        {"role": "assistant", "content": "长回答" * 5000},
        {"role": "user", "content": "失败而没有回答的问题"},
    ]
    messages = build_chat_messages("system", context, history, "现在的问题", 3000)
    assert len(messages) == 3
    assert json.loads(messages[1]["content"])["conversation_window"]["omitted_messages"] == len(history)


def test_document_notification_is_not_misread_as_answer_to_failed_question():
    history = [
        {"role": "user", "content": "此前失败的问题", "context_label": "提问"},
        {"role": "assistant", "content": "文件已保存，请重新匹配。"},
    ]
    messages = build_chat_messages("system", build_chat_context(request_body(), [], {}), history, "新问题", 10000)
    assert len(messages) == 3


def test_budget_window_does_not_delete_saved_history(environment):
    client, settings, calls, _ = environment
    state = create(client)
    store = EventStore(settings.harness_db_path)
    store.append(
        state["id"],
        state["version"],
        [
            ("user/message", {"content": "早前问题" * 8000, "context_label": "提问"}),
            ("assistant/message", {"content": "早前回答" * 8000, "context_label": "提问"}),
            ("user/message", {"content": "最近的问题", "context_label": "提问"}),
            ("assistant/message", {"content": "最近的回答", "context_label": "提问"}),
        ],
    )
    state = client.get(f"/api/v1/sessions/{state['id']}").json()
    before = store.read(state["id"])
    settings = set_budget(environment, len(CHAT_SYSTEM) + 5000)
    response = ask(client, state)
    assert response.status_code == 200, response.text
    assert size(calls[-1]["messages"]) <= settings.ark_max_prompt_characters
    assert store.read(state["id"])[: len(before)] == before
    assert response.json()["messages"][: len(state["messages"])] == state["messages"]
    assert json.loads(calls[-1]["messages"][1]["content"])["conversation_window"]["omitted_messages"] == 2


def test_all_product_prompt_falls_back_to_disclosed_overview(environment):
    client, _, calls, _ = environment
    state = match(client, create(client))
    context = build_chat_context(state["request"], state["effective_matches"], {}, overview=True)
    set_budget(environment, size(build_chat_messages(CHAT_SYSTEM, context, [], "总览", 200000)))
    response = ask(client, state, message="总览")
    assert response.status_code == 200, response.text
    actual = json.loads(calls[-1]["messages"][1]["content"])
    assert actual["context_projection"]["result_detail"] == "overview"
    assert actual["company_documents"] == state["request"]
    assert len(actual["current_result"]) == 2
    assert response.json()["effective_matches"] == state["effective_matches"]


@pytest.mark.parametrize("product_id", [None, "p1"])
def test_irreducible_oversize_has_actionable_error_and_no_writes_or_provider_calls(environment, product_id):
    client, settings, calls, _ = environment
    state = match(client, create(client))
    store = EventStore(settings.harness_db_path)
    before, count = store.read(state["id"]), len(calls)
    set_budget(environment, len(CHAT_SYSTEM) + 5)
    response = ask(client, state, product_id=product_id)
    assert response.status_code == 422
    assert "选择具体产品" in response.text
    assert "建立新会话" not in response.text
    assert store.read(state["id"]) == before
    assert len(calls) == count


def test_scoped_question_does_not_silently_discard_detailed_explanations(environment):
    client, _, calls, application = environment
    state = match(client, create(client))
    context = build_chat_context(
        state["request"], state["effective_matches"], {}, product=application.state.catalog.products[0], overview=True
    )
    set_budget(environment, size(build_chat_messages(CHAT_SYSTEM, context, [], "问题", 200000)))
    count = len(calls)
    assert ask(client, state, product_id="p1", message="问题").status_code == 422
    assert len(calls) == count


def test_large_latest_question_is_never_truncated_to_fit():
    with pytest.raises(DomainError, match="当前提问的资料范围"):
        build_chat_messages("system", build_chat_context(request_body(), [], {}), [], "完整新问题" * 1000, 500)
