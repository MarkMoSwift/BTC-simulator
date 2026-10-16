from __future__ import annotations

import hashlib

from app.core.block import (
    HEADER_BYTES,
    bits_for_target,
    block_hash,
    header_bytes,
    meets_bits,
    merkle_root,
    target_for_bits,
)


def sample_header(nonce: int = 0) -> dict:
    return {
        "version": 1,
        "prev_hash": "0" * 64,
        "merkle_root": "1" * 64,
        "timestamp": 1_700_000_000,
        "difficulty": 12,
        "miner_tag": 42,
        "nonce": nonce,
    }


def test_header_is_exactly_84_bytes() -> None:
    raw = header_bytes(sample_header())
    assert len(raw) == HEADER_BYTES == 84
    assert raw[:4] == (1).to_bytes(4, "little")
    assert raw[80:84] == (0).to_bytes(4, "little")


def test_block_hash_is_double_sha256_of_header() -> None:
    header = sample_header(nonce=7)
    expected = hashlib.sha256(hashlib.sha256(header_bytes(header)).digest()).hexdigest()
    assert block_hash(header) == expected


def test_target_round_trip() -> None:
    for bits in (0, 4, 12, 32, 200):
        assert bits_for_target(target_for_bits(bits)) == bits
    assert target_for_bits(0) == "f" * 64
    assert target_for_bits(256) == "0" * 64


def test_meets_bits_reads_leading_zero_bits() -> None:
    assert meets_bits("0" * 64, 256)
    assert meets_bits("0" * 12 + "1" + "0" * 51, 48)
    assert not meets_bits("0" * 11 + "1" + "0" * 52, 48)
    assert meets_bits("0" * 64, 0)
    assert not meets_bits("f" * 64, 1)


def test_merkle_root_matches_manual_computation() -> None:
    left = "11" * 32
    right = "22" * 32
    manual = hashlib.sha256(
        hashlib.sha256(bytes.fromhex(left) + bytes.fromhex(right)).digest()
    ).hexdigest()
    assert merkle_root([left, right]) == manual
    assert merkle_root([left]) == left
    duplicated_last = hashlib.sha256(
        hashlib.sha256(bytes.fromhex(left) + bytes.fromhex(left)).digest()
    ).hexdigest()
    odd = hashlib.sha256(
        hashlib.sha256(bytes.fromhex(manual) + bytes.fromhex(duplicated_last)).digest()
    ).hexdigest()
    assert merkle_root([left, right, left]) == odd
