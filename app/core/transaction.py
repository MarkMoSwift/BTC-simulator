from __future__ import annotations

import time
from typing import Any

from app.core.wallet import address_from_public_key, sign_payload, verify_signature
from app.utils.crypto import hash_json
from app.utils.serialization import canonical_json_bytes


TRANSFER_REQUIRED_FIELDS = {
    "tx_id",
    "type",
    "sender",
    "receiver",
    "amount",
    "fee",
    "timestamp",
    "public_key",
    "signature",
}

COINBASE_REQUIRED_FIELDS = {
    "tx_id",
    "type",
    "receiver",
    "amount",
    "fee",
    "timestamp",
}


def _normalized_amount(value: Any) -> float:
    return round(float(value), 8)


def transaction_body(tx: dict[str, Any]) -> dict[str, Any]:
    body = dict(tx)
    body.pop("tx_id", None)
    body.pop("signature", None)
    return body


def signable_payload(tx: dict[str, Any]) -> bytes:
    return canonical_json_bytes(transaction_body(tx))


def compute_tx_id(tx: dict[str, Any]) -> str:
    return hash_json(transaction_body(tx))


def create_coinbase(
    receiver: str,
    amount: float,
    timestamp: int | None = None,
    height: int | None = None,
) -> dict[str, Any]:
    tx = {
        "type": "coinbase",
        "receiver": receiver,
        "amount": _normalized_amount(amount),
        "fee": 0.0,
        "timestamp": int(timestamp or time.time()),
    }
    if height is not None:
        tx["height"] = int(height)
    tx["tx_id"] = compute_tx_id(tx)
    return tx


def create_transfer(
    sender: str,
    receiver: str,
    amount: float,
    fee: float,
    public_key: str,
    private_key: str,
    timestamp: int | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    tx = {
        "type": "transfer",
        "sender": sender,
        "receiver": receiver,
        "amount": _normalized_amount(amount),
        "fee": _normalized_amount(fee),
        "timestamp": int(timestamp or time.time()),
        "public_key": public_key,
    }
    if note:
        tx["note"] = note
    tx["tx_id"] = compute_tx_id(tx)
    tx["signature"] = sign_payload(private_key, signable_payload(tx))
    return tx


def validate_tx_id(tx: dict[str, Any]) -> None:
    actual = compute_tx_id(tx)
    if tx.get("tx_id") != actual:
        raise ValueError(f"tx_id mismatch: expected {actual}")


def validate_coinbase_shape(tx: dict[str, Any]) -> None:
    missing = COINBASE_REQUIRED_FIELDS - set(tx)
    if missing:
        raise ValueError(f"coinbase missing fields: {', '.join(sorted(missing))}")
    if tx.get("type") != "coinbase":
        raise ValueError("transaction is not coinbase")
    validate_tx_id(tx)
    if _normalized_amount(tx.get("amount")) < 0:
        raise ValueError("coinbase amount must be non-negative")
    if _normalized_amount(tx.get("fee")) != 0:
        raise ValueError("coinbase fee must be 0")


def validate_transfer_shape_and_signature(tx: dict[str, Any]) -> None:
    missing = TRANSFER_REQUIRED_FIELDS - set(tx)
    if missing:
        raise ValueError(f"transfer missing fields: {', '.join(sorted(missing))}")
    if tx.get("type") != "transfer":
        raise ValueError("transaction is not transfer")
    validate_tx_id(tx)

    amount = _normalized_amount(tx.get("amount"))
    fee = _normalized_amount(tx.get("fee"))
    if amount <= 0:
        raise ValueError("amount must be > 0")
    if fee < 0:
        raise ValueError("fee must be >= 0")
    if tx.get("receiver") == tx.get("sender"):
        raise ValueError("sender and receiver must differ")
    if address_from_public_key(tx["public_key"]) != tx["sender"]:
        raise ValueError("sender address does not match public_key")
    if not verify_signature(tx["public_key"], tx["signature"], signable_payload(tx)):
        raise ValueError("signature verification failed")


def estimate_transaction_bytes(tx: dict[str, Any]) -> int:
    return len(canonical_json_bytes(tx))
