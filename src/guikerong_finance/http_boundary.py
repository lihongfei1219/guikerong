"""Preserve HTTP/SSE contracts without shadowing Python's http package."""

import asyncio
import inspect
from functools import wraps
from json import JSONDecodeError
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError
from starlette.responses import JSONResponse, Response

from guikerong_finance.errors import DomainError


class RequestContext:
    """FrameX already injects Request; use a dependency to avoid a duplicate."""

    def __init__(self, request: Request):
        self.app = request.app
        self.state = request.state
        self.method = request.method
        self.json = request.json


def request_context(request: Request) -> RequestContext:
    return RequestContext(request)


FinanceRequest = Annotated[RequestContext, Depends(request_context)]


def boundary(*, status_code: int = 200):
    def decorate(function):
        @wraps(function)
        async def wrapped(self, request: Request, *args: Any, **kwargs: Any):
            from guikerong_finance.application import error_response, log_domain_error

            try:
                if inspect.iscoroutinefunction(function):
                    result = await function(self, request, *args, **kwargs)
                else:
                    result = await asyncio.to_thread(function, self, request, *args, **kwargs)
                if isinstance(result, Response):
                    result.headers["X-Raw-Output"] = "True"
                    return result
                return JSONResponse(
                    jsonable_encoder(result), status_code=status_code, headers={"X-Raw-Output": "True"}
                )
            except DomainError as exc:
                log_domain_error(request.state.request_id, exc)
                return error_response(request.state.request_id, exc.code, exc.message, exc.status_code, exc.details)
            except (ValidationError, JSONDecodeError) as exc:
                details = (
                    {
                        "errors": [
                            {"location": ["body", *item["loc"]], "type": item["type"], "message": item["msg"]}
                            for item in exc.errors()
                        ]
                    }
                    if isinstance(exc, ValidationError)
                    else {}
                )
                return error_response(
                    request.state.request_id, "INVALID_REQUEST", "请求参数不符合接口要求。", 400, details
                )

        return wrapped

    return decorate
