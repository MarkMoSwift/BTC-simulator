"""钱包：浏览器的 P-256 私钥签名，服务器只保存公钥与地址。"""
from __future__ import annotations

import hashlib
import secrets

from ecdsa import BadSignatureError, NIST256p, SigningKey, VerifyingKey
from ecdsa.util import sigdecode_string, sigencode_string

ADDRESS_PREFIX = "sim"
PUBLIC_KEY_BYTES = 64
SIGNATURE_BYTES = 64


class SignatureError(ValueError):
    """签名或公钥格式不合法。"""


def address_from_public_key(public_key_hex: str) -> str:
    """地址 = "sim" + sha256(公钥)[:20] 字节，模仿"地址来自公钥哈希"。"""
    raw = bytes.fromhex(public_key_hex)
    return ADDRESS_PREFIX + hashlib.sha256(raw).hexdigest()[:40]


def public_key_from_private(private_key_hex: str) -> str:
    signing_key = SigningKey.from_string(_private_bytes(private_key_hex), curve=NIST256p)
    return signing_key.get_verifying_key().to_string().hex()


def signature_digest(message: bytes) -> bytes:
    return hashlib.sha256(message).digest()


def verify_signature(public_key_hex: str, signature_hex: str, message: bytes) -> bool:
    try:
        verifying_key = VerifyingKey.from_string(
            _public_bytes(public_key_hex), curve=NIST256p, validate_point=True
        )
        return verifying_key.verify_digest(
            signature=_signature_bytes(signature_hex),
            digest=signature_digest(message),
            sigdecode=sigdecode_string,
            allow_truncate=False,
        )
    except (BadSignatureError, SignatureError, AssertionError):
        return False


def sign_message(private_key_hex: str, message: bytes) -> str:
    signing_key = SigningKey.from_string(_private_bytes(private_key_hex), curve=NIST256p)
    signature = signing_key.sign_digest_deterministic(
        signature_digest(message),
        hashfunc=hashlib.sha256,
        sigencode=sigencode_string,
    )
    return signature.hex()


def generate_private_key() -> str:
    return secrets.token_bytes(32).hex()


def _private_bytes(private_key_hex: str) -> bytes:
    try:
        raw = bytes.fromhex(str(private_key_hex).strip())
    except ValueError as exc:
        raise SignatureError("私钥不是合法十六进制") from exc
    if len(raw) != 32:
        raise SignatureError("私钥必须是 32 字节")
    return raw


def _public_bytes(public_key_hex: str) -> bytes:
    try:
        raw = bytes.fromhex(str(public_key_hex).strip())
    except ValueError as exc:
        raise SignatureError("公钥不是合法十六进制") from exc
    if len(raw) != PUBLIC_KEY_BYTES:
        raise SignatureError("公钥必须是 64 字节的 P-256 点")
    return raw


def _signature_bytes(signature_hex: str) -> bytes:
    try:
        raw = bytes.fromhex(str(signature_hex).strip())
    except ValueError as exc:
        raise SignatureError("签名不是合法十六进制") from exc
    if len(raw) != SIGNATURE_BYTES:
        raise SignatureError("签名必须是 64 字节的 r||s")
    return raw
