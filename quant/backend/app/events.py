"""进程内事件总线：引擎 -> SSE 订阅者的单向广播。

每个订阅者一个有界队列，慢消费者只会丢自己的旧事件（drop-oldest），
不会阻塞策略热循环。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any, AsyncIterator

log = logging.getLogger("jev.events")


class EventBus:
    def __init__(self, queue_size: int = 512, history: int = 300) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._queue_size = queue_size
        self._history: deque[dict[str, Any]] = deque(maxlen=history)
        self._seq = 0
        self.dropped = 0

    # ------------------------------------------------------------------ #
    def publish(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        event = {"id": self._seq, "type": event_type, "ts": time.time(), **payload}
        if event_type in ("decision", "trade", "status"):
            self._history.append(event)
        for queue in list(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()  # drop-oldest
                    self.dropped += 1
                except asyncio.QueueEmpty:  # pragma: no cover - 竞态兜底
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover
                self.dropped += 1
        return event

    # ------------------------------------------------------------------ #
    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        items = list(self._history)
        return items[-limit:]

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def stream(self, replay: int = 20) -> AsyncIterator[str]:
        """产出 SSE 文本帧。"""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        try:
            for event in self.history(replay):
                yield _frame(event)
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"  # 心跳，避免代理超时断开
                    continue
                yield _frame(event)
        finally:
            self._subscribers.discard(queue)


def _frame(event: dict[str, Any]) -> str:
    data = json.dumps(event, ensure_ascii=False, default=str)
    return f"id: {event.get('id', 0)}\nevent: {event.get('type', 'message')}\ndata: {data}\n\n"
