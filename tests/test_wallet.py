from __future__ import annotations

import hashlib

from app.core.wallet import (
    address_from_public_key,
    generate_private_key,
    public_key_from_private,
    sign_message,
    verify_signature,
)

# RFC 6979 附录 A.2.5：P-256 + SHA-256，消息 "sample" 的确定性签名。
RFC6979_PRIVATE_KEY = "C9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721"
RFC6979_SIGNATURE = (
    "efd48b2aacb6a8fd1140dd9cd45e81d69d2c877b56aaf991c34d0ea84eaf3716"
    "f7cb1c942d657c41d436c7a1b6e29f65f3e900dbb9aff4064dc4ab2f843acda8"
)


def test_rfc6979_vector_matches() -> None:
    assert sign_message(RFC6979_PRIVATE_KEY, b"sample") == RFC6979_SIGNATURE
    public_key = public_key_from_private(RFC6979_PRIVATE_KEY)
    assert verify_signature(public_key, RFC6979_SIGNATURE, b"sample")


def test_signature_rejects_tampered_message() -> None:
    private_key = generate_private_key()
    public_key = public_key_from_private(private_key)
    signature = sign_message(private_key, b"transfer 1 coin")
    assert verify_signature(public_key, signature, b"transfer 1 coin")
    assert not verify_signature(public_key, signature, b"transfer 2 coin")


def test_address_is_public_key_hash() -> None:
    private_key = generate_private_key()
    public_key = public_key_from_private(private_key)
    expected = "sim" + hashlib.sha256(bytes.fromhex(public_key)).hexdigest()[:40]
    assert address_from_public_key(public_key) == expected
    assert len(expected) == 43
