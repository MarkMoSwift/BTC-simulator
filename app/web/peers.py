"""显式连接的节点间区块与交易同步。"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from app.core.block import BlockError, block_hash
from app.core.chain import Blockchain, FutureBlock
from app.core.consensus import PROTOCOL_MAJOR
from app.core.state import RuleError
from app.core.transaction import TxError

PATH = "/p2p"
PAGE = 16
MAX_MESSAGE = 2 * 1024 * 1024
MAX_BRANCH_BYTES = 64 * 1024 * 1024


class Peers:
    def __init__(self, chain: Blockchain, addresses: list[str], on_chain: Callable[[], None],
                 on_tx: Callable[[dict[str, Any]], None]):
        self.chain = chain
        self.addresses = addresses
        self.on_chain = on_chain
        self.on_tx = on_tx
        self.tasks: list[asyncio.Task] = []
        self.connections: set[asyncio.Queue] = set()
        self.synced = asyncio.Event()
        self.events: list[dict[str, Any]] = []
        self.event_sequence = 0

    def log(self, message: str) -> None:
        self.event_sequence += 1
        self.events.append({"id": self.event_sequence, "at": int(time.time()), "message": message[:240]})
        self.events = self.events[-100:]

    def snapshot(self) -> dict[str, Any]:
        return {"connected": len(self.connections), "configured": len(self.addresses),
                "height": self.chain.height, "events": self.events.copy()}

    def start(self) -> None:
        self.tasks = [asyncio.create_task(self.dial(address)) for address in self.addresses]
        if not self.addresses:
            self.synced.set()
        else:
            self.tasks.append(asyncio.create_task(self._allow_offline()))

    async def _allow_offline(self) -> None:
        while not self.synced.is_set():
            await asyncio.sleep(5)
            if not self.connections:
                self.synced.set()

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def relay(self, message: dict[str, Any]) -> None:
        for queue in self.connections:
            if not queue.full():
                queue.put_nowait(message)

    async def dial(self, address: str) -> None:
        url = urlsplit(address)
        endpoint = urlunsplit(("wss" if url.scheme == "https" else "ws", url.netloc,
                               url.path + PATH, "", ""))
        while True:
            try:
                self.log(f"正在连接 {address}")
                async with connect(endpoint, open_timeout=4, max_size=MAX_MESSAGE) as socket:
                    await self.exchange(socket.send, socket.recv, address)
            except (OSError, TimeoutError, WebSocketException, ValueError, KeyError, TypeError) as exc:
                self.log(f"连接中断或失败 {address}（{type(exc).__name__}），稍后重试")
            await asyncio.sleep(3)

    def locator(self) -> list[str]:
        heights = [self.chain.height]
        step = 1
        while heights[-1]:
            heights.append(max(0, heights[-1] - step))
            step *= 2
        return [self.chain.hashes[height] for height in heights]

    async def exchange(self, send: Callable[[str], Awaitable], receive: Callable[[], Awaitable],
                       label: str = "接入节点") -> None:
        session = self.chain.session
        if session is None or len(self.connections) >= 16:
            return
        hello = {
            "type": "hello", "major": PROTOCOL_MAJOR, "network_id": session.network_id,
            "session_id": session.session_id, "genesis": session.genesis_hash,
        }

        write_lock = asyncio.Lock()

        async def write(message: dict[str, Any]) -> None:
            async with write_lock:
                await asyncio.wait_for(send(json.dumps(message)), 10)

        async def read() -> dict[str, Any]:
            raw = await asyncio.wait_for(receive(), 15)
            if not isinstance(raw, str) or len(raw) > MAX_MESSAGE:
                raise ValueError("P2P 消息过大或格式错误")
            message = json.loads(raw)
            if not isinstance(message, dict):
                raise ValueError("P2P 消息必须为对象")
            return message

        await write(hello)
        if await asyncio.wait_for(read(), 5) != hello:
            self.log(f"拒绝连接 {label}：协议或课堂邀请不一致")
            return
        queue: asyncio.Queue = asyncio.Queue(maxsize=32)
        self.connections.add(queue)
        self.log(f"连接成功 {label}：课堂与协议校验通过")

        async def sender() -> None:
            await write({"type": "tip", "hash": self.chain.tip_hash})
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), 3)
                except TimeoutError:
                    message = {"type": "tip", "hash": self.chain.tip_hash}
                await write(message)

        async def receiver() -> None:
            ancestor = ""
            blocks: list[dict[str, Any]] = []
            branch_bytes = 0
            requested = False
            requested_pending = False
            synced_tip = ""
            while True:
                message = await read()
                kind = message.get("type")
                if kind == "tip" and not requested:
                    if message.get("hash") == self.chain.tip_hash:
                        self.synced.set()
                        if synced_tip != self.chain.tip_hash:
                            synced_tip = self.chain.tip_hash
                            self.log(f"{label} 区块同步完成：链头一致，高度 {self.chain.height}")
                        if not requested_pending:
                            await write({"type": "pending"})
                            requested_pending = True
                    else:
                        self.log(f"{label} 链头不同，正在请求同步区块")
                        await write({"type": "get", "locator": self.locator()})
                        requested = True
                elif kind == "get":
                    locator = message.get("locator")
                    if (not isinstance(locator, list) or not 0 < len(locator) <= 64
                            or not all(isinstance(item, str) for item in locator)):
                        raise ValueError("无效的区块定位消息")
                    common = next((item for item in locator if item in self.chain.hashes), None)
                    if common is None:
                        await write({"type": "restart"})
                        continue
                    height = self.chain.hashes.index(common)
                    page = self.chain.store.blocks_after(height, PAGE)
                    await write({"type": "blocks", "ancestor": common, "blocks": page,
                                 "more": height + len(page) < self.chain.height})
                elif kind == "restart":
                    requested = False
                    blocks.clear()
                    branch_bytes = 0
                elif kind == "blocks" and requested:
                    page = message.get("blocks")
                    common = message.get("ancestor")
                    if not isinstance(common, str) or not isinstance(page, list) or len(page) > PAGE:
                        raise ValueError("无效的区块响应")
                    if blocks and common != block_hash(blocks[-1]["header"]):
                        raise ValueError("区块响应不连续")
                    if not blocks:
                        ancestor = common
                    blocks.extend(page)
                    if page:
                        self.log(f"从 {label} 收到 {len(page)} 个区块，累计 {len(blocks)} 个，正在校验")
                    branch_bytes += len(json.dumps(page))
                    if branch_bytes > MAX_BRANCH_BYTES:
                        raise ValueError("候选链过大")
                    if message.get("more"):
                        if not page:
                            raise ValueError("缺少后续区块")
                        await write({"type": "get", "locator": [block_hash(page[-1]["header"])]})
                        continue
                    try:
                        if self.chain.adopt_branch(ancestor, blocks, now=int(time.time())):
                            self.on_chain()
                            self.log(f"同步成功 {label}：当前区块高度 {self.chain.height}")
                        else:
                            self.log(f"{label} 区块校验结束，保留当前链，高度 {self.chain.height}")
                    except FutureBlock as exc:
                        await asyncio.sleep(min(max(exc.retry_at - time.time(), 0), 5))
                    except (BlockError, RuleError, TxError):
                        self.log(f"拒绝 {label} 的候选链：区块或交易校验失败")
                    else:
                        self.synced.set()
                        await write({"type": "pending"})
                        requested_pending = True
                    blocks.clear()
                    branch_bytes = 0
                    requested = False
                elif kind == "tx" and isinstance(message.get("tx"), dict):
                    try:
                        self.on_tx(message["tx"])
                    except (TxError, RuleError):
                        pass
                elif kind == "pending":
                    for tx, _ in self.chain.store.list_mempool()[:256]:
                        await write({"type": "tx", "tx": tx})

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            self.connections.discard(queue)
            self.log(f"连接已断开 {label}")
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
