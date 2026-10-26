"""Asynchronous DeepSeek/Ark Chat Completions with bounded, private errors."""

from __future__ import annotations

import asyncio
import json
import math
import ssl
from collections.abc import Callable
from typing import Any

import httpx

from app.config import Settings
from app.errors import DomainError

MAX_RESPONSE_BYTES = 1024 * 1024


class _TransientFailure(Exception):
    def __init__(self, error: DomainError):
        super().__init__(error.code)
        self.error = error


def _invalid_response() -> DomainError:
    return DomainError("ARK_INVALID_RESPONSE", "云端模型返回的数据不完整或格式无效，请重试。", 502)


def _reject_constant(_: str) -> None:
    raise ValueError("JSON constant is not finite")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("JSON number is not finite")
    return result


def _parse_object(value: str) -> dict[str, Any]:
    result = json.loads(value, parse_constant=_reject_constant, parse_float=_finite_float, object_pairs_hook=_unique_object)
    if not isinstance(result, dict):
        raise ValueError("Expected JSON object")
    return result


def _retryable_network_error(error: httpx.RequestError) -> bool:
    # TLS/certificate and local protocol errors are not transient connection loss.
    cause: BaseException | None = error
    visited: set[int] = set()
    while cause is not None and id(cause) not in visited:
        visited.add(id(cause))
        if isinstance(cause, ssl.SSLError):
            return False
        cause = cause.__cause__ or cause.__context__
    return isinstance(error, (httpx.NetworkError, httpx.RemoteProtocolError, httpx.TimeoutException))


class ArkClient:
    """Use with ``async with ArkClient(settings) as client``.

    The configured URL is never taken from a matching request. A semaphore bounds
    complete logical calls (including retries). Cancellation propagates unchanged.
    """

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        settings.validate_model()
        self._settings = settings
        self._semaphore = asyncio.Semaphore(settings.ark_concurrency)
        self._entered = False
        self._client = httpx.AsyncClient(
            base_url=settings.ark_base_url.rstrip("/") + "/",
            headers={"Authorization": "Bearer " + settings.ark_api_key, "Accept": "application/json"},
            timeout=httpx.Timeout(settings.ark_timeout_seconds),
            limits=httpx.Limits(max_connections=settings.ark_concurrency, max_keepalive_connections=settings.ark_concurrency),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def __aenter__(self) -> ArkClient:
        await self._client.__aenter__()
        self._entered = True
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self._entered = False
        await self._client.__aexit__(exc_type, exc, traceback)

    async def complete_json(
        self, messages: list[dict[str, str]], *, on_start: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        if not self._entered:
            raise DomainError("ARK_UNAVAILABLE", "云端模型客户端尚未初始化。", 503)
        if (
            not isinstance(messages, list) or not messages or any(
                not isinstance(message, dict)
                or not isinstance(message.get("role"), str)
                or message.get("role") not in {"system", "user", "assistant"}
                or not isinstance(message.get("content"), str)
                for message in messages
            )
            or sum(len(message["content"]) for message in messages) > self._settings.ark_max_prompt_characters
        ):
            raise DomainError("ARK_INVALID_REQUEST", "云端模型输入格式无效或超过字符限制。", 422)
        # Both providers require an explicit JSON instruction in JSON mode.
        payload_messages = [{"role": message["role"], "content": message["content"]} for message in messages]
        if not any("json" in message["content"].lower() for message in payload_messages):
            if payload_messages[0]["role"] == "system":
                payload_messages[0]["content"] = "Return one valid json object only.\n" + payload_messages[0]["content"]
            else:
                payload_messages.insert(0, {"role": "system", "content": "Return one valid json object only."})
        payload: dict[str, Any] = {
            "model": self._settings.ark_model,
            "messages": payload_messages,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        token_parameter = "max_tokens" if self._settings.matching_mode == "deepseek" else "max_completion_tokens"
        payload[token_parameter] = self._settings.ark_max_completion_tokens
        if self._settings.ark_thinking != "omit":
            payload["thinking"] = {"type": self._settings.ark_thinking}
        async with self._semaphore:
            if on_start is not None:
                on_start()
            for attempt in range(self._settings.ark_max_retries + 1):
                try:
                    return await self._request_once(payload)
                except _TransientFailure as failure:
                    if attempt == self._settings.ark_max_retries:
                        raise failure.error from None
                await asyncio.sleep(min(0.25 * (2 ** attempt), 2.0))
        raise DomainError("ARK_UNAVAILABLE", "云端模型服务暂时不可用。", 503)

    async def _request_once(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            async with asyncio.timeout(self._settings.ark_timeout_seconds):
                async with self._client.stream("POST", "chat/completions", json=payload) as response:
                    if response.status_code in {401, 403}:
                        raise DomainError("ARK_AUTH_ERROR", "云端模型鉴权失败，请检查服务端配置。", 503)
                    if response.status_code == 429:
                        raise _TransientFailure(DomainError("ARK_RATE_LIMITED", "云端模型服务暂时限流，请稍后重试。", 503))
                    if 500 <= response.status_code <= 599:
                        raise _TransientFailure(DomainError("ARK_UNAVAILABLE", "云端模型服务暂时不可用。", 503))
                    if response.status_code != 200:
                        raise DomainError("ARK_REQUEST_REJECTED", "云端模型服务拒绝了请求，请检查服务端配置。", 503)
                    length = response.headers.get("content-length")
                    if length is not None:
                        try:
                            declared_length = int(length)
                        except ValueError:
                            raise _invalid_response() from None
                        if not 0 <= declared_length <= MAX_RESPONSE_BYTES:
                            raise _invalid_response()
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_RESPONSE_BYTES:
                            raise _invalid_response()
                        chunks.append(chunk)
                    return self._parse_response(b"".join(chunks))
        except (TimeoutError, httpx.TimeoutException):
            raise _TransientFailure(DomainError("ARK_TIMEOUT", "云端模型响应超时，请稍后重试。", 504)) from None
        except httpx.DecodingError:
            raise _invalid_response() from None
        except httpx.RequestError as error:
            domain_error = DomainError("ARK_UNAVAILABLE", "无法连接云端模型服务，请稍后重试。", 503)
            if _retryable_network_error(error):
                raise _TransientFailure(domain_error) from None
            raise domain_error from None

    @staticmethod
    def _parse_response(raw: bytes) -> dict[str, Any]:
        try:
            envelope = _parse_object(raw.decode("utf-8"))
            choices = envelope.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise ValueError("Missing completion")
            choice = choices[0]
            if choice.get("finish_reason") != "stop":
                raise ValueError("Incomplete completion")
            message = choice.get("message")
            if not isinstance(message, dict):
                raise ValueError("Missing message")
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Missing content")
            return _parse_object(content)
        except (ValueError, UnicodeError, TypeError, RecursionError):
            raise _invalid_response() from None
