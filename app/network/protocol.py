from __future__ import annotations

import json
from asyncio import StreamReader, StreamWriter
from typing import Any

from app.core.block import compute_block_hash
from app.utils.crypto import message_id


async def send_json(writer: StreamWriter, message: dict[str, Any]) -> None:
    payload = json.dumps(message, ensure_ascii=True, sort_keys=True).encode("utf-8") + b"\n"
    writer.write(payload)
    await writer.drain()


async def read_json(reader: StreamReader) -> dict[str, Any] | None:
    line = await reader.readline()
    if not line:
        return None
    return json.loads(line.decode("utf-8"))


def tx_message_id(tx: dict[str, Any]) -> str:
    return message_id("TX", tx["tx_id"])


def block_message_id(block: dict[str, Any]) -> str:
    return message_id("BLOCK", compute_block_hash(block))
