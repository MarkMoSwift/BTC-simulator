from __future__ import annotations

import json
from typing import Any


def canonical_json(data: Any) -> str:
    """Stable JSON form used for hashes and signatures."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_json_bytes(data: Any) -> bytes:
    return canonical_json(data).encode("utf-8")


def pretty_json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)
