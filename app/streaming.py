"""SSE framing and deterministic cleanup when a browser closes a matching stream."""

from __future__ import annotations

import asyncio
import json

from starlette.responses import StreamingResponse


def encode_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


class MatchingStreamResponse(StreamingResponse):
    """Listen for disconnects even when ASGI 2.4 does not raise on a quiet socket."""

    media_type = "text/event-stream"

    async def __call__(self, scope, receive, send) -> None:
        streaming = asyncio.create_task(self.stream_response(send), name="matching-stream-send")
        disconnected = asyncio.create_task(self.listen_for_disconnect(receive), name="matching-stream-disconnect")
        try:
            done, _ = await asyncio.wait({streaming, disconnected}, return_when=asyncio.FIRST_COMPLETED)
            if streaming in done:
                await streaming
        except OSError:
            # A failed send is another form of client disconnect, not a model error.
            pass
        finally:
            for task in (streaming, disconnected):
                if not task.done():
                    task.cancel()
            await asyncio.gather(streaming, disconnected, return_exceptions=True)
            # A send failure can leave the iterator paused at a yield. Closing it
            # executes the matching worker's cancellation and draining finally.
            await self.body_iterator.aclose()
