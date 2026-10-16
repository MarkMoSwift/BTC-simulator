from __future__ import annotations

import time

from app.core.block import block_hash, meets_bits, merkle_root, target_for_bits
from app.core.mining_pool import split_reward
from tests.conftest import (
    COIN,
    find_nonce,
    header_from_job,
    make_player,
    make_service,
    signed_tx,
)


def submit_valid_share(service, player, job, *, now: float) -> dict:
    nonce = find_nonce(job, job["share_bits"])
    return service.submit_share(player.address, job["job_id"], nonce, now)


def mine_until_block(service, player, *, start_time: float | None = None) -> dict:
    """模拟学生矿工：作业下发的份额难度会被服务器自动调整，这里跟着更新。"""
    now = start_time if start_time is not None else time.time()
    job = service.issue_job(player.address, now)
    share_bits = job["share_bits"]
    for nonce in range(4_000_000):
        if not meets_bits(block_hash(header_from_job(job, nonce)), share_bits):
            continue
        now += 2.0
        result = service.submit_share(player.address, job["job_id"], nonce, now)
        if not result.get("accepted"):
            share_bits = service.pool.miner_stats(player.address, now)["share_bits"]
            continue
        share_bits = max(result["next_share_bits"], share_bits)
        if result.get("block"):
            return result
    raise AssertionError("没有在限定次数内挖到区块")


def test_share_must_be_really_hashed(tmp_path) -> None:
    service = make_service(tmp_path, chain={"initial_difficulty": 16})
    player = make_player(service, "alice")
    job = service.issue_job(player.address)

    forged = next(
        nonce
        for nonce in range(1000)
        if not meets_bits(block_hash(header_from_job(job, nonce)), job["share_bits"])
    )
    result = service.submit_share(player.address, job["job_id"], forged)
    assert result["accepted"] is False
    assert "份额难度" in result["reason"]

    valid = submit_valid_share(service, player, job, now=time.time() + 2)
    assert valid["accepted"] is True
    assert valid["hashrate"] > 0


def test_anyone_can_rebuild_the_header_from_a_job(tmp_path) -> None:
    """矿工协议是公开的：只用作业里的字段就能拼出服务器认可的区块头。"""
    service = make_service(tmp_path, chain={"initial_difficulty": 12})
    player = make_player(service, "alice")
    job = service.issue_job(player.address)
    assert merkle_root(job["tx_ids"]) == job["merkle_root"]

    nonce = find_nonce(job, job["share_bits"])
    result = service.submit_share(player.address, job["job_id"], nonce)
    assert result["accepted"], result["reason"]
    assert result["hash"] == block_hash(header_from_job(job, nonce))


def test_share_uses_the_difficulty_from_its_job(tmp_path) -> None:
    """份额难度只改下一份作业：还在按旧目标搜索的份额照收，并按旧难度记账。"""
    service = make_service(
        tmp_path,
        chain={"initial_difficulty": 20},
        pool={
            "initial_share_bits": 8,
            "share_bits_min": 8,
            "share_bits_max": 12,
            "share_target_seconds": 2.0,
        },
    )
    player = make_player(service, "alice")
    now = time.time()
    job = service.issue_job(player.address, now)
    assert job["share_bits"] == 8
    easy = [nonce for nonce in range(200_000) if meets_bits(block_hash(header_from_job(job, nonce)), 8)]

    first = service.submit_share(player.address, job["job_id"], easy[0], now + 2.0)
    assert first["accepted"], first["reason"]
    assert first["share_bits"] == 8
    assert first["next_share_bits"] == 9

    second = service.submit_share(player.address, job["job_id"], easy[1], now + 2.5)
    assert second["accepted"], second["reason"]
    assert second["share_bits"] == 8
    assert second["work"] == 1 << 8

    third = service.submit_share(player.address, job["job_id"], easy[2], now + 4.0)
    assert third["accepted"], third["reason"]
    assert third["share_bits"] == 8
    assert third["next_share_bits"] == 10

    harder = service.issue_job(player.address, now + 4.5)
    assert harder["share_bits"] == 10
    below_new_target = next(
        nonce for nonce in easy[3:] if not meets_bits(block_hash(header_from_job(harder, nonce)), 10)
    )
    rejected = service.submit_share(player.address, harder["job_id"], below_new_target, now + 6.0)
    assert rejected["accepted"] is False
    assert "份额难度" in rejected["reason"]


def test_slow_miner_gets_an_easier_share_target(tmp_path) -> None:
    """交得稀的同学份额难度往下走，目标始终是每人每份份额两秒。"""
    service = make_service(
        tmp_path,
        chain={"initial_difficulty": 24},
        pool={
            "initial_share_bits": 16,
            "share_bits_min": 8,
            "share_bits_max": 24,
            "share_target_seconds": 2.0,
        },
    )
    player = make_player(service, "bob")
    now = time.time()
    job = service.issue_job(player.address, now)
    first = find_nonce(job, 16)
    second = find_nonce(job, 16, start=first + 1)

    assert service.submit_share(player.address, job["job_id"], first, now + 1.0)["accepted"]
    slow = service.submit_share(player.address, job["job_id"], second, now + 20.0)
    assert slow["accepted"], slow["reason"]
    assert slow["next_share_bits"] < job["share_bits"]


