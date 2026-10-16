from __future__ import annotations

import time

from app.core.chain import Blockchain
from app.storage.sqlite_store import SQLiteStore
from tests.conftest import (
    COIN,
    make_chain,
    mine_direct,
    new_player,
    rebuild_block_body,
    remine,
    signed_tx,
)


def test_append_mined_block_updates_state(tmp_path) -> None:
    chain = make_chain(tmp_path)
    miner = new_player("miner")
    block = mine_direct(chain, miner.address)
    accepted, message = chain.append_block(block)
    assert accepted, message
    assert chain.height == 1
    assert chain.state.balance_of(miner.address) == 50 * COIN
    assert chain.state.mining_income[miner.address] == 50 * COIN


def test_tampered_nonce_is_rejected(tmp_path) -> None:
    chain = make_chain(tmp_path)
    miner = new_player("miner")
    block = mine_direct(chain, miner.address)
    block["header"]["nonce"] += 1
    accepted, message = chain.append_block(block)
    assert not accepted
    assert "工作量不足" in message


def test_tampered_merkle_root_is_rejected(tmp_path) -> None:
    chain = make_chain(tmp_path)
    miner = new_player("miner")
    block = mine_direct(chain, miner.address)
    block["header"]["merkle_root"] = "0" * 64
    remine(block)
    accepted, message = chain.append_block(block)
    assert not accepted
    assert "梅克尔根" in message


def test_coinbase_must_pay_exactly_subsidy_plus_fees(tmp_path) -> None:
    chain = make_chain(tmp_path)
    miner = new_player("miner")
    block = mine_direct(chain, miner.address)
    block["transactions"][0]["payouts"][0]["amount"] -= 1
    remine(rebuild_block_body(block))
    accepted, message = chain.append_block(block)
    assert not accepted
    assert "coinbase 应分配" in message


def test_transfer_inside_block_moves_coins(tmp_path) -> None:
    chain = make_chain(tmp_path)
    alice, bob = new_player("alice"), new_player("bob")
    chain.append_block(mine_direct(chain, alice.address))
    tx = signed_tx(alice, "transfer", receiver=bob.address, amount=10 * COIN, fee=10_000)
    accepted, message = chain.append_block(mine_direct(chain, alice.address, transactions=[tx]))
    assert accepted, message
    assert chain.state.balance_of(bob.address) == 10 * COIN
    # 手续费又作为出块奖励回到自己手里，所以只少了转出的 10 币。
    assert chain.state.balance_of(alice.address) == 90 * COIN
    assert chain.height == 2


def test_difficulty_retargets_up_when_blocks_are_fast(tmp_path) -> None:
    chain = make_chain(
        tmp_path,
        chain={"initial_difficulty": 8, "retarget_interval_blocks": 2, "target_block_seconds": 10},
    )
    miner = new_player("miner")
    now = int(time.time())
    chain.append_block(mine_direct(chain, miner.address, timestamp=now))
    chain.append_block(mine_direct(chain, miner.address, timestamp=now + 1))
    assert chain.difficulty_for(3) == 9


def test_difficulty_retargets_down_when_blocks_are_slow(tmp_path) -> None:
    chain = make_chain(
        tmp_path,
        chain={"initial_difficulty": 10, "retarget_interval_blocks": 2, "target_block_seconds": 10},
    )
    miner = new_player("miner")
    now = int(time.time())
    chain.append_block(mine_direct(chain, miner.address, timestamp=now))
    chain.append_block(mine_direct(chain, miner.address, timestamp=now + 120))
    assert chain.difficulty_for(3) == 9


def test_stalled_height_allows_one_bit_easier(tmp_path) -> None:
    chain = make_chain(tmp_path, chain={"initial_difficulty": 10, "stall_seconds": 30})
    miner = new_player("miner")
    now = int(time.time())
    chain.append_block(mine_direct(chain, miner.address, timestamp=now))
    allowed = chain.allowed_difficulties(2, now + 45)
    assert allowed == {10, 9}
    assert chain.allowed_difficulties(2, now + 5) == {10}


def test_chain_replays_from_database(tmp_path) -> None:
    chain = make_chain(tmp_path)
    miner, bob = new_player("miner"), new_player("bob")
    chain.append_block(mine_direct(chain, miner.address))
    tx = signed_tx(miner, "transfer", receiver=bob.address, amount=COIN, fee=10_000)
    chain.append_block(mine_direct(chain, miner.address, transactions=[tx]))
    expected_balance = chain.state.balance_of(bob.address)
    store = SQLiteStore(tmp_path / "chain.db")
    reopened = Blockchain(chain.settings, store)
    assert reopened.height == chain.height
    assert reopened.state.balance_of(bob.address) == expected_balance
    assert reopened.tip_hash == chain.tip_hash
