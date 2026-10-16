"""WebSocket 广播中心：核心逻辑只管 publish，发送由这里统一处理。"""
from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from fastapi import WebSocket


class ConnectionHub:
    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._task: asyncio.Task[None] | None = None

    def publish(self, event: dict[str, Any]) -> None:
        event.setdefault("at", int(time.time()))
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(event)

    async def register(self, socket: WebSocket) -> None:
        self.connections.add(socket)

    def unregister(self, socket: WebSocket) -> None:
        self.connections.discard(socket)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="hub-broadcast")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for socket in list(self.connections):
            with contextlib.suppress(Exception):
                await socket.close()
        self.connections.clear()

    async def _run(self) -> None:
        while True:
            event = await self.queue.get()
            await self.broadcast(event)

    async def broadcast(self, event: dict[str, Any]) -> None:
        if not self.connections:
            return
        stale: list[WebSocket] = []
        for socket in list(self.connections):
            try:
                await socket.send_json(event)
            except Exception:
                stale.append(socket)
        for socket in stale:
            self.unregister(socket)
