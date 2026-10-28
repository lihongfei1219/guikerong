"""匹配执行层：逐产品独立请求，统一控制预算、并发取消、一次纠正和结果汇总。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from time import monotonic
from uuid import uuid4

from guikerong_finance.config import Settings
from guikerong_finance.errors import DomainError
from guikerong_finance.models.domain import (
    Catalog,
    EvaluationResult,
    Evidence,
    FailedProduct,
    MatchRequest,
    MatchResponse,
    ModelProductAssessment,
    Product,
    ProductDecision,
    ProductMatch,
    Summary,
    UnavailableProduct,
)
from guikerong_finance.prompts.matching_context import MATCHING_PROMPT_VERSION, build_messages, build_repair_messages
from guikerong_finance.service.ark_client import ArkClient
from guikerong_finance.service.cloud_matching import (
    merge_assessment,
    recover_invalid_citations,
    validate_assessment,
)
from guikerong_finance.service.product_status import classify, product_sort_key, required_actions
from guikerong_finance.service.scoring import calculate_match_score

SHANGHAI = timezone(timedelta(hours=8))
CloudResult = tuple[EvaluationResult, ModelProductAssessment, dict[str, Evidence], list[str]]
ProductResult = CloudResult | FailedProduct


def validate_input(request: MatchRequest, settings: Settings) -> None:
    """按文档数量、UTF-8 字节数和字符数分别校验输入，保留原文。"""
    if len(request.documents) > settings.max_documents:
        raise DomainError("INPUT_TOO_LARGE", f"最多接收 {settings.max_documents} 份 Markdown。", 413)
    sizes = [len(document.content.encode("utf-8")) for document in request.documents]
    if max(sizes) > settings.max_document_bytes or sum(sizes) > settings.max_total_document_bytes:
        raise DomainError("INPUT_TOO_LARGE", "单文件限 1 MiB，正文合计限 10 MiB。", 413)
    if sum(len(document.content) for document in request.documents) > settings.max_characters:
        raise DomainError("INPUT_COMPLEXITY_EXCEEDED", f"正文超过当前 {settings.max_characters} 字符的处理预算。")


def _evaluation_time(value: datetime | None) -> datetime:
    value = value or datetime.now(SHANGHAI)
    if value.tzinfo is None:
        raise ValueError("now must include a timezone")
    return value.astimezone(SHANGHAI)


async def match_company_async(
    request: MatchRequest,
    catalog: Catalog,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    request_id: str | None = None,
    on_progress: Callable[[dict], None] | None = None,
    expert_knowledge: dict[str, list[dict]] | None = None,
    continuations: dict[str, dict] | None = None,
    on_model_request: Callable[[dict], None] | None = None,
) -> MatchResponse:
    """产品响应无效可局部失败；全局错误、超时或取消必须收回所有在途任务。"""
    settings = settings or Settings()
    settings.validate()
    validate_input(request, settings)
    started = monotonic()
    now = _evaluation_time(now)
    completed = 0

    def emit(event: dict) -> None:
        if on_progress is not None:
            on_progress(event)

    try:
        async with asyncio.timeout(settings.timeout_seconds):
            eligible = [product for product in catalog.products if product.availability_status != "UNAVAILABLE"]
            if not eligible:
                raise DomainError("CATALOG_UNAVAILABLE", "产品库没有可评估的完整产品。", 503)
            if len(eligible) > settings.ark_max_products:
                raise DomainError("CLOUD_PRODUCT_LIMIT", "产品数量超过本次云端匹配预算。", 422)
            settings.validate_model()
            eligible_ids = {product.product_id for product in eligible}
            emit(
                {
                    "type": "started",
                    "total": len(eligible),
                    "concurrency": settings.ark_concurrency,
                    "products": [
                        {
                            "product_id": product.product_id,
                            "product_name": product.product_name,
                            "status": "queued" if product.product_id in eligible_ids else "skipped",
                            "message": "等待评估"
                            if product.product_id in eligible_ids
                            else "产品资料不完整或不可用，已跳过",
                        }
                        for product in catalog.products
                    ],
                }
            )
            emit({"type": "phase", "phase": "preparing", "message": "正在准备全部企业资料、产品名称和完整申请条件"})

            def product_progress(product: Product, status: str, message: str, **details) -> None:
                emit(
                    {
                        "type": "product",
                        "product_id": product.product_id,
                        "product_name": product.product_name,
                        "status": status,
                        "message": message,
                        "completed": completed,
                        "total": len(eligible),
                        **details,
                    }
                )

            # 第一笔模型请求前检查所有完整提示词，避免部分调用后才发现输入超限。
            # JSON 组装放在线程中，事件循环才能继续执行超时和取消处理。
            def prepare_messages():
                return {
                    product.product_id: build_messages(
                        product,
                        request,
                        now.date(),
                        max_characters=settings.ark_max_prompt_characters,
                        expert_knowledge=(expert_knowledge or {}).get(product.product_id),
                        continuation=(continuations or {}).get(product.product_id),
                    )
                    for product in eligible
                }

            messages = await asyncio.to_thread(prepare_messages)
            if monotonic() - started >= settings.timeout_seconds:
                raise DomainError("MATCH_TIMEOUT", "准备云端上下文超时，尚未发送模型请求。", 504)
            emit(
                {
                    "type": "phase",
                    "phase": "matching",
                    "message": "金融专家模型正在逐产品判断企业是否满足申请条件中的各项要求",
                }
            )
            async with ArkClient(settings) as client:

                async def evaluate(product: Product) -> tuple[str, ProductResult]:
                    nonlocal completed
                    repair_attempted = False
                    validation_warnings: list[str] = []
                    try:
                        if on_model_request:
                            on_model_request(
                                {
                                    "product_id": product.product_id,
                                    "attempt": "initial",
                                    "messages": messages[product.product_id],
                                }
                            )
                        payload = await client.complete_json(
                            messages[product.product_id],
                            on_start=lambda: product_progress(product, "running", "正在评估企业资料与产品条件"),
                        )
                        product_progress(product, "validating", "正在核验判断结构与原文引用")
                        try:
                            assessment = validate_assessment(
                                payload, product, request, now.date(), check_consistency=True
                            )
                        except DomainError as exc:
                            if exc.code != "ARK_INVALID_RESPONSE":
                                raise
                            product_progress(
                                product,
                                "queued",
                                "校验未通过，准备一次自动纠正",
                                reason=exc.details.get("reason"),
                            )
                            # 只纠正当前产品，保留同一份完整输入；纠正结果仍须通过全部校验。
                            repair_messages = await asyncio.to_thread(
                                build_repair_messages,
                                product,
                                request,
                                now.date(),
                                payload,
                                exc.details,
                                max_characters=settings.ark_max_prompt_characters,
                                expert_knowledge=(expert_knowledge or {}).get(product.product_id),
                                continuation=(continuations or {}).get(product.product_id),
                            )
                            repair_attempted = True
                            if on_model_request:
                                on_model_request(
                                    {
                                        "product_id": product.product_id,
                                        "attempt": "repair",
                                        "messages": repair_messages,
                                    }
                                )
                            payload = await client.complete_json(
                                repair_messages,
                                on_start=lambda: product_progress(product, "repairing", "正在自动纠正模型响应（1/1）"),
                            )
                            product_progress(product, "validating", "正在重新核验纠正后的判断与引用")
                            try:
                                assessment = validate_assessment(
                                    payload, product, request, now.date(), check_consistency=True
                                )
                            except DomainError as repaired_error:
                                if repaired_error.details.get("reason") != "INVALID_EVIDENCE_CITATION":
                                    raise
                                assessment, validation_warnings = recover_invalid_citations(
                                    payload,
                                    product,
                                    request,
                                    now.date(),
                                )
                        eligibility, evidence = merge_assessment(product, request, now.date(), assessment)
                        if any(not condition.source_quote_verified for condition in eligibility.condition_results):
                            validation_warnings.append(
                                "部分条件采用模型整理的描述，未逐字对应产品原文；已保留判断和匹配得分。"
                            )
                        completed += 1
                        score, breakdown = calculate_match_score(eligibility)
                        note = (
                            f"匹配度 {score:g} 分，已匹配 {breakdown.matched_conditions} / "
                            f"{breakdown.total_conditions} 条；未匹配 {breakdown.unmatched_conditions} 条，"
                            f"待核验 {breakdown.unknown_conditions} 条"
                        )
                        if validation_warnings:
                            note += "；部分引用需核对，详见结果提示"
                        product_progress(
                            product,
                            "completed",
                            note,
                            eligibility_status=eligibility.status.value,
                            match_score=score,
                            matched_conditions=breakdown.matched_conditions,
                            total_conditions=breakdown.total_conditions,
                        )
                        return product.product_id, (eligibility, assessment, evidence, validation_warnings)
                    except asyncio.CancelledError:
                        product_progress(product, "cancelled", "本次匹配已停止，未交付部分结果")
                        raise
                    except DomainError as exc:
                        if exc.code in {"ARK_INVALID_RESPONSE", "CLOUD_CONTEXT_TOO_LARGE"}:
                            completed += 1
                            failure = FailedProduct(
                                product_id=product.product_id,
                                product_name=product.product_name,
                                code=exc.code,
                                reason=exc.details.get("reason"),
                                message=(
                                    "该产品的纠正请求超过完整输入预算，未再次发送；本次未取得有效评估结果。"
                                    if exc.code == "CLOUD_CONTEXT_TOO_LARGE"
                                    else "模型整体结论与逐条状态不一致，自动纠正后仍未解决，"
                                    "请重新匹配；不代表企业缺少材料。"
                                    if exc.details.get("reason") == "INCONSISTENT_OVERALL_STATUS"
                                    else "该产品的模型响应未通过校验，本次未取得有效评估结果；不代表企业不符合。"
                                ),
                            )
                            product_progress(product, "failed", failure.message, reason=failure.reason or failure.code)
                            return product.product_id, failure
                        exc.details.update(
                            product_id=product.product_id,
                            product_name=product.product_name,
                            repair_attempted=repair_attempted,
                        )
                        product_progress(product, "failed", exc.message, reason=exc.details.get("reason", exc.code))
                        raise

                tasks = [
                    asyncio.create_task(evaluate(product), name=f"match:{product.product_id}") for product in eligible
                ]
                try:
                    results = dict(await asyncio.gather(*tasks))
                finally:
                    # 单产品无效响应转成失败结果；全局错误及调用方取消则取消并等待所有任务退出。
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            emit(
                {
                    "type": "phase",
                    "phase": "finalizing",
                    "message": "全部产品已处理，正在汇总有效结果、待核验事项及评估失败信息",
                }
            )
            return _match_prepared(request, catalog, settings, now, started, request_id, results)
    except TimeoutError:
        raise DomainError("MATCH_TIMEOUT", "全量云端匹配超时，未交付部分结果。", 504) from None


def _match_prepared(
    request: MatchRequest,
    catalog: Catalog,
    settings: Settings,
    now: datetime,
    started: float,
    request_id: str | None,
    cloud_results: dict[str, ProductResult],
) -> MatchResponse:
    matches, unavailable, failed = [], [], []
    all_evidence: dict[str, Evidence] = {}
    for product in catalog.products:
        if monotonic() - started > settings.timeout_seconds:
            raise DomainError("MATCH_TIMEOUT", "匹配超时，未交付部分结果。", 504)
        if product.availability_status == "UNAVAILABLE":
            unavailable.append(
                UnavailableProduct(
                    product_id=product.product_id,
                    product_name=product.product_name,
                    reason=product.unavailable_reason or "产品原始资料不完整或不可用。",
                )
            )
            continue
        product_result = cloud_results[product.product_id]
        if isinstance(product_result, FailedProduct):
            failed.append(product_result)
            continue
        eligibility, assessment, evidence, warnings = product_result
        all_evidence.update(evidence)
        score, breakdown = calculate_match_score(eligibility)
        decision, needs = classify(eligibility)
        matches.append(
            ProductMatch(
                product_id=product.product_id,
                product_name=product.product_name,
                application_conditions=product.application_conditions,
                institution=product.institution,
                detail_url=product.detail_url,
                decision=decision,
                verification_needs=needs,
                eligibility=eligibility,
                required_actions=required_actions(eligibility),
                model_assessment=assessment,
                validation_warnings=warnings,
                match_score=score,
                match_breakdown=breakdown,
                product_terms={
                    "rate_text": product.raw_fields.get("综合利率") or None,
                    "amount_text": product.raw_fields.get("融资额度") or None,
                    "term_text": product.raw_fields.get("融资期限") or None,
                },
            )
        )
    if len(unavailable) == len(catalog.products):
        raise DomainError("CATALOG_UNAVAILABLE", "全部产品均不可用，请检查入库报告。", 503)
    matches.sort(key=product_sort_key)
    fingerprint_body = {
        "company": request.company.model_dump(mode="json"),
        "documents": [doc.model_dump(mode="json") for doc in sorted(request.documents, key=lambda d: d.document_id)],
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return MatchResponse(
        request_id=request_id or f"req_{uuid4().hex}",
        evaluated_at=now,
        company=request.company,
        status="PARTIAL" if failed else "COMPLETED",
        versions={
            "catalog": catalog.version,
            "input": "raw-documents-1.0",
            "assessment_scope": "application-conditions-only",
            "evidence_policy": "raw-citations-1.7",
            "scoring": "equal-condition-coverage-1.0",
            "product_status": "three-states-two-needs-1.0",
            "matching_mode": settings.matching_mode,
            "model": settings.ark_model,
            "prompt": MATCHING_PROMPT_VERSION,
            "response_handling": "supplement-feedback-2.0",
        },
        input_fingerprint=fingerprint,
        summary=Summary(
            catalog_total=len(catalog.products),
            raw_record_count=catalog.raw_record_count,
            evaluated_count=len(matches),
            unavailable_count=len(unavailable),
            failed_count=len(failed),
            fully_satisfied_count=sum(p.decision == ProductDecision.FULLY_SATISFIED for p in matches),
            pending_count=sum(p.decision == ProductDecision.PENDING_VERIFICATION for p in matches),
            not_matched_count=sum(p.decision == ProductDecision.NOT_ELIGIBLE for p in matches),
        ),
        matches=matches,
        unavailable_products=unavailable,
        failed_products=failed,
        evidence=all_evidence,
        warnings=[
            *catalog.warnings,
            *([f"{len(failed)} 个产品评估失败；失败不代表不符合申请条件。"] if failed else []),
            "分数仅表示条款覆盖程度，产品状态以整体资格判断为准；未执行外部征信或银行审批。",
        ],
    )
