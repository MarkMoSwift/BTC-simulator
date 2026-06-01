from __future__ import annotations

import time
from typing import Any, Callable

from app.core.blockchain import Blockchain
from app.storage.sqlite_store import SQLiteStore

LogFn = Callable[[str], None]


class Mempool:
    def __init__(self, store: SQLiteStore, blockchain: Blockchain, log: LogFn | None = None):
        self.store = store
        self.blockchain = blockchain
        self.log = log or (lambda _message: None)

    def add_transaction(self, tx: dict[str, Any], source: str = "local") -> tuple[bool, str]:
        try:
            self.blockchain.validate_transfer_transaction(tx)
            stats = self.store.mempool_stats()
            from app.core.transaction import estimate_transaction_bytes

            if stats["bytes"] + estimate_transaction_bytes(tx) > int(
                self.blockchain.config["mempool_max_bytes"]
            ):
                raise ValueError("mempool byte limit exceeded")
            self.store.add_mempool_transaction(tx, received_at=int(time.time()))
        except Exception as exc:
            self.log(f"Transaction rejected from {source}: {exc}")
            return False, str(exc)

        self.log(f"Transaction added from {source}: {tx['tx_id'][:16]}")
        return True, tx["tx_id"]

    def remove_confirmed(self, tx_ids: list[str]) -> None:
        self.store.remove_mempool_transactions(tx_ids)

    def stats(self) -> dict[str, int]:
        return self.store.mempool_stats()

    def ordered(self, limit: int | None = None) -> list[dict[str, Any]]:
        return self.store.list_mempool_transactions(limit=limit)
