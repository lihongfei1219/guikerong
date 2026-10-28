import asyncio
import json

import pytest
from test_harness import request_body, sample_catalog

from guikerong_finance.application import create_app
from guikerong_finance.config import Settings
from guikerong_finance.models.domain import MatchRequest


@pytest.mark.parametrize("spec_version", ["2.0", "2.4"])
@pytest.mark.parametrize("disconnect_kind", ["receive", "send", "cancel"])
def test_disconnect_cancels_and_drains_worker(tmp_path, spec_version, disconnect_kind):
    async def run():
        worker_started, worker_drained, chunk_sent = asyncio.Event(), asyncio.Event(), asyncio.Event()
        messages: asyncio.Queue[dict] = asyncio.Queue()
        messages.put_nowait(
            {"type": "http.request", "body": json.dumps({"expected_version": 1}).encode(), "more_body": False}
        )

        async def waiting(*_args: object, on_progress, **_kwargs):
            worker_started.set()
            on_progress({"type": "product", "product_id": "p1", "status": "running"})
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                worker_drained.set()

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                chunk_sent.set()
                if disconnect_kind == "receive":
                    messages.put_nowait({"type": "http.disconnect"})
                elif disconnect_kind == "send":
                    raise OSError("client socket closed")

        application = create_app(Settings(harness_db_path=tmp_path / "stream.sqlite3"), sample_catalog())
        application.state.harness.matcher = waiting
        session = application.state.harness.create(MatchRequest.model_validate(request_body()))
        stream_path = f"/api/v1/sessions/{session['id']}/match/stream"
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": spec_version},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": stream_path,
            "raw_path": stream_path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
        async with application.router.lifespan_context(application):
            task = asyncio.create_task(application(scope, messages.get, send))
            await asyncio.wait_for(chunk_sent.wait(), timeout=1)
            if disconnect_kind == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await asyncio.wait_for(task, timeout=1)
        assert worker_started.is_set()
        assert worker_drained.is_set()
        assert not [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and task.get_name().startswith(("stream-match:", "matching-stream-"))
        ]

    asyncio.run(run())
