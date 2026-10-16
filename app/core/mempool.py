"""内存池：等待被打包的交易，以及打包前的试算工具。"""
from __future__ import annotations

import time
from typing import Any, Callable

from app.core.state import ChainState, RuleError
from app.core.transaction import TxError, validate_signed_tx
from app.utils.canonical import canonical_bytes

LogFn = Callable[[str], None]
TX_FRESHNESS_SECONDS = 3600
TX_FUTURE_SECONDS = 300


def project_state(base: ChainState, transactions: list[dict[str, Any]], clock: int) -> ChainState:
    projected = base.clone()
    for tx in transactions:
        projected.apply_tx(tx, clock)
    return projected


def select_for_block(
    base: ChainState,
    candidates: list[dict[str, Any]],
    *,
    limit: int,
    max_bytes: int,
    clock: int,
) -> tuple[list[dict[str, Any]], ChainState]:
    """按手续费高者优先试算，只有真的能执行成功的交易才会被打包。"""
    projected = base.clone()
    selected: list[dict[str, Any]] = []
    used_bytes = 0
    for tx in candidates:
        if len(selected) >= limit:
            break
        size = len(canonical_bytes(tx))
        if used_bytes + size > max_bytes:
            continue
        try:
            projected.apply_tx(tx, clock)
        except (RuleError, TxError, ValueError):
            continue
        selected.append(tx)
        used_bytes += size
    return selected, projected


class Mempool:
    def __init__(self, store: Any, state_provider: Callable[[], ChainState], log: LogFn | None = None):
        self.store = store
        self.state_provider = state_provider
        self.log = log or (lambda _message: None)
        self.transactions: dict[str, dict[str, Any]] = {}
        self.received_at: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        for tx, received_at in self.store.list_mempool():
            self.transactions[tx["tx_id"]] = tx
            self.received_at[tx["tx_id"]] = int(received_at)
        if self.transactions:
            self.log(f"从数据库恢复 {len(self.transactions)} 笔未确认交易")

    def add(self, tx: dict[str, Any], now: int | None = None) -> str:
        now = int(now if now is not None else time.time())
        validate_signed_tx(tx)
        tx_id = tx["tx_id"]
        if tx_id in self.transactions:
            raise TxError("这笔交易已经在等待打包")
        if self.store.get_transaction(tx_id) is not None:
            raise TxError("这笔交易已经上链")
        timestamp = int(tx["timestamp"])
        if timestamp < now - TX_FRESHNESS_SECONDS:
            raise TxError("交易时间过旧，请重新发起")
        if timestamp > now + TX_FUTURE_SECONDS:
            raise TxError("交易时间超前，请检查设备时间")

        project_state(self.state_provider(), self.ordered(), now).apply_tx(tx, now)

        self.transactions[tx_id] = tx
        self.received_at[tx_id] = now
        self.store.add_mempool(tx, now)
        self.log(f"内存池新增 {tx['type']} {tx_id[:12]}")
        return tx_id

    def remove(self, tx_ids: list[str]) -> list[str]:
        removed = [tx_id for tx_id in tx_ids if tx_id in self.transactions]
        for tx_id in removed:
            self.transactions.pop(tx_id, None)
            self.received_at.pop(tx_id, None)
        if removed:
            self.store.remove_mempool(removed)
        return removed

    def clear(self) -> None:
        self.transactions.clear()
        self.received_at.clear()
        self.store.clear_mempool()

    def ordered(self) -> list[dict[str, Any]]:
        """手续费高、提交早的交易排在前面。"""
        return sorted(
            self.transactions.values(),
            key=lambda tx: (-int(tx["fee"]), self.received_at.get(tx["tx_id"], 0)),
        )

    def stats(self) -> dict[str, Any]:
        transactions = list(self.transactions.values())
        return {
            "count": len(transactions),
            "bytes": sum(len(canonical_bytes(tx)) for tx in transactions),
            "fees": sum(int(tx["fee"]) for tx in transactions),
        }

    def pending_claims(self, packet_id: str) -> set[int]:
        indexes: set[int] = set()
        for tx in self.transactions.values():
            if tx["type"] == "packet_claim" and tx["packet_id"] == packet_id:
                indexes.add(int(tx["index"]))
        return indexes
