"""链上红包的金额推导：完全由链上数据决定，任何人都能复算。"""
from __future__ import annotations

import hashlib

MIN_CLAIM_SAT = 10_000
_SCALE = 1 << 64


class PacketError(ValueError):
    """红包参数或领取规则不合法。"""


def derive_amounts(packet_id: str, total_sat: int, count: int) -> list[int]:
    """拼手气红包：每人至少 MIN_CLAIM_SAT，合计精确等于 total_sat。"""
    if count < 1:
        raise PacketError("红包个数必须大于 0")
    if total_sat < count * MIN_CLAIM_SAT:
        raise PacketError("红包总额太小，无法保证每个人都能领到")

    amounts: list[int] = []
    remaining = int(total_sat)
    for index in range(count):
        left = count - index
        if left == 1:
            amounts.append(remaining)
            continue
        average = remaining // left
        cap = min(2 * average - MIN_CLAIM_SAT, remaining - MIN_CLAIM_SAT * (left - 1))
        span = max(cap - MIN_CLAIM_SAT, 0)
        amount = MIN_CLAIM_SAT + (_random_unit(packet_id, index) * span) // _SCALE
        amount = max(MIN_CLAIM_SAT, min(amount, remaining - MIN_CLAIM_SAT * (left - 1)))
        amounts.append(amount)
        remaining -= amount
    return amounts


def amount_for_index(packet_id: str, total_sat: int, count: int, index: int) -> int:
    if index < 0 or index >= count:
        raise PacketError("领取序号超出红包个数")
    return derive_amounts(packet_id, total_sat, count)[index]


def _random_unit(packet_id: str, index: int) -> int:
    digest = hashlib.sha256(f"{packet_id}:{index}".encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big")
