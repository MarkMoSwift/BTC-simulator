from __future__ import annotations

import asyncio
import socket
import time
from collections import deque
from typing import Any

from app.config import network_identity, resolve_project_path, save_config
from app.core.blockchain import Blockchain
from app.core.mempool import Mempool
from app.core.miner import Miner
from app.core.transaction import create_transfer
from app.core.wallet import generate_wallet
from app.network.node import P2PNode
from app.storage.sqlite_store import SQLiteStore


class EventLog:
    def __init__(self, maxlen: int = 300):
        self.items: deque[dict[str, Any]] = deque(maxlen=maxlen)

    def add(self, message: str) -> None:
        self.items.appendleft({"time": int(time.time()), "message": message})

    def recent(self, limit: int = 80) -> list[dict[str, Any]]:
        return list(self.items)[:limit]


class NodeService:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        storage_path = resolve_project_path(config, config["storage"]["path"])
        self.events = EventLog()
        self.store = SQLiteStore(storage_path)
        self.blockchain = Blockchain(config, self.store, self.log)
        self._ensure_default_wallet()
        self.mempool = Mempool(self.store, self.blockchain, self.log)
        self.p2p = P2PNode(config, self, self.log)
        self.miner = Miner(self.blockchain, self.mempool, self.log, self._on_mined_block)

    def log(self, message: str) -> None:
        self.events.add(message)

    def _lan_ip(self) -> str:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
        except OSError:
            return "127.0.0.1"
        finally:
            sock.close()

    def _ensure_default_wallet(self) -> None:
        if self.store.get_default_wallet():
            return
        wallet = generate_wallet("default")
        self.store.save_wallet(wallet, make_default=True)
        self.log(f"Wallet generated: {wallet.address[:16]}")

    async def start(self) -> None:
        await self.p2p.start()

    async def shutdown(self) -> None:
        await self.miner.stop()
        await self.p2p.stop()
        self.store.close()

    async def _on_mined_block(self, block: dict[str, Any], block_hash: str) -> None:
        await self.p2p.broadcast_block(block)

    def default_wallet(self) -> dict[str, Any]:
        wallet = self.store.get_default_wallet()
        if not wallet:
            self._ensure_default_wallet()
            wallet = self.store.get_default_wallet()
        if not wallet:
            raise RuntimeError("wallet unavailable")
        return wallet

    def generate_new_wallet(self, name: str = "default") -> dict[str, Any]:
        wallet = generate_wallet(name)
        self.store.save_wallet(wallet, make_default=True)
        self.log(f"Wallet generated: {wallet.address[:16]}")
        return self.default_wallet()

    async def create_transaction(
        self,
        receiver: str,
        amount: float,
        fee: float,
        note: str | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        wallet = self.default_wallet()
        tx = create_transfer(
            sender=wallet["address"],
            receiver=receiver,
            amount=amount,
            fee=fee,
            public_key=wallet["public_key"],
            private_key=wallet["private_key"],
            note=note,
        )
        accepted, result = self.mempool.add_transaction(tx, source="local")
        if not accepted:
            return False, result, None
        await self.p2p.broadcast_tx(tx)
        return True, result, tx

    async def receive_transaction(self, tx: dict[str, Any], source: str = "peer") -> tuple[bool, str]:
        return self.mempool.add_transaction(tx, source=source)

    async def receive_block(self, block: dict[str, Any], source: str = "peer") -> tuple[bool, str]:
        accepted, message = self.blockchain.add_block(block, source=source)
        if not accepted:
            self.log(f"Block rejected from {source}: {message}")
        return accepted, message

    async def receive_blocks(
        self,
        blocks: list[dict[str, Any]],
        from_height: int = 0,
        source: str = "sync",
    ) -> tuple[bool, str]:
        if from_height == 0:
            accepted, message = self.blockchain.replace_with_chain(blocks, source=source)
            if not accepted:
                self.log(f"Chain replacement skipped from {source}: {message}")
            return accepted, message

        accepted_any = False
        last_message = "no blocks"
        for block in blocks:
            accepted, last_message = await self.receive_block(block, source=source)
            accepted_any = accepted_any or accepted
        return accepted_any, last_message

    async def connect_peer(self, ip: str, port: int) -> tuple[bool, str]:
        return await self.p2p.connect_peer(ip, int(port))

    async def sync_blocks(self) -> dict[str, Any]:
        requested = await self.p2p.request_blocks()
        self.log(f"Block sync requested from {requested} peer(s)")
        return {"requested_peers": requested}

    async def set_difficulty(self, difficulty: int) -> dict[str, Any]:
        difficulty = int(difficulty)
        minimum = int(self.config.get("min_difficulty", 0))
        maximum = int(self.config.get("max_difficulty", 12))
        if difficulty < minimum or difficulty > maximum:
            raise ValueError(f"difficulty must be between {minimum} and {maximum}")
        was_mining = self.miner.is_mining
        if was_mining:
            await self.miner.stop()
        self.config["difficulty"] = difficulty
        save_config(self.config)
        self.p2p.identity = network_identity(self.config)
        self.log(f"Difficulty set to {difficulty}")
        return {
            "difficulty": difficulty,
            "mining_stopped": was_mining,
            "message": "difficulty updated",
        }

    async def reset_chain(self) -> dict[str, Any]:
        was_mining = self.miner.is_mining
        if was_mining:
            await self.miner.stop()
        genesis_hash = self.blockchain.reset_to_genesis()
        return {
            "height": self.blockchain.height(),
            "tip_hash": genesis_hash,
            "mining_stopped": was_mining,
            "message": "blockchain reset to genesis",
        }

    def list_blocks(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        limit = min(max(int(limit), 1), 200)
        offset = max(int(offset), 0)
        return {
            "blocks": self.store.list_block_summaries(limit=limit, offset=offset),
            "limit": limit,
            "offset": offset,
            "height": self.blockchain.height(),
        }

    def block_detail(self, identifier: str) -> dict[str, Any] | None:
        return self.store.get_block_detail(identifier)

    def network_info(self) -> dict[str, Any]:
        identity = network_identity(self.config)
        lan_ip = self._lan_ip()
        web_host = str(self.config["web_host"])
        web_display_host = lan_ip if web_host == "0.0.0.0" else web_host
        listen_host = lan_ip if str(self.config["listen_ip"]) == "0.0.0.0" else str(self.config["listen_ip"])
        web_url = f"http://{web_display_host}:{int(self.config['web_port'])}"
        p2p_address = f"{listen_host}:{int(self.config['listen_port'])}"
        return {
            **identity,
            "lan_ip": lan_ip,
            "web_url": web_url,
            "p2p_address": p2p_address,
            "listen_ip": self.config["listen_ip"],
            "listen_port": int(self.config["listen_port"]),
            "web_host": self.config["web_host"],
            "web_port": int(self.config["web_port"]),
        }

    def classroom_status(self) -> dict[str, Any]:
        status = self.status()
        network = status["network"]
        own = {
            "name": status["node_name"],
            "ip": network["lan_ip"],
            "port": network["listen_port"],
            "address": status["wallet"]["address"],
            "status": "本机",
            "direction": "self",
            "height": status["height"],
            "difficulty": status["difficulty"],
            "mining_status": status["mining"]["status"],
            "network_id": network["network_id"],
            "chain_params_hash": network["chain_params_hash"],
            "last_seen": int(time.time()),
            "mismatch_reason": None,
        }
        peers = self.store.list_peers()
        mismatches = [
            peer for peer in peers
            if peer.get("status") == "参数不匹配"
            or "mismatch" in str(peer.get("mismatch_reason") or "")
        ]
        return {
            "self": own,
            "peers": peers,
            "nodes": [own, *peers],
            "mismatch_count": len(mismatches),
            "network": network,
        }

    def status(self) -> dict[str, Any]:
        wallet = self.default_wallet()
        tip = self.blockchain.tip()
        peers = self.p2p.connection_counts()
        mempool_stats = self.mempool.stats()
        return {
            "version": self.config["version"],
            "node_name": self.config["node_name"],
            "wallet": {
                "name": wallet["name"],
                "address": wallet["address"],
                "public_key": wallet["public_key"],
                "created_at": wallet["created_at"],
            },
            "balance": self.blockchain.get_balance(wallet["address"]),
            "available_balance": self.blockchain.get_available_balance(wallet["address"]),
            "height": int(tip["height"]),
            "tip_hash": tip["hash"],
            "last_block_time": int(tip["timestamp"]),
            "difficulty": self.blockchain.expected_difficulty(),
            "difficulty_policy": self.blockchain.difficulty_policy(),
            "network": self.network_info(),
            "mining": {
                "status": self.miner.status,
                "is_mining": self.miner.is_mining,
                "nonce": self.miner.current_nonce,
                "hash": self.miner.current_hash,
            },
            "mempool": mempool_stats,
            "peers": peers,
            "logs": self.events.recent(),
        }
