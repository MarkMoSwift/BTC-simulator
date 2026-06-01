from __future__ import annotations

import time
from typing import Any

from app.core.merkle import EMPTY_MERKLE_ROOT, merkle_root
from app.utils.crypto import hash_json

GENESIS_PREV_HASH = "0" * 64


def compute_block_hash(block_or_header: dict[str, Any]) -> str:
    header = block_or_header.get("header", block_or_header)
    return hash_json(header)


def create_block(
    prev_hash: str,
    transactions: list[dict[str, Any]],
    difficulty: int,
    nonce: int = 0,
    timestamp: int | None = None,
    version: int = 1,
) -> dict[str, Any]:
    tx_ids = [tx["tx_id"] for tx in transactions]
    return {
        "header": {
            "version": version,
            "prev_hash": prev_hash,
            "merkle_root": merkle_root(tx_ids),
            "timestamp": int(timestamp or time.time()),
            "difficulty": int(difficulty),
            "nonce": int(nonce),
        },
        "transactions": transactions,
    }


def genesis_block() -> dict[str, Any]:
    return {
        "header": {
            "version": 1,
            "prev_hash": GENESIS_PREV_HASH,
            "merkle_root": EMPTY_MERKLE_ROOT,
            "timestamp": 0,
            "difficulty": 0,
            "nonce": 0,
        },
        "transactions": [],
    }


def hash_meets_difficulty(block_hash: str, difficulty: int) -> bool:
    return block_hash.startswith("0" * max(int(difficulty), 0))
