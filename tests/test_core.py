from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path

from app.config import DEFAULT_CONFIG, network_identity
from app.core.block import (
    compute_block_hash,
    difficulty_to_target,
    hash_meets_difficulty,
    hash_meets_target,
    target_prefix_to_hex,
)
from app.core.blockchain import Blockchain
from app.core.mempool import Mempool
from app.core.transaction import create_transfer, validate_transfer_shape_and_signature
from app.core.wallet import generate_wallet
from app.runtime import NodeService
from app.storage.sqlite_store import SQLiteStore


def make_stack(tmp_path: Path):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["difficulty"] = 1
    config["initial_target_prefix"] = None
    config["storage"]["path"] = str(tmp_path / "chain.db")
    store = SQLiteStore(config["storage"]["path"])
    chain = Blockchain(config, store)
    mempool = Mempool(store, chain)
    return config, store, chain, mempool


def block_meets_work(block: dict, block_hash: str) -> bool:
    target = block["header"].get("target")
    if target is not None:
        return hash_meets_target(block_hash, target)
    return hash_meets_difficulty(block_hash, block["header"]["difficulty"])


def mine_block(chain: Blockchain, miner_address: str, timestamp: int | None = None):
    block = chain.create_candidate_block(miner_address, [])
    if timestamp is not None:
        block["header"]["timestamp"] = timestamp
    nonce = 0
    while True:
        block["header"]["nonce"] = nonce
        block_hash = compute_block_hash(block)
        if block_meets_work(block, block_hash):
            return block, block_hash
        nonce += 1


def test_wallet_signature_and_tx_id(tmp_path: Path):
    _config, _store, _chain, _mempool = make_stack(tmp_path)
    sender = generate_wallet("sender")
    receiver = generate_wallet("receiver")
    tx = create_transfer(
        sender=sender.address,
        receiver=receiver.address,
        amount=1.5,
        fee=0.01,
        public_key=sender.public_key,
        private_key=sender.private_key,
    )
    validate_transfer_shape_and_signature(tx)

    tampered = dict(tx)
    tampered["amount"] = 2.0
    try:
        validate_transfer_shape_and_signature(tampered)
    except ValueError as exc:
        assert "tx_id mismatch" in str(exc)
    else:
        raise AssertionError("tampered transaction should be rejected")


def test_hex_target_prefix_supports_fine_grained_work():
    target = target_prefix_to_hex("000b")

    assert target.startswith("000b")
    assert hash_meets_target("000a" + ("f" * 60), target)
    assert not hash_meets_target("000c" + ("0" * 60), target)


def test_mining_reward_and_balance(tmp_path: Path):
    _config, store, chain, _mempool = make_stack(tmp_path)
    miner = generate_wallet("miner")
    store.save_wallet(miner)

    block = chain.create_candidate_block(miner.address, [])
    nonce = 0
    while True:
        block["header"]["nonce"] = nonce
        block_hash = compute_block_hash(block)
        if block_meets_work(block, block_hash):
            break
        nonce += 1

    accepted, message = chain.add_block(block, source="test")
    assert accepted, message
    assert chain.height() == 1
    assert chain.get_balance(miner.address) == 50.0

    store.close()


def test_block_explorer_queries_include_block_and_transactions(tmp_path: Path):
    _config, store, chain, _mempool = make_stack(tmp_path)
    miner = generate_wallet("miner")
    store.save_wallet(miner)

    block = chain.create_candidate_block(miner.address, [])
    nonce = 0
    while True:
        block["header"]["nonce"] = nonce
        block_hash = compute_block_hash(block)
        if block_meets_work(block, block_hash):
            break
        nonce += 1
    accepted, message = chain.add_block(block, source="test")
    assert accepted, message

    summaries = store.list_block_summaries(limit=5)
    assert summaries[0]["height"] == 1
    assert summaries[0]["tx_count"] == 1
    assert summaries[0]["target"] == block["header"]["target"]

    detail = store.get_block_detail(1)
    assert detail is not None
    assert detail["hash"] == summaries[0]["hash"]
    assert detail["tx_count"] == 1
    assert detail["transactions"][0]["type"] == "coinbase"
    assert detail["block"]["header"]["merkle_root"] == detail["merkle_root"]

    by_hash = store.get_block_detail(detail["hash"])
    assert by_hash is not None
    assert by_hash["height"] == 1

    store.close()


