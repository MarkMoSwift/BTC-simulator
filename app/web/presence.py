"""课堂钱包状态通报；不参与区块、交易或奖励验证。"""
from __future__ import annotations

import math
import re
import time
from typing import Any

from app.core.wallet import LocalWallet, address_from_public_key, verify_signature
from app.utils.canonical import canonical_bytes

PRESENCE_SECONDS = 35
MAX_WALLETS = 128
MAX_NODES = 256
ADDRESS = re.compile(r"sim[0-9a-f]{40}\Z")


class Presence:
    def __init__(self, domain: str | None, wallet: LocalWallet | None):
        self.domain = domain
        self.wallet = wallet
        self.records: dict[str, dict[str, Any]] = {}

    def create(self, wallets: list[dict[str, Any]], now: float) -> dict[str, Any] | None:
        if self.wallet is None or not self.domain:
            return None
        data = {"domain": self.domain, "node": self.wallet.address,
                "public_key": self.wallet.public_key, "at": int(now),
                "wallets": wallets[:MAX_WALLETS]}
        message = {"type": "presence", "data": data,
                   "signature": self.wallet.sign(b"btc-sim-presence|" + canonical_bytes(data))}
        self.accept(message, now)
        return message

    def accept(self, message: dict[str, Any], now: float | None = None) -> bool:
        now = time.time() if now is None else now
        data = message.get("data")
        if not self.domain or not isinstance(data, dict) or data.get("domain") != self.domain:
            return False
        at, node = data.get("at"), data.get("node")
        if (type(at) is not int or not now - PRESENCE_SECONDS <= at <= now + 5
                or not isinstance(node, str) or not ADDRESS.fullmatch(node)):
            return False
        previous = self.records.get(node)
        if previous and previous["data"]["at"] >= at:
            return False
        rows = data.get("wallets")
        if not isinstance(rows, list) or len(rows) > MAX_WALLETS:
            return False
        addresses = set()
        for row in rows:
            if not isinstance(row, dict):
                return False
            address, name, rate = row.get("address"), row.get("nickname"), row.get("hashrate")
            if (not isinstance(address, str) or not ADDRESS.fullmatch(address) or address in addresses
                    or not isinstance(name, str) or not 0 < len(name) <= 64
                    or type(row.get("online")) is not bool
                    or type(rate) not in {int, float} or not 0 <= rate <= 1e24 or not math.isfinite(rate)):
                return False
            addresses.add(address)
        public_key, signature = data.get("public_key"), message.get("signature")
        if not isinstance(public_key, str) or len(public_key) != 128 or not isinstance(signature, str) or len(signature) != 128:
            return False
        try:
            payload = canonical_bytes(data)
            if (len(payload) > 65_536 or address_from_public_key(public_key) != node
                    or not verify_signature(public_key, signature, b"btc-sim-presence|" + payload)):
                return False
        except (ValueError, TypeError, OverflowError):
            return False
        if node not in self.records and len(self.records) >= MAX_NODES:
            oldest = min(self.records, key=lambda key: self.records[key]["data"]["at"])
            if self.records[oldest]["data"]["at"] >= now - PRESENCE_SECONDS:
                return False
            del self.records[oldest]
        self.records[node] = message
        return True

    def messages(self, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        return [message for message in self.records.values()
                if now - message["data"]["at"] <= PRESENCE_SECONDS]

    def wallets(self, now: float) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for message in sorted(self.records.values(), key=lambda item: item["data"]["at"]):
            fresh = now - message["data"]["at"] <= PRESENCE_SECONDS
            for row in message["data"]["wallets"]:
                current = result.get(row["address"])
                online = fresh and row["online"]
                rate = row["hashrate"] if online else 0.0
                if current is None:
                    result[row["address"]] = {**row, "online": online, "hashrate": rate}
                else:
                    current.update(nickname=row["nickname"], online=current["online"] or online,
                                   hashrate=max(current["hashrate"], rate))
        return result
