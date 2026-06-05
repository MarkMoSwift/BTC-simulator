from __future__ import annotations

import time
from typing import Any

from app.core.merkle import EMPTY_MERKLE_ROOT, merkle_root
from app.utils.crypto import hash_json

GENESIS_PREV_HASH = "0" * 64
MAX_TARGET_INT = (1 << 256) - 1
MAX_TARGET_HEX = "f" * 64


def compute_block_hash(block_or_header: dict[str, Any]) -> str:
    header = block_or_header.get("header", block_or_header)
    return hash_json(header)


def create_block(
    prev_hash: str,
    transactions: list[dict[str, Any]],
    difficulty: int,
    target: str | None = None,
    nonce: int = 0,
    timestamp: int | None = None,
    version: int = 1,
) -> dict[str, Any]:
    tx_ids = [tx["tx_id"] for tx in transactions]
    header = {
        "version": version,
        "prev_hash": prev_hash,
        "merkle_root": merkle_root(tx_ids),
        "timestamp": int(timestamp or time.time()),
        "difficulty": int(difficulty),
        "nonce": int(nonce),
    }
    if target is not None:
        header["target"] = normalize_target_hex(target)
    return {
        "header": header,
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


def normalize_target_hex(target: str | int) -> str:
    if isinstance(target, int):
        value = target
    else:
        text = str(target).strip().lower()
        if text.startswith("0x"):
            text = text[2:]
        if not text or len(text) > 64:
            raise ValueError("target must contain 1 to 64 hexadecimal characters")
        try:
            value = int(text, 16)
        except ValueError as exc:
            raise ValueError("target must be hexadecimal") from exc
    if value < 0 or value > MAX_TARGET_INT:
        raise ValueError("target is outside the 256-bit range")
    return f"{value:064x}"


def target_prefix_to_hex(prefix: str) -> str:
    text = str(prefix).strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if not text or len(text) > 64:
        raise ValueError("target prefix must contain 1 to 64 hexadecimal characters")
    try:
        int(text, 16)
    except ValueError as exc:
        raise ValueError("target prefix must be hexadecimal") from exc
    return normalize_target_hex(text + ("f" * (64 - len(text))))


def difficulty_to_target(difficulty: int) -> str:
    zeros = min(max(int(difficulty), 0), 64)
    return ("0" * zeros) + ("f" * (64 - zeros))


def target_to_difficulty(target: str | int) -> int:
    normalized = normalize_target_hex(target)
    return len(normalized) - len(normalized.lstrip("0"))


def target_preview(target: str | int, minimum_chars: int = 8) -> str:
    normalized = normalize_target_hex(target)
    leading_zeros = target_to_difficulty(normalized)
    visible = min(max(int(minimum_chars), leading_zeros + 2), 64)
    if visible == 64:
        return normalized
    return f"{normalized[:visible]}..."


def effective_target(header: dict[str, Any]) -> str:
    if header.get("target") is not None:
        return normalize_target_hex(header["target"])
    return difficulty_to_target(int(header.get("difficulty", 0)))


def hash_meets_target(block_hash: str, target: str | int) -> bool:
    return int(block_hash, 16) <= int(normalize_target_hex(target), 16)
