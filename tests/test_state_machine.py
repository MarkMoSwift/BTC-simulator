from __future__ import annotations

import time

import pytest

from app.config import ChainSettings
from app.core.redpacket import derive_amounts
from app.core.state import ChainState, RuleError
from app.core.transaction import compute_tx_id
from tests.conftest import new_player, signed_tx

FEE = 10_000
COIN = 100_000_000


def settings(**overrides) -> ChainSettings:
    return ChainSettings(**{"min_fee_sat": FEE, "max_nfts_per_player": 3, **overrides})


def fund(state: ChainState, address: str, amount: int) -> None:
    height = state.height + 1
    timestamp = int(time.time())
    coinbase = {
        "type": "coinbase",
        "height": height,
        "timestamp": timestamp,
        "subsidy": amount,
        "fees": 0,
        "finder": address,
        "payouts": [{"receiver": address, "amount": amount}],
    }
    coinbase["tx_id"] = compute_tx_id(coinbase)
    state.apply_block({"header": {"timestamp": timestamp}, "transactions": [coinbase]}, height, prevalidated=True)


@pytest.fixture
def state() -> ChainState:
    return ChainState(settings())


def test_transfer_moves_coins_and_charges_fee(state: ChainState) -> None:
    alice, bob = new_player("alice"), new_player("bob")
    fund(state, alice.address, 100 * COIN)
    tx = signed_tx(alice, "transfer", receiver=bob.address, amount=10 * COIN, fee=FEE)
    state.apply_tx(tx, int(time.time()))
    assert state.balance_of(alice.address) == 90 * COIN - FEE
    assert state.balance_of(bob.address) == 10 * COIN


