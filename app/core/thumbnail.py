"""Consensus limits for the JPEG preview embedded in a mint transaction."""
from __future__ import annotations

import base64
import binascii

MAX_BYTES = 16 * 1024
MAX_SIDE = 256


def jpeg_thumbnail(encoded: str) -> bytes:
    if not isinstance(encoded, str) or len(encoded) > ((MAX_BYTES + 2) // 3) * 4:
        raise ValueError("缩略图编码超过上限")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("缩略图编码无效") from exc
    if len(data) > MAX_BYTES or base64.b64encode(data).decode("ascii") != encoded:
        raise ValueError("缩略图必须使用规范编码且不超过 16 KiB")
    if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        raise ValueError("缩略图必须是 JPEG")
    offset = 2
    components = 0
    scans = 0
    while offset + 1 < len(data) and data[offset] == 0xFF:
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset == len(data) - 1 and data[offset] == 0xD9 and scans:
            return data
        if offset + 3 > len(data):
            break
        marker = data[offset]
        size = int.from_bytes(data[offset + 1:offset + 3], "big")
        end = offset + 1 + size
        if size < 2 or end >= len(data) - 2:
            break
        if marker in (0xC0, 0xC2):
            if size < 8 or components or data[offset + 3] != 8:
                break
            height = int.from_bytes(data[offset + 4:offset + 6], "big")
            width = int.from_bytes(data[offset + 6:offset + 8], "big")
            components = data[offset + 8]
            if components not in (1, 3) or size != 8 + 3 * components:
                break
            if not (0 < height <= MAX_SIDE and 0 < width <= MAX_SIDE):
                break
        if marker == 0xDA:
            count = data[offset + 3]
            if not 0 < count <= components or size != 6 + 2 * count:
                break
            scans += 1
            while (next_marker := data.find(b"\xff", end)) != -1:
                value = data[next_marker + 1]
                end = next_marker
                if value != 0 and not 0xD0 <= value <= 0xD7:
                    break
                end += 2
            if next_marker == -1:
                break
        offset = end
    raise ValueError("JPEG 缩略图无效或最长边超过 256 像素")
