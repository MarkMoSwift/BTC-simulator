"""规范化 JSON 与哈希：签名、交易号、区块头都依赖这套规则。"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(data: Any) -> str:
    """按 key 排序、无空格、ASCII 转义的稳定 JSON 文本。"""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_bytes(data: Any) -> bytes:
    return canonical_json(data).encode("utf-8")


def sha256d(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def sha256d_hex(data: bytes) -> str:
    return sha256d(data).hex()


def hash_json(data: Any) -> str:
    return sha256d_hex(canonical_bytes(data))
