"""Session, knowledge and human review commands for the shared workspace."""

import asyncio

from fastapi import APIRouter

from app.errors import DomainError
from app.harness_models import (
    ChatCommand,
    DocumentEditCommand,
    KnowledgeDecision,
    KnowledgeDraft,
    MatchCommand,
    ReviewCommand,
    SupplementCommand,
)
from app.models import MatchRequest
from app.streaming import MatchingStreamResponse, encode_event


def public_state(state):
    return {key: value for key, value in state.items() if key != "events"}


def create_harness_router(harness, get_catalog):
    router = APIRouter(prefix="/api/v1", tags=["会话与人工协作"])

    @router.get("/sessions")
    def sessions():
        return {"sessions": harness.store.sessions()}

    @router.post("/sessions", status_code=201)
    def create(payload: MatchRequest):
        return public_state(harness.create(payload))

    @router.get("/sessions/{identifier}")
    def session(identifier: str):
        return public_state(harness.session(identifier, get_catalog()))

    @router.get("/sessions/{identifier}/runs/{run_id}")
    def run_detail(identifier: str, run_id: str):
        return harness.run_detail(identifier, run_id)

    @router.get("/sessions/{identifier}/products/{product_id}/history")
    def product_history(identifier: str, product_id: str):
        return harness.product_history(identifier, product_id)

    @router.get("/sessions/{identifier}/events")
    def events(identifier: str):
        history = harness.session(identifier)["events"]
        return {"events": history, "model_requests": {
            event["data"]["id"]: harness.store.read(event["data"]["id"])
            for event in history if event["kind"] == "match/requested"
        }}

    @router.post("/sessions/{identifier}/documents")
    def supplement(identifier: str, payload: SupplementCommand):
        return public_state(harness.supplement(identifier, payload, get_catalog()))

    @router.post("/sessions/{identifier}/documents/edit")
    def edit_document(identifier: str, payload: DocumentEditCommand):
        return public_state(harness.edit_document(identifier, payload, get_catalog()))

    @router.post("/sessions/{identifier}/messages")
    async def message(identifier: str, payload: ChatCommand):
        return public_state(await harness.chat(identifier, payload, get_catalog()))

    @router.post("/sessions/{identifier}/match")
    async def match(identifier: str, payload: MatchCommand):
        return public_state(await harness.match(identifier, payload, get_catalog()))

    @router.post("/sessions/{identifier}/match/stream", response_class=MatchingStreamResponse)
    async def match_stream(identifier: str, payload: MatchCommand):
        catalog = get_catalog()

        async def events():
            queue = asyncio.Queue()

            async def run():
                try:
                    result = await harness.match(identifier, payload, catalog,
                                                 on_progress=lambda data: queue.put_nowait(("progress", data)))
                    queue.put_nowait(("result", public_state(result)))
                except DomainError as exc:
                    queue.put_nowait(("error", {"error": {"code": exc.code, "message": exc.message}}))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    queue.put_nowait(("error", {"error": {"code": "MATCH_ENGINE_UNAVAILABLE",
                                                         "message": "匹配中断，请刷新会话查看已保存记录。"}}))

            worker = asyncio.create_task(run())
            try:
                while True:
                    try:
                        event, data = await asyncio.wait_for(queue.get(), timeout=10)
                    except TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    yield encode_event(event, data)
                    if event in {"result", "error"}:
                        break
            finally:
                if not worker.done():
                    worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        return MatchingStreamResponse(events(), headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.post("/sessions/{identifier}/reviews")
    def review(identifier: str, payload: ReviewCommand):
        return public_state(harness.review(identifier, payload, get_catalog()))

    @router.get("/knowledge")
    def knowledge():
        result = harness.knowledge()
        for item in result["items"]:
            item["applicable"] = item["status"] == "PUBLISHED" and harness.knowledge_valid(item, get_catalog())
        return result

    @router.post("/knowledge", status_code=201)
    def draft(payload: KnowledgeDraft):
        return harness.draft_knowledge(payload, get_catalog())

    @router.post("/knowledge/{identifier}/decision")
    def decision(identifier: str, payload: KnowledgeDecision):
        return harness.decide_knowledge(identifier, payload, get_catalog())

    return router
