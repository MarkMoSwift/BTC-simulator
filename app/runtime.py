"""课堂服务：把链、内存池、矿池、学生账号和广播接在一起。"""
from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import re
import secrets
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from app.config import (
    ConfigError,
    SESSION_VERSION,
    Settings,
    VERSION,
    load_settings,
    resolve_config_path,
    resolve_session_storage,
    resolve_storage_path,
    save_settings,
    settings_from_raw,
    settings_to_raw,
)
from app.core.block import target_for_bits
from app.core.chain import Blockchain
from app.core.local_miner import LocalMiner
from app.core.mempool import Mempool
from app.core.mining_pool import MiningPool
from app.core.redpacket import amount_for_index
from app.core.transaction import compute_tx_id, tx_bytes
from app.core.wallet import LocalWallet, address_from_public_key, verify_signature
from app.storage.sqlite_store import SQLiteStore
from app.units import format_coins
from app.web.hub import ConnectionHub
from app.web.peers import Peers
from app.web.presence import Presence

NICKNAME_PATTERN = re.compile(r"^[\w\u4e00-\u9fff-]{1,12}$")
LOGIN_NONCE_SECONDS = 300
SESSION_SECONDS = 12 * 3600
ONLINE_SECONDS = 30
TOUCH_SECONDS = 5
RESERVATION_SECONDS = 90
TICK_SECONDS = 1.0
FLUSH_SECONDS = 5.0


class ClassroomError(Exception):
    """可以安全展示给学生的业务错误。"""


