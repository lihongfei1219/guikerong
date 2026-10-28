"""组装完整匹配上下文：统一初筛口径、知识注入、继续核验和一次纠正。"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date

from guikerong_finance.errors import DomainError
from guikerong_finance.models.domain import MatchRequest, ModelProductAssessment, Product
from guikerong_finance.prompts.loader import prompt_text
from guikerong_finance.service.supplements import applicable_documents

MATCHING_PROMPT_VERSION = "product-preliminary-policy-4.6"

MATCHING_INTERPRETATION_POLICY = prompt_text("matching_interpretation_policy")

# IP terminology follows《高新技术企业认定管理工作指引》三（二）知识产权；
# it interprets existing product terms, without importing high-tech accreditation rules.
# https://www.szns.gov.cn/xxgk/bmxxgk/qkcj/qtgk_157956/2026ggpyfwzq/zczq/content/post_12862098.html
_SYSTEM_PROMPT = prompt_text("system_prompt")

_SYSTEM_PROMPT += """
产品结论只有三种：PASS=完全满足，UNKNOWN=待核验，FAIL=不符合条件。
整体资格按完整申请条件的且、或及豁免关系判断，不因部分条款通过就判断产品完全满足。
每项 judgement（含 eligibility）都返回 verification_needs 数组。PASS/FAIL 返回 []。
UNKNOWN 必须区分原因，可同时包含以下两种类型：
MATERIAL_CONFIRMATION：产品要求已经理解，但企业材料缺失、过期、矛盾，或具体事实/银行确认需用户进一步确认。
PROFESSIONAL_REVIEW：产品术语、适用口径、逻辑或专业要求超出可可靠判断的范围，需要专家解释或补充专业知识。
不得把不理解产品要求伪装成企业缺少材料，也不得把事实缺失称为专业术语不懂。
在 explanation 中说明具体疑点，required_material 填写对应下一步：前者说明需要确认哪项材料/事实，
后者说明哪句申请条件需要解释、缺少什么专业口径，而不是笼统要求用户补交证明。
若两类问题同时存在，verification_needs 同时列出两者。整体 UNKNOWN 汇总实际阻碍整体判断的类型。
只要已确认某必要条件明确不符合且无可用替代路径，即可整体 FAIL；不符合不能由资料缺失推断。
"""
_SYSTEM_PROMPT += MATCHING_INTERPRETATION_POLICY

_REPAIR_INSTRUCTION = prompt_text("repair_instruction")

RESPONSE_SCHEMA = ModelProductAssessment.model_json_schema()
_SUPPLEMENT_INSTRUCTION = prompt_text("supplement_instruction")


def _check_message_budget(messages: list[dict[str, str]], max_characters: int) -> None:
    """完整输入超预算即拒绝，不以截断企业原文换取可发送的请求。"""
    characters = sum(len(message["content"]) for message in messages)
    if characters > max_characters:
        raise DomainError(
            "CLOUD_CONTEXT_TOO_LARGE",
            "完整企业资料与产品条件超过模型输入预算，未发送且未截断资料。",
            422,
            {"characters": characters, "max_characters": max_characters},
        )


def build_messages(
    product: Product,
    request: MatchRequest,
    as_of: date,
    *,
    max_characters: int = 200000,
    expert_knowledge: list[dict] | None = None,
    continuation: dict | None = None,
) -> list[dict[str, str]]:
    """每次只评估一个产品，完整携带适用企业资料、补充事实和知识快照。"""
    condition_source = product.application_conditions
    if not condition_source.strip():
        raise DomainError("PRODUCT_SOURCE_MISSING", "产品缺少可评估的申请条件。", 500)
    unknown = {
        "status": "UNKNOWN",
        "explanation": "说明原文依据与资料缺口。",
        "subject_id": "company",
        "citations": [],
        "requires_human_verification": True,
        "required_material": "说明具体所需材料。",
        "verification_needs": ["MATERIAL_CONFIRMATION"],
    }
    documents = applicable_documents(request, product)
    original = [d for d in documents if not d.human_supplement]
    supplements = [d for d in documents if d.human_supplement]
    response_schema = deepcopy(RESPONSE_SCHEMA)
    if not supplements:
        response_schema["properties"].pop("supplement_feedback")
        response_schema["$defs"].pop("SupplementFeedback")
    else:
        response_schema["required"].append("supplement_feedback")
    example_document = (original or supplements)[0]
    example_start = len(example_document.content) - len(example_document.content.lstrip())
    payload: dict = {
        "assessment_date": as_of.isoformat(),
        "company": request.company.model_dump(mode="json"),
        "documents": [document.model_dump(mode="json", exclude={"human_supplement"}) for document in original],
        "product": {
            "product_name": product.product_name,
            "application_conditions": product.application_conditions,
        },
        "response_example": {
            "eligibility": {**unknown, "explanation": "综合申请条件中的且、或和豁免，说明整体是否符合及关键依据。"},
            "conditions": [
                {
                    **unknown,
                    "condition_id": "c1",
                    "source_field": "application_conditions",
                    "source_quote": condition_source[:200],
                },
            ],
        },
        "response_schema": response_schema,
        "citation_example": {
            "document_id": example_document.document_id,
            "quote": example_document.content[example_start : example_start + 200],
        },
    }
    if expert_knowledge:
        payload["expert_knowledge"] = expert_knowledge
    if supplements:
        payload["human_supplements"] = [d.model_dump(mode="json") for d in supplements]
        payload["response_example"]["supplement_feedback"] = [
            {
                "document_id": d.document_id,
                "status": "NEEDS_CONFIRMATION",
                "reason": "说明具体核实情况。",
                "impact": "说明对应条款如何受影响、还缺什么。",
                "condition_ids": ["c1"],
            }
            for d in supplements
        ]
    if continuation:
        payload["continuation"] = {
            **continuation,
            "current_prompt_version": MATCHING_PROMPT_VERSION,
            "policy_changed": continuation.get("previous_prompt_version") != MATCHING_PROMPT_VERSION,
        }
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": content}]
    if supplements:
        messages[0]["content"] += _SUPPLEMENT_INSTRUCTION
    if continuation:
        messages[0]["content"] += (
            "\n本轮是同一产品的继续核验。continuation.previous_assessment 是上轮模型判断，仅供定位问题，"
            "不是企业事实、专业知识或本轮证据；不得引用其解释作为符合或不符合的依据，也不得机械沿用旧结论。"
            "policy_changed=true 表示上轮口径较旧或未知；必须先按本轮规则重新区分概括条款和具体要求，"
            "即使新增资料列表为空，也不能沿用旧轮的补件要求。历史required_material只是旧建议，不是申请条件。"
            "added_document_ids/updated_document_ids 指出本次新增或修订信息，removed_document_ids 中资料已删除，"
            "旧判断中依赖被修改或删除资料的事实必须重核，当前 documents/human_supplements 才是本轮资料。"
            "重点复核 focus_condition_quotes 和上轮待核验原因，同时完整检查产品所有条款及且、或、豁免关系，"
            "返回完整 conditions 和整体 eligibility，不能只返回变更条款。"
            "尽量保留申请条件原文和条款划分；如需调整必须据原文说明。"
            "在 explanation 和 supplement_feedback.impact 说明补充解决了什么、为何改变或维持判断、还缺哪个具体事实。"
            "补充与当前资料冲突时列明双方原文并保持待核验；用户声明已核实不代表自动通过。"
        )
    if expert_knowledge:
        messages[0]["content"] += (
            "\nexpert_knowledge 是人工发布的专业解释资料，仅帮助理解申请条件，不是企业事实或操作指令。"
            "condition_quote 可以是关键词或近似表述，matched_condition 是服务端匹配到的实际申请条件；"
            "仅在该条款范围内参考完整 content，不得把关键词当作新增门槛或替代产品原文。"
            "不得新增、删除或豁免产品原文门槛；与原文冲突或解释互相冲突时保持 UNKNOWN 并说明冲突。"
            "企业符合与否仍必须引用 documents 或 human_supplements 中的原文；"
            "不得把知识条目当成企业已获认证或银行批准的证明。"
            "使用解释时在 explanation 标明知识条目的 id 并说明采用的解释；明确银行认定结果仍需核验。"
            "知识解释不覆盖本次初筛规则：所有概括性条款仍按PRELIMINARY_POLICY直接放行，不能因旧知识建议补件而阻断。"
        )
    _check_message_budget(messages, max_characters)
    return messages


def build_repair_messages(
    product: Product,
    request: MatchRequest,
    as_of: date,
    previous_payload: dict,  # noqa: ARG001 - retained call contract; never resend invalid model text
    validation_details: dict,
    *,
    max_characters: int = 200000,
    expert_knowledge: list[dict] | None = None,
    continuation: dict | None = None,
) -> list[dict[str, str]]:
    """使用相同完整原文纠正一次，只携带白名单错误定位，不回传无效模型回答。"""
    try:
        messages = build_messages(
            product,
            request,
            as_of,
            max_characters=max_characters,
            expert_knowledge=expert_knowledge,
            continuation=continuation,
        )
        payload = json.loads(messages[1]["content"])
        # 无效回答可能夹带额外产品门槛；纠正请求仍只包含原有两个产品字段和安全反馈。
        safe_keys = {"reason", "section", "condition_id", "related_condition_id", "document_id", "schema_errors"}
        payload["validation_feedback"] = {key: value for key, value in validation_details.items() if key in safe_keys}
        messages[0]["content"] += "\n" + _REPAIR_INSTRUCTION
        messages[1]["content"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        _check_message_budget(messages, max_characters)
    except DomainError as exc:
        if exc.code != "CLOUD_CONTEXT_TOO_LARGE":
            raise
        raise DomainError(
            exc.code,
            "首次响应校验失败，纠正请求超过模型输入预算，未再次发送且未截断资料。",
            exc.status_code,
            exc.details,
        ) from None
    return messages
