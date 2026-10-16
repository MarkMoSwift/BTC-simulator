"""链状态机：余额、NFT、红包全部由区块重放得到。"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable

from app.config import ChainSettings
from app.core.redpacket import PacketError, amount_for_index
from app.core.transaction import COINBASE, validate_coinbase, validate_signed_tx

LogFn = Callable[[str], None]


class RuleError(ValueError):
    """交易违反链上规则（余额不足、重复领取、已售出等）。"""


@dataclass(frozen=True)
class TransactionIndex:
    """交易索引：参与地址与对手方由规则本身给出，用于账单查询和列表展示。"""

    addresses: frozenset[str]
    counterparty: str | None
    amount: int


class ChainState:
    """可克隆的状态机：克隆用于试算，正式状态只在区块确认后替换。"""

    def __init__(self, settings: ChainSettings):
        self.settings = settings
        self.height = 0
        self.balances: dict[str, int] = {}
        self.mining_income: dict[str, int] = {}
        self.nfts: dict[str, dict[str, Any]] = {}
        self.listings: dict[str, dict[str, Any]] = {}
        self.packets: dict[str, dict[str, Any]] = {}
        self.seen_tx_ids: set[str] = set()

    def clone(self) -> "ChainState":
        return copy.deepcopy(self)

    def balance_of(self, address: str) -> int:
        return self.balances.get(address, 0)

    def nfts_of(self, address: str) -> list[dict[str, Any]]:
        return [nft for nft in self.nfts.values() if nft["owner"] == address]

    def apply_block(
        self, block: dict[str, Any], height: int, *, prevalidated: bool = False
    ) -> dict[str, TransactionIndex]:
        """执行一个区块，并返回每笔交易动了哪些钱包的索引。"""
        transactions = block.get("transactions", [])
        if not transactions:
            raise RuleError("非创世区块必须包含 coinbase")
        coinbase = transactions[0]
        validate_coinbase(coinbase)
        clock = int(block["header"]["timestamp"])
        self.height = height
        self._credit_payouts(coinbase["payouts"])
        self.seen_tx_ids.add(coinbase["tx_id"])
        index = {
            coinbase["tx_id"]: TransactionIndex(
                frozenset(payout["receiver"] for payout in coinbase["payouts"]),
                None,
                sum(int(payout["amount"]) for payout in coinbase["payouts"]),
            )
        }
        for tx in transactions[1:]:
            index[tx["tx_id"]] = self.apply_tx(tx, clock, height=height, prevalidated=prevalidated)
        return index

    def apply_tx(
        self,
        tx: dict[str, Any],
        clock: int,
        *,
        height: int | None = None,
        prevalidated: bool = False,
    ) -> TransactionIndex:
        tx_type = tx.get("type")
        if tx_type == COINBASE:
            raise RuleError("coinbase 只能出现在区块首位")
        if not prevalidated:
            validate_signed_tx(tx)
        if tx["tx_id"] in self.seen_tx_ids:
            raise RuleError("这笔交易已经上链，不能重复执行")
        fee = int(tx["fee"])
        if fee < self.settings.min_fee_sat or fee > self.settings.max_fee_sat:
            raise RuleError(
                f"手续费必须在 {self.settings.min_fee_sat} 到 {self.settings.max_fee_sat} 聪之间"
            )

        handler = getattr(self, f"_apply_{tx_type}")
        index = handler(tx, clock, height if height is not None else self.height + 1)
        self.seen_tx_ids.add(tx["tx_id"])
        return index

    def _apply_transfer(self, tx: dict[str, Any], _clock: int, _height: int) -> TransactionIndex:
        self._spend(tx["sender"], tx["amount"] + tx["fee"])
        self.balances[tx["receiver"]] = self.balance_of(tx["receiver"]) + tx["amount"]
        return TransactionIndex(
            frozenset((tx["sender"], tx["receiver"])), tx["receiver"], int(tx["amount"])
        )

    def _apply_nft_mint(self, tx: dict[str, Any], _clock: int, height: int) -> TransactionIndex:
        if len(self.nfts_of(tx["sender"])) >= self.settings.max_nfts_per_player:
            raise RuleError(f"每人最多铸造 {self.settings.max_nfts_per_player} 件藏品")
        self._spend(tx["sender"], tx["fee"])
        self.nfts[tx["tx_id"]] = {
            "token_id": tx["tx_id"],
            "owner": tx["sender"],
            "name": tx["name"],
            "seed": tx["seed"],
            "created_at": tx["timestamp"],
            "mint_height": height,
        }
        return TransactionIndex(frozenset((tx["sender"],)), None, 0)

    def _apply_nft_list(self, tx: dict[str, Any], _clock: int, _height: int) -> TransactionIndex:
        nft = self._require_nft_owner(tx["token_id"], tx["sender"])
        self._spend(tx["sender"], tx["fee"])
        self.listings[tx["token_id"]] = {
            "token_id": nft["token_id"],
            "seller": tx["sender"],
            "price": tx["price"],
            "listed_at": tx["timestamp"],
        }
        return TransactionIndex(frozenset((tx["sender"],)), None, 0)

    def _apply_nft_unlist(self, tx: dict[str, Any], _clock: int, _height: int) -> TransactionIndex:
        listing = self.listings.get(tx["token_id"])
        if not listing or listing["seller"] != tx["sender"]:
            raise RuleError("这件藏品没有由你上架")
        self._spend(tx["sender"], tx["fee"])
        del self.listings[tx["token_id"]]
        return TransactionIndex(frozenset((tx["sender"],)), None, 0)

    def _apply_nft_buy(self, tx: dict[str, Any], _clock: int, _height: int) -> TransactionIndex:
        listing = self.listings.get(tx["token_id"])
        if not listing:
            raise RuleError("藏品当前没有在售")
        if listing["seller"] == tx["sender"]:
            raise RuleError("不能购买自己上架的藏品")
        nft = self.nfts.get(tx["token_id"])
        if not nft:
            raise RuleError("藏品不存在")
        price = int(listing["price"])
        self._spend(tx["sender"], price + tx["fee"])
        self.balances[listing["seller"]] = self.balance_of(listing["seller"]) + price
        nft["owner"] = tx["sender"]
        del self.listings[tx["token_id"]]
        return TransactionIndex(
            frozenset((tx["sender"], listing["seller"])), listing["seller"], price
        )

    def _apply_packet_create(self, tx: dict[str, Any], _clock: int, _height: int) -> TransactionIndex:
        if tx["count"] > self.settings.max_packet_claims:
            raise RuleError(f"红包个数最多 {self.settings.max_packet_claims} 个")
        try:
            amount_for_index(tx["tx_id"], tx["total"], tx["count"], 0)
        except PacketError as exc:
            raise RuleError(str(exc)) from exc
        self._spend(tx["sender"], tx["total"] + tx["fee"])
        self.packets[tx["tx_id"]] = {
            "packet_id": tx["tx_id"],
            "sender": tx["sender"],
            "total": tx["total"],
            "count": tx["count"],
            "note": tx.get("note", ""),
            "created_at": tx["timestamp"],
            "expires_at": tx["timestamp"] + self.settings.packet_expiry_seconds,
            "remaining": tx["total"],
            "claims": {},
            "refunded": False,
        }
        return TransactionIndex(frozenset((tx["sender"],)), None, int(tx["total"]))

    def _apply_packet_claim(self, tx: dict[str, Any], clock: int, _height: int) -> TransactionIndex:
        packet = self.packets.get(tx["packet_id"])
        if not packet:
            raise RuleError("红包不存在")
        if clock > packet["expires_at"]:
            raise RuleError("红包已过期")
        index = tx["index"]
        if index >= packet["count"]:
            raise RuleError("领取序号超出红包个数")
        if index in packet["claims"]:
            raise RuleError("这个红包已经被领走了")
        try:
            amount = amount_for_index(packet["packet_id"], packet["total"], packet["count"], index)
        except PacketError as exc:
            raise RuleError(str(exc)) from exc
        if amount > packet["remaining"]:
            raise RuleError("红包余额不足")
        self._spend(tx["sender"], tx["fee"])
        packet["claims"][index] = {"receiver": tx["sender"], "amount": amount, "at": clock}
        packet["remaining"] -= amount
        self.balances[tx["sender"]] = self.balance_of(tx["sender"]) + amount
        return TransactionIndex(
            frozenset((tx["sender"], packet["sender"])), packet["sender"], amount
        )

    def _apply_packet_refund(self, tx: dict[str, Any], clock: int, _height: int) -> TransactionIndex:
        packet = self.packets.get(tx["packet_id"])
        if not packet:
            raise RuleError("红包不存在")
        if packet["sender"] != tx["sender"]:
            raise RuleError("只有发红包的人可以退回")
        if clock <= packet["expires_at"]:
            raise RuleError("红包还没有过期")
        remaining = int(packet["remaining"])
        if remaining <= 0:
            raise RuleError("红包已经领完，没有可退回的金额")
        self._spend(tx["sender"], tx["fee"])
        packet["remaining"] = 0
        packet["refunded"] = True
        self.balances[tx["sender"]] = self.balance_of(tx["sender"]) + remaining
        return TransactionIndex(frozenset((tx["sender"],)), None, remaining)

    def _credit_payouts(self, payouts: list[dict[str, Any]]) -> None:
        for payout in payouts:
            receiver = payout["receiver"]
            amount = int(payout["amount"])
            self.balances[receiver] = self.balance_of(receiver) + amount
            self.mining_income[receiver] = self.mining_income.get(receiver, 0) + amount

    def _spend(self, address: str, amount: int) -> None:
        if amount < 0:
            raise RuleError("金额不能为负")
        balance = self.balance_of(address)
        if balance < amount:
            raise RuleError("余额不足")
        if amount:
            self.balances[address] = balance - amount

    def _require_nft_owner(self, token_id: str, owner: str) -> dict[str, Any]:
        nft = self.nfts.get(token_id)
        if not nft:
            raise RuleError("藏品不存在")
        if nft["owner"] != owner:
            raise RuleError("你不是这件藏品的持有人")
        return nft