def test_longer_replacement_chain_can_reorg_from_genesis(tmp_path: Path):
    config_a, store_a, chain_a, _mempool_a = make_stack(tmp_path / "a")
    config_b, store_b, chain_b, _mempool_b = make_stack(tmp_path / "b")
    miner_a = generate_wallet("miner-a")
    miner_b = generate_wallet("miner-b")
    store_a.save_wallet(miner_a)
    store_b.save_wallet(miner_b)

    def mine_one(chain: Blockchain, miner_address: str):
        block = chain.create_candidate_block(miner_address, [])
        nonce = 0
        while True:
            block["header"]["nonce"] = nonce
            block_hash = compute_block_hash(block)
            if block_meets_work(block, block_hash):
                break
            nonce += 1
        accepted, message = chain.add_block(block, source="test")
        assert accepted, message

    mine_one(chain_a, miner_a.address)
    mine_one(chain_b, miner_b.address)
    mine_one(chain_b, miner_b.address)

    assert chain_a.height() == 1
    assert chain_b.height() == 2
    assert chain_a.tip_hash() != chain_b.tip_hash()

    replacement_blocks = store_b.get_blocks_from_height(0)
    accepted, message = chain_a.replace_with_chain(replacement_blocks, source="test")
    assert accepted, message
    assert chain_a.height() == 2
    assert chain_a.tip_hash() == chain_b.tip_hash()
    assert chain_a.get_balance(miner_a.address) == 0.0
    assert chain_a.get_balance(miner_b.address) == 100.0

    store_a.close()
    store_b.close()


def test_coinbase_tx_id_is_unique_per_height(tmp_path: Path):
    _config, store, chain, _mempool = make_stack(tmp_path)
    miner = generate_wallet("miner")
    store.save_wallet(miner)

    first = chain.create_candidate_block(miner.address, [])
    second = chain.create_candidate_block(miner.address, [])

    assert first["transactions"][0]["height"] == 1
    assert second["transactions"][0]["height"] == 1
    assert first["transactions"][0]["tx_id"] == second["transactions"][0]["tx_id"]

    nonce = 0
    while True:
        first["header"]["nonce"] = nonce
        if block_meets_work(first, compute_block_hash(first)):
            break
        nonce += 1
    accepted, message = chain.add_block(first, source="test")
    assert accepted, message

    next_block = chain.create_candidate_block(miner.address, [])
    assert next_block["transactions"][0]["height"] == 2
    assert next_block["transactions"][0]["tx_id"] != first["transactions"][0]["tx_id"]

    store.close()


def test_block_rejects_non_consensus_difficulty(tmp_path: Path):
    _config, store, chain, _mempool = make_stack(tmp_path)
    miner = generate_wallet("miner")
    store.save_wallet(miner)

    block = chain.create_candidate_block(miner.address, [])
    block["header"]["difficulty"] = 0
    accepted, reason = chain.add_block(block, source="test")

    assert not accepted
    assert "difficulty display mismatch" in reason
    store.close()


def test_auto_difficulty_increases_when_blocks_are_fast(tmp_path: Path):
    config, store, chain, _mempool = make_stack(tmp_path)
    config["difficulty"] = 1
    config["difficulty_adjustment_interval"] = 3
    config["target_block_seconds"] = 60
    miner = generate_wallet("miner")
    store.save_wallet(miner)
    start = int(time.time()) - 600

    for offset in (0, 1, 2):
        block, _block_hash = mine_block(chain, miner.address, timestamp=start + offset)
        accepted, message = chain.add_block(block, source="test")
        assert accepted, message

    assert chain.height() == 3
    previous_target = chain.store.get_block_by_height(3)["target"]
    expected_target = chain.expected_target(4)
    candidate = chain.create_candidate_block(miner.address, [])
    assert int(expected_target, 16) < int(previous_target, 16)
    assert candidate["header"]["target"] == expected_target
    assert candidate["header"]["difficulty"] == 1
    store.close()


