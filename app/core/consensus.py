"""3.x 场次身份与工作量规则。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from app.core.block import block_hash, genesis_block
from app.utils.canonical import hash_json

PROTOCOL_MAJOR = 3
INITIAL_DIFFICULTY = 20
MIN_DIFFICULTY = 4
MAX_DIFFICULTY = 56
RETARGET_INTERVAL = 4
TARGET_SECONDS = 30
STALL_SECONDS = 120
FUTURE_SECONDS = 15


@dataclass(frozen=True)
class Session:
    network_id: str
    session_id: str
    start_time: int

    def __post_init__(self) -> None:
        for name in ("network_id", "session_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip() or len(value) > 128:
                raise ValueError(f"{name} 必须为 1 至 128 个字符，且首尾不能有空白")
        if type(self.start_time) is not int or not 0 < self.start_time <= 0xFFFFFFFF:
            raise ValueError("场次开始时间必须是有效的 Unix 秒数")

    def genesis(self) -> dict[str, Any]:
        block = genesis_block()
        block["header"].update(
            version=PROTOCOL_MAJOR,
            timestamp=self.start_time,
            merkle_root=hash_json({
                "protocol_major": PROTOCOL_MAJOR,
                "network_id": self.network_id,
                "session_id": self.session_id,
                "start_time": self.start_time,
            }),
        )
        return block

    @property
    def genesis_hash(self) -> str:
        return block_hash(self.genesis()["header"])


def normal_difficulty(timestamps: Sequence[int], height: int, previous: int) -> int:
    """根据当前分支计算下一块的正常难度，序列从创世块开始。"""
    if height == 1:
        return INITIAL_DIFFICULTY
    if height <= RETARGET_INTERVAL + 1 or (height - 2) % RETARGET_INTERVAL:
        return previous
    span = timestamps[height - 1] - timestamps[height - 1 - RETARGET_INTERVAL]
    expected = RETARGET_INTERVAL * TARGET_SECONDS
    if span * 5 < expected * 4:
        return min(previous + 1, MAX_DIFFICULTY)
    if span * 5 > expected * 6:
        return max(previous - 1, MIN_DIFFICULTY)
    return previous


def allowed_difficulties(normal: int, parent_time: int, timestamp: int) -> set[int]:
    if timestamp - parent_time >= STALL_SECONDS and normal > MIN_DIFFICULTY:
        return {normal, normal - 1}
    return {normal}
