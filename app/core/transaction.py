"""交易：字段定义、交易号计算与签名校验。"""
from __future__ import annotations

from typing import Any

from app.core.wallet import address_from_public_key, verify_signature
from app.utils.canonical import canonical_bytes, hash_json

HEX64 = 64
SIGNED_TX_TYPES = (
    "transfer",
    "nft_mint",
    "nft_list",
    "nft_unlist",
    "nft_buy",
    "packet_create",
    "packet_claim",
    "packet_refund",
)
COINBASE = "coinbase"

_OPTIONAL_FIELDS = {"note"}
_REQUIRED_FIELDS: dict[str, set[str]] = {
    "transfer": {"type", "sender", "receiver", "amount", "fee", "timestamp", "public_key"},
    "nft_mint": {"type", "sender", "name", "seed", "fee", "timestamp", "public_key"},
    "nft_list": {"type", "sender", "token_id", "price", "fee", "timestamp", "public_key"},
    "nft_unlist": {"type", "sender", "token_id", "fee", "timestamp", "public_key"},
    "nft_buy": {"type", "sender", "token_id", "fee", "timestamp", "public_key"},
    "packet_create": {"type", "sender", "total", "count", "fee", "timestamp", "public_key"},
    "packet_claim": {"type", "sender", "packet_id", "index", "fee", "timestamp", "public_key"},
    "packet_refund": {"type", "sender", "packet_id", "fee", "timestamp", "public_key"},
}
_NOTE_TYPES = {"transfer", "packet_create"}

MAX_NAME_LENGTH = 16
MAX_NOTE_LENGTH = 40
MAX_AMOUNT_SAT = 100_000_000 * 1_000_000


class TxError(ValueError):
    """交易结构、签名或取值不合法。"""


def tx_body(tx: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in tx.items() if key not in {"tx_id", "signature"}}


def tx_bytes(tx: dict[str, Any]) -> bytes:
    return canonical_bytes(tx_body(tx))


def compute_tx_id(tx: dict[str, Any]) -> str:
    return hash_json(tx_body(tx))


def validate_signed_tx(tx: dict[str, Any]) -> None:
    if not isinstance(tx, dict):
        raise TxError("交易必须是对象")
    tx_type = tx.get("type")
    if tx_type not in _REQUIRED_FIELDS:
        raise TxError(f"不支持的交易类型：{tx_type}")
    _validate_fields(tx, tx_type)
    _validate_sender(tx)
    _validate_tx_id(tx)
    if not verify_signature(tx["public_key"], str(tx.get("signature", "")), tx_bytes(tx)):
        raise TxError("签名校验失败")


def validate_coinbase(tx: dict[str, Any]) -> None:
    allowed = {"type", "height", "timestamp", "subsidy", "fees", "finder", "payouts", "tx_id"}
    unknown = set(tx) - allowed
    if unknown:
        raise TxError(f"coinbase 含未知字段：{', '.join(sorted(unknown))}")
    if missing := allowed - set(tx):
        raise TxError(f"coinbase 缺字段：{', '.join(sorted(missing))}")
    if tx["type"] != COINBASE:
        raise TxError("交易类型不是 coinbase")
    _require_int(tx, "height", 1, 10**9)
    _require_int(tx, "timestamp", 0, 10**12)
    _require_int(tx, "subsidy", 0, MAX_AMOUNT_SAT)
    _require_int(tx, "fees", 0, MAX_AMOUNT_SAT)
    _require_address(tx["finder"], "finder")

    payouts = tx["payouts"]
    if not isinstance(payouts, list) or not payouts:
        raise TxError("coinbase 必须包含至少一个分配对象")
    if len(payouts) > 200:
        raise TxError("coinbase 分配对象过多")
    seen: set[str] = set()
    for payout in payouts:
        if not isinstance(payout, dict) or set(payout) != {"receiver", "amount"}:
            raise TxError("coinbase 分配项必须是 {receiver, amount}")
        _require_address(payout["receiver"], "receiver")
        _require_int(payout, "amount", 1, MAX_AMOUNT_SAT)
        if payout["receiver"] in seen:
            raise TxError("coinbase 分配对象重复")
        seen.add(payout["receiver"])
    _validate_tx_id(tx)


