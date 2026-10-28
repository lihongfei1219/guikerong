"""Session, knowledge and human review commands for the shared workspace."""

from framex.plugin import BasePlugin, on_register, on_request

from guikerong_finance import __plugin_meta__ as __plugin_meta__
from guikerong_finance.http_boundary import (
    FinanceRequest as Request,
    boundary,
)
from guikerong_finance.models.commands import (
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
from guikerong_finance.models.domain import MatchRequest
from guikerong_finance.service.streaming import MatchingStreamResponse, matching_events


def public_state(state):
    return {key: value for key, value in state.items() if key != "events"}


@on_register()
class FinancePlugin(BasePlugin):
    @boundary(status_code=200)
    def sessions(self, request: Request):
        return {"sessions": request.app.state.harness.store.sessions()}

    @boundary(status_code=200)
    def create(self, request: Request, payload: MatchRequest):
        return public_state(request.app.state.harness.create(payload))

    @boundary(status_code=200)
    def session(self, request: Request, identifier: str):
        return public_state(request.app.state.harness.session(identifier, request.app.state.get_catalog()))

    @boundary(status_code=200)
    def delete_session(self, request: Request, identifier: str, payload: VersionedCommand):
        return request.app.state.harness.store.delete_session(identifier, payload.expected_version)

    @on_request("/api/v1/sessions/{identifier}/runs/{run_id}", methods=["GET"], api_prefix=False, raw_response=True)
    @boundary(status_code=200)
    def run_detail(self, request: Request, identifier: str, run_id: str):
        return request.app.state.harness.run_detail(identifier, run_id)

    @on_request(
        "/api/v1/sessions/{identifier}/runs/{run_id}/continue", methods=["POST"], api_prefix=False, raw_response=True
    )
    @boundary(status_code=201)
    def continue_run(self, request: Request, identifier: str, run_id: str, payload: ContinueRunCommand):
        return public_state(
            request.app.state.harness.continue_run(identifier, run_id, payload, request.app.state.get_catalog())
        )

    @on_request(
        "/api/v1/sessions/{identifier}/products/{product_id}/history",
        methods=["GET"],
        api_prefix=False,
        raw_response=True,
    )
    @boundary(status_code=200)
    def product_history(self, request: Request, identifier: str, product_id: str):
        return request.app.state.harness.product_history(identifier, product_id)

    @on_request("/api/v1/sessions/{identifier}/events", methods=["GET"], api_prefix=False, raw_response=True)
    @boundary(status_code=200)
    def events(self, request: Request, identifier: str):
        history = request.app.state.harness.session(identifier)["events"]
        return {
            "events": history,
            "model_requests": {
                event["data"]["id"]: request.app.state.harness.store.read(event["data"]["id"])
                for event in history
                if event["kind"] == "match/requested"
            },
        }

    @on_request("/api/v1/sessions/{identifier}/documents", methods=["POST"], api_prefix=False, raw_response=True)
    @boundary(status_code=200)
    def supplement(self, request: Request, identifier: str, payload: SupplementCommand):
        return public_state(request.app.state.harness.supplement(identifier, payload, request.app.state.get_catalog()))

    @on_request(
        "/api/v1/sessions/{identifier}/products/{product_id}/supplements",
        methods=["POST"],
        api_prefix=False,
        raw_response=True,
    )
    @boundary(status_code=200)
    def product_supplements(
        self, request: Request, identifier: str, product_id: str, payload: ProductSupplementsCommand
    ):
        return public_state(
            request.app.state.harness.product_supplements(
                identifier, product_id, payload, request.app.state.get_catalog()
            )
        )

    @on_request("/api/v1/sessions/{identifier}/documents/edit", methods=["POST"], api_prefix=False, raw_response=True)
    @boundary(status_code=200)
    def edit_document(self, request: Request, identifier: str, payload: DocumentEditCommand):
        return public_state(
            request.app.state.harness.edit_document(identifier, payload, request.app.state.get_catalog())
        )

    @on_request(
        "/api/v1/sessions/{identifier}/documents/delete", methods=["POST"], api_prefix=False, raw_response=True
    )
    @boundary(status_code=200)
    def delete_document(self, request: Request, identifier: str, payload: DocumentDeleteCommand):
        return public_state(
            request.app.state.harness.delete_document(identifier, payload, request.app.state.get_catalog())
        )

    @on_request("/api/v1/sessions/{identifier}/messages", methods=["POST"], api_prefix=False, raw_response=True)
    @boundary(status_code=200)
    async def message(self, request: Request, identifier: str, payload: ChatCommand):
        return public_state(await request.app.state.harness.chat(identifier, payload, request.app.state.get_catalog()))

    @on_request("/api/v1/sessions/{identifier}/match", methods=["POST"], api_prefix=False, raw_response=True)
    @boundary(status_code=200)
    async def match(self, request: Request, identifier: str, payload: MatchCommand):
        return public_state(
            await request.app.state.harness.match(identifier, payload, request.app.state.get_catalog())
        )

    @on_request("/api/v1/sessions/{identifier}/match/stream", methods=["POST"], api_prefix=False, raw_response=True)
    @boundary(status_code=200)
    async def match_stream(self, request: Request, identifier: str, payload: MatchCommand):
        catalog = request.app.state.get_catalog()

        async def execute(*, on_progress):
            return public_state(
                await request.app.state.harness.match(identifier, payload, catalog, on_progress=on_progress)
            )

        return MatchingStreamResponse(
            matching_events(execute), headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        )

    @on_request("/api/v1/sessions/{identifier}/reviews", methods=["POST"], api_prefix=False, raw_response=True)
    @boundary(status_code=200)
    def review(self, request: Request, identifier: str, payload: ReviewCommand):
        return public_state(request.app.state.harness.review(identifier, payload, request.app.state.get_catalog()))

    @boundary(status_code=200)
    def knowledge(self, request: Request):
        result = request.app.state.harness.knowledge()
        for item in result["items"]:
            item["applicable"] = item["status"] == "PUBLISHED" and request.app.state.harness.knowledge_valid(
                item, request.app.state.get_catalog()
            )
        return result

    @boundary(status_code=200)
    def draft(self, request: Request, payload: KnowledgeDraft):
        return request.app.state.harness.draft_knowledge(payload, request.app.state.get_catalog())

    @on_request("/api/v1/knowledge/{identifier}/decision", methods=["POST"], api_prefix=False, raw_response=True)
    @boundary(status_code=200)
    def decision(self, request: Request, identifier: str, payload: KnowledgeDecision):
        return request.app.state.harness.decide_knowledge(identifier, payload, request.app.state.get_catalog())

    @on_request("/api/v1/sessions", methods=["GET", "POST"], api_prefix=False, raw_response=True)
    @boundary()
    async def sessions_endpoint(self, request: Request):
        if request.method == "POST":
            payload = MatchRequest.model_validate(await request.json())
            response = await self.create(request, payload)
            if response.status_code == 200:
                response.status_code = 201
            return response
        return await self.sessions(request)

    @on_request("/api/v1/sessions/{identifier}", methods=["GET", "DELETE"], api_prefix=False, raw_response=True)
    @boundary()
    async def session_endpoint(self, request: Request, identifier: str):
        if request.method == "DELETE":
            payload = VersionedCommand.model_validate(await request.json())
            return await self.delete_session(request, identifier, payload)
        return await self.session(request, identifier)

    @on_request("/api/v1/knowledge", methods=["GET", "POST"], api_prefix=False, raw_response=True)
    @boundary()
    async def knowledge_endpoint(self, request: Request):
        if request.method == "POST":
            payload = KnowledgeDraft.model_validate(await request.json())
            response = await self.draft(request, payload)
            if response.status_code == 200:
                response.status_code = 201
            return response
        return await self.knowledge(request)

    @on_request("/api/v1/financial-products", methods=["GET"], api_prefix=False, raw_response=True)
    @boundary()
    def products(self, request: Request):
        return request.app.state.get_catalog()