def test_transfer_rejects_overspend_and_replay(state: ChainState) -> None:
    alice, bob = new_player("alice"), new_player("bob")
    fund(state, alice.address, COIN)
    with pytest.raises(RuleError, match="余额不足"):
        state.apply_tx(signed_tx(alice, "transfer", receiver=bob.address, amount=2 * COIN, fee=FEE), int(time.time()))
    tx = signed_tx(alice, "transfer", receiver=bob.address, amount=COIN // 2, fee=FEE)
    state.apply_tx(tx, int(time.time()))
    with pytest.raises(RuleError, match="已经上链"):
        state.apply_tx(tx, int(time.time()))


def test_fee_below_consensus_minimum_is_rejected(state: ChainState) -> None:
    alice, bob = new_player("alice"), new_player("bob")
    fund(state, alice.address, COIN)
    with pytest.raises(RuleError, match="手续费"):
        state.apply_tx(signed_tx(alice, "transfer", receiver=bob.address, amount=COIN // 2, fee=0), int(time.time()))


def test_nft_mint_list_buy_lifecycle(state: ChainState) -> None:
    alice, bob = new_player("alice"), new_player("bob")
    fund(state, alice.address, 100 * COIN)
    fund(state, bob.address, 100 * COIN)

    mint = signed_tx(alice, "nft_mint", name="创世徽章", seed="a1b2c3d4e5f60718", fee=FEE)
    state.apply_tx(mint, int(time.time()))
    token_id = mint["tx_id"]
    assert state.nfts[token_id]["owner"] == alice.address

    state.apply_tx(signed_tx(alice, "nft_list", token_id=token_id, price=5 * COIN, fee=FEE), int(time.time()))
    assert state.listings[token_id]["price"] == 5 * COIN

    with pytest.raises(RuleError, match="不能购买自己"):
        state.apply_tx(signed_tx(alice, "nft_buy", token_id=token_id, fee=FEE), int(time.time()))

    buyer_balance = state.balance_of(bob.address)
    state.apply_tx(signed_tx(bob, "nft_buy", token_id=token_id, fee=FEE), int(time.time()))
    assert state.nfts[token_id]["owner"] == bob.address
    assert token_id not in state.listings
    assert state.balance_of(bob.address) == buyer_balance - 5 * COIN - FEE
    assert state.balance_of(alice.address) == 100 * COIN - FEE * 2 + 5 * COIN


def test_nft_unlist_requires_owner(state: ChainState) -> None:
    alice, bob = new_player("alice"), new_player("bob")
    fund(state, alice.address, 100 * COIN)
    fund(state, bob.address, 100 * COIN)
    mint = signed_tx(alice, "nft_mint", name="头盔", seed="0011223344556677", fee=FEE)
    state.apply_tx(mint, int(time.time()))
    state.apply_tx(signed_tx(alice, "nft_list", token_id=mint["tx_id"], price=COIN, fee=FEE), int(time.time()))
    with pytest.raises(RuleError, match="没有由你上架"):
        state.apply_tx(signed_tx(bob, "nft_unlist", token_id=mint["tx_id"], fee=FEE), int(time.time()))


def test_nft_mint_limit_is_enforced(state: ChainState) -> None:
    alice = new_player("alice")
    fund(state, alice.address, 100 * COIN)
    for index in range(3):
        state.apply_tx(
            signed_tx(alice, "nft_mint", name=f"藏品{index}", seed=f"{index:016x}", fee=FEE),
            int(time.time()),
        )
    with pytest.raises(RuleError, match="最多铸造"):
        state.apply_tx(signed_tx(alice, "nft_mint", name="再来一个", seed="f" * 16, fee=FEE), int(time.time()))


def test_packet_split_is_deterministic_and_sums_to_total(state: ChainState) -> None:
    alice = new_player("alice")
    fund(state, alice.address, 100 * COIN)
    create = signed_tx(alice, "packet_create", total=10 * COIN, count=4, note="新年快乐", fee=FEE)
    state.apply_tx(create, int(time.time()))
    packet = state.packets[create["tx_id"]]
    amounts = derive_amounts(create["tx_id"], 10 * COIN, 4)
    assert sum(amounts) == 10 * COIN
    assert all(amount >= 10_000 for amount in amounts)
    assert state.balance_of(alice.address) == 90 * COIN - FEE
    assert packet["remaining"] == 10 * COIN


def test_packet_claim_transfers_locked_coins(state: ChainState) -> None:
    alice, bob, carol = new_player("alice"), new_player("bob"), new_player("carol")
    fund(state, alice.address, 100 * COIN)
    fund(state, bob.address, FEE)
    create = signed_tx(alice, "packet_create", total=10 * COIN, count=3, fee=FEE)
    clock = int(time.time())
    state.apply_tx(create, clock)
    packet_id = create["tx_id"]
    expected = derive_amounts(packet_id, 10 * COIN, 3)[1]

    state.apply_tx(signed_tx(bob, "packet_claim", packet_id=packet_id, index=1, fee=FEE), clock)
    assert state.balance_of(bob.address) == expected
    with pytest.raises(RuleError, match="已经被领走"):
        state.apply_tx(signed_tx(carol, "packet_claim", packet_id=packet_id, index=1, fee=FEE), clock)
    with pytest.raises(RuleError, match="余额不足"):
        state.apply_tx(signed_tx(carol, "packet_claim", packet_id=packet_id, index=2, fee=FEE), clock)


def test_packet_expires_and_sender_can_refund(state: ChainState) -> None:
    alice, bob = new_player("alice"), new_player("bob")
    fund(state, alice.address, 100 * COIN)
    create = signed_tx(alice, "packet_create", total=10 * COIN, count=2, fee=FEE)
    clock = int(time.time())
    state.apply_tx(create, clock)
    packet_id = create["tx_id"]
    expired = clock + state.settings.packet_expiry_seconds + 1

    with pytest.raises(RuleError, match="已过期"):
        state.apply_tx(signed_tx(bob, "packet_claim", packet_id=packet_id, index=0, fee=FEE), expired)
    with pytest.raises(RuleError, match="只有发红包的人"):
        state.apply_tx(signed_tx(bob, "packet_refund", packet_id=packet_id, fee=FEE), expired)
    with pytest.raises(RuleError, match="还没有过期"):
        state.apply_tx(signed_tx(alice, "packet_refund", packet_id=packet_id, fee=FEE), clock)

    state.apply_tx(signed_tx(alice, "packet_refund", packet_id=packet_id, fee=FEE), expired)
    assert state.balance_of(alice.address) == 100 * COIN - FEE * 2
    assert state.packets[packet_id]["refunded"] is True