def coinbase_tx_id(tx: dict[str, Any]) -> str:
    return compute_tx_id(tx)


def _validate_fields(tx: dict[str, Any], tx_type: str) -> None:
    required = _REQUIRED_FIELDS[tx_type]
    optional = _OPTIONAL_FIELDS if tx_type in _NOTE_TYPES else set()
    allowed = required | optional | {"signature", "tx_id"}
    unknown = set(tx) - allowed
    if unknown:
        raise TxError(f"{tx_type} 含未知字段：{', '.join(sorted(unknown))}")
    missing = required - set(tx)
    if missing:
        raise TxError(f"{tx_type} 缺字段：{', '.join(sorted(missing))}")
    _require_int(tx, "timestamp", 0, 10**12)
    _require_int(tx, "fee", 0, MAX_AMOUNT_SAT)

    if tx_type == "transfer":
        _require_address(tx["receiver"], "receiver")
        _require_int(tx, "amount", 1, MAX_AMOUNT_SAT)
    elif tx_type == "nft_mint":
        _require_text(tx, "name", MAX_NAME_LENGTH)
        _require_hex(tx, "seed", length=16)
    elif tx_type == "nft_list":
        _require_hex(tx, "token_id", length=HEX64)
        _require_int(tx, "price", 1, MAX_AMOUNT_SAT)
    elif tx_type in {"nft_unlist", "nft_buy"}:
        _require_hex(tx, "token_id", length=HEX64)
    elif tx_type == "packet_create":
        _require_int(tx, "total", 1, MAX_AMOUNT_SAT)
        _require_int(tx, "count", 1, 500)
    elif tx_type == "packet_claim":
        _require_hex(tx, "packet_id", length=HEX64)
        _require_int(tx, "index", 0, 500)
    elif tx_type == "packet_refund":
        _require_hex(tx, "packet_id", length=HEX64)

    if "note" in tx:
        _require_text(tx, "note", MAX_NOTE_LENGTH, allow_empty=True)


def _validate_sender(tx: dict[str, Any]) -> None:
    sender = _require_address(tx["sender"], "sender")
    public_key = tx["public_key"]
    if not isinstance(public_key, str) or len(public_key) != 128:
        raise TxError("public_key 必须是 128 位十六进制")
    try:
        derived = address_from_public_key(public_key)
    except ValueError as exc:
        raise TxError("public_key 不是合法十六进制") from exc
    if derived != sender:
        raise TxError("sender 与 public_key 不匹配")


def _validate_tx_id(tx: dict[str, Any]) -> None:
    expected = compute_tx_id(tx)
    if tx.get("tx_id") != expected:
        raise TxError("交易号与交易内容不一致")


def validate_address(value: Any, field: str = "地址") -> str:
    return _require_address(value, field)


def _require_address(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("sim") or len(value) != 43:
        raise TxError(f"{field} 必须是 sim 开头的地址")
    if any(character not in "0123456789abcdef" for character in value[3:]):
        raise TxError(f"{field} 包含非法字符")
    return value


def _require_int(container: dict[str, Any], field: str, minimum: int, maximum: int) -> int:
    value = container.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TxError(f"{field} 必须是整数")
    if value < minimum or value > maximum:
        raise TxError(f"{field} 超出允许范围")
    return value


def _require_text(container: dict[str, Any], field: str, max_length: int, *, allow_empty: bool = False) -> str:
    value = container.get(field)
    if not isinstance(value, str):
        raise TxError(f"{field} 必须是文本")
    text = value.strip()
    if not allow_empty and not text:
        raise TxError(f"{field} 不能为空")
    if len(text) > max_length:
        raise TxError(f"{field} 最长 {max_length} 个字")
    return text


def _require_hex(container: dict[str, Any], field: str, *, length: int) -> str:
    value = container.get(field)
    if not isinstance(value, str) or len(value) != length:
        raise TxError(f"{field} 必须是 {length} 位十六进制")
    if any(character not in "0123456789abcdef" for character in value):
        raise TxError(f"{field} 含非十六进制字符")
    return value