def test_auto_difficulty_decreases_when_blocks_are_slow(tmp_path: Path):
    config, store, chain, _mempool = make_stack(tmp_path)
    config["difficulty"] = 2
    config["difficulty_adjustment_interval"] = 3
    config["target_block_seconds"] = 60
    miner = generate_wallet("miner")
    store.save_wallet(miner)
    start = int(time.time()) - 1200

    for offset in (0, 300, 600):
        block, _block_hash = mine_block(chain, miner.address, timestamp=start + offset)
        accepted, message = chain.add_block(block, source="test")
        assert accepted, message

    assert chain.height() == 3
    previous_target = chain.store.get_block_by_height(3)["target"]
    assert int(chain.expected_target(4), 16) > int(previous_target, 16)
    store.close()


def test_adaptive_target_accelerates_after_repeated_fast_windows(tmp_path: Path):
    config, store, chain, _mempool = make_stack(tmp_path)
    config["difficulty_adjustment_interval"] = 3
    config["target_block_seconds"] = 60
    config["target_adjustment_base_bps"] = 1000
    miner = generate_wallet("miner")
    store.save_wallet(miner)
    start = int(time.time()) - 600

    for offset in range(3):
        block, _block_hash = mine_block(chain, miner.address, timestamp=start + offset)
        assert chain.add_block(block, source="test")[0]
    base_target = int(store.get_block_by_height(3)["target"], 16)
    first_target = int(chain.expected_target(4), 16)

    for offset in range(3, 6):
        block, _block_hash = mine_block(chain, miner.address, timestamp=start + offset)
        assert chain.add_block(block, source="test")[0]
    second_target = int(chain.expected_target(7), 16)
    policy = chain.difficulty_policy()

    assert base_target - first_target < first_target - second_target
    assert policy["momentum_streak"] == 2
    assert policy["momentum_adjustment_bps"] == 2000
    store.close()


def test_block_rejects_wrong_auto_adjusted_difficulty(tmp_path: Path):
    config, store, chain, _mempool = make_stack(tmp_path)
    config["difficulty"] = 1
    config["difficulty_adjustment_interval"] = 3
    config["target_block_seconds"] = 60
    miner = generate_wallet("miner")
    store.save_wallet(miner)
    start = int(time.time()) - 600

    for offset in (0, 1, 2):
        block, _block_hash = mine_block(chain, miner.address, timestamp=start + offset)
        accepted, message = chain.add_block(block, source="test")
        assert accepted, message

    wrong = chain.create_candidate_block(miner.address, [])
    wrong["header"]["target"] = difficulty_to_target(1)
    nonce = 0
    while True:
        wrong["header"]["nonce"] = nonce
        if block_meets_work(wrong, compute_block_hash(wrong)):
            break
        nonce += 1

    accepted, reason = chain.add_block(wrong, source="test")
    assert not accepted
    assert "target mismatch" in reason
    store.close()


def test_mempool_rejects_overspend_and_sorts_fees(tmp_path: Path):
    _config, store, chain, mempool = make_stack(tmp_path)
    sender = generate_wallet("sender")
    receiver_a = generate_wallet("receiver-a")
    receiver_b = generate_wallet("receiver-b")
    store.save_wallet(sender)

    reward_block = chain.create_candidate_block(sender.address, [])
    nonce = 0
    while True:
        reward_block["header"]["nonce"] = nonce
        if block_meets_work(reward_block, compute_block_hash(reward_block)):
            break
        nonce += 1
    accepted, message = chain.add_block(reward_block, source="test")
    assert accepted, message

    low_fee = create_transfer(
        sender.address,
        receiver_a.address,
        amount=5,
        fee=0.01,
        public_key=sender.public_key,
        private_key=sender.private_key,
    )
    high_fee = create_transfer(
        sender.address,
        receiver_b.address,
        amount=4,
        fee=0.2,
        public_key=sender.public_key,
        private_key=sender.private_key,
    )
    assert mempool.add_transaction(low_fee)[0]
    assert mempool.add_transaction(high_fee)[0]
    ordered = mempool.ordered()
    assert ordered[0]["tx_id"] == high_fee["tx_id"]

    overspend = create_transfer(
        sender.address,
        receiver_b.address,
        amount=100,
        fee=0,
        public_key=sender.public_key,
        private_key=sender.private_key,
    )
    accepted, reason = mempool.add_transaction(overspend)
    assert not accepted
    assert "insufficient" in reason

    store.close()


