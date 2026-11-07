"""显式连接的节点间区块与交易同步。"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from app.core.block import BlockError, block_hash
from app.peer_address import peer_url
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
        self.connection_info: dict[asyncio.Queue, dict[str, Any]] = {}
        self.node_name = ""
        self.node_id = secrets.token_hex(16)
        self.synced = asyncio.Event()
        self.events: list[dict[str, Any]] = []
        self.event_sequence = 0
        self.dial_tasks: dict[str, asyncio.Task] = {}
        self.paused: set[str] = set()
        self.statuses: dict[str, str] = {}
        self.presence_messages: Callable[[], list[dict[str, Any]]] = lambda: []
        self.on_presence: Callable[[dict[str, Any]], bool] = lambda _message: False

    def log(self, message: str) -> None:
        self.event_sequence += 1
        self.events.append({"id": self.event_sequence, "at": int(time.time()), "message": message[:240]})
        self.events = self.events[-100:]

    def snapshot(self) -> dict[str, Any]:
        outgoing = {row["address"]: row for row in self.connection_info.values()
                    if row["direction"] == "outbound"}
        peers = [{"address": address, "status": "paused" if address in self.paused
                  else self.statuses.get(address, "connecting"), "direction": "outbound",
                  "name": outgoing.get(address, {}).get("name", "")}
                 for address in self.addresses]
        peers.extend({**row, "status": "connected"} for row in self.connection_info.values()
                     if row["direction"] == "inbound")
        return {"connected": len(self.connections), "configured": len(self.addresses),
                "height": self.chain.height, "events": self.events.copy(),
                "peers": peers}

    def connect_address(self, address: str) -> None:
        self.paused.discard(address)
        task = self.dial_tasks.get(address)
        if task is None or task.done():
            self.statuses[address] = "connecting"
            self.dial_tasks[address] = asyncio.create_task(self.dial(address))

    async def disconnect_address(self, address: str) -> None:
        self.paused.add(address)
        task = self.dial_tasks.pop(address, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.statuses[address] = "paused"
        self.log(f"已暂停连接 {address}，停止自动重试")

    def start(self) -> None:
        for address in self.addresses:
            if address not in self.paused:
                self.connect_address(address)
        if not any(address not in self.paused for address in self.addresses):
            self.synced.set()
        else:
            self.tasks.append(asyncio.create_task(self._allow_offline()))

    async def _allow_offline(self) -> None:
        while not self.synced.is_set():
            await asyncio.sleep(5)
            if not self.connections:
                self.synced.set()

    async def stop(self) -> None:
        tasks = [*self.tasks, *self.dial_tasks.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.dial_tasks.clear()
        self.tasks.clear()

    def relay(self, message: dict[str, Any]) -> None:
        for queue in self.connections:
            if not queue.full():
                queue.put_nowait(message)

    async def dial(self, address: str) -> None:
        url = urlsplit(peer_url(address))
        endpoint = urlunsplit(("wss" if url.scheme == "https" else "ws", url.netloc,
                               url.path + PATH, "", ""))
        while True:
            try:
                self.statuses[address] = "connecting"
                self.log(f"正在连接 {address}")
                async with connect(endpoint, open_timeout=4, max_size=MAX_MESSAGE) as socket:
                    await self.exchange(socket.send, socket.recv, address, direction="outbound")
            except (OSError, asyncio.TimeoutError, WebSocketException, ValueError, KeyError, TypeError) as exc:
                detail = " ".join(str(exc).split())[:120]
                self.log(f"连接中断或失败 {address}（{type(exc).__name__}{': ' + detail if detail else ''}），稍后重试")
            self.statuses[address] = "retrying"
            await asyncio.sleep(3)

    def locator(self) -> list[str]:
        heights = [self.chain.height]
        step = 1
        while heights[-1]:
            heights.append(max(0, heights[-1] - step))
            step *= 2
        return [self.chain.hashes[height] for height in heights]

    async def exchange(self, send: Callable[[str], Awaitable], receive: Callable[[], Awaitable],
                       label: str = "接入节点", *, direction: str = "inbound") -> None:
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
        info = {"address": label, "direction": direction, "name": "", "node_id": ""}
        self.connection_info[queue] = info
        if label in self.addresses:
            self.statuses[label] = "connected"
        self.log(f"连接成功 {label}：课堂与协议校验通过")

        async def sender() -> None:
            await write({"type": "tip", "hash": self.chain.tip_hash})
            await write({"type": "node_info", "name": self.node_name, "node_id": self.node_id})
            for message in self.presence_messages():
                await write(message)
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), 3)
                except asyncio.TimeoutError:
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
                elif kind == "node_info":
                    name, node_id = message.get("name"), message.get("node_id")
                    if isinstance(name, str) and len(name) <= 64:
                        info["name"] = name
                    if (isinstance(node_id, str) and len(node_id) == 32
                            and all(character in "0123456789abcdef" for character in node_id)):
                        info["node_id"] = node_id
                elif kind == "presence":
                    if self.on_presence(message):
                        self.relay(message)
                elif kind == "pending":
                    for tx, _ in self.chain.store.list_mempool()[:256]:
                        await write({"type": "tx", "tx": tx})

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except Exception as exc:
            detail = " ".join(str(exc).split())[:120]
            self.log(f"{label} 通信异常（{type(exc).__name__}{': ' + detail if detail else ''}）")
            raise
        finally:
            self.connections.discard(queue)
            self.connection_info.pop(queue, None)
            self.log(f"连接已断开 {label}")
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
