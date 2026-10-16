from __future__ import annotations

import time

import pytest

from app.config import ChainSettings
from app.core.state import ChainState, RuleError
from app.core.transaction import TxError, compute_tx_id, tx_bytes
from app.core.wallet import sign_message
from tests.conftest import Player, make_player, make_service, new_player, signed_tx

FEE = 10_000
COIN = 100_000_000


def fund(state: ChainState, address: str, amount: int) -> None:
    state.balances[address] = state.balance_of(address) + amount


def mint(state: ChainState, owner: Player, name: str = "创世徽章") -> str:
    tx = signed_tx(owner, "nft_mint", name=name, seed="a1b2c3d4e5f60718", fee=FEE)
    state.apply_tx(tx, int(time.time()))
    return tx["tx_id"]


def start_auction(
    state: ChainState, seller: Player, token_id: str, reserve: int, duration: int, clock: int
) -> dict:
    tx = signed_tx(
        seller,
        "nft_auction_create",
        token_id=token_id,
        reserve_price=reserve,
        duration=duration,
        fee=FEE,
        timestamp=clock,
    )
    state.apply_tx(tx, clock)
    return tx


def bid(
    state: ChainState, bidder: Player, auction: dict, amount: int, clock: int, fee: int = FEE
) -> dict:
    tx = signed_tx(
        bidder,
        "nft_bid",
        auction_id=auction["tx_id"],
        amount=amount,
        fee=fee,
        timestamp=clock,
    )
    state.apply_tx(tx, clock)
    return tx


def settle(state: ChainState, submitter: Player, auction: dict, clock: int, fee: int = 0) -> dict:
    tx = signed_tx(
        submitter,
        "nft_auction_settle",
        auction_id=auction["tx_id"],
        fee=fee,
        timestamp=clock,
    )
    state.apply_tx(tx, clock)
    return tx


@pytest.fixture
def world():
    settings = ChainSettings(min_fee_sat=FEE)
    state = ChainState(settings)
    seller, first, second = new_player("seller"), new_player("first"), new_player("second")
    for player in (seller, first, second):
        fund(state, player.address, 200 * COIN)
    token_id = mint(state, seller)
    clock = int(time.time())
    auction = start_auction(state, seller, token_id, 5 * COIN, 120, clock)
    return state, seller, first, second, auction, clock


def test_first_bid_must_meet_the_reserve_price(world) -> None:
    state, _seller, first, _second, auction, clock = world
    with pytest.raises(RuleError, match="起拍价"):
        bid(state, first, auction, 4 * COIN, clock + 1)
    bid(state, first, auction, 5 * COIN, clock + 1)
    active = state.auctions[auction["tx_id"]]
    assert active["highest_bid"] == 5 * COIN
    assert active["highest_bidder"] == first.address


def test_each_bid_must_raise_by_ten_percent(world) -> None:
    state, _seller, first, second, auction, clock = world
    bid(state, first, auction, 10 * COIN, clock + 1)
    with pytest.raises(RuleError, match="10%"):
        bid(state, second, auction, 11 * COIN - 1, clock + 2)
    bid(state, second, auction, 11 * COIN, clock + 2)
    assert state.auctions[auction["tx_id"]]["highest_bidder"] == second.address


def test_seller_cannot_bid_and_the_leader_cannot_bid_again(world) -> None:
    state, seller, first, _second, auction, clock = world
    with pytest.raises(RuleError, match="卖家不能"):
        bid(state, seller, auction, 5 * COIN, clock + 1)
    bid(state, first, auction, 5 * COIN, clock + 1)
    with pytest.raises(RuleError, match="已经是当前最高价"):
        bid(state, first, auction, 6 * COIN, clock + 2)


def test_bid_after_the_deadline_is_rejected(world) -> None:
    state, _seller, _first, second, auction, clock = world
    with pytest.raises(RuleError, match="已经结束"):
        bid(state, second, auction, 9 * COIN, clock + 121)


def test_bid_locks_coins_and_outbid_refunds_the_previous_leader(world) -> None:
    state, _seller, first, second, auction, clock = world
    start_first = state.balance_of(first.address)
    bid(state, first, auction, 5 * COIN, clock + 1)
    assert state.balance_of(first.address) == start_first - 5 * COIN - FEE

    start_second = state.balance_of(second.address)
    bid(state, second, auction, 6 * COIN, clock + 2)
    assert state.balance_of(first.address) == start_first - FEE
    assert state.balance_of(second.address) == start_second - 6 * COIN - FEE


def test_bid_beyond_the_balance_is_rejected(world) -> None:
    state, _seller, _first, _second, auction, clock = world
    poor = new_player("poor")
    fund(state, poor.address, COIN)
    with pytest.raises(RuleError, match="余额不足"):
        bid(state, poor, auction, 5 * COIN, clock + 1)


