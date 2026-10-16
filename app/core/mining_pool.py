"""矿池：下发挖矿作业、校验份额、按上一阶段的工作量分配区块奖励。

区块头必须在挖矿开始前就锁定梅克尔根，所以 coinbase 的分配名单不可能等到"谁挖到
这个块"的时候再决定。这里的做法是：每份作业携带一份已经封盘的工作量快照，矿工拿到
的区块头里已经包含按这份快照分钱的 coinbase；发现区块的人再额外拿一份发现者奖励。
"""
from __future__ import annotations

import hashlib
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from app.config import PoolSettings
from app.core.block import block_hash, meets_bits, merkle_root, target_for_bits
from app.core.chain import Blockchain
from app.core.mempool import Mempool, select_for_block
from app.core.transaction import compute_tx_id

LogFn = Callable[[str], None]
BlockCallback = Callable[[dict[str, Any]], None]
RATE_WINDOW_SECONDS = 30


@dataclass
class Job:
    """一份挖矿作业：交易列表与分配名单都固定下来，谁挖到都不会改变。"""

    job_id: str
    height: int
    prev_hash: str
    timestamp: int
    difficulty: int
    subsidy: int
    transactions: list[dict[str, Any]]
    window: dict[str, int]
    finder_bonus_percent: int
    created_at: float

    @property
    def fees(self) -> int:
        return sum(int(tx["fee"]) for tx in self.transactions)

    def coinbase(self, finder: str) -> dict[str, Any]:
        coinbase = {
            "type": "coinbase",
            "height": self.height,
            "timestamp": self.timestamp,
            "subsidy": self.subsidy,
            "fees": self.fees,
            "finder": finder,
            "payouts": split_reward(
                self.window,
                self.subsidy + self.fees,
                finder=finder,
                bonus_percent=self.finder_bonus_percent,
            ),
        }
        coinbase["tx_id"] = compute_tx_id(coinbase)
        return coinbase

    def transactions_for(self, finder: str) -> list[dict[str, Any]]:
        return [self.coinbase(finder), *self.transactions]

    def merkle_root_for(self, finder: str) -> str:
        return merkle_root([tx["tx_id"] for tx in self.transactions_for(finder)])

    def header(self, miner_tag: int, finder: str, nonce: int) -> dict[str, Any]:
        return {
            "version": 1,
            "prev_hash": self.prev_hash,
            "merkle_root": self.merkle_root_for(finder),
            "timestamp": self.timestamp,
            "difficulty": self.difficulty,
            "miner_tag": miner_tag,
            "nonce": nonce,
        }


@dataclass
class PlayerMining:
    address: str
    share_bits: int
    work: int = 0
    shares: int = 0
    best_share_bits: int = 0
    blocks_found: int = 0
    last_share_at: float = 0.0
    samples: deque[tuple[float, int]] = field(default_factory=deque)
    unflushed_work: int = 0
    unflushed_shares: int = 0
    unflushed_blocks: int = 0

    def hashrate(self, now: float) -> float:
        cutoff = now - RATE_WINDOW_SECONDS
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
        if not self.samples:
            return 0.0
        total = sum(work for _, work in self.samples)
        return total / max(now - self.samples[0][0], 1.0)


@dataclass
class RateGate:
    """令牌桶：每秒放行 `rate` 份提交，允许同样大小的突发。

    洪水量超过上限时只挡掉多出来的部分，客户端始终能按比例拿到放行额度。
    """

    rate: float
    tokens: float
    updated_at: float

    @classmethod
    def opening(cls, rate: float, now: float) -> "RateGate":
        return cls(rate=rate, tokens=rate, updated_at=now)

    def take(self, now: float) -> bool:
        self.tokens = min(self.rate, self.tokens + max(now - self.updated_at, 0.0) * self.rate)
        self.updated_at = now
        if self.tokens < 1.0:
            return False
        self.tokens -= 1.0
        return True


