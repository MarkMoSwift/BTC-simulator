"""业务实现。"""
from __future__ import annotations

import time
from collections.abc import Iterable
from copy import copy
from dataclasses import replace
from typing import Any, Callable

from app.config import ChainSettings
from app.core import consensus
from app.core.block import (
    EMPTY_MERKLE_ROOT,
    GENESIS_PREV_HASH,
    HEADER_FIELDS,
    BlockError,
    block_hash,
    genesis_block,
    meets_bits,
    merkle_root,
    target_for_bits,
)
from app.core.state import ChainState, RuleError, TransactionIndex
from app.core.transaction import TxError, validate_coinbase
from app.units import SAT_PER_COIN
from app.utils.canonical import canonical_bytes

LogFn = Callable[[str], None]


class FutureBlock(BlockError):
    def __init__(self, retry_at: int):
        super().__init__("区块时间过于超前")
        self.retry_at = retry_at


class Blockchain:
    def __init__(
        self, settings: ChainSettings, store: Any, log: LogFn | None = None,
        *, session: consensus.Session | None = None,
    ):
        self.settings = settings if session is None else replace(
            ChainSettings(),
            initial_difficulty=consensus.INITIAL_DIFFICULTY,
            difficulty_min=consensus.MIN_DIFFICULTY,
            difficulty_max=consensus.MAX_DIFFICULTY,
            retarget_interval_blocks=consensus.RETARGET_INTERVAL,
            target_block_seconds=consensus.TARGET_SECONDS,
            stall_seconds=consensus.STALL_SECONDS,
            min_fee_sat=0,
        )
        self.session = session
        self.store = store
        self.log = log or (lambda _message: None)
        self._clear_state()
        self._ensure_genesis()
        self._load()

    def _clear_state(self) -> None:
        self.headers: list[dict[str, Any]] = []
        self.hashes: list[str] = []
        self.timestamps: list[int] = []
        self.normal_bits: list[int] = []
        self.cumulative_work = 0
        self.state = ChainState(self.settings, self.session.genesis_hash if self.session else None)

    @property
    def height(self) -> int:
        return len(self.hashes) - 1

    @property
    def tip_header(self) -> dict[str, Any]:
        return self.headers[-1]

    @property
    def tip_hash(self) -> str:
        return self.hashes[-1]

    def update_settings(self, settings: ChainSettings) -> None:
        if self.session is not None:
            raise RuleError("课堂场次的共识参数不能在运行时修改")
        self.settings = settings
        self.state.settings = settings

    def subsidy(self, height: int) -> int:
        if self.session is not None:
            return 50 * SAT_PER_COIN
        return self.settings.subsidy_sat(height)

    def difficulty_for(self, height: int) -> int:
        if self.session is not None:
            return consensus.normal_difficulty(self.timestamps, height, self.normal_bits[height - 1])
        settings = self.settings
        if settings.difficulty_override is not None:
            return self._clamp(settings.difficulty_override)
        if height <= 1:
            return self._clamp(settings.initial_difficulty)

        interval = settings.retarget_interval_blocks
        previous = int(self.headers[height - 1]["difficulty"])
        if (height - 1) % interval != 0:
            return self._clamp(previous)

        start = self.headers[max(height - 1 - interval, 0)]
        span = int(self.headers[height - 1]["timestamp"]) - int(start["timestamp"])
        expected = interval * settings.target_block_seconds
        tolerance = settings.retarget_tolerance_percent / 100
        if span < expected * (1 - tolerance):
            return self._clamp(previous + 1)
        if span > expected * (1 + tolerance):
            return self._clamp(previous - 1)
        return self._clamp(previous)

    def allowed_difficulties(self, height: int, timestamp: int) -> set[int]:
        """allowed_difficulties。"""
        computed = self.difficulty_for(height)
        if self.session is not None:
            return consensus.allowed_difficulties(computed, self.timestamps[height - 1], timestamp)
        if self.settings.difficulty_override is not None:
            return {computed}
        previous_timestamp = int(self.headers[height - 1]["timestamp"])
        if timestamp - previous_timestamp > self.settings.stall_seconds:
            return {computed, self._clamp(computed - 1)}
        return {computed}

    def target_for(self, height: int) -> str:
        return target_for_bits(self.difficulty_for(height))

    def next_retarget_height(self) -> int:
        """next_retarget_height。"""
        if self.session is not None:
            interval = consensus.RETARGET_INTERVAL
            return max(interval + 2, self.height + 1 + (1 - self.height) % interval)
        interval = self.settings.retarget_interval_blocks
        return self.height + 1 + (interval - self.height % interval) % interval

    def validate_block(self, block: dict[str, Any], now: int | None = None) -> str:
        if not isinstance(block, dict) or set(block) != {"header", "transactions"}:
            raise BlockError("区块必须只包含 header 与 transactions")
        header = block["header"]
        if not isinstance(header, dict) or set(header) != set(HEADER_FIELDS):
            raise BlockError("区块头字段必须是 " + "、".join(HEADER_FIELDS))
        transactions = block["transactions"]
        if not isinstance(transactions, list) or not transactions:
            raise BlockError("区块必须包含 coinbase")
        if len(transactions) > self.settings.max_block_transactions:
            raise BlockError(f"一个区块最多 {self.settings.max_block_transactions} 笔交易")
        if len(canonical_bytes(block)) > self.settings.block_max_bytes:
            raise BlockError("区块体积超过上限")

        height = self.height + 1
        if self.session is not None:
            numeric_fields = ("version", "timestamp", "difficulty", "miner_tag", "nonce")
            if any(type(header[key]) is not int or not 0 <= header[key] <= 0xFFFFFFFF for key in numeric_fields):
                raise BlockError("区块头整数必须为无符号 32 位整数")
            if header["version"] != consensus.PROTOCOL_MAJOR:
                raise BlockError("区块协议大版本不兼容")
            if any(not isinstance(tx, dict) or not isinstance(tx.get("tx_id"), str) for tx in transactions):
                raise BlockError("区块包含格式无效的交易")
        if header["prev_hash"] != self.tip_hash:
            raise BlockError("区块没有接在当前链尖之后")
        timestamp = int(header["timestamp"])
        if timestamp < int(self.tip_header["timestamp"]):
            raise BlockError("区块时间早于前一个区块")
        future_seconds = consensus.FUTURE_SECONDS if self.session is not None else 7200
        if now is not None and timestamp > now + future_seconds:
            raise FutureBlock(timestamp - future_seconds)
        difficulty = int(header["difficulty"])
        allowed = self.allowed_difficulties(height, timestamp)
        if difficulty not in allowed:
            raise BlockError(f"难度应为 {' 或 '.join(str(item) for item in sorted(allowed))}，实际为 {difficulty}")

        block_hash_hex = block_hash(header)
        if not meets_bits(block_hash_hex, difficulty):
            raise BlockError("区块哈希没有达到目标难度（工作量不足）")
        try:
            root = merkle_root([tx["tx_id"] for tx in transactions])
        except (KeyError, TypeError, ValueError) as exc:
            raise BlockError("交易号格式无效") from exc
        if root != header["merkle_root"]:
            raise BlockError("梅克尔根与交易列表不一致")
        if transactions[0].get("type") != "coinbase":
            raise BlockError("第一笔交易必须是 coinbase")
        if sum(1 for tx in transactions if tx.get("type") == "coinbase") != 1:
            raise BlockError("区块只能有一笔 coinbase")

        coinbase = transactions[0]
        try:
            validate_coinbase(coinbase)
        except TxError as exc:
            raise BlockError(f"coinbase 不合法：{exc}") from exc
        if self.session is not None and (
            len(coinbase["payouts"]) != 1 or coinbase["payouts"][0]["receiver"] != coinbase["finder"]
        ):
            raise BlockError("区块奖励必须全部支付给出块钱包")
        if int(coinbase["height"]) != height:
            raise BlockError("coinbase 高度与区块高度不一致")
        try:
            fees = sum(int(tx["fee"]) for tx in transactions[1:])
        except (KeyError, TypeError, ValueError) as exc:
            raise BlockError("普通交易缺少手续费字段") from exc
        expected_total = self.subsidy(height) + fees
        paid = sum(int(payout["amount"]) for payout in coinbase["payouts"])
        if paid != expected_total:
            raise BlockError(f"coinbase 应分配 {expected_total} 聪，实际 {paid} 聪")
        if int(coinbase["subsidy"]) != self.subsidy(height) or int(coinbase["fees"]) != fees:
            raise BlockError("coinbase 的 subsidy 或 fees 字段与区块不一致")
        return block_hash_hex

    def append_block(self, block: dict[str, Any], now: int | None = None) -> tuple[bool, str]:
        height = self.height + 1
        try:
            block_hash_hex = self.validate_block(block, now=now)
            candidate = self.state.clone()
            index = candidate.apply_block(block, height)
        except (BlockError, RuleError, TxError) as exc:
            return False, str(exc)

        self.store.insert_block(height, block_hash_hex, block, index)
        self._record_header(block["header"], block_hash_hex)
        self.state = candidate
        self.log(f"新区块 #{height} {block_hash_hex[:16]} 难度 {block['header']['difficulty']}")
        return True, block_hash_hex

    def adopt_branch(
        self, ancestor: str, blocks: list[dict[str, Any]], *, now: int,
    ) -> bool:
        """按已验证的累计工作量切换主链。调用方持有节点写锁。"""
        if self.session is None or ancestor not in self.hashes or not blocks:
            return False
        fork_height = self.hashes.index(ancestor)
        old_blocks = self.store.list_blocks_ascending()
        candidate = copy(self)
        candidate._load(old_blocks[:fork_height + 1])
        records: list[tuple[int, str, dict[str, Any], dict[str, TransactionIndex]]] = []
        for block in blocks:
            height = candidate.height + 1
            block_id = candidate.validate_block(block, now=now)
            index = candidate.state.apply_block(block, height)
            candidate._record_header(block["header"], block_id)
            records.append((height, block_id, block, index))
        if candidate.cumulative_work <= self.cumulative_work:
            return False

        pending = self.store.list_mempool()
        pending.extend(
            (tx, now) for block in old_blocks[fork_height + 1:] for tx in block["transactions"][1:]
        )
        projected = candidate.state.clone()
        projected.settle_due(projected.height + 1, now)
        valid = []
        for tx, received in pending:
            trial = projected.clone()
            try:
                trial.apply_tx(tx, now)
            except (RuleError, TxError):
                continue
            projected = trial
            valid.append((tx, received))

        self.store.replace_branch(fork_height, records, valid)
        self.headers = candidate.headers
        self.hashes = candidate.hashes
        self.timestamps = candidate.timestamps
        self.normal_bits = candidate.normal_bits
        self.cumulative_work = candidate.cumulative_work
        self.state = candidate.state
        self.log(f"已采纳主链 #{self.height} {self.tip_hash[:16]}")
        return True

    def reset_to_genesis(self) -> str:
        self.store.reset_chain()
        self._ensure_genesis()
        self._load()
        self.log("链已重置为创世块")
        return self.tip_hash

    def snapshot(self) -> dict[str, Any]:
        height = self.height
        difficulty = self.difficulty_for(height + 1)
        return {
            "height": height,
            "tip_hash": self.tip_hash,
            "difficulty": difficulty,
            "target": target_for_bits(difficulty),
            "expected_hashes": 1 << difficulty,
            "subsidy": self.subsidy(height + 1),
            "next_retarget_height": self.next_retarget_height(),
            "retarget_interval_blocks": self.settings.retarget_interval_blocks,
            "target_block_seconds": self.settings.target_block_seconds,
            "halving_interval_blocks": None if self.session else self.settings.halving_interval_blocks,
            "next_halving_height": None if self.session else self.settings.next_halving_height(height),
            "min_fee_sat": self.settings.min_fee_sat,
            "max_fee_sat": self.settings.max_fee_sat,
            "max_nfts_per_player": self.settings.max_nfts_per_player,
            "packet_expiry_seconds": self.settings.packet_expiry_seconds,
            "difficulty_override": self.settings.difficulty_override,
            "tip_timestamp": int(self.tip_header["timestamp"]),
        }

    def _ensure_genesis(self) -> None:
        if self.store.tip_height() is not None:
            return
        if self.session is not None:
            block = self.session.genesis()
        else:
            block = genesis_block()
            block["header"]["timestamp"] = int(time.time())
        self.store.insert_block(0, block_hash(block["header"]), block, {})
        self.log("创世块已创建")

    def _load(self, blocks: Iterable[dict[str, Any]] | None = None) -> None:
        self._clear_state()
        if blocks is None:
            blocks = self.store.list_blocks_ascending()
        for height, block in enumerate(blocks):
            header = block["header"]
            block_hash_hex = block_hash(header)
            if height == 0:
                if self.session is not None:
                    if block != self.session.genesis():
                        raise BlockError("数据库的创世块与当前场次不匹配")
                elif header["prev_hash"] != GENESIS_PREV_HASH or header["merkle_root"] != EMPTY_MERKLE_ROOT:
                    raise BlockError("数据库中的创世块已被破坏")
            elif self.session is not None:
                self.validate_block(block)
                self.state.apply_block(block, height)
            else:
                if header["prev_hash"] != self.hashes[-1]:
                    raise BlockError(f"数据库中的第 {height} 个区块与前一区块断链")
                if not meets_bits(block_hash_hex, int(header["difficulty"])):
                    raise BlockError(f"数据库中的第 {height} 个区块工作量不足")
                if merkle_root([tx["tx_id"] for tx in block["transactions"]]) != header["merkle_root"]:
                    raise BlockError(f"数据库中的第 {height} 个区块梅克尔根不匹配")
                self.state.apply_block(block, height, prevalidated=True)
            self._record_header(header, block_hash_hex)

    def _record_header(self, header: dict[str, Any], block_hash_hex: str) -> None:
        if self.session is not None:
            normal = self.difficulty_for(len(self.headers)) if self.headers else 0
            self.normal_bits.append(normal)
            self.timestamps.append(header["timestamp"])
            if self.headers:
                self.cumulative_work += 1 << header["difficulty"]
        self.headers.append(header)
        self.hashes.append(block_hash_hex)

    def _clamp(self, difficulty: int) -> int:
        return min(max(int(difficulty), self.settings.difficulty_min), self.settings.difficulty_max)
