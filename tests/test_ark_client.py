import asyncio
import json
import ssl
from dataclasses import replace

import httpx
import pytest

from guikerong_finance.config import Settings
from guikerong_finance.errors import DomainError
from guikerong_finance.service.ark_client import MAX_RESPONSE_BYTES, ArkClient

MESSAGES = [{"role": "user", "content": "请返回企业匹配结果"}]
BASE = Settings(matching_mode="ark", ark_api_key="fake-private-key", ark_model="ep-test", ark_max_retries=0)


def response(content='{"result": "ok"}', *, finish_reason="stop"):
    return httpx.Response(200, json={"choices": [{"finish_reason": finish_reason, "message": {"content": content}}]})


def run_request(handler, settings=BASE, messages=MESSAGES):
    async def run():
        async with ArkClient(settings, transport=httpx.MockTransport(handler)) as client:
            return await client.complete_json(messages)

    return asyncio.run(run())


def test_request_uses_configured_endpoint_auth_and_json_contract():
    requests = []

    def handler(request):
        requests.append(request)
        return response()

    assert run_request(handler) == {"result": "ok"}
    request = requests[0]
    assert str(request.url) == "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
    assert request.headers["authorization"] == "Bearer fake-private-key"
    body = json.loads(request.content)
    assert body["model"] == "ep-test"
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_completion_tokens"] == 4096
    assert body["stream"] is False
    assert body["thinking"] == {"type": "disabled"}
    assert any("json" in message["content"].lower() for message in body["messages"])
    assert MESSAGES == [{"role": "user", "content": "请返回企业匹配结果"}]


def test_native_deepseek_uses_official_endpoint_and_max_tokens():
    configuration = replace(
        BASE,
        matching_mode="deepseek",
        ark_base_url="https://api.deepseek.com",
        ark_model="deepseek-flash",
        ark_api_key="fake-deepseek-private-key",
        ark_max_completion_tokens=2048,
    )
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        assert request.headers["authorization"] == "Bearer fake-deepseek-private-key"
        body = json.loads(request.content)
        assert body["model"] == "deepseek-flash"
        assert body["max_tokens"] == 2048
        assert "max_completion_tokens" not in body
        assert body["thinking"] == {"type": "disabled"}
        assert body["response_format"] == {"type": "json_object"}
        return response()

    assert run_request(handler, configuration) == {"result": "ok"}
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["disabled", "enabled", "omit"])
def test_thinking_configuration(mode):
    def handler(request):
        body = json.loads(request.content)
        if mode == "omit":
            assert "thinking" not in body
        else:
            assert body["thinking"] == {"type": mode}
        return response()

    run_request(handler, replace(BASE, ark_thinking=mode))


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "ARK_AUTH_ERROR"),
        (403, "ARK_AUTH_ERROR"),
        (400, "ARK_REQUEST_REJECTED"),
        (404, "ARK_REQUEST_REJECTED"),
        (422, "ARK_REQUEST_REJECTED"),
    ],
)
def test_permanent_http_failures_are_private_and_not_retried(status, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="upstream-private-data fake-private-key")

    with pytest.raises(DomainError) as caught:
        run_request(handler, replace(BASE, ark_max_retries=2))
    assert caught.value.code == code
    assert caught.value.status_code == 503
    assert len(calls) == 1
    assert "private" not in str(caught.value)
    assert caught.value.details == {}


def test_redirect_is_not_followed():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(307, headers={"location": "https://other.example.test/steal"})

    with pytest.raises(DomainError, match="服务拒绝") as caught:
        run_request(handler, replace(BASE, ark_max_retries=2))
    assert len(calls) == 1
    assert caught.value.code == "ARK_REQUEST_REJECTED"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_status_retries_with_bounded_delay(monkeypatch, status):
    delays = []
    calls = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("guikerong_finance.service.ark_client.asyncio.sleep", sleep)

    def handler(request):
        calls.append(request)
        return httpx.Response(status) if len(calls) < 3 else response()

    assert run_request(handler, replace(BASE, ark_max_retries=2)) == {"result": "ok"}
    assert len(calls) == 3
    assert len(delays) == 2
    assert all(0 <= delay <= 2 for delay in delays)


@pytest.mark.parametrize(("status", "code"), [(429, "ARK_RATE_LIMITED"), (503, "ARK_UNAVAILABLE")])
def test_exhausted_retry_preserves_static_domain_error(monkeypatch, status, code):
    async def sleep(_):
        pass

    monkeypatch.setattr("guikerong_finance.service.ark_client.asyncio.sleep", sleep)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="upstream-secret")

    with pytest.raises(DomainError) as caught:
        run_request(handler, replace(BASE, ark_max_retries=1))
    assert caught.value.code == code
    assert len(calls) == 2
    assert "secret" not in str(caught.value)


def test_transient_connection_loss_retries(monkeypatch):
    async def sleep(_):
        pass

    monkeypatch.setattr("guikerong_finance.service.ark_client.asyncio.sleep", sleep)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("private network detail", request=request)
        return response()

    assert run_request(handler, replace(BASE, ark_max_retries=1)) == {"result": "ok"}
    assert len(calls) == 2


