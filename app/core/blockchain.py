from __future__ import annotations

import math
import time
from typing import Any, Callable

from app.core.block import (
    compute_block_hash,
    create_block,
    difficulty_to_target,
    effective_target,
    genesis_block,
    hash_meets_difficulty,
    hash_meets_target,
    normalize_target_hex,
    target_prefix_to_hex,
    target_preview,
    target_to_difficulty,
)
from app.core.merkle import merkle_root
from app.core.transaction import (
    create_coinbase,
    validate_coinbase_shape,
    validate_transfer_shape_and_signature,
)
from app.storage.sqlite_store import SQLiteStore

LogFn = Callable[[str], None]


class Blockchain:
    def __init__(self, config: dict[str, Any], store: SQLiteStore, log: LogFn | None = None):
        self.config = config
        self.store = store
        self.log = log or (lambda _message: None)
        self.ensure_genesis()

    @property
    def mining_reward(self) -> float:
        return round(float(self.config["mining_reward"]), 8)

    @property
    def max_block_transactions(self) -> int:
        return int(self.config["max_block_transactions"])

    @property
    def auto_difficulty(self) -> bool:
        return bool(self.config.get("auto_difficulty", False))

    @property
    def difficulty_mode(self) -> str:
        return str(self.config.get("difficulty_mode", "adaptive_target"))

    @property
    def adaptive_target(self) -> bool:
        return self.difficulty_mode == "adaptive_target"

    @property
    def target_block_seconds(self) -> int:
        return max(int(self.config.get("target_block_seconds", 60)), 1)

    @property
    def difficulty_adjustment_interval(self) -> int:
        return max(int(self.config.get("difficulty_adjustment_interval", 10)), 1)

    @property
    def difficulty_adjustment_tolerance(self) -> float:
        return max(float(self.config.get("difficulty_adjustment_tolerance", 0.25)), 0.0)

    @property
    def difficulty_max_step(self) -> int:
        return max(int(self.config.get("difficulty_max_step", 1)), 1)

    @property
    def target_adjustment_base_bps(self) -> int:
        return min(max(int(self.config.get("target_adjustment_base_bps", 1000)), 1), 5000)

    @property
    def target_adjustment_max_multiplier(self) -> int:
        return min(max(int(self.config.get("target_adjustment_max_multiplier", 5)), 1), 10)

    def _clamp_difficulty(self, difficulty: int) -> int:
        minimum = int(self.config.get("min_difficulty", 0))
        maximum = int(self.config.get("max_difficulty", 12))
        return min(max(int(difficulty), minimum), maximum)

    def _base_difficulty(self) -> int:
        return self._clamp_difficulty(int(self.config["difficulty"]))

    def _clamp_target(self, target: str | int) -> str:
        value = int(normalize_target_hex(target), 16)
        hardest = int(difficulty_to_target(int(self.config.get("max_difficulty", 12))), 16)
        easiest = int(difficulty_to_target(int(self.config.get("min_difficulty", 0))), 16)
        return normalize_target_hex(min(max(value, hardest), easiest))

    def _base_target(self) -> str:
        prefix = self.config.get("initial_target_prefix")
        if prefix:
            return self._clamp_target(target_prefix_to_hex(str(prefix)))
        return self._clamp_target(difficulty_to_target(self._base_difficulty()))

    def _retarget_difficulty(
        self,
        previous_difficulty: int,
        first_timestamp: int,
        last_timestamp: int,
    ) -> int:
        expected_span = self.difficulty_adjustment_interval * self.target_block_seconds
        actual_span = max(int(last_timestamp) - int(first_timestamp), 1)
        tolerance = self.difficulty_adjustment_tolerance
        step = self.difficulty_max_step
        previous = self._clamp_difficulty(previous_difficulty)

        if actual_span < expected_span * (1 - tolerance):
            return self._clamp_difficulty(previous + step)
        if actual_span > expected_span * (1 + tolerance):
            return self._clamp_difficulty(previous - step)
        return previous

    def _expected_legacy_difficulty(self, height: int | None = None) -> int:
        target_height = int(height or (self.height() + 1))
        base = self._base_difficulty()
        if not self.auto_difficulty or target_height <= 1:
            return base

        previous = self.store.get_block_by_height(target_height - 1)
        if not previous:
            return base
        previous_difficulty = int(previous["difficulty"])
        interval = self.difficulty_adjustment_interval
        if target_height <= interval or (target_height - 1) % interval != 0:
            return self._clamp_difficulty(previous_difficulty)

        start_height = max(1, target_height - interval)
        start = self.store.get_block_by_height(start_height)
        if not start:
            return self._clamp_difficulty(previous_difficulty)
        return self._retarget_difficulty(
            previous_difficulty,
            int(start["timestamp"]),
            int(previous["timestamp"]),
        )

    def _expected_legacy_difficulty_from_chain(
        self,
        blocks: list[dict[str, Any]],
        height: int,
    ) -> int:
        base = self._base_difficulty()
        if not self.auto_difficulty or height <= 1:
            return base

        previous_difficulty = int(blocks[height - 1]["header"]["difficulty"])
        interval = self.difficulty_adjustment_interval
        if height <= interval or (height - 1) % interval != 0:
            return self._clamp_difficulty(previous_difficulty)

        start_height = max(1, height - interval)
        return self._retarget_difficulty(
            previous_difficulty,
            int(blocks[start_height]["header"]["timestamp"]),
            int(blocks[height - 1]["header"]["timestamp"]),
        )

    def _target_adjustment_context(
        self,
        end_height: int,
        timestamp_at: Callable[[int], int | None],
    ) -> dict[str, int]:
        interval = self.difficulty_adjustment_interval
        expected_span = interval * self.target_block_seconds
        start_height = max(1, end_height - interval + 1)
        first_timestamp = timestamp_at(start_height)
        last_timestamp = timestamp_at(end_height)
        if first_timestamp is None or last_timestamp is None:
            return {
                "direction": 0,
                "streak": 0,
                "actual_span": 0,
                "expected_span": expected_span,
                "adjustment_bps": 0,
            }

        actual_span = max(int(last_timestamp) - int(first_timestamp), 1)
        tolerance = self.difficulty_adjustment_tolerance
        if actual_span < expected_span * (1 - tolerance):
            direction = -1
        elif actual_span > expected_span * (1 + tolerance):
            direction = 1
        else:
            direction = 0

        streak = 0
        previous_end = end_height
        while direction and streak < self.target_adjustment_max_multiplier:
            previous_start = max(1, previous_end - interval + 1)
            previous_first = timestamp_at(previous_start)
            previous_last = timestamp_at(previous_end)
            if previous_first is None or previous_last is None:
                break
            previous_span = max(int(previous_last) - int(previous_first), 1)
            if previous_span < expected_span * (1 - tolerance):
                previous_direction = -1
            elif previous_span > expected_span * (1 + tolerance):
                previous_direction = 1
            else:
                previous_direction = 0
            if previous_direction != direction:
                break
            streak += 1
            previous_end -= interval
            if previous_end < interval:
                break

        return {
            "direction": direction,
            "streak": streak,
            "actual_span": actual_span,
            "expected_span": expected_span,
            "adjustment_bps": min(
                self.target_adjustment_base_bps * streak,
                9000,
            ),
        }

    def _retarget_target(self, previous_target: str, direction: int, streak: int) -> str:
        if not direction or not streak:
            return self._clamp_target(previous_target)
        previous = int(normalize_target_hex(previous_target), 16)
        adjustment_bps = min(self.target_adjustment_base_bps * streak, 9000)
        scale = 10000 + (direction * adjustment_bps)
        adjusted = (previous * scale) // 10000
        if adjusted == previous:
            adjusted += direction
        return self._clamp_target(adjusted)

    def expected_target(self, height: int | None = None) -> str:
        target_height = int(height or (self.height() + 1))
        base = self._base_target()
        if not self.adaptive_target:
            return difficulty_to_target(self._expected_legacy_difficulty(target_height))
        if not self.auto_difficulty or target_height <= 1:
            return base

        previous = self.store.get_block_by_height(target_height - 1)
        if not previous:
            return base
        previous_target = (
            normalize_target_hex(previous["target"])
            if previous.get("target")
            else difficulty_to_target(int(previous["difficulty"]))
        )
        interval = self.difficulty_adjustment_interval
        if target_height <= interval or (target_height - 1) % interval != 0:
            return self._clamp_target(previous_target)

        context = self._target_adjustment_context(
            target_height - 1,
            lambda block_height: (
                int(block["timestamp"])
                if (block := self.store.get_block_by_height(block_height))
                else None
            ),
        )
        return self._retarget_target(
            previous_target,
            context["direction"],
            context["streak"],
        )

    def _expected_target_from_chain(self, blocks: list[dict[str, Any]], height: int) -> str:
        base = self._base_target()
        if not self.adaptive_target:
            return difficulty_to_target(
                self._expected_legacy_difficulty_from_chain(blocks, height)
            )
        if not self.auto_difficulty or height <= 1:
            return base

        previous_target = effective_target(blocks[height - 1]["header"])
        interval = self.difficulty_adjustment_interval
        if height <= interval or (height - 1) % interval != 0:
            return self._clamp_target(previous_target)

        context = self._target_adjustment_context(
            height - 1,
            lambda block_height: (
                int(blocks[block_height]["header"]["timestamp"])
                if 0 <= block_height < len(blocks)
                else None
            ),
        )
        return self._retarget_target(
            previous_target,
            context["direction"],
            context["streak"],
        )

    def expected_difficulty(self, height: int | None = None) -> int:
        if self.adaptive_target:
            return target_to_difficulty(self.expected_target(height))
        return self._expected_legacy_difficulty(height)

    def next_difficulty_adjustment_height(self) -> int | None:
        if not self.auto_difficulty:
            return None
        height = self.height()
        interval = self.difficulty_adjustment_interval
        if height < interval:
            return interval + 1
        if height % interval == 0:
            return height + 1
        return ((height // interval) + 1) * interval + 1

    def difficulty_policy(self) -> dict[str, Any]:
        current_target = self.expected_target()
        height = self.height()
        last_adjustment_end = (height // self.difficulty_adjustment_interval) * self.difficulty_adjustment_interval
        context = self._target_adjustment_context(
            last_adjustment_end,
            lambda block_height: (
                int(block["timestamp"])
                if (block := self.store.get_block_by_height(block_height))
                else None
            ),
        ) if last_adjustment_end >= self.difficulty_adjustment_interval else {
            "direction": 0,
            "streak": 0,
            "actual_span": 0,
            "expected_span": self.difficulty_adjustment_interval * self.target_block_seconds,
            "adjustment_bps": 0,
        }
        return {
            "auto": self.auto_difficulty,
            "mode": self.difficulty_mode,
            "base_difficulty": self._base_difficulty(),
            "base_target": self._base_target(),
            "base_target_preview": target_preview(self._base_target()),
            "current_target": current_target,
            "current_target_preview": target_preview(current_target),
            "target_block_seconds": self.target_block_seconds,
            "adjustment_interval_blocks": self.difficulty_adjustment_interval,
            "adjustment_base_bps": self.target_adjustment_base_bps,
            "adjustment_max_multiplier": self.target_adjustment_max_multiplier,
            "momentum_direction": context["direction"],
            "momentum_streak": context["streak"],
            "momentum_adjustment_bps": context["adjustment_bps"],
            "last_actual_span": context["actual_span"],
            "expected_span": context["expected_span"],
            "next_adjustment_height": self.next_difficulty_adjustment_height(),
        }

    def ensure_genesis(self) -> None:
        if self.store.get_tip() is not None:
            return
        block = genesis_block()
        block_hash = compute_block_hash(block)
        self.store.insert_block(0, block_hash, block)
        self.log(f"Genesis block created: {block_hash[:16]}")

    def reset_to_genesis(self) -> str:
        """Reset chain data and mempool while keeping wallet keys and peers."""
        block = genesis_block()
        block_hash = compute_block_hash(block)
        self.store.replace_chain([block], [block_hash])
        self.log(f"Blockchain reset to genesis: {block_hash[:16]}")
        return block_hash

    def tip(self) -> dict[str, Any]:
        tip = self.store.get_tip()
        if tip is None:
            self.ensure_genesis()
            tip = self.store.get_tip()
        if tip is None:
            raise RuntimeError("blockchain tip is unavailable")
        return tip

    def height(self) -> int:
        return int(self.tip()["height"])

    def tip_hash(self) -> str:
        return str(self.tip()["hash"])

    def get_balance(self, address: str) -> float:
        return self.store.confirmed_balance(address)

    def get_available_balance(self, address: str, exclude_tx_id: str | None = None) -> float:
        pending = self.store.pending_outgoing(address, exclude_tx_id=exclude_tx_id)
        return round(self.get_balance(address) - pending, 8)

    def validate_transfer_transaction(
        self,
        tx: dict[str, Any],
        *,
        include_mempool: bool = True,
        allow_existing_mempool: bool = False,
    ) -> None:
        validate_transfer_shape_and_signature(tx)
        tx_id = tx["tx_id"]
        if self.store.is_tx_confirmed(tx_id):
            raise ValueError("transaction already confirmed")
        if self.store.is_tx_in_mempool(tx_id) and not allow_existing_mempool:
            raise ValueError("transaction already in mempool")

        required = round(float(tx["amount"]) + float(tx["fee"]), 8)
        if include_mempool:
            available = self.get_available_balance(tx["sender"], exclude_tx_id=tx_id)
        else:
            available = self.get_balance(tx["sender"])
        if available + 1e-8 < required:
            raise ValueError(f"insufficient available balance: need {required}, have {available}")

        now = int(time.time())
        if abs(now - int(tx["timestamp"])) > 7 * 24 * 60 * 60:
            raise ValueError("transaction timestamp is outside MVP tolerance")

    def _validate_block_header(self, block: dict[str, Any]) -> str:
        if "header" not in block or "transactions" not in block:
            raise ValueError("block must contain header and transactions")
        header = block["header"]
        for field in ("version", "prev_hash", "merkle_root", "timestamp", "difficulty", "nonce"):
            if field not in header:
                raise ValueError(f"block header missing {field}")

        block_hash = compute_block_hash(block)
        next_height = self.height() + 1
        if self.adaptive_target and header.get("target") is None:
            raise ValueError("adaptive target block header missing target")
        if not self.adaptive_target and header.get("target") is not None:
            raise ValueError("legacy difficulty block must not contain target")
        if header.get("target") is not None:
            expected_target = self.expected_target(next_height)
            actual_target = normalize_target_hex(header["target"])
            if actual_target != expected_target:
                raise ValueError(
                    f"target mismatch at height {next_height}: "
                    f"expected {target_preview(expected_target)}, "
                    f"got {target_preview(actual_target)}"
                )
            expected_difficulty = target_to_difficulty(expected_target)
            if int(header["difficulty"]) != expected_difficulty:
                raise ValueError(
                    f"difficulty display mismatch at height {next_height}: "
                    f"expected {expected_difficulty}, got {header['difficulty']}"
                )
            if not hash_meets_target(block_hash, actual_target):
                raise ValueError("block hash does not meet target")
        else:
            expected_difficulty = self._expected_legacy_difficulty(next_height)
            if int(header["difficulty"]) != expected_difficulty:
                raise ValueError(
                    f"difficulty mismatch at height {next_height}: "
                    f"expected {expected_difficulty}, got {header['difficulty']}"
                )
            if not hash_meets_difficulty(block_hash, int(header["difficulty"])):
                raise ValueError("block hash does not meet difficulty")
        if self.store.has_block_hash(block_hash):
            raise ValueError("block already exists")
        if header["prev_hash"] != self.tip_hash():
            raise ValueError("block does not connect to current tip")

        now = int(time.time())
        if int(header["timestamp"]) > now + 2 * 60 * 60:
            raise ValueError("block timestamp is too far in the future")
        return block_hash

    def validate_block(self, block: dict[str, Any]) -> str:
        block_hash = self._validate_block_header(block)
        transactions = block.get("transactions", [])
        if not isinstance(transactions, list):
            raise ValueError("block transactions must be a list")
        if not transactions:
            raise ValueError("non-genesis block must contain a coinbase transaction")
        if len(transactions) > self.max_block_transactions:
            raise ValueError("block contains too many transactions")

        from app.core.merkle import merkle_root

        computed_merkle = merkle_root([tx["tx_id"] for tx in transactions])
        if block["header"]["merkle_root"] != computed_merkle:
            raise ValueError("merkle_root mismatch")

        coinbases = [tx for tx in transactions if tx.get("type") == "coinbase"]
        if len(coinbases) != 1:
            raise ValueError("block must contain exactly one coinbase transaction")
        if transactions[0].get("type") != "coinbase":
            raise ValueError("coinbase transaction must be first")

        coinbase = transactions[0]
        validate_coinbase_shape(coinbase)

        seen_tx_ids: set[str] = set()
        temp_balances: dict[str, float] = {}
        total_fees = 0.0
        for tx in transactions:
            tx_id = tx["tx_id"]
            if tx_id in seen_tx_ids:
                raise ValueError("duplicate transaction inside block")
            seen_tx_ids.add(tx_id)
            if tx.get("type") == "coinbase":
                continue

            validate_transfer_shape_and_signature(tx)
            if self.store.is_tx_confirmed(tx_id):
                raise ValueError(f"transaction already confirmed: {tx_id}")

            sender = tx["sender"]
            receiver = tx["receiver"]
            if sender not in temp_balances:
                temp_balances[sender] = self.get_balance(sender)
            if receiver not in temp_balances:
                temp_balances[receiver] = self.get_balance(receiver)

            required = round(float(tx["amount"]) + float(tx["fee"]), 8)
            if temp_balances[sender] + 1e-8 < required:
                raise ValueError(f"block spends more than available balance for {sender[:16]}")
            temp_balances[sender] = round(temp_balances[sender] - required, 8)
            temp_balances[receiver] = round(temp_balances[receiver] + float(tx["amount"]), 8)
            total_fees = round(total_fees + float(tx["fee"]), 8)

        expected_coinbase = round(self.mining_reward + total_fees, 8)
        if not math.isclose(float(coinbase["amount"]), expected_coinbase, abs_tol=1e-8):
            raise ValueError(
                f"coinbase amount must equal reward plus fees: expected {expected_coinbase}"
            )
        return block_hash

    def validate_chain_replacement(self, blocks: list[dict[str, Any]]) -> list[str]:
        if not blocks:
            raise ValueError("replacement chain is empty")

        expected_genesis = genesis_block()
        expected_genesis_hash = compute_block_hash(expected_genesis)
        if blocks[0] != expected_genesis:
            raise ValueError("replacement chain has unexpected genesis block")

        block_hashes = [expected_genesis_hash]
        balances: dict[str, float] = {}
        seen_tx_ids: set[str] = set()
        target_mode_started = False

        for height, block in enumerate(blocks[1:], start=1):
            if "header" not in block or "transactions" not in block:
                raise ValueError(f"block {height} must contain header and transactions")
            header = block["header"]
            for field in ("version", "prev_hash", "merkle_root", "timestamp", "difficulty", "nonce"):
                if field not in header:
                    raise ValueError(f"block {height} header missing {field}")

            if header["prev_hash"] != block_hashes[-1]:
                raise ValueError(f"block {height} does not connect to replacement chain")

            block_hash = compute_block_hash(block)
            if header.get("target") is not None:
                target_mode_started = True
                expected_target = self._expected_target_from_chain(blocks, height)
                actual_target = normalize_target_hex(header["target"])
                if actual_target != expected_target:
                    raise ValueError(
                        f"block {height} target mismatch: "
                        f"expected {target_preview(expected_target)}, "
                        f"got {target_preview(actual_target)}"
                    )
                expected_difficulty = target_to_difficulty(expected_target)
                if int(header["difficulty"]) != expected_difficulty:
                    raise ValueError(
                        f"block {height} difficulty display mismatch: "
                        f"expected {expected_difficulty}, got {header['difficulty']}"
                    )
                if not hash_meets_target(block_hash, actual_target):
                    raise ValueError(f"block {height} hash does not meet its header target")
            else:
                if target_mode_started:
                    raise ValueError(f"block {height} is missing target after target mode started")
                expected_difficulty = self._expected_legacy_difficulty_from_chain(blocks, height)
                if int(header["difficulty"]) != expected_difficulty:
                    raise ValueError(
                        f"block {height} difficulty mismatch: "
                        f"expected {expected_difficulty}, got {header['difficulty']}"
                    )
                if not hash_meets_difficulty(block_hash, int(header["difficulty"])):
                    raise ValueError(f"block {height} hash does not meet its header difficulty")

            transactions = block.get("transactions", [])
            if not isinstance(transactions, list):
                raise ValueError(f"block {height} transactions must be a list")
            if not transactions:
                raise ValueError(f"block {height} must contain a coinbase transaction")
            if len(transactions) > self.max_block_transactions:
                raise ValueError(f"block {height} contains too many transactions")

            if header["merkle_root"] != merkle_root([tx["tx_id"] for tx in transactions]):
                raise ValueError(f"block {height} merkle_root mismatch")

            coinbases = [tx for tx in transactions if tx.get("type") == "coinbase"]
            if len(coinbases) != 1 or transactions[0].get("type") != "coinbase":
                raise ValueError(f"block {height} must have exactly one first-position coinbase")
            coinbase = transactions[0]
            validate_coinbase_shape(coinbase)

            total_fees = 0.0
            balance_delta: dict[str, float] = {}
            for tx in transactions[1:]:
                validate_transfer_shape_and_signature(tx)
                tx_id = tx["tx_id"]
                if tx_id in seen_tx_ids:
                    raise ValueError(f"duplicate transaction in replacement chain: {tx_id}")
                seen_tx_ids.add(tx_id)

                sender = tx["sender"]
                receiver = tx["receiver"]
                required = round(float(tx["amount"]) + float(tx["fee"]), 8)
                available = round(balances.get(sender, 0.0) + balance_delta.get(sender, 0.0), 8)
                if available + 1e-8 < required:
                    raise ValueError(f"block {height} spends more than available balance")
                balance_delta[sender] = round(balance_delta.get(sender, 0.0) - required, 8)
                balance_delta[receiver] = round(balance_delta.get(receiver, 0.0) + float(tx["amount"]), 8)
                total_fees = round(total_fees + float(tx["fee"]), 8)

            expected_coinbase = round(self.mining_reward + total_fees, 8)
            if not math.isclose(float(coinbase["amount"]), expected_coinbase, abs_tol=1e-8):
                raise ValueError(f"block {height} coinbase amount is invalid")
            seen_tx_ids.add(coinbase["tx_id"])
            balance_delta[coinbase["receiver"]] = round(
                balance_delta.get(coinbase["receiver"], 0.0) + float(coinbase["amount"]),
                8,
            )
            for address, delta in balance_delta.items():
                balances[address] = round(balances.get(address, 0.0) + delta, 8)

            block_hashes.append(block_hash)

        return block_hashes

    def replace_with_chain(self, blocks: list[dict[str, Any]], source: str = "sync") -> tuple[bool, str]:
        remote_height = len(blocks) - 1
        if remote_height <= self.height():
            return False, "replacement chain is not longer"
        try:
            block_hashes = self.validate_chain_replacement(blocks)
        except ValueError as exc:
            return False, str(exc)

        self.store.replace_chain(blocks, block_hashes)
        self.log(
            f"Chain replaced from {source}: height {remote_height}, "
            f"tip {block_hashes[-1][:16]}"
        )
        return True, block_hashes[-1]

    def add_block(self, block: dict[str, Any], source: str = "local") -> tuple[bool, str]:
        block_hash = compute_block_hash(block)
        if self.store.has_block_hash(block_hash):
            return False, "block already exists"
        try:
            validated_hash = self.validate_block(block)
        except ValueError as exc:
            return False, str(exc)

        height = self.height() + 1
        self.store.insert_block(height, validated_hash, block)
        confirmed_ids = [tx["tx_id"] for tx in block.get("transactions", []) if tx.get("type") != "coinbase"]
        self.store.remove_mempool_transactions(confirmed_ids)
        self.log(f"Block accepted from {source}: height {height}, hash {validated_hash[:16]}")
        return True, validated_hash

    def select_transactions_for_block(self, max_transfers: int) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        temp_balances: dict[str, float] = {}

        for tx in self.store.list_mempool_transactions():
            if len(selected) >= max_transfers:
                break
            try:
                validate_transfer_shape_and_signature(tx)
                if self.store.is_tx_confirmed(tx["tx_id"]):
                    continue
                sender = tx["sender"]
                receiver = tx["receiver"]
                if sender not in temp_balances:
                    temp_balances[sender] = self.get_balance(sender)
                if receiver not in temp_balances:
                    temp_balances[receiver] = self.get_balance(receiver)
                required = round(float(tx["amount"]) + float(tx["fee"]), 8)
                if temp_balances[sender] + 1e-8 < required:
                    continue
                temp_balances[sender] = round(temp_balances[sender] - required, 8)
                temp_balances[receiver] = round(temp_balances[receiver] + float(tx["amount"]), 8)
                selected.append(tx)
            except ValueError:
                continue
        return selected

    def create_candidate_block(self, miner_address: str, transfers: list[dict[str, Any]]) -> dict[str, Any]:
        fees = round(sum(float(tx["fee"]) for tx in transfers), 8)
        coinbase = create_coinbase(
            miner_address,
            round(self.mining_reward + fees, 8),
            height=self.height() + 1,
        )
        next_height = self.height() + 1
        if self.adaptive_target:
            target = self.expected_target(next_height)
            return create_block(
                prev_hash=self.tip_hash(),
                transactions=[coinbase, *transfers],
                difficulty=target_to_difficulty(target),
                target=target,
            )
        return create_block(
            prev_hash=self.tip_hash(),
            transactions=[coinbase, *transfers],
            difficulty=self.expected_difficulty(next_height),
        )