def test_late_bid_extends_the_auction(world) -> None:
    state, _seller, first, _second, auction, clock = world
    bid(state, first, auction, 5 * COIN, clock + 100)
    assert state.auctions[auction["tx_id"]]["ends_at"] == clock + 130


def test_bid_must_be_signed_by_the_bidder(world) -> None:
    state, _seller, first, second, auction, clock = world
    forged = signed_tx(first, "nft_bid", auction_id=auction["tx_id"], amount=5 * COIN, fee=FEE)
    forged["signature"] = sign_message(second.private_key, tx_bytes(forged))
    forged["tx_id"] = compute_tx_id(forged)
    with pytest.raises(TxError, match="签名"):
        state.apply_tx(forged, clock + 1)


def test_settlement_pays_the_seller_and_moves_the_nft(world) -> None:
    state, seller, first, second, auction, clock = world
    bid(state, first, auction, 5 * COIN, clock + 1)
    bid(state, second, auction, 6 * COIN, clock + 2)
    seller_balance = state.balance_of(seller.address)

    with pytest.raises(RuleError, match="还在进行中"):
        settle(state, second, auction, clock + 5)

    settle(state, second, auction, clock + 121)
    assert state.nfts[auction["token_id"]]["owner"] == second.address
    assert auction["tx_id"] not in state.auctions
    assert state.balance_of(seller.address) == seller_balance + 6 * COIN
    assert state.auction_history[-1]["outcome"] == "sold"


def test_anyone_can_settle_without_a_fee(world) -> None:
    state, _seller, first, _second, auction, clock = world
    bid(state, first, auction, 5 * COIN, clock + 1)
    helper = new_player("helper")
    settle(state, helper, auction, clock + 121)
    assert state.nfts[auction["token_id"]]["owner"] == first.address
    assert state.balance_of(helper.address) == 0

    with pytest.raises(TxError, match="fee 必须是 0"):
        state.apply_tx(
            signed_tx(helper, "nft_auction_settle", auction_id=auction["tx_id"], fee=FEE),
            clock + 122,
        )


def test_settlement_needs_a_bid(world) -> None:
    state, seller, _first, _second, auction, clock = world
    with pytest.raises(RuleError, match="没有人出价"):
        settle(state, seller, auction, clock + 121)


def test_seller_cancels_before_any_bid(world) -> None:
    state, seller, first, _second, auction, clock = world
    cancel = signed_tx(seller, "nft_auction_cancel", auction_id=auction["tx_id"], fee=FEE)
    state.apply_tx(cancel, clock + 1)
    assert auction["tx_id"] not in state.auctions
    assert state.auction_history[-1]["outcome"] == "cancelled"

    again = start_auction(state, seller, auction["token_id"], 5 * COIN, 120, clock + 2)
    bid(state, first, again, 5 * COIN, clock + 3)
    with pytest.raises(RuleError, match="不能取消"):
        state.apply_tx(
            signed_tx(seller, "nft_auction_cancel", auction_id=again["tx_id"], fee=FEE),
            clock + 4,
        )


def test_fixed_price_listing_cannot_overlap_an_auction(world) -> None:
    state, seller, _first, _second, auction, clock = world
    with pytest.raises(RuleError, match="正在拍卖"):
        state.apply_tx(
            signed_tx(seller, "nft_list", token_id=auction["token_id"], price=COIN, fee=FEE),
            clock + 1,
        )


def test_countdown_reaches_zero_exactly_when_settlement_is_allowed(tmp_path) -> None:
    service = make_service(tmp_path)
    seller = make_player(service, "seller")
    bidder = make_player(service, "bidder")
    state = service.chain.state
    fund(state, seller.address, 10 * COIN)
    fund(state, bidder.address, 10 * COIN)
    clock = int(time.time())
    token_id = mint(state, seller)
    auction = start_auction(state, seller, token_id, COIN, 60, clock)
    bid(state, bidder, auction, COIN, clock + 1)
    ends_at = state.auctions[auction["tx_id"]]["ends_at"]

    def seconds_left(now: float) -> int:
        active = service.auctions_view(now)["active"]
        return next(row["seconds_left"] for row in active if row["auction_id"] == auction["tx_id"])

    def settle_tx(at: int) -> dict:
        return signed_tx(bidder, "nft_auction_settle", auction_id=auction["tx_id"], fee=0, timestamp=at)

    # 倒计时和链上规则共用同一个整秒时钟：显示还有截止秒时，结算必须被拒。
    with pytest.raises(RuleError, match="还在进行中"):
        service.mempool.add(settle_tx(ends_at - 1), now=ends_at - 0.5)
    assert seconds_left(ends_at - 0.5) == 1

    assert seconds_left(ends_at) == 0
    service.mempool.add(settle_tx(ends_at), now=ends_at)
    assert service.mempool.stats()["count"] == 1
