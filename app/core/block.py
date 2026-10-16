"""区块头、目标值与梅克尔根：所有哈希规则集中在这里。"""
from __future__ import annotations

import struct
from typing import Any

from app.utils.canonical import sha256d_hex

GENESIS_PREV_HASH = "0" * 64
EMPTY_MERKLE_ROOT = "0" * 64
HEADER_FIELDS = ("version", "prev_hash", "merkle_root", "timestamp", "difficulty", "miner_tag", "nonce")
HEADER_BYTES = 84
HASH_BYTES = 32


class BlockError(ValueError):
    """区块结构或工作量不合法。"""


def header_bytes(header: dict[str, Any]) -> bytes:
    """84 字节区块头：4+32+32+4+4+4+4，整数按小端序编码。"""
    try:
        return (
            struct.pack("<I", int(header["version"]))
            + _hex_to_bytes(header["prev_hash"])
            + _hex_to_bytes(header["merkle_root"])
            + struct.pack("<I", int(header["timestamp"]))
            + struct.pack("<I", int(header["difficulty"]))
            + struct.pack("<I", int(header["miner_tag"]))
            + struct.pack("<I", int(header["nonce"]))
        )
    except KeyError as exc:
        raise BlockError(f"区块头缺少字段：{exc.args[0]}") from exc
    except (struct.error, ValueError) as exc:
        raise BlockError(f"区块头字段超出范围：{exc}") from exc


def block_hash(header: dict[str, Any]) -> str:
    """区块哈希 = sha256d(84 字节区块头)。"""
    return sha256d_hex(header_bytes(header))


def merkle_root(tx_ids: list[str]) -> str:
    if not tx_ids:
        return EMPTY_MERKLE_ROOT
    layer = [_hex_to_bytes(tx_id) for tx_id in tx_ids]
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer.append(layer[-1])
        layer = [
            sha256d_bytes(layer[index] + layer[index + 1])
            for index in range(0, len(layer), 2)
        ]
    return layer[0].hex()


def sha256d_bytes(data: bytes) -> bytes:
    import hashlib

    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def target_for_bits(bits: int) -> str:
    """难度 d 表示哈希的二进制前导 0 位数，对应目标值 (1 << (256-d)) - 1。"""
    value = int(bits)
    if value <= 0:
        return "f" * 64
    if value >= 256:
        return "0" * 64
    return f"{(1 << (256 - value)) - 1:064x}"


def bits_for_target(target: str) -> int:
    value = int(_normalize_target(target), 16)
    return 256 - value.bit_length() if value else 256


def meets_target(hash_hex: str, target_hex: str) -> bool:
    return int(hash_hex, 16) <= int(_normalize_target(target_hex), 16)


def meets_bits(hash_hex: str, bits: int) -> bool:
    return meets_target(hash_hex, target_for_bits(bits))


def expected_hashes(bits: int) -> int:
    """达到该难度平均需要尝试的哈希次数。"""
    return 1 << max(int(bits), 0)


def genesis_block() -> dict[str, Any]:
    """创世块固定不变，因此所有课堂的链从同一个哈希开始。"""
    return {
        "header": {
            "version": 1,
            "prev_hash": GENESIS_PREV_HASH,
            "merkle_root": EMPTY_MERKLE_ROOT,
            "timestamp": 0,
            "difficulty": 0,
            "miner_tag": 0,
            "nonce": 0,
        },
        "transactions": [],
    }


def genesis_hash() -> str:
    return block_hash(genesis_block()["header"])


def _normalize_target(target: str | int) -> str:
    if isinstance(target, int):
        value = target
    else:
        text = str(target).strip().lower()
        if text.startswith("0x"):
            text = text[2:]
        if not text or len(text) > 64:
            raise BlockError("目标值必须是 1 到 64 位十六进制")
        try:
            value = int(text, 16)
        except ValueError as exc:
            raise BlockError("目标值必须是十六进制") from exc
    if value < 0 or value > (1 << 256) - 1:
        raise BlockError("目标值超出 256 位范围")
    return f"{value:064x}"


def _hex_to_bytes(value: str) -> bytes:
    raw = bytes.fromhex(str(value))
    if len(raw) != HASH_BYTES:
        raise BlockError("哈希必须是 32 字节")
    return raw