def test_tls_certificate_error_is_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        try:
            raise ssl.SSLCertVerificationError("private certificate detail")
        except ssl.SSLCertVerificationError as cause:
            raise httpx.ConnectError("private network detail", request=request) from cause

    with pytest.raises(DomainError) as caught:
        run_request(handler, replace(BASE, ark_max_retries=2))
    assert caught.value.code == "ARK_UNAVAILABLE"
    assert len(calls) == 1
    assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    "content",
    ["", "not json", "[]", "null", '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}', '{"x":1,"x":2}', "```json\n{}\n```"],
)
def test_invalid_model_content_is_not_retried(content):
    calls = []

    def handler(request):
        calls.append(request)
        return response(content)

    with pytest.raises(DomainError) as caught:
        run_request(handler, replace(BASE, ark_max_retries=2))
    assert caught.value.code == "ARK_INVALID_RESPONSE"
    assert caught.value.status_code == 502
    assert len(calls) == 1


@pytest.mark.parametrize("finish_reason", ["length", "content_filter", "tool_calls", None])
def test_truncated_or_nonfinal_completion_rejected(finish_reason):
    with pytest.raises(DomainError) as caught:
        run_request(lambda _: response("{}", finish_reason=finish_reason))
    assert caught.value.code == "ARK_INVALID_RESPONSE"


@pytest.mark.parametrize(
    "envelope",
    [{}, {"choices": []}, {"choices": [None]}, {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}],
)
def test_malformed_response_envelope_rejected(envelope):
    with pytest.raises(DomainError) as caught:
        run_request(lambda _: httpx.Response(200, json=envelope))
    assert caught.value.code == "ARK_INVALID_RESPONSE"


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks = chunks
        self.delay = delay
        self.closed = False
        self.read_count = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.read_count += 1
            yield chunk

    async def aclose(self):
        self.closed = True


def test_streamed_response_size_is_bounded_even_without_content_length():
    stream = Chunks([b"x" * 600000, b"x" * 600000, b"unused"])
    with pytest.raises(DomainError) as caught:
        run_request(lambda _: httpx.Response(200, stream=stream))
    assert caught.value.code == "ARK_INVALID_RESPONSE"
    assert stream.closed
    assert stream.read_count == 2


def test_oversized_declared_response_is_rejected_before_reading():
    stream = Chunks([b"unused"])
    with pytest.raises(DomainError):
        run_request(
            lambda _: httpx.Response(200, headers={"content-length": str(MAX_RESPONSE_BYTES + 1)}, stream=stream)
        )
    assert stream.closed
    assert stream.read_count == 0


def test_total_request_timeout_covers_slow_body_not_only_idle_periods():
    stream = Chunks([b" "] * 30, delay=0.01)
    with pytest.raises(DomainError) as caught:
        run_request(lambda _: httpx.Response(200, stream=stream), replace(BASE, ark_timeout_seconds=0.035))
    assert caught.value.code == "ARK_TIMEOUT"
    assert caught.value.status_code == 504
    assert stream.closed
    assert stream.read_count < 30


def test_cancellation_propagates_and_closes_response_without_retry():
    async def run():
        entered = asyncio.Event()
        calls = []

        class BlockedStream(Chunks):
            async def __aiter__(self):
                entered.set()
                await asyncio.Event().wait()
                yield b"unreachable"

        stream = BlockedStream([])

        def handler(request):
            calls.append(request)
            return httpx.Response(200, stream=stream)

        async with ArkClient(replace(BASE, ark_max_retries=2), transport=httpx.MockTransport(handler)) as client:
            task = asyncio.create_task(client.complete_json(MESSAGES))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert stream.closed
        assert len(calls) == 1

    asyncio.run(run())


def test_client_bounds_concurrent_calls():
    async def run():
        active = 0
        maximum = 0

        async def handler(_):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return response()

        async with ArkClient(replace(BASE, ark_concurrency=2), transport=httpx.MockTransport(handler)) as client:
            results = await asyncio.gather(*(client.complete_json(MESSAGES) for _ in range(5)))
        assert maximum == 2
        assert results == [{"result": "ok"}] * 5

    asyncio.run(run())


def test_client_validates_direct_configuration_before_http_setup():
    with pytest.raises(ValueError, match=".+"):
        ArkClient(replace(BASE, ark_base_url="http://unsafe.test"))


@pytest.mark.parametrize("changes", [{"ark_api_key": None}, {"ark_model": ""}])
def test_client_requires_model_configuration_before_http_setup(monkeypatch, changes):
    def forbidden(*_args: object, **_kwargs):
        pytest.fail("missing credentials must be rejected before HTTP client setup")

    monkeypatch.setattr("guikerong_finance.service.ark_client.httpx.AsyncClient", forbidden)
    with pytest.raises(DomainError) as caught:
        ArkClient(replace(BASE, **changes))
    assert caught.value.code == "MODEL_NOT_CONFIGURED"
    assert caught.value.status_code == 503


def test_oversized_prompt_is_rejected_without_network_call():
    def handler(_):
        raise AssertionError("must not send oversized input")

    with pytest.raises(DomainError) as caught:
        run_request(handler, replace(BASE, ark_max_prompt_characters=1))
    assert caught.value.code == "ARK_INVALID_REQUEST"