def test_duplicate_and_stale_shares_are_rejected(tmp_path) -> None:
    service = make_service(tmp_path, chain={"initial_difficulty": 16})
    player = make_player(service, "alice")
    job = service.issue_job(player.address)
    now = time.time()
    nonce = find_nonce(job, job["share_bits"])
    assert service.submit_share(player.address, job["job_id"], nonce, now)["accepted"]
    duplicate = service.submit_share(player.address, job["job_id"], nonce, now + 2)
    assert duplicate["accepted"] is False
    assert "提交过" in duplicate["reason"]

    result = mine_until_block(service, player, start_time=now + 10)
    assert result["block"]["height"] == 1
    stale = service.submit_share(player.address, job["job_id"], nonce + 1, now + 40)
    assert stale["accepted"] is False
    assert "作业已经更新" in stale["reason"]


def test_flooding_client_keeps_getting_shares_accepted(tmp_path) -> None:
    """提交速度超过上限时只挡掉多出来的部分：整条通道锁死会让快矿工永远拿不到份额。"""
    service = make_service(
        tmp_path,
        chain={"initial_difficulty": 28, "difficulty_max": 32},
        pool={
            "initial_share_bits": 8,
            "share_bits_min": 8,
            "share_bits_max": 8,
            "max_shares_per_second": 8,
        },
    )
    player = make_player(service, "alice")
    started = time.time()
    job = service.issue_job(player.address, started)
    valid_nonces = [
        nonce
        for nonce in range(80_000)
        if meets_bits(block_hash(header_from_job(job, nonce)), 8)
    ][:200]
    assert len(valid_nonces) == 200

    accepted_at = [
        index * 0.05
        for index, nonce in enumerate(valid_nonces)
        if service.submit_share(player.address, job["job_id"], nonce, started + index * 0.05)["accepted"]
    ]

    assert accepted_at, "洪水式提交后一份都没被接受"
    assert accepted_at[-1] >= 9.0, accepted_at
    assert 60 <= len(accepted_at) <= 100, len(accepted_at)


def test_block_reward_is_shared_by_work_in_the_previous_window(tmp_path) -> None:
    service = make_service(tmp_path, chain={"initial_difficulty": 16})
    alice = make_player(service, "alice")
    bob = make_player(service, "bob")
    now = time.time()
    first = mine_until_block(service, alice, start_time=now)["block"]
    assert [payout["receiver"] for payout in first["payouts"]] == [alice.address]
    assert first["payouts"][0]["amount"] == first["subsidy"]

    job = service.issue_job(bob.address)
    assert submit_valid_share(service, bob, job, now=now + 20)["accepted"]
    second = mine_until_block(service, bob, start_time=now + 30)["block"]
    receivers = {payout["receiver"] for payout in second["payouts"]}
    assert receivers == {alice.address, bob.address}, "第二个区块按上一阶段的工作量分配"
    payments = {payout["receiver"]: payout["amount"] for payout in second["payouts"]}
    assert sum(payments.values()) == second["subsidy"] + second["fees"]
    assert payments[alice.address] > payments[bob.address], "发现者只多拿一份奖励，不能反超全部工作量"
    assert set(service.pool.stats()["payout_window"]) == {bob.address}, "新阶段重新开始累计工作量"


def test_mempool_transaction_is_included_in_next_job(tmp_path) -> None:
    service = make_service(
        tmp_path,
        chain={"initial_difficulty": 12},
        pool={"initial_share_bits": 8, "share_bits_min": 8, "job_refresh_seconds": 1},
    )
    alice = make_player(service, "alice")
    bob = make_player(service, "bob")
    now = time.time()
    mine_until_block(service, alice, start_time=now)

    tx = signed_tx(alice, "transfer", receiver=bob.address, amount=COIN, fee=10_000, timestamp=int(now))
    service.submit_transaction(tx)
    service.pool.tick(service.pool.job.created_at + 2)
    job = service.issue_job(alice.address)
    assert job["tx_count"] == 1

    result = mine_until_block(service, alice)
    block = result["block"]
    assert block["fees"] == 10_000
    assert service.chain.state.balance_of(bob.address) == COIN
    assert service.mempool.stats()["count"] == 0


def test_split_reward_keeps_integer_sum_exact() -> None:
    payouts = split_reward({"a": 1, "b": 3}, 1000, finder="a", bonus_percent=0)
    amounts = {item["receiver"]: item["amount"] for item in payouts}
    assert amounts == {"a": 250, "b": 750}

    with_bonus = split_reward({"a": 1, "b": 1}, 1001, finder="a", bonus_percent=10)
    amounts = {item["receiver"]: item["amount"] for item in with_bonus}
    assert sum(amounts.values()) == 1001
    assert amounts["a"] > amounts["b"]


def test_teacher_difficulty_change_reissues_the_job(tmp_path) -> None:
    service = make_service(tmp_path, chain={"initial_difficulty": 16})
    alice = make_player(service, "alice")
    job = service.issue_job(alice.address)

    service.update_settings({"chain": {"difficulty_override": 10}})
    updated = service.issue_job(alice.address)

    assert updated["job_id"] != job["job_id"], "旧作业已经不符合新难度，必须换一份"
    assert updated["difficulty"] == 10
    assert updated["block_target"] == target_for_bits(10)


def test_teacher_reward_change_reissues_the_job(tmp_path) -> None:
    service = make_service(tmp_path, chain={"initial_difficulty": 16})
    alice = make_player(service, "alice")
    job = service.issue_job(alice.address)

    service.update_settings({"chain": {"initial_reward_coins": 20}})
    updated = service.issue_job(alice.address)

    assert updated["job_id"] != job["job_id"], "旧作业的 coinbase 奖励已经过期，必须换一份"
    assert updated["subsidy"] == 20 * COIN
