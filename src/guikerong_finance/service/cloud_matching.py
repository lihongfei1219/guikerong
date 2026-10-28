"""校验模型判断、定位原文证据并生成领域结果；不负责 HTTP 或提示词组装。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date

from pydantic import ValidationError

from guikerong_finance.errors import DomainError
from guikerong_finance.models.domain import (
    ConditionResult,
    EvaluationResult,
    Evidence,
    MatchRequest,
    ModelCitation,
    ModelCondition,
    ModelJudgement,
    ModelProductAssessment,
    Product,
    Status,
    VerificationNeed,
)

# 保留原导入路径；业务代码直接依赖上下文模块，校验层不再负责提示词组装。
from guikerong_finance.prompts.matching_context import (
    MATCHING_INTERPRETATION_POLICY as MATCHING_INTERPRETATION_POLICY,
    MATCHING_PROMPT_VERSION as MATCHING_PROMPT_VERSION,
    RESPONSE_SCHEMA,
    build_messages as build_messages,
    build_repair_messages as build_repair_messages,
)
from guikerong_finance.service.preliminary_policy import PRELIMINARY_NOTICE, preliminary_scope
from guikerong_finance.service.supplements import applicable_documents, clause_in_scope, feedback_for

_SCHEMA_FIELD_NAMES = {
    field
    for schema in [RESPONSE_SCHEMA, *RESPONSE_SCHEMA.get("$defs", {}).values()]
    for field in schema.get("properties", {})
}


def _invalid(reason: str, **details) -> DomainError:
    return DomainError(
        "ARK_INVALID_RESPONSE",
        "云端模型响应未通过结构与原文引用校验。",
        502,
        {"reason": reason, **details},
    )


def _downgrade(judgement: ModelJudgement, reason: str, needs=None) -> None:
    judgement.status = Status.UNKNOWN
    judgement.assessment_basis = "EVIDENCE"
    judgement.verification_needs = list(
        dict.fromkeys(
            [
                *judgement.verification_needs,
                *(needs or [VerificationNeed.PROFESSIONAL_REVIEW]),
            ]
        )
    )
    judgement.requires_human_verification = True
    if reason not in judgement.explanation:
        judgement.explanation = ("待核验：" + reason + " " + judgement.explanation)[:3000]
    judgement.required_material = judgement.required_material or reason


def _missing_evidence(judgement: ModelJudgement, section: str) -> None:
    """没有证据的明确结论降为待核验，不能据此认定企业通过或不符合。"""
    judgement.status = Status.UNKNOWN
    judgement.assessment_basis = "EVIDENCE"
    judgement.verification_needs = [VerificationNeed.PROFESSIONAL_REVIEW]
    judgement.requires_human_verification = True
    # Replace the unsupported claim instead of displaying it after a warning prefix.
    judgement.explanation = (
        "待核验：模型未提供支持该结论的企业原文引用，需要专业人员核对现有资料，不能认定符合或不符合。"
    )
    if judgement.required_material and judgement.required_material.strip():
        return
    if section == "conditions" and isinstance(judgement, ModelCondition):
        judgement.required_material = (
            f"请专业人员核对现有资料能否支持以下条件：“{judgement.source_quote[:600]}”；模型缺少引用不代表企业必然缺件。"
        )[:1000]
    else:
        judgement.required_material = "请专业人员核对模型整体结论与企业原文依据；这不代表企业必然缺件。"


def _quote_span(content: str, quote: str) -> tuple[int, int] | None:
    """定位连续原文片段，仅兼容换行编码差异，禁止拼接、改写或省略引用。"""
    if not quote.strip():
        return None
    parts = re.split(r"\r\n|\r|\n", quote)
    if len(parts) > 1:
        # Match all other characters exactly and return the actual original slice.
        # Always match whole newlines: literal find could start at the LF half of a CRLF.
        pattern = r"(?:\r\n|\r(?!\n)|(?<!\r)\n)".join(re.escape(part) for part in parts)
        match = re.search(pattern, content)
        if match is None:
            return None
        return match.span()
    offset = content.find(quote)
    return (offset, offset + len(quote)) if offset >= 0 else None


def citation_evidence(citation: ModelCitation, request: MatchRequest, subject: str) -> Evidence:
    """Locate a literal quote, allowing only differences in newline encoding."""
    document = next((doc for doc in request.documents if doc.document_id == citation.document_id), None)
    if document is None:
        raise _invalid("INVALID_EVIDENCE_CITATION", document_id="<unknown_document>")
    span = _quote_span(document.content, citation.quote)
    if span is None:
        raise _invalid("INVALID_EVIDENCE_CITATION", document_id=citation.document_id)
    offset, end = span
    original_quote = document.content[offset:end]
    # Subject participates in the ID to avoid overwriting different people's citations.
    identifier = hashlib.sha256(
        json.dumps(
            [document.document_id, offset, original_quote, subject],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()[:24]
    newline_ends = [match.end() for match in re.finditer(r"\r\n|\r|\n", document.content[: end + 1])]
    line_start = sum(position <= offset for position in newline_ends) + 1
    line_end = sum(position <= end for position in newline_ends) + 1
    return Evidence(
        evidence_id="ev_" + identifier,
        document_id=document.document_id,
        filename=document.filename,
        quote=original_quote,
        line_start=line_start,
        line_end=line_end,
        subject_id=subject,
        observed_at=document.source_date,
    )


def _normalize_response(payload: dict) -> dict:
    """Adapt harmless model formatting differences without inventing positive verdicts."""
    if not isinstance(payload, dict):
        raise _invalid("UNUSABLE_MODEL_RESPONSE")
    conditions = payload.get("conditions")
    if isinstance(conditions, dict):
        conditions = [conditions]
    if not isinstance(conditions, list) or not conditions:
        raise _invalid("UNUSABLE_MODEL_RESPONSE", section="conditions")

    def text(value, default=""):
        return value if isinstance(value, str) and value.strip() else default

    def judgement(value):
        raw = value if isinstance(value, dict) else {}
        label = text(raw.get("status")).strip().upper()
        status = {
            "符合": "PASS",
            "满足": "PASS",
            "通过": "PASS",
            "不符合": "FAIL",
            "不满足": "FAIL",
            "未通过": "FAIL",
            "待核验": "UNKNOWN",
            "待核实": "UNKNOWN",
            "未知": "UNKNOWN",
        }.get(label, label)
        if status not in {"PASS", "FAIL", "UNKNOWN"}:
            status = "UNKNOWN"
        human = raw.get("requires_human_verification", status == "UNKNOWN")
        if isinstance(human, str):
            human = {
                "true": True,
                "1": True,
                "yes": True,
                "是": True,
                "false": False,
                "0": False,
                "no": False,
                "否": False,
            }.get(human.strip().lower(), status == "UNKNOWN")
        elif type(human) is int and human in (0, 1):
            human = bool(human)
        elif not isinstance(human, bool):
            human = status == "UNKNOWN"
        citations = raw.get("citations") or []
        if isinstance(citations, dict):
            citations = [citations]
        if not isinstance(citations, list):
            citations = []
        needs = raw.get("verification_needs", [])
        if isinstance(needs, str):
            needs = [needs]
        if not isinstance(needs, list):
            needs = []
        needs = list(
            dict.fromkeys(
                need
                for need in needs
                if isinstance(need, str) and need in {"MATERIAL_CONFIRMATION", "PROFESSIONAL_REVIEW"}
            )
        )
        return {
            "status": status,
            "assessment_basis": "PRELIMINARY_POLICY"
            if raw.get("assessment_basis") == "PRELIMINARY_POLICY"
            else "EVIDENCE",
            "verification_needs": needs if status == "UNKNOWN" else [],
            "explanation": text(
                raw.get("explanation"), text(raw.get("reason"), "模型未提供完整判断说明，请核对该项。")
            ),
            "subject_id": text(raw.get("subject_id"), "company"),
            "citations": [
                {"document_id": item["document_id"], "quote": item["quote"]}
                for item in citations
                if isinstance(item, dict) and text(item.get("document_id")) and text(item.get("quote"))
            ],
            "requires_human_verification": human,
            "required_material": text(raw.get("required_material")) or None,
        }

    normalized = []
    seen = set()
    for index, value in enumerate(conditions, start=1):
        raw = value if isinstance(value, dict) else {}
        identifier = text(raw.get("condition_id"))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", identifier) or identifier in seen:
            identifier = f"condition_{index}"
            while identifier in seen:
                identifier += "_"
        seen.add(identifier)
        normalized.append(
            {
                **judgement(raw),
                "condition_id": identifier,
                "source_field": "application_conditions",
                "source_quote": text(raw.get("source_quote"), "模型未提供申请条件描述"),
            }
        )
    feedback = payload.get("supplement_feedback", [])
    return {
        "eligibility": judgement(payload.get("eligibility")),
        "conditions": normalized,
        "supplement_feedback": feedback if isinstance(feedback, list) else [],
    }


def validate_assessment(
    payload: dict,
    product: Product,
    request: MatchRequest,
    as_of: date,
    *,
    check_consistency: bool = False,
) -> ModelProductAssessment:
    """先校验条款再校验整体，核对引用主体、日期、补充范围及初筛豁免边界。"""
    full_request = request
    request = request.model_copy(update={"documents": applicable_documents(request, product)})
    try:
        assessment = ModelProductAssessment.model_validate_json(
            json.dumps(_normalize_response(payload), ensure_ascii=False, allow_nan=False),
            strict=True,
        )
    except ValidationError as exc:
        # Report locations and types, never submitted values or quoted company text.
        schema_errors = [
            {
                "loc": [
                    part if isinstance(part, int) or part in _SCHEMA_FIELD_NAMES else "<unknown_field>"
                    for part in item["loc"]
                ],
                "type": item["type"],
            }
            for item in exc.errors(include_input=False, include_context=False, include_url=False)[:20]
        ]
        raise _invalid("UNUSABLE_MODEL_RESPONSE", product_id=product.product_id, schema_errors=schema_errors) from None
    except (TypeError, ValueError):
        raise _invalid("UNUSABLE_MODEL_RESPONSE", product_id=product.product_id) from None
    # Check fresh model output, before evidence checks can legitimately downgrade
    # the overall result. Revalidation/recovery must preserve those downgrades.
    if (
        check_consistency
        and isinstance(payload.get("eligibility"), dict)
        and payload["eligibility"].get("status")
        and assessment.eligibility.status != Status.PASS
        and all(c.status == Status.PASS for c in assessment.conditions)
    ):
        # Only reconcile an exhaustive result: an omitted concrete requirement
        # must not be hidden by several passing fragments. This checks quotation
        # coverage, not product-specific eligibility rules or AND/OR semantics.
        remaining = list(product.application_conditions)
        for condition in assessment.conditions:
            span = _quote_span(product.application_conditions, condition.source_quote)
            if span is None:
                break
            remaining[span[0] : span[1]] = " " * (span[1] - span[0])
        else:
            if all(ch.isspace() or unicodedata.category(ch).startswith("P") for ch in remaining):
                assessment.eligibility.status = Status.PASS
                assessment.eligibility.assessment_basis = "EVIDENCE"
                assessment.eligibility.requires_human_verification = False
                assessment.eligibility.verification_needs = []
                assessment.eligibility.required_material = None
                assessment.eligibility.explanation = (
                    f"全部 {len(assessment.conditions)} 项申请条件均已匹配通过，整体为完全满足。"
                    "整体结论已按完整逐条结果统一，具体依据见各申请条件。"
                )
        if assessment.eligibility.status != Status.PASS:
            raise _invalid("INCONSISTENT_OVERALL_STATUS", product_id=product.product_id, section="eligibility")
    # 必须先检查逐条依据，整体引用豁免不能掩盖某个条款缺少有效证据。
    pairs: list[tuple[ModelJudgement, str, str, str | None]] = [
        (item, item.subject_id, "conditions", item.condition_id) for item in assessment.conditions
    ]
    pairs.append((assessment.eligibility, "company", "eligibility", None))
    unsafe_condition = False
    for judgement, subject, section, condition_id in pairs:
        original_status = judgement.status
        location = {"product_id": product.product_id, "section": section}
        if condition_id is not None:
            location["condition_id"] = condition_id
        if (
            section == "eligibility"
            and judgement.status == Status.PASS
            and not judgement.citations
            and all(c.status == Status.PASS for c in assessment.conditions)
        ):
            # Clause validation has already established these references. The overall
            # PASS summarizes those clauses; it need not repeat the same citations.
            # Do not rescue an UNKNOWN/FAIL or replace an explicitly invalid reference.
            references = {
                (ref.document_id, ref.quote): ref for condition in assessment.conditions for ref in condition.citations
            }
            judgement.citations = [ref.model_copy(deep=True) for ref in references.values()]
        if (
            section == "eligibility"
            and judgement.status == Status.PASS
            and any(c.assessment_basis == "PRELIMINARY_POLICY" for c in assessment.conditions)
        ):
            judgement.assessment_basis = "PRELIMINARY_POLICY"
            if any(c.status != Status.PASS for c in assessment.conditions):
                _downgrade(judgement, "仍有具体条件未满足或待核验，不能用概括性条款初筛通过代替整体判断。")
        policy_pass = judgement.assessment_basis == "PRELIMINARY_POLICY" and judgement.status == Status.PASS
        if judgement.assessment_basis == "PRELIMINARY_POLICY" and not policy_pass:
            judgement.assessment_basis = "EVIDENCE"
        if (
            policy_pass
            and section == "conditions"
            and isinstance(judgement, ModelCondition)
            and not preliminary_scope(judgement.source_quote, product.application_conditions)
        ):
            if check_consistency and _quote_span(product.application_conditions, judgement.source_quote) is None:
                raise _invalid("INVALID_POLICY_SOURCE_QUOTE", **location)
            _downgrade(judgement, "该条款含具体要求或原文范围不完整，不能按概括性条款免审核，请核对具体要求。")
            policy_pass = False
        if policy_pass and section == "eligibility":
            policy_pass = any(
                c.assessment_basis == "PRELIMINARY_POLICY" and c.status == Status.PASS for c in assessment.conditions
            )
            if not policy_pass:
                judgement.assessment_basis = "EVIDENCE"
        citation_exempt = policy_pass and (
            section == "conditions"
            or (
                preliminary_scope(product.application_conditions, product.application_conditions)
                and all(
                    c.status == Status.PASS and c.assessment_basis == "PRELIMINARY_POLICY"
                    for c in assessment.conditions
                )
            )
        )
        if judgement.status != Status.UNKNOWN and not judgement.citations and not citation_exempt:
            _missing_evidence(judgement, section)
        # Even UNKNOWN responses must not contain fabricated references.
        try:
            evidence = [citation_evidence(citation, request, subject) for citation in judgement.citations]
        except DomainError as exc:
            raise _invalid(exc.details["reason"], **location, document_id=exc.details["document_id"]) from None
        problems = []
        if judgement.subject_id != subject:
            problems.append("模型判断的主体与条件要求的主体不一致。")
        if any(item.observed_at and item.observed_at > as_of for item in evidence):
            _downgrade(
                judgement,
                "引用资料日期晚于本次评估日，请用户确认资料日期与适用时期。",
                [VerificationNeed.MATERIAL_CONFIRMATION],
            )
        cited_docs = {c.document_id for c in judgement.citations}
        if section == "conditions" and any(
            d.document_id in cited_docs and d.human_supplement and not clause_in_scope(d.human_supplement, judgement)
            for d in request.documents
        ):
            _downgrade(judgement, "引用的人工补充限定于其他条款，不能扩展用于本条判断。")
        if any(
            d.document_id in cited_docs
            and d.human_supplement
            and (d.human_supplement.verification != "CONFIRMED" or d.human_supplement.legacy)
            for d in request.documents
        ):
            _downgrade(judgement, "引用的人工补充尚未核实，请核对事实。", [VerificationNeed.MATERIAL_CONFIRMATION])
        if judgement.requires_human_verification and judgement.status != Status.UNKNOWN:
            problems.append("模型标记该项仍需人工核验。")
        if problems:
            _downgrade(judgement, " ".join(problems))
        if judgement.status == Status.PASS and judgement.assessment_basis == "PRELIMINARY_POLICY":
            if PRELIMINARY_NOTICE not in judgement.explanation:
                judgement.explanation = (PRELIMINARY_NOTICE + " " + judgement.explanation)[:3000]
            judgement.required_material = None
        if section == "conditions" and original_status != Status.UNKNOWN and judgement.status == Status.UNKNOWN:
            unsafe_condition = True
    if unsafe_condition:
        # A purportedly decisive condition lost its supporting evidence during validation.
        # Do not preserve an approval/rejection based on that unsupported model response.
        _downgrade(
            assessment.eligibility,
            "部分明确条件结论未通过证据或核验状态检查，整体申请资格须重新核验。",
            [need for c in assessment.conditions if c.status == Status.UNKNOWN for need in c.verification_needs],
        )
    for condition in assessment.conditions:
        if condition.status == Status.UNKNOWN and not condition.verification_needs:
            condition.verification_needs = [VerificationNeed.PROFESSIONAL_REVIEW]
            condition.explanation += "（模型未明确待核验类型，请专业人员确认原因。）"
    if assessment.eligibility.status == Status.UNKNOWN and not assessment.eligibility.verification_needs:
        assessment.eligibility.verification_needs = list(
            dict.fromkeys(
                need for c in assessment.conditions if c.status == Status.UNKNOWN for need in c.verification_needs
            )
        ) or [VerificationNeed.PROFESSIONAL_REVIEW]
    assessment.supplement_feedback, feedback_warnings = feedback_for(assessment, full_request, product)
    for warning in feedback_warnings:
        _downgrade(assessment.eligibility, warning)
    for item in assessment.supplement_feedback:
        if item.status in {"CONFLICT", "NEEDS_CONFIRMATION"}:
            for condition in assessment.conditions:
                if condition.condition_id in item.condition_ids and condition.status != Status.UNKNOWN:
                    _downgrade(
                        condition,
                        "人工补充反馈表明该条款仍存在事实冲突或未核实信息。",
                        [VerificationNeed.MATERIAL_CONFIRMATION],
                    )
                    _downgrade(
                        assessment.eligibility,
                        "人工补充反馈与明确条款结论冲突，请重新核验。",
                        [VerificationNeed.MATERIAL_CONFIRMATION],
                    )
    return assessment


def recover_invalid_citations(
    payload: dict,
    product: Product,
    request: MatchRequest,
    as_of: date,
) -> tuple[ModelProductAssessment, list[str]]:
    """纠正仍失败时移除无法定位的企业引用，并将相关结论降为待核验。"""
    try:
        return validate_assessment(payload, product, request, as_of), []
    except DomainError as exc:
        if exc.code != "ARK_INVALID_RESPONSE" or exc.details.get("reason") != "INVALID_EVIDENCE_CITATION":
            raise

    # Parse a normalized fresh copy without mutating the model response.
    assessment = ModelProductAssessment.model_validate_json(
        json.dumps(_normalize_response(payload), ensure_ascii=False, allow_nan=False),
        strict=True,
    )

    def mark_unverifiable(judgement: ModelJudgement) -> None:
        judgement.status = Status.UNKNOWN
        judgement.assessment_basis = "EVIDENCE"
        judgement.verification_needs = [VerificationNeed.PROFESSIONAL_REVIEW]
        judgement.requires_human_verification = True
        # Replace, rather than prefix, the unsupported claim and any implied request
        # for missing company documents: this was a model citation failure.
        judgement.explanation = "待核验：模型引用无法定位，不能据此认定符合或不符合。请重新评估并核对企业原文引用。"
        judgement.required_material = "请重新评估并核对模型引用是否对应企业原文；这不代表企业资料必然缺失。"

    for judgement in [assessment.eligibility, *assessment.conditions]:
        valid_citations = []
        removed = False
        for citation in judgement.citations:
            try:
                citation_evidence(
                    citation,
                    request.model_copy(update={"documents": applicable_documents(request, product)}),
                    judgement.subject_id,
                )
            except DomainError as exc:
                if exc.code != "ARK_INVALID_RESPONSE" or exc.details.get("reason") != "INVALID_EVIDENCE_CITATION":
                    raise
                removed = True
            else:
                valid_citations.append(citation)
        if removed:
            judgement.citations = valid_citations
            mark_unverifiable(judgement)

    # One bad reference invalidates the product's overall approval/rejection even
    # when other genuine citations or an alternative qualifying branch remain.
    mark_unverifiable(assessment.eligibility)
    safe = validate_assessment(assessment.model_dump(mode="json"), product, request, as_of)
    return safe, ["部分模型引用无法定位，已移除无效引用并将该产品转为待核验，请重新评估或核对引用。"]


def merge_assessment(
    product: Product,
    request: MatchRequest,
    as_of: date,
    assessment: ModelProductAssessment,
) -> tuple[EvaluationResult, dict[str, Evidence]]:
    """将已校验判断转换为条款结果和去重证据，保留模型对且、或、豁免的综合判断。"""
    safe = validate_assessment(assessment.model_dump(mode="json"), product, request, as_of)
    evidence: dict[str, Evidence] = {}

    def references(judgement: ModelJudgement) -> list[str]:
        records = [citation_evidence(citation, request, judgement.subject_id) for citation in judgement.citations]
        evidence.update((item.evidence_id, item) for item in records)
        return list(dict.fromkeys(item.evidence_id for item in records))

    results = []
    for judgement in safe.conditions:
        source = product.application_conditions
        span = _quote_span(source, judgement.source_quote)
        results.append(
            ConditionResult(
                condition_id=judgement.condition_id,
                source_field=judgement.source_field,
                source_quote=source[span[0] : span[1]] if span else judgement.source_quote,
                source_quote_verified=span is not None,
                subject_id=judgement.subject_id,
                status=judgement.status,
                assessment_basis=judgement.assessment_basis,
                verification_needs=judgement.verification_needs,
                reason_code="PRELIMINARY_POLICY_PASS"
                if judgement.assessment_basis == "PRELIMINARY_POLICY"
                else {
                    Status.PASS: "CLOUD_CONDITION_MET",
                    Status.FAIL: "CLOUD_CONDITION_NOT_MET",
                    Status.UNKNOWN: "CLOUD_CONDITION_UNKNOWN",
                }[judgement.status],
                explanation=judgement.explanation,
                evidence_ids=references(judgement),
                required_material=(judgement.required_material or judgement.explanation)
                if judgement.status == Status.UNKNOWN
                else None,
            )
        )
    eligibility = EvaluationResult(
        status=safe.eligibility.status,
        logic_expression="模型综合判断",
        assessment_basis=safe.eligibility.assessment_basis,
        verification_needs=safe.eligibility.verification_needs,
        explanation=safe.eligibility.explanation,
        evidence_ids=references(safe.eligibility),
        condition_results=results,
    )
    return eligibility, evidence
