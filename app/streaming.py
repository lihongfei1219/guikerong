"""SSE 传输：进度排队、心跳和断流取消；不负责业务判断或事件持久化。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable

from starlette.responses import StreamingResponse

from app.errors import DomainError


def encode_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


async def matching_events(operation: Callable[..., Awaitable[dict]]) -> AsyncIterator[str]:
    """转发临时进度；结束或断流时取消并等待模型任务退出。"""
    queue = asyncio.Queue()

    async def run():
        try:
            result = await operation(on_progress=lambda data: queue.put_nowait(("progress", data)))
            queue.put_nowait(("result", result))
        except DomainError as exc:
            queue.put_nowait(("error", {"error": {"code": exc.code, "message": exc.message}}))
        except asyncio.CancelledError:
            raise
        except Exception:
            queue.put_nowait(("error", {"error": {"code": "MATCH_ENGINE_UNAVAILABLE",
                                                 "message": "匹配中断，请刷新会话查看已保存记录。"}}))

    worker = asyncio.create_task(run(), name="stream-match-worker")
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


class MatchingStreamResponse(StreamingResponse):
    """主动监听连接关闭，兼容 ASGI 2.4 下没有发送动作时的静默断流。"""

    media_type = "text/event-stream"

    async def __call__(self, scope, receive, send) -> None:
        streaming = asyncio.create_task(self.stream_response(send), name="matching-stream-send")
        disconnected = asyncio.create_task(self.listen_for_disconnect(receive), name="matching-stream-disconnect")
        try:
            done, _ = await asyncio.wait({streaming, disconnected}, return_when=asyncio.FIRST_COMPLETED)
            if streaming in done:
                await streaming
        except OSError:
            # 发送失败同样视为客户端断开，不把它记成模型判断错误。
            pass
        finally:
            for task in (streaming, disconnected):
                if not task.done():
                    task.cancel()
            await asyncio.gather(streaming, disconnected, return_exceptions=True)
            # 发送失败可能使生成器停在 yield；显式关闭，确保其 finally 收回匹配任务。
            await self.body_iterator.aclose()
