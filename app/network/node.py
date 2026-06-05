from __future__ import annotations

import asyncio
import socket
import time
from asyncio import Server, StreamReader, StreamWriter
from dataclasses import dataclass
from typing import Any, Callable

from app.config import network_identity
from app.network.address import (
    configured_listen_hosts,
    format_host_port,
    is_loopback_or_unspecified,
    normalize_host,
)
from app.core.block import compute_block_hash
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
    target: str | None = None
    mining_status: str | None = None
    web_port: int | None = None
    mismatch_reason: str | None = None
    final_status: str = "offline"

    @property
    def key(self) -> str:
        return format_host_port(self.ip, self.listen_port or self.port)


class P2PNode:
    def __init__(self, config: dict[str, Any], service: Any, log: LogFn | None = None):
        self.config = config
        self.service = service
        self.log = log or (lambda _message: None)
        self.servers: list[Server] = []
        self.listen_hosts = configured_listen_hosts(config)
        self.connections: dict[str, PeerConnection] = {}
        self.seen_message_ids: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self.identity = network_identity(config)

    def _is_self(self, ip: str, port: int) -> bool:
        own_port = int(self.config["listen_port"])
        host = normalize_host(ip)
        own_hosts = {normalize_host(item) for item in self.listen_hosts}
        return int(port) == own_port and (
            host in own_hosts or is_loopback_or_unspecified(host)
        )

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
            "target": self.service.blockchain.expected_target(),
            "mining_status": self.service.miner.status,
        }

    def _peer_fields(self, conn: PeerConnection) -> dict[str, Any]:
        return {
            "network_id": conn.network_id,
            "chain_params_hash": conn.chain_params_hash,
            "height": conn.height,
            "difficulty": conn.difficulty,
            "target": conn.target,
            "mining_status": conn.mining_status,
            "web_port": conn.web_port,
            "mismatch_reason": conn.mismatch_reason,
        }

    @staticmethod
    def _accepts_ipv4(server: Server) -> bool:
        for server_socket in server.sockets or []:
            if server_socket.family != socket.AF_INET6:
                continue
            try:
                if server_socket.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 0:
                    return True
            except OSError:
                continue
        return False

    async def start(self) -> None:
        port = int(self.config["listen_port"])
        requested_hosts = configured_listen_hosts(self.config)
        bound_hosts: list[str] = []
        errors: list[OSError] = []

        for host in requested_hosts:
            try:
                server = await asyncio.start_server(
                    self._handle_inbound,
                    host,
                    port,
                    limit=STREAM_LIMIT_BYTES,
                )
            except OSError as exc:
                errors.append(exc)
                self.log(f"P2P bind skipped {format_host_port(host, port)}: {exc}")
                continue
            self.servers.append(server)
            if host not in bound_hosts:
                bound_hosts.append(host)
            if host == "::" and self._accepts_ipv4(server) and "0.0.0.0" not in bound_hosts:
                bound_hosts.append("0.0.0.0")
            if host == "::1" and self._accepts_ipv4(server) and "127.0.0.1" not in bound_hosts:
                bound_hosts.append("127.0.0.1")

        if not self.servers:
            fallback_hosts = (
                ["::1", "127.0.0.1"]
                if self.config.get("enable_ipv6", True)
                else ["127.0.0.1"]
            )
            for host in fallback_hosts:
                try:
                    server = await asyncio.start_server(
                        self._handle_inbound,
                        host,
                        port,
                        limit=STREAM_LIMIT_BYTES,
                    )
                except OSError as exc:
                    errors.append(exc)
                    continue
                self.servers.append(server)
                if host not in bound_hosts:
                    bound_hosts.append(host)
            if not self.servers:
                raise errors[-1]
            self.log("P2P wildcard bind was denied; using loopback addresses")

        self.listen_hosts = bound_hosts
        addresses = ", ".join(format_host_port(host, port) for host in bound_hosts)
        self.log(f"P2P listening on {addresses}")
        self._track(asyncio.create_task(self.connect_configured_peers()))

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for conn in list(self.connections.values()):
            conn.writer.close()
            await conn.writer.wait_closed()
        self.connections.clear()
        for server in self.servers:
            server.close()
            await server.wait_closed()
        self.servers.clear()
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
        ip = normalize_host(ip)
        if self._is_self(ip, port):
            return False, "ignored self peer"
        key = format_host_port(ip, port)
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
        ip = normalize_host(peer_info[0]) if peer_info else "unknown"
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
        ip = normalize_host(ip)
        conn = PeerConnection(reader=reader, writer=writer, ip=ip, port=port, direction=direction)
        temp_key = format_host_port(ip, port)
        self.connections[temp_key] = conn
        if direction == "outbound":
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
            await self._handle_tx(conn, message)
        elif msg_type == "BLOCK":
            await self._handle_block(conn, message)
        elif msg_type == "GET_BLOCKS":
            await self._handle_get_blocks(conn, message)
        elif msg_type == "BLOCKS":
            await self._handle_blocks(conn, message)
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
        conn.target = str(message.get("target") or "") or None
        conn.mining_status = str(message.get("mining_status") or "")
        conn.web_port = int(message.get("web_port") or 0) or None
        identity_conflict = self._identity_conflict_reason(conn)
        if identity_conflict:
            conn.mismatch_reason = identity_conflict
            conn.final_status = "self connection"
            self.log(f"Ignored peer {conn.name or conn.key}: {identity_conflict}")
            conn.writer.close()
            return
        if conn.listen_port != conn.port:
            self.service.store.delete_peer(conn.ip, conn.port)
        old_keys = [key for key, value in self.connections.items() if value is conn]
        for key in old_keys:
            self.connections.pop(key, None)
        existing = self.connections.get(conn.key)
        if existing is not None and existing is not conn:
            conn.mismatch_reason = "peer address already connected"
            conn.final_status = "duplicate peer address"
            self.log(f"Ignored peer {conn.name or conn.key}: {conn.mismatch_reason}")
            conn.writer.close()
            return
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
        self.log(f"Connected peer {conn.name or conn.key} at {format_host_port(conn.ip, conn.listen_port)}")
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

    def _identity_conflict_reason(self, conn: PeerConnection) -> str | None:
        if conn.name and conn.name == self.config.get("node_name"):
            return "same node name"
        own_wallet = self.service.default_wallet()
        if conn.address and conn.address == own_wallet["address"]:
            return "same wallet address"
        if self._is_self(conn.ip, int(conn.listen_port or conn.port)):
            return "same listen address"
        return None

    async def _handle_peers(self, message: dict[str, Any]) -> None:
        for peer in message.get("peers", []):
            ip = normalize_host(peer.get("ip", ""))
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
            if format_host_port(ip, port) not in self.connections:
                self._track(asyncio.create_task(self.connect_peer(ip, port)))

    def _source_label(self, conn: PeerConnection) -> str:
        return f"{conn.name or 'unknown'} {format_host_port(conn.ip, int(conn.listen_port or conn.port))}"

    def _security_peer_fields(self, conn: PeerConnection) -> dict[str, Any]:
        return {
            "peer_name": conn.name,
            "peer_ip": conn.ip,
            "peer_port": int(conn.listen_port or conn.port),
            "wallet_address": conn.address,
        }

    async def _handle_tx(self, conn: PeerConnection, message: dict[str, Any]) -> None:
        tx = message.get("tx")
        if not isinstance(tx, dict):
            self.service.record_security_event(
                "malformed transaction",
                self._source_label(conn),
                "TX message missing transaction object",
                **self._security_peer_fields(conn),
            )
            return
        mid = message.get("message_id") or (
            tx_message_id(tx) if tx.get("tx_id") else f"TX:{id(tx)}"
        )
        if mid in self.seen_message_ids:
            return
        self.seen_message_ids.add(mid)
        source = self._source_label(conn)
        accepted, result = await self.service.receive_transaction(tx, source=source)
        if not accepted:
            self.service.record_security_event(
                "invalid transaction",
                source,
                result,
                tx_id=tx.get("tx_id"),
                wallet_address=tx.get("sender") or conn.address,
                **{
                    key: value
                    for key, value in self._security_peer_fields(conn).items()
                    if key != "wallet_address"
                },
            )
            return
        if accepted and int(message.get("ttl", 0)) > 1:
            await self.broadcast_tx(tx, ttl=int(message["ttl"]) - 1, message_id=mid)

    async def _handle_block(self, conn: PeerConnection, message: dict[str, Any]) -> None:
        block = message.get("block")
        if not isinstance(block, dict):
            self.service.record_security_event(
                "malformed block",
                self._source_label(conn),
                "BLOCK message missing block object",
                **self._security_peer_fields(conn),
            )
            return
        mid = message.get("message_id") or block_message_id(block)
        if mid in self.seen_message_ids:
            return
        self.seen_message_ids.add(mid)
        source = self._source_label(conn)
        accepted, result = await self.service.receive_block(block, source=source)
        if not accepted:
            self.service.record_security_event(
                "invalid block",
                source,
                result,
                block_hash=compute_block_hash(block),
                **self._security_peer_fields(conn),
            )
            return
        if accepted and int(message.get("ttl", 0)) > 1:
            await self.broadcast_block(block, ttl=int(message["ttl"]) - 1, message_id=mid)

    async def _handle_get_blocks(self, conn: PeerConnection, message: dict[str, Any]) -> None:
        from_height = int(message.get("from_height") or 0)
        blocks = self.service.store.get_blocks_from_height(from_height)
        await send_json(conn.writer, {"type": "BLOCKS", "from_height": from_height, "blocks": blocks})

    async def _handle_blocks(self, conn: PeerConnection, message: dict[str, Any]) -> None:
        accepted, result = await self.service.receive_blocks(
            message.get("blocks", []),
            from_height=int(message.get("from_height") or 0),
            source=self._source_label(conn),
        )
        if not accepted:
            self.service.record_security_event(
                "invalid chain sync",
                self._source_label(conn),
                result,
                **self._security_peer_fields(conn),
            )

    def known_peers(self) -> list[dict[str, Any]]:
        peers = self.service.store.list_peers()
        configured = [
            {"ip": normalize_host(host), "port": int(port), "name": None}
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