class ClassroomService:
    def __init__(
        self, config_path: str | Path | None = None, *, native_wallet: bool = True,
        builtin_miner: bool = True,
    ):
        self.config_path = resolve_config_path(config_path)
        self.settings: Settings = load_settings(self.config_path)
        session = self.settings.session()
        if self.settings.peers and session is None:
            raise ConfigError("当前版本尚未实现独立节点间的 P2P 同步，请将 peers 设为 []；不会自动连接列表中的节点")
        storage = (
            resolve_session_storage(self.settings, self.config_path, session)
            if session else resolve_storage_path(self.settings, self.config_path)
        )
        self.store = SQLiteStore(storage)
        if session and self.settings.reset_chain_on_start:
            self.store.reset_session()
        self.hub = ConnectionHub()
        self.screen_hub = ConnectionHub()
        self.events: list[dict[str, Any]] = []
        self.chain = Blockchain(self.settings.chain, self.store, log=self._log, session=session)
        self._observed_tip = self.chain.tip_hash
        self.mempool = Mempool(self.store, lambda: self.chain.state, log=self._log)
        self.peers = Peers(self.chain, self.settings.peers, self._on_peer_chain, self.submit_transaction)
        self.peers.node_name = self.settings.node_name
        self.peers.paused.update(self.settings.paused_peers)
        self.pool = MiningPool(
            self.chain,
            self.mempool,
            self.settings.pool,
            log=self._log,
            on_block=self._on_block,
        )
        self.sessions: dict[str, dict[str, Any]] = {}
        self.login_nonces: dict[str, tuple[str, float]] = {}
        self.last_touch: dict[str, float] = {}
        self.claim_reservations: dict[str, dict[int, tuple[str, float]]] = {}
        self.announcement: dict[str, Any] | None = None
        self._tick_task: asyncio.Task[None] | None = None
        self._last_flush = time.time()
        self.wallet = (
            LocalWallet(self.config_path.with_suffix(".wallet.key")) if session and native_wallet else None
        )
        self.presence = Presence(self.chain.state.domain, self.wallet)
        self.peers.presence_messages = self.presence.messages
        self.peers.on_presence = self.presence.accept
        self._last_presence = 0.0
        self.local_miner = (
            LocalMiner(self._local_job, self._local_solution) if self.wallet and builtin_miner else None
        )

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        await self.hub.start()
        await self.screen_hub.start()
        if self.chain.session is not None:
            self.peers.start()
        if self.wallet is not None:
            self._local_player()
            self._announce_presence(time.time())
        if self._tick_task is None:
            self._tick_task = asyncio.create_task(self._tick_loop(), name="classroom-tick")

    async def shutdown(self) -> None:
        if self.local_miner is not None:
            await self.local_miner.stop()
        await self.peers.stop()
        if self._tick_task is not None:
            self._tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tick_task
            self._tick_task = None
        self.pool.flush_stats()
        await self.hub.stop()
        await self.screen_hub.stop()
        self.store.close()

    def _local_player(self) -> dict[str, Any]:
        if self.wallet is None:
            raise ClassroomError("此节点没有本机钱包")
        player = self.store.get_player_by_address(self.wallet.address)
        nickname = self.settings.node_name
        owner = self.store.get_player(nickname)
        if owner is not None and owner["address"] != self.wallet.address:
            raise ClassroomError("node_name 已被其他钱包使用，请修改配置中的节点名称")
        if player is None:
            self.store.upsert_player(nickname, self.wallet.address, self.wallet.public_key, int(time.time()))
            player = self.store.get_player_by_address(self.wallet.address)
        elif player["nickname"] != nickname:
            previous = player["nickname"]
            self.store.rename_player(self.wallet.address, nickname)
            for record in self.sessions.values():
                if record["role"] == "student" and record["nickname"] == previous:
                    record["nickname"] = nickname
            player = self.store.get_player_by_address(self.wallet.address)
        return player

    def local_login(self, *, internal: bool = False) -> dict[str, Any]:
        player = self._local_player()
        token = secrets.token_urlsafe(24)
        self.sessions[token] = {"nickname": player["nickname"], "role": "student",
                                "client_kind": "native",
                                "expires_at": float("inf") if internal else time.time() + SESSION_SECONDS}
        return {"token": token, "player": self.player_view_by_record(player)}

    def _local_job(self) -> dict[str, Any]:
        try:
            return self.issue_job(self.wallet.address)
        except ClassroomError:
            return {}

    def _local_solution(self, job: dict[str, Any], nonce: int) -> dict[str, Any]:
        return self.submit_share(self.wallet.address, job["job_id"], nonce, job["share_bits"])

    def local_transaction(self, fields: dict[str, Any]) -> dict[str, Any]:
        if self.wallet is None:
            raise ClassroomError("此节点没有本机钱包")
        tx = {
            **fields, "sender": self.wallet.address, "public_key": self.wallet.public_key,
            "domain": self.chain.state.domain, "timestamp": int(time.time()),
        }
        tx.pop("tx_id", None)
        tx.pop("signature", None)
        tx["tx_id"] = compute_tx_id(tx)
        tx["signature"] = self.wallet.sign(tx_bytes(tx))
        return self.submit_transaction(tx)

    def connect_peer(self, address: str) -> dict[str, Any]:
        if self.chain.session is None:
            raise ClassroomError("请选择课堂场次")
        normalized = settings_from_raw({"peers": [address]}).peers[0]
        updated = replace(self.settings,
                          peers=list(dict.fromkeys([*self.settings.peers, normalized])),
                          paused_peers=[peer for peer in self.settings.paused_peers if peer != normalized])
        save_settings(updated, self.config_path)
        self.settings = updated
        self.peers.addresses = updated.peers
        self.peers.connect_address(normalized)
        return {"peers": updated.peers}

    async def disconnect_peer(self, address: str, *, delete: bool = False) -> dict[str, Any]:
        normalized = settings_from_raw({"peers": [address]}).peers[0]
        if normalized not in self.settings.peers:
            raise ClassroomError("此节点不在已添加的列表中")
        updated = replace(self.settings,
                          peers=[peer for peer in self.settings.peers if not delete or peer != normalized],
                          paused_peers=([peer for peer in self.settings.paused_peers if peer != normalized]
                                        if delete else list(dict.fromkeys([*self.settings.paused_peers, normalized]))))
        save_settings(updated, self.config_path)
        self.settings = updated
        await self.peers.disconnect_address(normalized)
        self.peers.addresses = updated.peers
        if delete:
            self.peers.paused.discard(normalized)
            self.peers.statuses.pop(normalized, None)
            self.peers.log(f"已删除节点 {normalized}")
        return {"peers": updated.peers, "paused_peers": updated.paused_peers}

    def _log(self, message: str) -> None:
        self.events.append({"at": int(time.time()), "message": message})
        del self.events[:-200]

    def _on_block(self, block: dict[str, Any]) -> None:
        self._observed_tip = self.chain.tip_hash
        self.peers.relay({"type": "tip", "hash": self.chain.tip_hash})
        coinbase = block["transactions"][0]
        self.publish(
            "block",
            {
                "height": self.chain.height,
                "hash": self.chain.tip_hash,
                "difficulty": block["header"]["difficulty"],
                "timestamp": block["header"]["timestamp"],
                "tx_count": len(block["transactions"]),
                "finder": coinbase["finder"],
                "subsidy": coinbase["subsidy"],
                "fees": coinbase["fees"],
                "payouts": coinbase["payouts"],
            },
        )

    def _on_peer_chain(self) -> None:
        self.mempool.reload()
        reorganized = self._observed_tip not in self.chain.hashes
        self._observed_tip = self.chain.tip_hash
        self.peers.relay({"type": "tip", "hash": self.chain.tip_hash})
        self.publish("chain_changed", {"height": self.chain.height, "hash": self.chain.tip_hash,
                                      "reorganized": reorganized})

    def publish(self, kind: str, data: dict[str, Any]) -> None:
        self.hub.publish({"type": kind, "data": data})
        if kind != "tick" and self.screen_hub.connections:
            self.screen_hub.publish({"type": kind, "data": data})

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            now = time.time()
            if now - self._last_presence >= 10:
                self._announce_presence(now)
            if self.pool.tick(now)["refreshed"] and self.chain.session:
                self.publish("mining_job_changed", {})
            self._expire_reservations(now)
            self._expire_sessions(now)
            if now - self._last_flush >= FLUSH_SECONDS:
                self._last_flush = now
                self.pool.flush_stats()
            if self.hub.connections or self.screen_hub.connections:
                tick = self.live_tick(now)
                if self.hub.connections:
                    self.publish("tick", tick)
                if self.screen_hub.connections:
                    self.screen_hub.publish({"type": "tick", "data": self.screen_tick(now, tick)})

    def _expire_reservations(self, now: float) -> None:
        for packet_id, reservations in list(self.claim_reservations.items()):
            for index, (_, expires_at) in list(reservations.items()):
                if expires_at < now:
                    del reservations[index]
            if not reservations:
                del self.claim_reservations[packet_id]

    def _expire_sessions(self, now: float) -> None:
        for token, session in list(self.sessions.items()):
            if session["expires_at"] < now:
                del self.sessions[token]
        for nickname, (_, expires_at) in list(self.login_nonces.items()):
            if expires_at < now:
                del self.login_nonces[nickname]

    # ---------- 学生账号 ----------
    def begin_login(self, nickname: str) -> dict[str, Any]:
        nickname = self.normalize_nickname(nickname)
        nonce = secrets.token_hex(8)
        self.login_nonces[nickname] = (nonce, time.time() + LOGIN_NONCE_SECONDS)
        player = self.store.get_player(nickname)
        return {"nickname": nickname, "nonce": nonce, "registered": player is not None}

    def finish_login(self, nickname: str, nonce: str, public_key: str, signature: str) -> dict[str, Any]:
        nickname = self.normalize_nickname(nickname)
        stored = self.login_nonces.get(nickname)
        if not stored or stored[0] != nonce or stored[1] < time.time():
            raise ClassroomError("登录凭证已过期，请刷新页面重试")
        del self.login_nonces[nickname]

        try:
            address = address_from_public_key(public_key)
        except ValueError as exc:
            raise ClassroomError("公钥格式不正确") from exc
        message = login_message(nickname, nonce)
        if not verify_signature(public_key, signature, message):
            raise ClassroomError("签名校验失败，请确认浏览器存储的私钥与昵称对应")

        player = self.store.get_player(nickname)
        now = int(time.time())
        if player is None:
            existing = self.store.get_player_by_address(address)
            if existing is not None:
                if existing["banned"]:
                    raise ClassroomError("这个钱包已被教师停用")
                if self.wallet is not None and address == self.wallet.address:
                    raise ClassroomError("本机节点钱包名称请在 node_name 中修改")
                previous = existing["nickname"]
                self.store.rename_player(address, nickname)
                self.store.touch_player(nickname, now)
                for record in self.sessions.values():
                    if record["role"] == "student" and record["nickname"] == previous:
                        record["nickname"] = nickname
                for identity in self.hub.identities.values():
                    if identity.get("address") == address:
                        identity["nickname"] = nickname
            else:
                self.store.upsert_player(nickname, address, public_key, now)
                self._log(f"新同学加入：{nickname}")
        else:
            if player["banned"]:
                raise ClassroomError("这个昵称已被教师停用")
            if player["address"] != address:
                raise ClassroomError("昵称已被其他钱包占用，请换一个昵称")
            self.store.touch_player(nickname, now)

        token = secrets.token_urlsafe(24)
        self.sessions[token] = {
            "nickname": nickname,
            "role": "student",
            "expires_at": time.time() + SESSION_SECONDS,
        }
        return {"token": token, "player": self.player_view(nickname)}

    def admin_login(self, key: str) -> dict[str, Any]:
        if self.chain.session is not None:
            raise ClassroomError("当前场次没有教师管理权限")
        if not self.settings.admin_key or not secrets.compare_digest(key.strip(), self.settings.admin_key):
            raise ClassroomError("教师口令不正确")
        token = secrets.token_urlsafe(24)
        self.sessions[token] = {
            "nickname": "教师",
            "role": "teacher",
            "expires_at": time.time() + SESSION_SECONDS,
        }
        return {"token": token}

    def session(self, token: str | None) -> dict[str, Any]:
        if not token:
            raise ClassroomError("请先登录")
        record = self.sessions.get(token)
        if not record or record["expires_at"] < time.time():
            raise ClassroomError("登录状态已过期，请重新登录")
        return record

    def student(self, token: str | None) -> dict[str, Any]:
        record = self.session(token)
        if record["role"] != "student":
            raise ClassroomError("该操作需要学生账号")
        player = self.store.get_player(record["nickname"])
        if player is None:
            raise ClassroomError("这个昵称已经被教师移除，请重新登录")
        if player["banned"]:
            raise ClassroomError("这个昵称已被教师停用")
        return player

    def teacher(self, token: str | None) -> None:
        if self.session(token)["role"] != "teacher":
            raise ClassroomError("该操作需要教师权限")

    def normalize_nickname(self, nickname: str) -> str:
        name = str(nickname or "").strip()
        if not NICKNAME_PATTERN.match(name):
            raise ClassroomError("昵称只能是 1-12 个汉字、字母、数字、下划线或短横线")
        return name

    def _touch(self, address: str, now: float) -> None:
        if now - self.last_touch.get(address, 0.0) < TOUCH_SECONDS:
            return
        self.last_touch[address] = now
        player = self.store.get_player_by_address(address)
        if player is not None:
            self.store.touch_player(player["nickname"], int(now))

    # ---------- 视图 ----------
    def _observed_wallets(self, now: float) -> dict[str, dict[str, Any]]:
        mining = self.pool.stats(now)["miners"]
        browsers = {row["address"] for row in self.hub.browser_participants()}
        native = self.wallet.address if self.wallet else None
        return {player["address"]: {
                    "address": player["address"], "nickname": player["nickname"],
                    "online": (player["address"] == native or player["address"] in browsers
                               or now - int(player["last_seen"]) <= ONLINE_SECONDS),
                    "hashrate": float(mining.get(player["address"], {}).get("hashrate", 0.0)),
                } for player in self.store.list_players()}

    def _announce_presence(self, now: float) -> None:
        rows = sorted(self._observed_wallets(now).values(),
                      key=lambda row: (not row["online"], row["address"]))
        message = self.presence.create(rows, now)
        if message is not None:
            self.peers.relay(message)
        self._last_presence = now

    def _wallet_status(self, now: float) -> dict[str, dict[str, Any]]:
        rows = self.presence.wallets(now)
        for address, local in self._observed_wallets(now).items():
            remote = rows.get(address)
            if remote is None:
                rows[address] = local
            else:
                rows[address] = {**local,
                                 "nickname": local["nickname"] if local["online"] else remote["nickname"],
                                 "online": local["online"] or remote["online"],
                                 "hashrate": max(local["hashrate"], remote["hashrate"])}
        return rows

    def nicknames(self) -> dict[str, str]:
        return {**{address: row["nickname"] for address, row in self.presence.wallets(time.time()).items()},
                **{player["address"]: player["nickname"] for player in self.store.list_players()}}

    def live_tick(self, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        wallets = self._wallet_status(now)
        tick = {
            "chain": self.chain.snapshot(),
            "mempool": self.mempool.stats(),
            "nodes": {**self.peers.snapshot(), "node_name": self.settings.node_name,
                      "browsers": self.hub.browser_participants()},
            "packets": self.packets_view(now),
            "market": self.market_view()["listings"],
            "auctions": self.auctions_view(now),
            "announcement": self.announcement,
            "online": sum(row["online"] for row in wallets.values()),
            "names": {address: row["nickname"] for address, row in wallets.items()},
        }
        if self.chain.session is None:
            tick.update(pool=self.pool.stats(now), leaderboard=self.leaderboard(now))
        return tick

    def screen_tick(self, now: float | None = None, tick: dict[str, Any] | None = None) -> dict[str, Any]:
        common = self.live_tick(now) if tick is None else tick
        return {
            **common,
            "leaderboard": common.get("leaderboard") or self.leaderboard(now),
            "recent_blocks": [block for block in self.recent_blocks(limit=5) if block["height"]],
        }

    def leaderboard(self, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        pool_stats = self.pool.stats(now)["miners"]
        state = self.chain.state
        rows: list[dict[str, Any]] = []
        players = {player["address"]: player for player in self.store.list_players()}
        wallets = self._wallet_status(now)
        addresses = list(dict.fromkeys([*players, *wallets]))
        if self.chain.session:
            addresses.extend(address for address in state.mining_income if address not in addresses)
        for address in addresses:
            player = players.get(address, {})
            mining = pool_stats.get(address, {})
            observed = wallets.get(address, {})
            rows.append(
                {
                    "nickname": observed.get("nickname", player.get("nickname", address[:12] + "…")),
                    "address": address,
                    "hashrate": float(observed.get("hashrate", mining.get("hashrate", 0.0))),
                    "share_bits": int(mining.get("share_bits", self.settings.pool.initial_share_bits)),
                    "best_share_bits": int(mining.get("best_share_bits", player.get("best_share_bits", 0))),
                    "blocks_found": (state.mined_blocks.get(address, 0) if self.chain.session
                                     else int(mining.get("blocks_found", 0)) + int(player["blocks_found"])),
                    "session_work": int(mining.get("work", 0)),
                    "total_work": int(player.get("total_work", 0)),
                    "mined": int(state.mining_income.get(address, 0)),
                    "balance": int(state.balance_of(address)),
                    "nfts": len(state.nfts_of(address)),
                    "online": bool(observed.get("online", False)),
                    "banned": bool(player.get("banned", False)),
                }
            )
        by_hashrate = sorted(rows, key=lambda row: (-row["hashrate"], -row["mined"], row["nickname"]))
        by_mined = sorted(rows, key=lambda row: (-row["mined"], -row["hashrate"], row["nickname"]))
        for index, row in enumerate(by_hashrate, start=1):
            row["hashrate_rank"] = index
        for index, row in enumerate(by_mined, start=1):
            row["mined_rank"] = index
        return {
            "rows": rows,
            "by_hashrate": by_hashrate,
            "by_mined": by_mined,
            "network_hashrate": sum(row["hashrate"] for row in rows),
        }

    def player_view(self, nickname: str) -> dict[str, Any]:
        player = self.store.get_player(nickname)
        if player is None:
            raise ClassroomError("学生不存在")
        return self.player_view_by_record(player)

    def player_view_by_record(self, player: dict[str, Any]) -> dict[str, Any]:
        address = player["address"]
        state = self.chain.state
        now = time.time()
        mining = self.pool.miner_stats(address, now)
        if self.chain.session:
            mining["blocks_found"] = state.mined_blocks.get(address, 0)
        return {
            "nickname": player["nickname"],
            "address": address,
            "balance": state.balance_of(address),
            "mined": int(state.mining_income.get(address, 0)),
            "mining": mining,
            "cumulative": {
                "total_work": int(player["total_work"]),
                "total_shares": int(player["total_shares"]),
                "best_share_bits": int(player["best_share_bits"]),
                "blocks_found": int(player["blocks_found"]),
            },
            "nfts": [self._nft_view(nft) for nft in state.nfts_of(address)],
            "packets_created": [
                self._packet_view(packet, now)
                for packet in state.packets.values()
                if packet["sender"] == address
            ][::-1],
            "packets_claimed": [
                {
                    "packet_id": packet["packet_id"],
                    "amount": claim["amount"],
                    "index": index,
                    "note": packet["note"],
                    "sender": packet["sender"],
                }
                for packet in state.packets.values()
                for index, claim in packet["claims"].items()
                if claim["receiver"] == address
            ][::-1],
            "transactions": [
                {**tx, "confirmations": self.chain.height - tx["block_height"] + 1}
                for tx in self.store.list_transactions(address=address, limit=12)
            ],
        }

    def _nft_view(self, nft: dict[str, Any]) -> dict[str, Any]:
        listing = self.chain.state.listings.get(nft["token_id"])
        auction = next(
            (item for item in self.chain.state.auctions.values() if item["token_id"] == nft["token_id"]),
            None,
        )
        return {
            "token_id": nft["token_id"],
            "name": nft["name"],
            "seed": nft["seed"],
            "thumbnail_id": nft.get("thumbnail_id"),
            "owner": nft["owner"],
            "created_at": nft["created_at"],
            "mint_height": nft["mint_height"],
            "price": int(listing["price"]) if listing else None,
            "auction_id": auction["auction_id"] if auction else None,
        }

    def auctions_view(self, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        names = self.nicknames()
        active = [self._auction_view(auction, names, now) for auction in self.chain.state.auctions.values()]
        active.sort(key=lambda item: item["ends_height"] or item["ends_at"])
        history = [
            {
                **record,
                "seller_name": names.get(record["seller"], "未知"),
                "winner_name": names.get(record["winner"], "") if record["winner"] else "",
            }
            for record in reversed(self.chain.state.auction_history)
        ]
        return {"active": active[:50], "history": history[:50]}

    def _auction_view(self, auction: dict[str, Any], names: dict[str, str], now: float) -> dict[str, Any]:
        nft = self.chain.state.nfts.get(auction["token_id"])
        highest = int(auction["highest_bid"]) if auction["highest_bid"] else None
        return {
            "auction_id": auction["auction_id"],
            "token_id": auction["token_id"],
            "name": nft["name"] if nft else "未知藏品",
            "mint_height": nft["mint_height"] if nft else None,
            "thumbnail_id": nft.get("thumbnail_id") if nft else None,
            "seller": auction["seller"],
            "seller_name": names.get(auction["seller"], "未知"),
            "reserve_price": int(auction["reserve_price"]),
            "minimum_bid": self.chain.state.minimum_bid(auction),
            "highest_bid": highest,
            "highest_bidder": auction["highest_bidder"],
            "highest_bidder_name": names.get(auction["highest_bidder"], "") if auction["highest_bidder"] else "",
            "bid_count": int(auction["bid_count"]),
            "created_at": int(auction["created_at"]),
            "ends_at": auction["ends_at"],
            "ends_height": auction["ends_height"],
            "blocks_left": (auction["ends_height"] - self.chain.height if self.chain.session else None),
            "seconds_left": (None if self.chain.session else max(auction["ends_at"] - int(now), 0)),
        }

    def _packet_view(self, packet: dict[str, Any], now: float) -> dict[str, Any]:
        claims = [
            {
                "index": int(index),
                "receiver": claim["receiver"],
                "amount": int(claim["amount"]),
                "at": int(claim["at"]),
            }
            for index, claim in sorted(packet["claims"].items())
        ]
        if packet.get("refunded"):
            status = "refunded"
        elif len(claims) >= packet["count"]:
            status = "finished"
        elif not self.chain.session and now > packet["expires_at"]:
            status = "expired"
        else:
            status = "active"
        return {
            "packet_id": packet["packet_id"],
            "sender": packet["sender"],
            "total": int(packet["total"]),
            "count": int(packet["count"]),
            "note": packet["note"],
            "created_at": int(packet["created_at"]),
            "expires_at": packet["expires_at"],
            "expires_height": packet["expires_height"],
            "remaining": int(packet["remaining"]),
            "claimed_count": len(claims),
            "claims": claims,
            "status": status,
        }

    def packets_view(self, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        packets = [self._packet_view(packet, now) for packet in self.chain.state.packets.values()]
        packets.sort(key=lambda item: item["created_at"], reverse=True)
        return {
            "active": [item for item in packets if item["status"] == "active"],
            "history": [item for item in packets if item["status"] != "active"],
        }

    def market_view(self) -> dict[str, Any]:
        state = self.chain.state
        names = self.nicknames()
        listings: list[dict[str, Any]] = []
        for token_id, listing in state.listings.items():
            nft = state.nfts.get(token_id)
            if not nft:
                continue
            listings.append(
                {
                    **self._nft_view(nft),
                    "seller": listing["seller"],
                    "seller_name": names.get(listing["seller"], "未知"),
                    "price": int(listing["price"]),
                    "listed_at": int(listing["listed_at"]),
                }
            )
        listings.sort(key=lambda item: -item["listed_at"])
        return {"listings": listings, "names": names}

    def recent_blocks(self, limit: int = 8) -> list[dict[str, Any]]:
        return self.store.list_blocks_descending(limit=limit)

    def public_state(self) -> dict[str, Any]:
        return {
            "classroom": self.settings.classroom_name,
            "node_name": self.settings.node_name,
            "version": SESSION_VERSION if self.chain.session else VERSION,
            "transaction_domain": self.chain.state.domain,
            "chain": self.chain.snapshot(),
            "mempool": self.mempool.stats(),
            "recent_blocks": self.recent_blocks(),
            "announcement": self.announcement,
            "storage": self.store.stats(),
        }

    # ---------- 交易与挖矿 ----------
    def submit_transaction(self, tx: dict[str, Any]) -> dict[str, Any]:
        tx_id = self.mempool.add(tx)
        self.peers.relay({"type": "tx", "tx": tx})
        self.publish("mempool", {"tx_id": tx_id, "type": tx["type"]})
        return {"tx_id": tx_id, "mempool": self.mempool.stats()}

    def issue_job(self, address: str, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        if self.chain.session is not None and now < self.chain.session.start_time:
            raise ClassroomError("课堂场次尚未开场")
        if self.chain.session is not None and not self.peers.synced.is_set():
            raise ClassroomError("正在尝试同步课堂区块")
        self._touch(address, now)
        return self.pool.issue_job(address, now)

    def submit_share(
        self,
        address: str,
        job_id: str,
        nonce: int,
        share_bits: int,
        now: float | None = None,
    ) -> dict[str, Any]:
        now = now if now is not None else time.time()
        self._touch(address, now)
        result = self.pool.submit_share(address, job_id, nonce, share_bits, now)
        if result.get("accepted"):
            self.publish(
                "share",
                {
                    "address": address,
                    "share_bits": result["share_bits"],
                    "block": result.get("block"),
                },
            )
        return result

    def reserve_claim(self, address: str, packet_id: str) -> dict[str, Any]:
        now = time.time()
        state = self.chain.state
        packet = state.packets.get(packet_id)
        if not packet:
            raise ClassroomError("红包不存在")
        if (state.height + 1 >= packet["expires_height"] if state.domain else now > packet["expires_at"]):
            raise ClassroomError("红包已过期")
        if packet.get("refunded"):
            raise ClassroomError("红包已经被退回")
        taken = set(packet["claims"]) | self.mempool.pending_claims(packet_id)
        reservations = self.claim_reservations.setdefault(packet_id, {})
        taken |= {index for index in reservations if reservations[index][1] > now}
        if any(claim["receiver"] == address for claim in packet["claims"].values()):
            raise ClassroomError("你已经领过这个红包了")
        if any(
            tx["type"] == "packet_claim" and tx["packet_id"] == packet_id and tx["sender"] == address
            for tx in self.mempool.transactions.values()
        ):
            raise ClassroomError("你已经抢过这个红包，正在等待打包")
        index = next((candidate for candidate in range(packet["count"]) if candidate not in taken), None)
        if index is None:
            raise ClassroomError("红包已经被抢完了")
        amount = amount_for_index(packet_id, packet["total"], packet["count"], index)
        reservations[index] = (address, now + RESERVATION_SECONDS)
        return {"packet_id": packet_id, "index": index, "amount": amount}

    # ---------- 教师操作 ----------
    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        if self.chain.session:
            raise ClassroomError("3.0 场次的共识规则不可在运行中修改")
        raw = settings_to_raw(self.settings)
        raw.update(chain=asdict(self.settings.chain), pool=asdict(self.settings.pool))
        for section in ("chain", "pool"):
            if section in patch:
                if not isinstance(patch[section], dict):
                    raise ClassroomError(f"{section} 必须是对象")
                raw[section].update(patch[section])
        for key in ("classroom_name",):
            if key in patch:
                raw[key] = patch[key]
        try:
            updated = settings_from_raw(raw)
        except ConfigError as exc:
            raise ClassroomError(str(exc)) from exc
        self.settings = updated
        self.chain.update_settings(updated.chain)
        self.pool.update_settings(updated.pool)
        save_settings(updated, self.config_path)
        self.publish("settings", self.settings_view())
        return self.settings_view()

    def reset_chain(self) -> dict[str, Any]:
        self.mempool.clear()
        self.pool.reset()
        self.chain.reset_to_genesis()
        self.store.reset_mining_stats()
        self.last_touch.clear()
        self.claim_reservations.clear()
        self.publish("reset", self.public_state())
        return {"height": self.chain.height, "tip_hash": self.chain.tip_hash}

    def set_banned(self, nickname: str, banned: bool) -> dict[str, Any]:
        player = self.store.get_player(nickname)
        if player is None:
            raise ClassroomError("学生不存在")
        self.store.set_player_banned(nickname, banned)
        self.publish("players", {"nickname": nickname, "banned": banned})
        return {"nickname": nickname, "banned": banned}

    def reset_player(self, nickname: str) -> dict[str, Any]:
        player = self.store.get_player(nickname)
        if player is None:
            raise ClassroomError("学生不存在")
        self.store.delete_player(nickname)
        for token, session in list(self.sessions.items()):
            if session["nickname"] == nickname:
                del self.sessions[token]
        self.publish("players", {"nickname": nickname, "removed": True})
        return {"nickname": nickname, "removed": True}

    def announce(self, message: str) -> dict[str, Any]:
        text = str(message or "").strip()[:120]
        self.announcement = {"message": text, "at": int(time.time())}
        self.publish("announcement", self.announcement)
        return self.announcement

    def settings_view(self) -> dict[str, Any]:
        raw = settings_to_raw(self.settings)
        if not self.chain.session:
            raw.update(chain=asdict(self.settings.chain), pool=asdict(self.settings.pool))
        raw["current_difficulty"] = self.chain.difficulty_for(self.chain.height + 1)
        raw["current_target"] = target_for_bits(self.chain.difficulty_for(self.chain.height + 1))
        return raw

    def teacher_overview(self) -> dict[str, Any]:
        now = time.time()
        leaderboard = self.leaderboard(now)
        return {
            "settings": self.settings_view(),
            "players": leaderboard["rows"],
            "chain": self.chain.snapshot(),
            "mempool": self.mempool.stats(),
            "pool": {
                "network_hashrate": leaderboard["network_hashrate"],
                "job": self.pool.stats(now),
            },
            "storage": self.store.stats(),
            "events": self.events[-40:][::-1],
            "announcement": self.announcement,
            "web_urls": self.web_urls(),
        }

    def export_results(self) -> str:
        now = time.time()
        rows = self.leaderboard(now)["rows"]
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "昵称",
                "钱包地址",
                "当前余额",
                "累计挖矿",
                "实时算力(H/s)",
                "本次工作量",
                "累计工作量",
                "份额数",
                "最高份额难度",
                "找到区块数",
                "藏品数",
                "在线",
            ]
        )
        for row in rows:
            player = self.store.get_player(row["nickname"]) or {}
            writer.writerow(
                [
                    row["nickname"],
                    row["address"],
                    format_coins(row["balance"], digits=8),
                    format_coins(row["mined"], digits=8),
                    f"{row['hashrate']:.0f}",
                    row["session_work"],
                    row["total_work"],
                    int(player.get("total_shares", 0)),
                    row["best_share_bits"],
                    row["blocks_found"],
                    row["nfts"],
                    "是" if row["online"] else "否",
                ]
            )
        return buffer.getvalue()

    def web_urls(self) -> list[str]:
        scheme = "https" if self.settings.tls_certificate_dir else "http"
        return [f"{scheme}://{ip}:{self.settings.web_port}" for ip in self.local_addresses()]

    def local_addresses(self) -> list[str]:
        import socket

        addresses: list[str] = []
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("8.8.8.8", 80))
            addresses.append(probe.getsockname()[0])
            probe.close()
        except OSError:
            pass
        if self.settings.web_host in {"127.0.0.1", "localhost"}:
            return ["127.0.0.1"]
        return addresses or ["127.0.0.1"]


def login_message(nickname: str, nonce: str) -> bytes:
    return f"btc-sim-login|{nickname}|{nonce}".encode("utf-8")
