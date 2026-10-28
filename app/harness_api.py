"""Session, knowledge and human review commands for the shared workspace."""

from fastapi import APIRouter

from app.harness_models import (
    ChatCommand,
    ContinueRunCommand,
    DocumentDeleteCommand,
    DocumentEditCommand,
    KnowledgeDecision,
    KnowledgeDraft,
    MatchCommand,
    ProductSupplementsCommand,
    ReviewCommand,
    SupplementCommand,
    VersionedCommand,
)
from app.models import MatchRequest
from app.streaming import MatchingStreamResponse, matching_events


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

    @router.delete("/sessions/{identifier}")
    def delete_session(identifier: str, payload: VersionedCommand):
        return harness.store.delete_session(identifier, payload.expected_version)

    @router.get("/sessions/{identifier}/runs/{run_id}")
    def run_detail(identifier: str, run_id: str):
        return harness.run_detail(identifier, run_id)

    @router.post("/sessions/{identifier}/runs/{run_id}/continue", status_code=201)
    def continue_run(identifier: str, run_id: str, payload: ContinueRunCommand):
        return public_state(harness.continue_run(identifier, run_id, payload, get_catalog()))

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

    @router.post("/sessions/{identifier}/products/{product_id}/supplements")
    def product_supplements(identifier: str, product_id: str, payload: ProductSupplementsCommand):
        return public_state(harness.product_supplements(identifier, product_id, payload, get_catalog()))

    @router.post("/sessions/{identifier}/documents/edit")
    def edit_document(identifier: str, payload: DocumentEditCommand):
        return public_state(harness.edit_document(identifier, payload, get_catalog()))

    @router.post("/sessions/{identifier}/documents/delete")
    def delete_document(identifier: str, payload: DocumentDeleteCommand):
        return public_state(harness.delete_document(identifier, payload, get_catalog()))

    @router.post("/sessions/{identifier}/messages")
    async def message(identifier: str, payload: ChatCommand):
        return public_state(await harness.chat(identifier, payload, get_catalog()))

    @router.post("/sessions/{identifier}/match")
    async def match(identifier: str, payload: MatchCommand):
        return public_state(await harness.match(identifier, payload, get_catalog()))

    @router.post("/sessions/{identifier}/match/stream", response_class=MatchingStreamResponse)
    async def match_stream(identifier: str, payload: MatchCommand):
        catalog = get_catalog()

        async def execute(*, on_progress):
            return public_state(await harness.match(identifier, payload, catalog, on_progress=on_progress))

        return MatchingStreamResponse(matching_events(execute),
                                      headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

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
