"""WebSocket 广播中心：核心逻辑只管 publish，发送由这里统一处理。"""
from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from fastapi import WebSocket

from app.peer_address import peer_source


class ConnectionHub:
    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()
        self.identities: dict[WebSocket, dict[str, Any]] = {}
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

    async def register(self, socket: WebSocket, identity: dict[str, Any] | None = None) -> None:
        self.connections.add(socket)
        if identity is not None:
            self.identities[socket] = identity.copy()

    def unregister(self, socket: WebSocket) -> None:
        self.connections.discard(socket)
        self.identities.pop(socket, None)

    def browser_participants(self) -> list[dict[str, Any]]:
        wallets: dict[str, dict[str, Any]] = {}
        for socket, identity in self.identities.items():
            if socket not in self.connections or identity.get("kind") != "browser":
                continue
            address = identity["address"]
            row = wallets.setdefault(address, {"address": address, "nickname": identity["nickname"],
                                               "connections": 0, "source_origins": [], "source_ips": []})
            row["connections"] += 1
            origin = identity.get("source_origin")
            if origin and origin not in row["source_origins"]:
                row["source_origins"].append(origin)
            source_ip = identity.get("source_ip")
            if not source_ip:
                client = getattr(socket, "client", None)
                if client:
                    source_ip = peer_source(client.host, 0).rsplit(":", 1)[0].strip("[]")
            if source_ip and source_ip not in row["source_ips"]:
                row["source_ips"].append(source_ip)
        return sorted(wallets.values(), key=lambda row: (row["nickname"], row["address"]))

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
        self.identities.clear()

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
