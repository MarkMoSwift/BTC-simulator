from __future__ import annotations

import hashlib
import time
from typing import Any

from app.utils.serialization import canonical_json_bytes


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    result = hashlib.sha256(data).hexdigest()
    return result


def double_sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    first = hashlib.sha256(data).digest()
    result = hashlib.sha256(first).hexdigest()
    return result


def hash_json(data: Any) -> str:
    return double_sha256_hex(canonical_json_bytes(data))


def message_id(kind: str, payload_hash: str) -> str:
    return double_sha256_hex(f"{kind}:{payload_hash}")
