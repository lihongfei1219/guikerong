"""Build isolated application state from routes registered by FrameX 0.4.2."""

from copy import copy
from functools import lru_cache

import framex
from fastapi import FastAPI
from fastapi.routing import APIRoute
from framex.consts import OPENAPI_URL
from framex.driver.application import create_fastapi_application
from starlette.middleware.base import BaseHTTPMiddleware

from guikerong_finance.models.commands import KnowledgeDraft, VersionedCommand
from guikerong_finance.models.domain import MatchRequest


def request_schema(model) -> dict:
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def inline(value):
        if isinstance(value, dict):
            if "$ref" in value:
                return inline(definitions[value["$ref"].rsplit("/", 1)[-1]])
            return {key: inline(item) for key, item in value.items()}
        if isinstance(value, list):
            return [inline(item) for item in value]
        return value

    return {"requestBody": {"required": True, "content": {"application/json": {"schema": inline(schema)}}}}


@lru_cache(maxsize=1)
def registered_application() -> FastAPI:
    plugins = framex.load_plugins("guikerong_finance.api")
    if not plugins:
        raise RuntimeError("Finance plugin failed to load")
    app = framex.run(test_mode=True, use_ray=False, enable_proxy=False, load_builtin_plugins=[], load_plugins=[])
    if not isinstance(app, FastAPI):
        raise RuntimeError("FrameX did not create a local ASGI application")
    from framex.plugin import get_http_plugin_apis

    expected = {api.api for api in get_http_plugin_apis() if api.api}
    actual = {getattr(route, "path", None) for route in app.routes}
    if not expected <= actual:
        raise RuntimeError(f"FrameX routes failed to register: {expected - actual}")
    return app


def create_framework_application() -> FastAPI:
    registered = registered_application()
    app: FastAPI = create_fastapi_application()
    # FrameX's response-envelope middleware uses BaseHTTPMiddleware, whose task
    # cancellation interrupts the existing SSE worker drain on disconnect. This
    # plugin returns raw responses for every route, so that envelope is redundant.
    app.user_middleware = [
        middleware
        for middleware in app.user_middleware
        if not (
            middleware.cls is BaseHTTPMiddleware
            and getattr(middleware.kwargs.get("dispatch"), "__name__", "") == "log_response"
        )
    ]
    # FrameX keeps its ingress as a module singleton. Routes are stateless and
    # read runtime services from request.app.state, so factories remain isolated.
    for route in registered.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/v1/") or route.path == OPENAPI_URL:
            continue
        # Keep FrameX registration, but give each HTTP method a unique OpenAPI ID.
        for method in sorted(route.methods or set()):
            copied = copy(route)
            copied.methods = {method}
            copied.unique_id = f"{route.unique_id}_{method.lower()}"
            command = {
                ("/api/v1/sessions", "POST"): MatchRequest,
                ("/api/v1/sessions/{identifier}", "DELETE"): VersionedCommand,
                ("/api/v1/knowledge", "POST"): KnowledgeDraft,
            }.get((route.path, method))
            if command:
                copied.openapi_extra = request_schema(command)
            if (route.path in {"/api/v1/sessions", "/api/v1/knowledge"} and method == "POST") or (
                route.path.endswith("/continue") and method == "POST"
            ):
                copied.status_code = 201
            app.router.routes.append(copied)
    app.state.deployments_dict = registered.state.deployments_dict
    app.state.plugin_info_map = registered.state.plugin_info_map
    app.state.tags_metadata_map = registered.state.tags_metadata_map
    return app
