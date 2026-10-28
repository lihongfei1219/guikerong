"""FastAPI entry point: uv run uvicorn guikerong_finance.application:app --reload."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from framex.consts import OPENAPI_URL
from starlette.exceptions import HTTPException

from guikerong_finance.config import Settings
from guikerong_finance.db.catalog import CatalogStore
from guikerong_finance.db.events import EventStore
from guikerong_finance.errors import DomainError
from guikerong_finance.framework import create_framework_application
from guikerong_finance.models.domain import Catalog
from guikerong_finance.prompts.matching_context import MATCHING_PROMPT_VERSION
from guikerong_finance.service.harness import FinancialHarness

logger = logging.getLogger("guikerong")
STATIC_DIR = Path(__file__).resolve().parent / "static"


def error_payload(request_id: str, code: str, message: str, status: int, details: dict | None = None) -> dict:
    return {
        "request_id": request_id,
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "retryable": status in {429, 503, 504}
            and code not in {"ARK_AUTH_ERROR", "ARK_REQUEST_REJECTED", "MODEL_NOT_CONFIGURED"},
        },
    }


def error_response(request_id: str, code: str, message: str, status: int, details: dict | None = None):
    return JSONResponse(
        status_code=status,
        content=error_payload(request_id, code, message, status, details),
        headers={"X-Raw-Output": "True"},
    )


def log_domain_error(request_id: str, exc: DomainError) -> None:
    if exc.code == "ARK_INVALID_RESPONSE":
        # Never log the exception, document text, or provider response here.
        diagnostic = {
            key: exc.details[key]
            for key in ("reason", "product_id", "condition_id")
            if isinstance(exc.details.get(key), str)
        }
        logger.warning("Request %s returned ARK_INVALID_RESPONSE: %s", request_id, diagnostic)


class RequestBoundaryMiddleware:
    """Bound the actual body, including chunked requests, before JSON parsing."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = f"req_{uuid4().hex}"
        scope.setdefault("state", {})["request_id"] = request_id
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                await error_response(request_id, "INPUT_TOO_LARGE", "HTTP 请求体超过 12 MiB。", 413)(
                    scope, receive, send
                )
                return
            if not message.get("more_body", False):
                break
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), (b"x-request-id", request_id.encode())]
            await send(message)

        await self.app(scope, replay, send_with_id)


def create_app(settings: Settings | None = None, catalog: Catalog | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    catalog_store = CatalogStore(settings.catalog_db_path)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.catalog_error = None
        application.state.catalog = catalog
        if catalog is None:
            try:
                application.state.catalog = catalog_store.load()
            except (DomainError, OSError, ValueError) as exc:
                logger.error("Catalog startup failed: %s", type(exc).__name__)
                application.state.catalog_error = "产品数据库加载失败，请先导入或检查产品数据库。"
        yield

    application = create_framework_application()
    framework_lifespan = application.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(app: FastAPI):
        async with framework_lifespan(app), lifespan(app):
            yield

    application.router.lifespan_context = combined_lifespan
    application.add_middleware(RequestBoundaryMiddleware, max_bytes=settings.max_body_bytes)
    application.mount("/assets", StaticFiles(directory=STATIC_DIR, check_dir=False), name="assets")
    harness = FinancialHarness(settings, EventStore(settings.harness_db_path))
    application.state.harness = harness

    @application.get("/openapi.json", include_in_schema=False)
    def legacy_openapi():
        return RedirectResponse(OPENAPI_URL)

    @application.get("/", include_in_schema=False)
    @application.get("/chat", include_in_schema=False)
    def chat_workspace():
        html = (STATIC_DIR / "chat.html").read_text(encoding="utf-8")
        for name in ("chat.js", "chat.css"):
            version = sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:16]
            html = html.replace(f'/assets/{name}"', f'/assets/{name}?v={version}"')
        return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @application.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        log_domain_error(request.state.request_id, exc)
        return error_response(request.state.request_id, exc.code, exc.message, exc.status_code, exc.details)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        details = {
            "errors": [
                {"location": list(item["loc"]), "type": item["type"], "message": item["msg"]} for item in exc.errors()
            ]
        }
        return error_response(request.state.request_id, "INVALID_REQUEST", "请求参数不符合接口要求。", 400, details)

    @application.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        logger.exception("Request %s failed (%s)", request.state.request_id, type(exc).__name__)
        return error_response(request.state.request_id, "MATCH_ENGINE_UNAVAILABLE", "处理失败，未交付部分结果。", 503)

    def get_catalog() -> Catalog:
        # Each operation gets one coherent published snapshot. Explicit imports
        # become visible on the next request without a server restart.
        current = catalog_store.load() if catalog is None else application.state.catalog
        if current is None or not current.products:
            raise DomainError("CATALOG_UNAVAILABLE", "没有可用产品库，请先导入或检查产品数据库。", 503)
        return current

    @application.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        return JSONResponse(
            {"detail": exc.detail},
            status_code=exc.status_code,
            headers={**(exc.headers or {}), "X-Raw-Output": "True"},
        )

    @application.get("/health", tags=["运行状态"])
    def health():
        try:
            current = get_catalog()
        except DomainError:
            current = None
        ready = current is not None and any(p.availability_status != "UNAVAILABLE" for p in current.products)
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "status": "ok" if ready else "degraded",
                "version": "0.1.0",
                "environment": settings.environment,
                "catalog_count": len(current.products) if current else 0,
                "raw_record_count": current.raw_record_count if current else 0,
                "catalog_storage": "sqlite" if catalog is None else "provided",
                "catalog_version": current.version if current else None,
                "input_mode": "raw_documents",
                "external_verification": False,
                "matching_mode": settings.matching_mode,
                "matching_prompt_version": MATCHING_PROMPT_VERSION,
                "model_provider": settings.model_provider,
                "model_configured": bool(settings.ark_api_key and settings.ark_model),
                "model_connectivity_checked": False,
                "match_timeout_seconds": settings.timeout_seconds,
                "upload_limits": {
                    "max_documents": settings.max_documents,
                    "max_document_bytes": settings.max_document_bytes,
                    "max_total_document_bytes": settings.max_total_document_bytes,
                    "max_characters": settings.max_characters,
                    "max_body_bytes": settings.max_body_bytes,
                },
            },
        )

    application.state.get_catalog = get_catalog
    return application
