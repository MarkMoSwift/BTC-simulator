from __future__ import annotations

from app.utils.crypto import sha256_hex

EMPTY_MERKLE_ROOT = "0" * 64


def merkle_root(tx_ids: list[str]) -> str:
    if not tx_ids:
        return EMPTY_MERKLE_ROOT

    layer = list(tx_ids)
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer.append(layer[-1])
        next_layer: list[str] = []
        for index in range(0, len(layer), 2):
            left = layer[index]
            right = layer[index + 1]
            try:
                payload = bytes.fromhex(left) + bytes.fromhex(right)
            except ValueError:
                payload = f"{left}{right}".encode("utf-8")
            next_layer.append(sha256_hex(payload))
        layer = next_layer
    return layer[0]