def test_reset_to_genesis_keeps_wallet_and_clears_chain_data(tmp_path: Path):
    _config, store, chain, mempool = make_stack(tmp_path)
    miner = generate_wallet("miner")
    receiver = generate_wallet("receiver")
    store.save_wallet(miner)

    reward_block = chain.create_candidate_block(miner.address, [])
    nonce = 0
    while True:
        reward_block["header"]["nonce"] = nonce
        if block_meets_work(reward_block, compute_block_hash(reward_block)):
            break
        nonce += 1
    accepted, message = chain.add_block(reward_block, source="test")
    assert accepted, message

    tx = create_transfer(
        miner.address,
        receiver.address,
        amount=1,
        fee=0.01,
        public_key=miner.public_key,
        private_key=miner.private_key,
    )
    assert mempool.add_transaction(tx)[0]

    genesis_hash = chain.reset_to_genesis()

    assert chain.height() == 0
    assert chain.tip_hash() == genesis_hash
    assert chain.get_balance(miner.address) == 0.0
    assert mempool.stats()["count"] == 0
    assert store.get_default_wallet()["address"] == miner.address
    assert len(store.list_block_summaries(limit=10)) == 1
    store.close()


def test_network_identity_changes_when_consensus_params_change():
    config = copy.deepcopy(DEFAULT_CONFIG)
    first = network_identity(config)
    config["target_block_seconds"] = 30
    second = network_identity(config)

    assert first["network_id"] == "btc-sim-classroom"
    assert first["chain_params_hash"] != second["chain_params_hash"]


def test_peer_metadata_and_classroom_status(tmp_path: Path):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["storage"]["path"] = str(tmp_path / "node.db")
    service = NodeService(config)
    identity = network_identity(config)
    service.store.upsert_peer(
        "192.168.1.20",
        7464,
        "student-a",
        "addr",
        "outbound",
        "connected",
        int(time.time()),
        network_id=identity["network_id"],
        chain_params_hash=identity["chain_params_hash"],
        height=3,
        difficulty=2,
        mining_status="暂停",
        web_port=8000,
    )

    classroom = service.classroom_status()

    assert classroom["self"]["name"] == config["node_name"]
    assert classroom["peers"][0]["name"] == "student-a"
    assert classroom["peers"][0]["height"] == 3
    assert classroom["mismatch_count"] == 0
    asyncio.run(service.shutdown())


def test_classroom_status_counts_only_parameter_mismatches(tmp_path: Path):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["storage"]["path"] = str(tmp_path / "node.db")
    service = NodeService(config)
    service.store.upsert_peer(
        "192.168.1.21",
        7464,
        "offline-student",
        None,
        "outbound",
        "offline",
        int(time.time()),
        mismatch_reason="[Errno 61] Connection refused",
    )
    service.store.upsert_peer(
        "192.168.1.22",
        7464,
        "wrong-class",
        None,
        "outbound",
        "参数不匹配",
        int(time.time()),
        mismatch_reason="network_id mismatch: expected class-a, got class-b",
    )

    classroom = service.classroom_status()

    assert classroom["mismatch_count"] == 1
    asyncio.run(service.shutdown())


def test_service_set_difficulty_persists_to_config(tmp_path: Path):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["node_name"] = "test-node"
    config["listen_port"] = 17464
    config["web_port"] = 18000
    config["storage"]["path"] = str(tmp_path / "node.db")
    config_path = tmp_path / "node.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    config["_config_path"] = str(config_path)
    config["_config_dir"] = str(tmp_path)

    service = NodeService(config)
    result = asyncio.run(service.set_difficulty(8))

    assert result["difficulty"] == 8
    assert service.blockchain.expected_difficulty() == 8
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["difficulty"] == 8

    asyncio.run(service.shutdown())


def test_service_set_target_prefix_persists_to_config(tmp_path: Path):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["storage"]["path"] = str(tmp_path / "node.db")
    config_path = tmp_path / "node.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    config["_config_path"] = str(config_path)
    config["_config_dir"] = str(tmp_path)

    service = NodeService(config)
    result = asyncio.run(service.set_target_prefix("000b"))

    assert result["target_prefix"] == "000b"
    assert service.blockchain.expected_target().startswith("000b")
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["initial_target_prefix"] == "000b"

    asyncio.run(service.shutdown())