class MiningPool:
    def __init__(
        self,
        chain: Blockchain,
        mempool: Mempool,
        settings: PoolSettings,
        *,
        log: LogFn | None = None,
        on_block: BlockCallback | None = None,
    ):
        self.chain = chain
        self.mempool = mempool
        self.settings = settings
        self.log = log or (lambda _message: None)
        self.on_block = on_block
        self.players: dict[str, PlayerMining] = {}
        self.window: dict[str, int] = {}
        self.stage: dict[str, int] = {}
        self.job: Job | None = None
        self.seen_shares: set[str] = set()
        self.gates: dict[str, RateGate] = {}

    def update_settings(self, settings: PoolSettings) -> None:
        self.settings = settings

    # ---------- 作业 ----------
    def issue_job(self, address: str, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        job = self._ensure_job(int(now))
        player = self._ensure_player(address)
        player.share_bits = self._clamp_share_bits(player.share_bits, job.difficulty)
        return self._job_payload(job, address)

    def tick(self, now: float | None = None) -> dict[str, Any]:
        """作业老化处理：内存池有新交易就重发作业，长时间不出块就临时降一位难度。"""
        now = now if now is not None else time.time()
        job = self._ensure_job(int(now))
        changes: dict[str, Any] = {"refreshed": False, "difficulty_relaxed": False}
        age = now - job.created_at
        if age > self.settings.job_refresh_seconds and self._has_new_transactions(job):
            self._refresh_job(int(now))
            changes["refreshed"] = True
            return changes
        if age > self.chain.settings.stall_seconds and self.chain.settings.difficulty_override is None:
            relaxed = max(job.difficulty - 1, self.chain.settings.difficulty_min)
            if relaxed < job.difficulty:
                self._refresh_job(int(now), difficulty=relaxed)
                self.log(f"作业停滞 {int(age)} 秒，难度临时降到 {relaxed} 位")
                changes.update(refreshed=True, difficulty_relaxed=True)
        return changes

    # ---------- 份额 ----------
    def submit_share(self, address: str, job_id: str, nonce: int, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        if isinstance(nonce, bool) or not isinstance(nonce, int) or not 0 <= nonce <= 0xFFFFFFFF:
            return self._reject("nonce 必须是 0 到 2^32-1 之间的整数")
        job = self.job
        if job is None or job.job_id != job_id:
            return self._reject("作业已经更新，请重新获取")
        if not self._allow_submission(address, now):
            return self._reject("提交过于频繁")

        player = self._ensure_player(address)
        share_bits = self._clamp_share_bits(player.share_bits, job.difficulty)
        key = f"{job_id}:{address}:{nonce}"
        if key in self.seen_shares:
            return self._reject("这个 nonce 已经提交过")

        digest = block_hash(job.header(self.miner_tag(address), address, nonce))
        if not meets_bits(digest, share_bits):
            return self._reject("哈希没有达到份额难度")

        self.seen_shares.add(key)
        work = 1 << share_bits
        player.work += work
        player.unflushed_work += work
        player.shares += 1
        player.unflushed_shares += 1
        player.best_share_bits = max(player.best_share_bits, actual_bits(digest))
        player.samples.append((now, work))
        previous_share_at = player.last_share_at
        player.last_share_at = now
        self.stage[address] = self.stage.get(address, 0) + work
        self._adjust_share_bits(player, now, previous_share_at, job.difficulty)

        result: dict[str, Any] = {
            "accepted": True,
            "reason": None,
            "share_bits": share_bits,
            "next_share_bits": player.share_bits,
            "work": work,
            "hash": digest,
            "hashrate": player.hashrate(now),
            "block": None,
            "balance": self.chain.state.balance_of(address),
        }
        if meets_bits(digest, job.difficulty):
            result.update(self._close_block(address, job, nonce, now))
        return result

    # ---------- 统计 ----------
    def stats(self, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        miners = {
            address: {
                "hashrate": player.hashrate(now),
                "share_bits": player.share_bits,
                "work": player.work,
                "shares": player.shares,
                "best_share_bits": player.best_share_bits,
                "blocks_found": player.blocks_found,
                "last_share_at": player.last_share_at,
            }
            for address, player in self.players.items()
        }
        job = self.job
        return {
            "network_hashrate": sum(miner["hashrate"] for miner in miners.values()),
            "miners": miners,
            "stage": dict(self.stage),
            "payout_window": dict(self.window),
            "height": job.height if job else self.chain.height + 1,
            "job_id": job.job_id if job else None,
            "job_age": (now - job.created_at) if job else 0.0,
            "difficulty": job.difficulty if job else self.chain.difficulty_for(self.chain.height + 1),
        }

    def miner_stats(self, address: str, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        player = self._ensure_player(address)
        return {
            "hashrate": player.hashrate(now),
            "share_bits": player.share_bits,
            "work": player.work,
            "shares": player.shares,
            "best_share_bits": player.best_share_bits,
            "blocks_found": player.blocks_found,
            "last_share_at": player.last_share_at,
            "stage_work": self.stage.get(address, 0),
            "in_payout_window": address in self.window or not self.window,
        }

    def flush_stats(self) -> None:
        for player in self.players.values():
            if not player.unflushed_work and not player.unflushed_blocks:
                continue
            self.chain.store.record_mining_stats(
                player.address,
                work=player.unflushed_work,
                shares=player.unflushed_shares,
                best_share_bits=player.best_share_bits,
                blocks_found=player.unflushed_blocks,
            )
            player.unflushed_work = 0
            player.unflushed_shares = 0
            player.unflushed_blocks = 0

    def reset(self) -> None:
        self.players.clear()
        self.window.clear()
        self.stage.clear()
        self.job = None
        self.seen_shares.clear()
        self.gates.clear()

    def miner_tag(self, address: str) -> int:
        return int(hashlib.sha256(address.encode("ascii")).hexdigest()[:8], 16)

    # ---------- 内部 ----------
    def _ensure_job(self, now: int) -> Job:
        if self.job is None or self._job_is_stale(self.job):
            self._refresh_job(now)
        assert self.job is not None
        return self.job

    def _job_is_stale(self, job: Job) -> bool:
        """教师改过共识参数后旧作业会挖出无效区块，必须立刻换成新作业。"""
        if job.height != self.chain.height + 1 or job.prev_hash != self.chain.tip_hash:
            return True
        settings = self.chain.settings
        return (
            job.subsidy != self.chain.subsidy(job.height)
            or job.finder_bonus_percent != settings.finder_bonus_percent
            or job.difficulty not in self.chain.allowed_difficulties(job.height, job.timestamp)
        )

    def _refresh_job(self, now: int, difficulty: int | None = None) -> Job:
        height = self.chain.height + 1
        limit = max(self.chain.settings.max_block_transactions - 1, 0)
        selected, _ = select_for_block(
            self.chain.state,
            self.mempool.ordered(),
            limit=limit,
            max_bytes=self.chain.settings.block_max_bytes // 2,
            clock=now,
        )
        self.job = Job(
            job_id=secrets.token_hex(8),
            height=height,
            prev_hash=self.chain.tip_hash,
            timestamp=now,
            difficulty=difficulty if difficulty is not None else self.chain.difficulty_for(height),
            subsidy=self.chain.subsidy(height),
            transactions=selected,
            window=dict(self.window),
            finder_bonus_percent=self.chain.settings.finder_bonus_percent,
            created_at=float(now),
        )
        self.seen_shares.clear()
        return self.job

    def _has_new_transactions(self, job: Job) -> bool:
        included = {tx["tx_id"] for tx in job.transactions}
        return any(tx["tx_id"] not in included for tx in self.mempool.transactions.values())

    def _job_payload(self, job: Job, address: str) -> dict[str, Any]:
        player = self._ensure_player(address)
        share_bits = self._clamp_share_bits(player.share_bits, job.difficulty)
        coinbase = job.coinbase(address)
        tx_ids = [coinbase["tx_id"], *(tx["tx_id"] for tx in job.transactions)]
        return {
            "job_id": job.job_id,
            "height": job.height,
            "version": 1,
            "prev_hash": job.prev_hash,
            "merkle_root": merkle_root(tx_ids),
            "tx_ids": tx_ids,
            "timestamp": job.timestamp,
            "difficulty": job.difficulty,
            "miner_tag": self.miner_tag(address),
            "share_bits": share_bits,
            "share_target": target_for_bits(share_bits),
            "block_target": target_for_bits(job.difficulty),
            "subsidy": job.subsidy,
            "fees": job.fees,
            "tx_count": len(job.transactions),
            "payout_count": len(coinbase["payouts"]),
            "issued_at": int(job.created_at),
        }

    def _close_block(self, winner: str, job: Job, nonce: int, now: float) -> dict[str, Any]:
        block = {
            "header": job.header(self.miner_tag(winner), winner, nonce),
            "transactions": job.transactions_for(winner),
        }
        accepted, message = self.chain.append_block(block, now=int(now))
        if not accepted:
            self.log(f"出块被拒绝：{message}")
            return {"block": None, "block_error": message}

        self.mempool.remove([tx["tx_id"] for tx in job.transactions])
        player = self._ensure_player(winner)
        player.blocks_found += 1
        player.unflushed_blocks += 1
        self.window = dict(self.stage)
        self.stage.clear()
        self.log(f"区块 #{job.height} 由 {winner[:12]} 发现，本块奖励 {job.subsidy + job.fees} 聪")

        self.job = None
        next_job = self._ensure_job(int(now))
        if self.on_block is not None:
            self.on_block(block)
        coinbase = block["transactions"][0]
        payout = next(
            (item["amount"] for item in coinbase["payouts"] if item["receiver"] == winner),
            0,
        )
        return {
            "block": {
                "height": job.height,
                "hash": self.chain.tip_hash,
                "difficulty": job.difficulty,
                "finder": winner,
                "payout": payout,
                "subsidy": job.subsidy,
                "fees": job.fees,
                "payouts": coinbase["payouts"],
                "tx_count": len(block["transactions"]),
            },
            "job": self._job_payload(next_job, winner),
            "balance": self.chain.state.balance_of(winner),
        }

    def _ensure_player(self, address: str) -> PlayerMining:
        player = self.players.get(address)
        if player is None:
            player = PlayerMining(address=address, share_bits=self.settings.initial_share_bits)
            self.players[address] = player
        return player

    def _clamp_share_bits(self, bits: int, job_difficulty: int) -> int:
        upper = min(self.settings.share_bits_max, max(job_difficulty, self.settings.share_bits_min))
        return min(max(int(bits), self.settings.share_bits_min), upper)

    def _adjust_share_bits(
        self, player: PlayerMining, now: float, previous_share_at: float, job_difficulty: int
    ) -> None:
        """自动变速：份额来得太快就加一位难度，太慢就减一位，目标是每人两秒交一次。"""
        if previous_share_at <= 0:
            return
        target = self.settings.share_target_seconds
        interval = now - previous_share_at
        if interval < target * 0.6:
            player.share_bits += 1
        elif interval > target * 2.0:
            player.share_bits -= 1
        player.share_bits = self._clamp_share_bits(player.share_bits, job_difficulty)

    def _allow_submission(self, address: str, now: float) -> bool:
        rate = float(max(self.settings.max_shares_per_second, 1))
        gate = self.gates.get(address)
        if gate is None:
            gate = self.gates[address] = RateGate.opening(rate, now)
        gate.rate = rate
        return gate.take(now)

    def _reject(self, reason: str) -> dict[str, Any]:
        return {"accepted": False, "reason": reason, "block": None}


def actual_bits(digest: str) -> int:
    """哈希实际达到的难度位数，用来记录"最高份额"。"""
    value = int(digest, 16)
    return 256 - value.bit_length() if value else 256


def split_reward(
    window: dict[str, int],
    total_sat: int,
    *,
    finder: str,
    bonus_percent: int,
) -> list[dict[str, Any]]:
    """按工作量比例分配奖励：发现者额外拿一份，其余按份额分配，余数按小数部分补足。"""
    shares = {address: int(work) for address, work in window.items() if int(work) > 0}
    shares.setdefault(finder, 1)
    total_work = sum(shares.values())
    bonus = total_sat * max(int(bonus_percent), 0) // 100
    remaining = total_sat - bonus

    amounts: dict[str, int] = {}
    remainders: list[tuple[int, str]] = []
    for address, work in shares.items():
        exact = remaining * work
        amounts[address] = exact // total_work
        remainders.append((exact % total_work, address))

    leftover = remaining - sum(amounts.values())
    remainders.sort(key=lambda item: (-item[0], item[1]))
    for index in range(leftover):
        amounts[remainders[index % len(remainders)][1]] += 1

    amounts[finder] += bonus
    for address in [item for item, amount in amounts.items() if amount <= 0]:
        largest = max(amounts, key=lambda item: (amounts[item], item))
        if amounts[largest] <= 1:
            break
        amounts[largest] -= 1
        amounts[address] += 1

    return [
        {"receiver": address, "amount": amount}
        for address, amount in sorted(amounts.items(), key=lambda item: (-item[1], item[0]))
        if amount > 0
    ]
