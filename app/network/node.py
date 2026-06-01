from __future__ import annotations

import asyncio
import time
from asyncio import Server, StreamReader, StreamWriter
from dataclasses import dataclass
from typing import Any, Callable

from app.config import network_identity
from app.network.protocol import block_message_id, read_json, send_json, tx_message_id

LogFn = Callable[[str], None]
STREAM_LIMIT_BYTES = 64 * 1024 * 1024


@dataclass
class PeerConnection:
    reader: StreamReader
    writer: StreamWriter
    ip: str
    port: int
    direction: str
    name: str | None = None
    address: str | None = None
    listen_port: int | None = None
    network_id: str | None = None
    chain_params_hash: str | None = None
    height: int | None = None
    difficulty: int | None = None
    mining_status: str | None = None
    web_port: int | None = None
    mismatch_reason: str | None = None
    final_status: str = "offline"

    @property
    def key(self) -> str:
        return f"{self.ip}:{self.listen_port or self.port}"


class P2PNode:
    def __init__(self, config: dict[str, Any], service: Any, log: LogFn | None = None):
        self.config = config
        self.service = service
        self.log = log or (lambda _message: None)
        self.server: Server | None = None
        self.connections: dict[str, PeerConnection] = {}
        self.seen_message_ids: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self.identity = network_identity(config)

    def _is_self(self, ip: str, port: int) -> bool:
        own_port = int(self.config["listen_port"])
        return int(port) == own_port and ip in {"127.0.0.1", "localhost", "0.0.0.0", self.config["listen_ip"]}

    def _hello(self) -> dict[str, Any]:
        wallet = self.service.store.get_default_wallet() or {}
        return {
            "type": "HELLO",
            "version": self.config["version"],
            "network_id": self.identity["network_id"],
            "chain_params_hash": self.identity["chain_params_hash"],
            "node_name": self.config["node_name"],
            "address": wallet.get("address", ""),
            "listen_port": int(self.config["listen_port"]),
            "web_port": int(self.config["web_port"]),
            "height": self.service.blockchain.height(),
            "difficulty": self.service.blockchain.expected_difficulty(),
            "mining_status": self.service.miner.status,
        }

    def _peer_fields(self, conn: PeerConnection) -> dict[str, Any]:
        return {
            "network_id": conn.network_id,
            "chain_params_hash": conn.chain_params_hash,
            "height": conn.height,
            "difficulty": conn.difficulty,
            "mining_status": conn.mining_status,
            "web_port": conn.web_port,
            "mismatch_reason": conn.mismatch_reason,
        }

    async def start(self) -> None:
        listen_ip = self.config["listen_ip"]
        try:
            self.server = await asyncio.start_server(
                self._handle_inbound,
                listen_ip,
                int(self.config["listen_port"]),
                limit=STREAM_LIMIT_BYTES,
            )
        except PermissionError:
            if listen_ip != "0.0.0.0":
                raise
            listen_ip = "127.0.0.1"
            self.server = await asyncio.start_server(
                self._handle_inbound,
                listen_ip,
                int(self.config["listen_port"]),
                limit=STREAM_LIMIT_BYTES,
            )
            self.log("P2P bind to 0.0.0.0 was denied; using 127.0.0.1")
        self.log(f"P2P listening on {listen_ip}:{self.config['listen_port']}")
        self._track(asyncio.create_task(self.connect_configured_peers()))

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for conn in list(self.connections.values()):
            conn.writer.close()
            await conn.writer.wait_closed()
        self.connections.clear()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        self.log("P2P stopped")

    def _track(self, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def connect_configured_peers(self) -> None:
        await asyncio.sleep(0.2)
        for host, port in self.config.get("servers", []):
            if not self._is_self(str(host), int(port)):
                await self.connect_peer(str(host), int(port))

    async def connect_peer(self, ip: str, port: int) -> tuple[bool, str]:
        if self._is_self(ip, port):
            return False, "ignored self peer"
        key = f"{ip}:{port}"
        if key in self.connections:
            return False, "peer already connected"
        try:
            reader, writer = await asyncio.open_connection(
                ip,
                int(port),
                limit=STREAM_LIMIT_BYTES,
            )
        except OSError as exc:
            self.service.store.upsert_peer(
                ip,
                int(port),
                None,
                None,
                "outbound",
                "offline",
                int(time.time()),
                mismatch_reason=str(exc),
            )
            return False, str(exc)

        self._track(asyncio.create_task(self._connection_loop(reader, writer, ip, int(port), "outbound")))
        return True, "connected"

    async def _handle_inbound(self, reader: StreamReader, writer: StreamWriter) -> None:
        peer_info = writer.get_extra_info("peername")
        ip = str(peer_info[0]) if peer_info else "unknown"
        port = int(peer_info[1]) if peer_info else 0
        await self._connection_loop(reader, writer, ip, port, "inbound")

    async def _connection_loop(
        self,
        reader: StreamReader,
        writer: StreamWriter,
        ip: str,
        port: int,
        direction: str,
    ) -> None:
        conn = PeerConnection(reader=reader, writer=writer, ip=ip, port=port, direction=direction)
        temp_key = f"{ip}:{port}"
        self.connections[temp_key] = conn
        self.service.store.upsert_peer(ip, port, None, None, direction, "connected", int(time.time()))
        try:
            await send_json(writer, self._hello())
            while True:
                message = await read_json(reader)
                if message is None:
                    break
                await self._handle_message(conn, message)
        except (asyncio.CancelledError, ConnectionError):
            raise
        except Exception as exc:
            self.log(f"Peer error {conn.key}: {exc}")
        finally:
            status = conn.final_status
            for key, value in list(self.connections.items()):
                if value is conn:
                    self.connections.pop(key, None)
            self.service.store.upsert_peer(
                conn.ip,
                int(conn.listen_port or conn.port),
                conn.name,
                conn.address,
                conn.direction,
                status,
                int(time.time()),
                **self._peer_fields(conn),
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_message(self, conn: PeerConnection, message: dict[str, Any]) -> None:
        msg_type = message.get("type")
        if msg_type == "HELLO":
            await self._handle_hello(conn, message)
        elif msg_type == "PEERS":
            await self._handle_peers(message)
        elif msg_type == "TX":
            await self._handle_tx(message)
        elif msg_type == "BLOCK":
            await self._handle_block(message)
        elif msg_type == "GET_BLOCKS":
            await self._handle_get_blocks(conn, message)
        elif msg_type == "BLOCKS":
            await self._handle_blocks(message)
        elif msg_type == "PING":
            await send_json(conn.writer, {"type": "PONG", "timestamp": int(time.time())})
        elif msg_type == "PONG":
            self.service.store.upsert_peer(
                conn.ip,
                int(conn.listen_port or conn.port),
                conn.name,
                conn.address,
                conn.direction,
                "connected",
                int(time.time()),
                **self._peer_fields(conn),
            )

    async def _handle_hello(self, conn: PeerConnection, message: dict[str, Any]) -> None:
        conn.name = message.get("node_name")
        conn.address = message.get("address")
        conn.listen_port = int(message.get("listen_port") or conn.port)
        conn.network_id = str(message.get("network_id") or "")
        conn.chain_params_hash = str(message.get("chain_params_hash") or "")
        conn.height = int(message.get("height") or 0)
        conn.difficulty = int(message.get("difficulty") or 0)
        conn.mining_status = str(message.get("mining_status") or "")
        conn.web_port = int(message.get("web_port") or 0) or None
        old_keys = [key for key, value in self.connections.items() if value is conn]
        for key in old_keys:
            self.connections.pop(key, None)
        self.connections[conn.key] = conn
        mismatch = self._network_mismatch_reason(conn)
        if mismatch:
            conn.mismatch_reason = mismatch
            conn.final_status = "参数不匹配"
            self.service.store.upsert_peer(
                conn.ip,
                int(conn.listen_port),
                conn.name,
                conn.address,
                conn.direction,
                conn.final_status,
                int(time.time()),
                **self._peer_fields(conn),
            )
            self.log(f"Peer rejected {conn.name or conn.key}: {mismatch}")
            conn.writer.close()
            return

        conn.final_status = "offline"
        self.service.store.upsert_peer(
            conn.ip,
            int(conn.listen_port),
            conn.name,
            conn.address,
            conn.direction,
            "connected",
            int(time.time()),
            **self._peer_fields(conn),
        )
        self.log(f"Connected peer {conn.name or conn.key} at {conn.ip}:{conn.listen_port}")
        await send_json(conn.writer, {"type": "PEERS", "peers": self.known_peers()})
        remote_height = int(message.get("height") or 0)
        if remote_height > self.service.blockchain.height():
            await send_json(conn.writer, {"type": "GET_BLOCKS", "from_height": 0})

    def _network_mismatch_reason(self, conn: PeerConnection) -> str | None:
        expected_network = self.identity["network_id"]
        expected_hash = self.identity["chain_params_hash"]
        if conn.network_id and conn.network_id != expected_network:
            return f"network_id mismatch: expected {expected_network}, got {conn.network_id}"
        if conn.chain_params_hash and conn.chain_params_hash != expected_hash:
            return (
                "chain params mismatch: "
                f"expected {expected_hash[:12]}, got {conn.chain_params_hash[:12]}"
            )
        return None

    async def _handle_peers(self, message: dict[str, Any]) -> None:
        for peer in message.get("peers", []):
            ip = str(peer.get("ip", ""))
            port = int(peer.get("port", 0) or 0)
            if not ip or not port or self._is_self(ip, port):
                continue
            self.service.store.upsert_peer(
                ip,
                port,
                peer.get("name"),
                peer.get("address"),
                "outbound",
                "known",
                int(time.time()),
            )
            if f"{ip}:{port}" not in self.connections:
                self._track(asyncio.create_task(self.connect_peer(ip, port)))

    async def _handle_tx(self, message: dict[str, Any]) -> None:
        tx = message.get("tx")
        if not isinstance(tx, dict):
            return
        mid = message.get("message_id") or tx_message_id(tx)
        if mid in self.seen_message_ids:
            return
        self.seen_message_ids.add(mid)
        accepted, _result = await self.service.receive_transaction(tx, source="peer")
        if accepted and int(message.get("ttl", 0)) > 1:
            await self.broadcast_tx(tx, ttl=int(message["ttl"]) - 1, message_id=mid)

    async def _handle_block(self, message: dict[str, Any]) -> None:
        block = message.get("block")
        if not isinstance(block, dict):
            return
        mid = message.get("message_id") or block_message_id(block)
        if mid in self.seen_message_ids:
            return
        self.seen_message_ids.add(mid)
        accepted, _result = await self.service.receive_block(block, source="peer")
        if accepted and int(message.get("ttl", 0)) > 1:
            await self.broadcast_block(block, ttl=int(message["ttl"]) - 1, message_id=mid)

    async def _handle_get_blocks(self, conn: PeerConnection, message: dict[str, Any]) -> None:
        from_height = int(message.get("from_height") or 0)
        blocks = self.service.store.get_blocks_from_height(from_height)
        await send_json(conn.writer, {"type": "BLOCKS", "from_height": from_height, "blocks": blocks})

    async def _handle_blocks(self, message: dict[str, Any]) -> None:
        await self.service.receive_blocks(
            message.get("blocks", []),
            from_height=int(message.get("from_height") or 0),
            source="sync",
        )

    def known_peers(self) -> list[dict[str, Any]]:
        peers = self.service.store.list_peers()
        configured = [
            {"ip": str(host), "port": int(port), "name": None}
            for host, port in self.config.get("servers", [])
            if not self._is_self(str(host), int(port))
        ]
        seen = {(peer["ip"], int(peer["port"])) for peer in peers}
        peers.extend(peer for peer in configured if (peer["ip"], int(peer["port"])) not in seen)
        return peers

    async def broadcast_tx(
        self,
        tx: dict[str, Any],
        ttl: int = 8,
        message_id: str | None = None,
    ) -> None:
        mid = message_id or tx_message_id(tx)
        self.seen_message_ids.add(mid)
        await self._broadcast({"type": "TX", "tx": tx, "ttl": ttl, "message_id": mid})

    async def broadcast_block(
        self,
        block: dict[str, Any],
        ttl: int = 8,
        message_id: str | None = None,
    ) -> None:
        mid = message_id or block_message_id(block)
        self.seen_message_ids.add(mid)
        await self._broadcast({"type": "BLOCK", "block": block, "ttl": ttl, "message_id": mid})

    async def request_blocks(self) -> int:
        message = {"type": "GET_BLOCKS", "from_height": 0}
        count = 0
        for conn in list(self.connections.values()):
            try:
                await send_json(conn.writer, message)
                count += 1
            except Exception:
                continue
        return count

    async def _broadcast(self, message: dict[str, Any]) -> None:
        dead: list[str] = []
        for key, conn in list(self.connections.items()):
            try:
                await send_json(conn.writer, message)
            except Exception:
                dead.append(key)
        for key in dead:
            self.connections.pop(key, None)

    def connection_counts(self) -> dict[str, int]:
        inbound = sum(1 for conn in self.connections.values() if conn.direction == "inbound")
        outbound = sum(1 for conn in self.connections.values() if conn.direction == "outbound")
        return {"inbound": inbound, "outbound": outbound, "total": inbound + outbound}
