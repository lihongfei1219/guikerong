"""业务命令编排：冻结输入、调用匹配器并追加事件，展示结果交由投影层生成。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

from pydantic import ValidationError

from guikerong_finance.config import Settings
from guikerong_finance.db.events import EventStore
from guikerong_finance.errors import DomainError
from guikerong_finance.models.commands import KnowledgeDecision, KnowledgeDraft, ReviewCommand
from guikerong_finance.models.domain import Catalog, Document, HumanSupplement, MatchRequest, Product
from guikerong_finance.prompts.chat_context import CHAT_SYSTEM, build_chat_context, build_chat_messages
from guikerong_finance.service.ark_client import ArkClient
from guikerong_finance.service.continuation import continuation_context
from guikerong_finance.service.harness_projection import (
    effective_matches,
    project,
    replay_session,
    run_detail,
    runs_from,
)
from guikerong_finance.service.history import read_knowledge, read_request
from guikerong_finance.service.knowledge_matching import matching_knowledge
from guikerong_finance.service.matching import SHANGHAI, match_company_async, validate_input
from guikerong_finance.service.supplements import input_fingerprint, original_contains


class FinancialHarness:
    """统一协调会话命令；存储与匹配器从构造函数注入，便于离线验证。"""

    def __init__(self, settings: Settings, store: EventStore, matcher=None):
        self.settings = settings
        self.store = store
        self.matcher = matcher or match_company_async

    def create(self, request: MatchRequest) -> dict:
        if any(d.human_supplement for d in request.documents):
            raise DomainError("INVALID_SUPPLEMENT", "人工补充请通过补充材料入口保存。", 400)
        validate_input(request, self.settings)
        identifier = f"session_{uuid4().hex}"
        self.store.append(identifier, 0, [("session/created", request.model_dump(mode="json"))])
        return self.session(identifier)

    def session(self, identifier: str, catalog: Catalog | None = None) -> dict:
        state = replay_session(identifier, self.store.read(identifier))
        if state.get("origin") and not self.store.read(state["origin"]["session_id"]):
            state["origin"] = {**state["origin"], "deleted": True}
        knowledge = self.knowledge_context(catalog) if catalog is not None else None
        project(state, catalog, knowledge)
        return state

    def run_detail(self, identifier, run_id):
        return run_detail(self.session(identifier), run_id)

    def continue_run(self, identifier, run_id, command, catalog):
        """从指定历史输入创建独立会话，保留原始模型判断但不继承人工复核。"""
        source = runs_from(self.session(identifier)["events"]).get(run_id)
        if source is None:
            raise DomainError("RUN_NOT_FOUND", "匹配轮次不存在。", 404)
        if not source["snapshot"].get("request") or not (source.get("result") or {}).get("matches"):
            raise DomainError("RUN_NOT_CONTINUABLE", "该轮缺少企业资料快照或成功评估，请选择有完整记录的轮次。", 400)
        if command.product_id and not any(p["product_id"] == command.product_id for p in source["result"]["matches"]):
            raise DomainError("PRODUCT_NOT_FOUND", "该轮没有此产品的成功评估。", 400)
        request = read_request(source["snapshot"]["request"])
        validate_input(request, self.settings)
        origin = {
            "session_id": identifier,
            "run_id": run_id,
            "product_id": command.product_id,
            "evaluated_at": source.get("completed_at", source["created_at"]),
        }
        baseline = deepcopy(source)
        baseline.update(id=f"run_{uuid4().hex}", input_revision=1, origin=origin)
        baseline["snapshot"].update(id=baseline["id"], input_revision=1)
        # 导入基线只复制历史评估，不发起模型调用；人工复核留在原会话。
        new_id = f"session_{uuid4().hex}"
        self.store.append(
            new_id,
            0,
            [
                ("session/created", request.model_dump(mode="json")),
                ("session/continued", origin),
                ("baseline/imported", baseline),
                (
                    "assistant/message",
                    {
                        "content": "已从所选历史轮次继续，企业资料和原始模型评估已载入。"
                        "可补充信息后重新匹配；新评估使用当前产品条件和已发布知识。",
                        "run_id": baseline["id"],
                    },
                ),
            ],
        )
        return self.session(new_id, catalog)

    def product_history(self, identifier, product_id):
        state = self.session(identifier)
        return {
            "product_id": product_id,
            "runs": [run for run in state["runs"] if any(a["product_id"] == product_id for a in run["attempts"])],
        }

    # 保留既有调用入口，实际展示逻辑集中于无副作用的投影层。
    effective_matches = staticmethod(effective_matches)

    def supplement(self, identifier, command, catalog=None):
        state = self.session(identifier)
        if any(d.human_supplement for d in command.documents):
            raise DomainError("INVALID_SUPPLEMENT", "请通过补充信息字段指定范围及核实状态。", 400)
        details = command.supplement
        conditions = None
        if details and (details.product_id):
            product = (
                next((p for p in catalog.products if p.product_id == details.product_id), None) if catalog else None
            )
            if not product or (
                details.condition_quote
                and not original_contains(product.application_conditions, details.condition_quote)
            ):
                raise DomainError("INVALID_SUPPLEMENT_SCOPE", "请选择有效产品及其申请条件原文。", 400)
            conditions = product.application_conditions
        recorded_at = datetime.now(SHANGHAI)
        info = HumanSupplement(
            **(details.model_dump() if details else {"legacy": True}),
            recorded_at=recorded_at,
            application_conditions=conditions,
        )
        additions = [d.model_copy(update={"human_supplement": info}) for d in command.documents]
        try:
            request = MatchRequest.model_validate(
                {
                    **state["request"],
                    "documents": state["request"]["documents"] + [d.model_dump(mode="json") for d in additions],
                }
            )
        except ValidationError:
            raise DomainError(
                "INVALID_DOCUMENTS", "补充资料的 document_id 必须唯一且不能与已有资料重复。", 400
            ) from None
        validate_input(request, self.settings)
        self.store.append(
            identifier,
            command.expected_version,
            [
                ("documents/added", request.model_dump(mode="json")),
                ("user/message", {"content": "\n\n".join(d.content for d in additions), "context_label": "补充事实"}),
                (
                    "assistant/message",
                    {"content": "补充资料已保存。请重新匹配相关产品；旧结果和人工确认保留在历史中。"},
                ),
            ],
        )
        return self.session(identifier, catalog)

    def edit_document(self, identifier, command, catalog=None):
        state = self.session(identifier)
        if state["version"] != command.expected_version:
            raise DomainError("SESSION_CONFLICT", "记录已更新，请刷新后再操作。", 409)
        request = read_request(state["request"])
        document = next((d for d in request.documents if d.document_id == command.document_id), None)
        if document is None:
            raise DomainError("DOCUMENT_NOT_FOUND", "所选文件不存在，请重新选择。", 404)
        if document.content == command.content:
            return self.session(identifier, catalog)
        document.content = command.content
        validate_input(request, self.settings)
        self.store.append(
            identifier,
            command.expected_version,
            [
                ("documents/updated", request.model_dump(mode="json")),
                ("assistant/message", {"content": f"文件“{document.filename}”已保存，请重新匹配相关产品。"}),
            ],
        )
        return self.session(identifier, catalog)

    def product_supplements(self, identifier, product_id, command, catalog):
        """校验产品、轮次及条款范围后，原子保存本批补充与对应用户消息。"""
        state = self.session(identifier, catalog)
        if state["version"] != command.expected_version:
            raise DomainError("SESSION_CONFLICT", "记录已更新，请刷新后再操作。", 409)
        product = next((p for p in catalog.products if p.product_id == product_id), None)
        previous = next((p for p in state["effective_matches"] if p["product_id"] == product_id), None)
        if not product or not previous:
            raise DomainError("PRODUCT_NOT_FOUND", "请先匹配此产品。", 400)
        if previous["run_id"] != command.base_run_id:
            raise DomainError("SESSION_CONFLICT", "产品已有新评估，请刷新后补充。", 409)
        if product.application_conditions != previous["application_conditions"]:
            raise DomainError("INVALID_SUPPLEMENT_SCOPE", "申请条件已变化，请重新匹配后补充。", 409)
        conditions = {c["condition_id"]: c for c in previous["eligibility"]["condition_results"]}
        request = read_request(state["request"])
        documents = {d.document_id: d for d in request.documents}
        edited = set()
        for entry in command.entries:
            condition = conditions.get(entry.condition_id)
            if not condition and entry.condition_id != "__eligibility__":
                raise DomainError("INVALID_SUPPLEMENT_SCOPE", "所选条款不存在，请刷新后补充。", 400)
            quote = condition["source_quote"] if condition and condition["source_quote_verified"] else ""
            if quote and not original_contains(product.application_conditions, quote):
                raise DomainError("INVALID_SUPPLEMENT_SCOPE", "条款未对应申请条件原文，请刷新后补充。", 400)
            if entry.document_id:
                document = documents.get(entry.document_id)
                info = document.human_supplement if document else None
                if (
                    not info
                    or info.product_id != product_id
                    or info.condition_quote != quote
                    or info.application_conditions != product.application_conditions
                    or entry.document_id in edited
                ):
                    raise DomainError(
                        "INVALID_SUPPLEMENT_SCOPE", "只能修改当前条款的补充，且同一条补充不能重复提交。", 400
                    )
                assert document is not None
                edited.add(entry.document_id)
                if document.content == entry.content and info.verification == entry.verification and not info.legacy:
                    continue
                document.content = entry.content
                document.human_supplement = info.model_copy(
                    update={
                        "verification": entry.verification,
                        "recorded_at": datetime.now(SHANGHAI),
                        "legacy": False,
                    }
                )
            else:
                request.documents.append(
                    Document(
                        document_id=f"supplement_{uuid4().hex}",
                        filename=f"{product.product_name}补充事实.md",
                        content=entry.content,
                        human_supplement=HumanSupplement(
                            product_id=product_id,
                            condition_quote=quote,
                            verification=entry.verification,
                            application_conditions=product.application_conditions,
                            recorded_at=datetime.now(SHANGHAI),
                        ),
                    )
                )
        validate_input(request, self.settings)
        if request.model_dump(mode="json") == state["request"]:
            return state
        self.store.append(
            identifier,
            command.expected_version,
            [
                ("documents/updated", request.model_dump(mode="json")),
                (
                    "user/message",
                    {
                        "content": "\n\n".join(e.content for e in command.entries),
                        "context_label": f"补充事实 · {product.product_name}",
                    },
                ),
                (
                    "assistant/message",
                    {"content": f"{product.product_name}的 {len(command.entries)} 条补充已保存，待重新匹配。"},
                ),
            ],
        )
        return self.session(identifier, catalog)

    def delete_document(self, identifier, command, catalog=None):
        state = self.session(identifier)
        if state["version"] != command.expected_version:
            raise DomainError("SESSION_CONFLICT", "记录已更新，请刷新后再操作。", 409)
        request = read_request(state["request"])
        document = next((d for d in request.documents if d.document_id == command.document_id), None)
        if document is None:
            raise DomainError("DOCUMENT_NOT_FOUND", "所选文件不存在，请重新选择。", 404)
        if len(request.documents) == 1:
            raise DomainError("LAST_DOCUMENT", "至少保留一份企业资料，请先补充新资料再删除此文件。", 400)
        request.documents = [d for d in request.documents if d.document_id != command.document_id]
        self.store.append(
            identifier,
            command.expected_version,
            [
                ("documents/deleted", request.model_dump(mode="json")),
                (
                    "assistant/message",
                    {"content": f"文件“{document.filename}”已删除，请重新匹配相关产品。历史评估保留原资料。"},
                ),
            ],
        )
        return self.session(identifier, catalog)

    def knowledge(self) -> dict:
        events = self.store.read("knowledge")
        items = {}
        for event in events:
            data = read_knowledge(event["data"])
            if event["kind"] == "knowledge/drafted":
                items[data["id"]] = {
                    **data,
                    "status": "DRAFT",
                    "version": event["version"],
                    "created_at": event["created_at"],
                }
            elif event["kind"] == "knowledge/decided":
                items[data["id"]].update(data, version=event["version"])
        return {"version": events[-1]["version"] if events else 0, "items": list(items.values())}

    def draft_knowledge(self, draft: KnowledgeDraft, catalog: Catalog):
        data = draft.model_dump(mode="json")
        if draft.product_id:
            product = next((p for p in catalog.products if p.product_id == draft.product_id), None)
            if not product:
                raise DomainError("INVALID_KNOWLEDGE_SCOPE", "请选择有效产品。", 400)
            data["application_conditions"] = product.application_conditions
        if draft.condition_quote and not self.knowledge_valid(data, catalog):
            raise DomainError(
                "INVALID_KNOWLEDGE_SCOPE", "未匹配到适用产品的申请条件，请调整条款关键词或近似表述。", 400
            )
        data["id"] = f"knowledge_{uuid4().hex}"
        self.store.append("knowledge", self.knowledge()["version"], [("knowledge/drafted", data)])
        return self.knowledge()

    def decide_knowledge(self, identifier: str, command: KnowledgeDecision, catalog: Catalog):
        knowledge = self.knowledge()
        item = next((k for k in knowledge["items"] if k["id"] == identifier), None)
        if not item:
            raise DomainError("KNOWLEDGE_NOT_FOUND", "知识不存在。", 404)
        if item["version"] != command.expected_version:
            raise DomainError("SESSION_CONFLICT", "知识已更新，请刷新。", 409)
        if command.status == "PUBLISHED" and (item["status"] != "DRAFT" or not self.knowledge_valid(item, catalog)):
            raise DomainError("KNOWLEDGE_NOT_PUBLISHABLE", "仅能发布有效草稿；修改内容请新建版本。", 409)
        self.store.append(
            "knowledge",
            knowledge["version"],
            [
                (
                    "knowledge/decided",
                    {
                        **command.model_dump(mode="json"),
                        "id": identifier,
                    },
                )
            ],
        )
        return self.knowledge()

    @staticmethod
    def knowledge_valid(item, catalog) -> bool:
        return any(matching_knowledge(item, product) is not None for product in catalog.products)

    def knowledge_context(self, catalog):
        items = [k for k in self.knowledge()["items"] if k["status"] == "PUBLISHED"]
        return {
            p.product_id: [matched for item in items if (matched := matching_knowledge(item, p)) is not None]
            for p in catalog.products
        }

    async def match(self, identifier, command, catalog, *, on_progress=None):
        """先保存本轮完整输入，再执行匹配；完成时以版本校验阻止覆盖新资料。"""
        full_catalog = catalog
        state = self.session(identifier)
        if command.product_ids:
            selected = set(command.product_ids)
            if selected - {p.product_id for p in catalog.products}:
                raise DomainError("PRODUCT_NOT_FOUND", "产品不存在。", 404)
            catalog = catalog.model_copy(
                update={"products": [p for p in catalog.products if p.product_id in selected]}
            )
        knowledge = self.knowledge_context(catalog)
        # 一次解析供本轮上下文、逐产品指纹和匹配器共用，避免按产品重复构造资料模型。
        request = MatchRequest.model_validate(state["request"])
        continuations = continuation_context(runs_from(state["events"]), request, catalog)
        run_id = f"run_{uuid4().hex}"
        context = {
            "id": run_id,
            "request": state["request"],
            "knowledge": knowledge,
            "catalog": catalog.model_dump(mode="json"),
            "input_revision": state["input_revision"],
            "model": self.settings.ark_model,
            "provider": self.settings.model_provider,
            "continuations": continuations,
        }
        context["input_fingerprints"] = {p.product_id: input_fingerprint(request, p) for p in catalog.products}
        self.store.append(identifier, command.expected_version, [("match/requested", context)])
        trace_version = 0

        def trace(data):
            nonlocal trace_version
            # 请求日志归属于会话；存储层在同一事务中检查会话未删除，拒绝迟到回调。
            self.store.append(
                run_id,
                trace_version,
                [
                    (
                        "model/request",
                        {
                            **data,
                            "model": self.settings.ark_model,
                            "provider": self.settings.model_provider,
                        },
                    )
                ],
                owner_session=identifier,
            )
            trace_version += 1

        try:
            result = await self.matcher(
                request,
                catalog,
                self.settings,
                expert_knowledge=knowledge,
                continuations=continuations,
                on_model_request=trace,
                on_progress=on_progress,
            )
            self.store.append(
                identifier,
                command.expected_version + 1,
                [
                    (
                        "match/completed",
                        {
                            "id": run_id,
                            "input_revision": state["input_revision"],
                            "catalog_version": catalog.version,
                            "result": result.model_dump(mode="json"),
                        },
                    ),
                    (
                        "assistant/message",
                        {
                            "run_id": run_id,
                            "content": f"已评估 {len(result.matches)} 个产品，"
                            f"{len(result.failed_products)} 个评估失败。请查看右侧条款，"
                            "待核验事项可补充资料、知识或人工确认。",
                        },
                    ),
                ],
            )
        except BaseException as exc:
            self.record_failure(identifier, run_id, exc)
            raise
        return self.session(identifier, full_catalog)

    def record_failure(self, identifier, run_id, exc):
        # 失败与取消也写入历史，但只记录安全错误码，不保存供应商错误正文。
        code = exc.code if isinstance(exc, DomainError) else "TURN_INTERRUPTED"
        for _ in range(3):
            try:
                events = self.store.read(identifier)
                if not events:
                    return  # 会话已经删除，迟到的失败回调不得重建它。
                self.store.append(identifier, events[-1]["version"], [("turn/failed", {"id": run_id, "code": code})])
                return
            except DomainError:
                continue

    async def chat(self, identifier, command, catalog):
        """只解释当前或历史快照；提问及回复不修改已保存的匹配结论。"""
        state = self.session(identifier, catalog)
        knowledge = self.knowledge_context(catalog)
        request, results = state["request"], state["effective_matches"]
        history = state["messages"]
        source_products = [p.model_dump(mode="json") for p in catalog.products]
        if command.run_id:
            detail = self.run_detail(identifier, command.run_id)
            if not detail["snapshot"].get("request"):
                raise DomainError("RUN_NOT_CONTINUABLE", "该历史轮次缺少资料快照，无法据此回答。", 400)
            request, results = detail["snapshot"]["request"], detail["effective_matches"]
            knowledge = detail["snapshot"].get("knowledge", {})
            source_products = detail["snapshot"].get("catalog", {}).get("products", [])
            history = []  # 历史提问不得混入该轮之后的对话或新事实。
        product = None
        if command.product_id:
            raw_product = next((p for p in source_products if p["product_id"] == command.product_id), None)
            if not raw_product or (
                command.condition_quote
                and not original_contains(raw_product["application_conditions"], command.condition_quote)
            ):
                raise DomainError("INVALID_CHAT_SCOPE", "产品或条款已变化，请重新选择讨论范围。", 400)
            product = Product.model_validate(raw_product)
        for overview in (False, True):
            context = build_chat_context(
                request,
                results,
                knowledge,
                product=product,
                condition_quote=command.condition_quote,
                historical_run_id=command.run_id,
                overview=overview,
            )
            try:
                messages = build_chat_messages(
                    CHAT_SYSTEM, context, history, command.message, self.settings.ark_max_prompt_characters
                )
                break
            except DomainError as exc:
                # 产品提问保留完整解释；仅全产品总览允许省略详细理由，并在上下文中明示。
                if exc.code != "CLOUD_CONTEXT_TOO_LARGE" or product is not None or overview:
                    raise
        context_label = ("历史轮次提问" if command.run_id else "提问") + (
            f" · {context['focus']['product_name']}" if context.get("focus") else ""
        )
        self.settings.validate_model()
        turn_id = f"turn_{uuid4().hex}"
        self.store.append(
            identifier,
            command.expected_version,
            [
                ("user/message", {"content": command.message, "context_label": context_label}),
                (
                    "chat/requested",
                    {
                        "id": turn_id,
                        "messages": messages,
                        "model": self.settings.ark_model,
                        "provider": self.settings.model_provider,
                    },
                ),
            ],
        )
        try:
            async with asyncio.timeout(self.settings.timeout_seconds), ArkClient(self.settings) as client:
                response = await client.complete_json(messages)
            reply = response.get("reply")
            if not isinstance(reply, str) or not reply.strip() or len(reply) > 24000:
                raise DomainError("ARK_INVALID_RESPONSE", "聊天模型未返回有效回答。", 502)
            self.store.append(
                identifier,
                command.expected_version + 2,
                [
                    ("assistant/message", {"content": reply, "context_label": context_label}),
                ],
            )
        except BaseException as exc:
            self.record_failure(identifier, turn_id, exc)
            if isinstance(exc, TimeoutError):
                raise DomainError("CHAT_TIMEOUT", "聊天超时，请稍后重试。", 504) from None
            raise
        return self.session(identifier, catalog)

    def review(self, identifier, command: ReviewCommand, catalog: Catalog):
        state = self.session(identifier, catalog)
        product = next((p for p in state["effective_matches"] if p["product_id"] == command.product_id), None)
        if not product or product["run_id"] != command.run_id or not product["can_review"]:
            raise DomainError("STALE_REVIEW", "资料或评估轮次已变化，请重新匹配后审核。", 409)
        current = next((p for p in catalog.products if p.product_id == command.product_id), None)
        if not product or not current or current.application_conditions != product["application_conditions"]:
            raise DomainError("STALE_REVIEW", "产品或申请条件已变化，请重新匹配。", 409)
        identifiers = {c["condition_id"] for c in product["eligibility"]["condition_results"]} | {"__eligibility__"}
        if command.condition_id not in identifiers:
            raise DomainError("CONDITION_NOT_FOUND", "条款不存在。", 404)
        self.store.append(identifier, command.expected_version, [("review/recorded", command.model_dump(mode="json"))])
        return self.session(identifier, catalog)
