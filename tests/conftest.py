from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from app.config import ChainSettings, PoolSettings
from app.core.block import block_hash, meets_bits, merkle_root
from app.core.chain import Blockchain
from app.core.transaction import compute_tx_id, tx_bytes
from app.core.wallet import (
    address_from_public_key,
    generate_private_key,
    public_key_from_private,
    sign_message,
)
from app.runtime import ClassroomService, login_message
from app.storage.sqlite_store import SQLiteStore
from app.units import SAT_PER_COIN

COIN = SAT_PER_COIN


@dataclass
class Player:
    nickname: str
    private_key: str
    public_key: str
    address: str
    token: str


def write_config(
    tmp_path: Path,
    *,
    chain: dict | None = None,
    pool: dict | None = None,
    admin_key: str = "TESTKEY",
) -> Path:
    chain_raw = {**asdict(ChainSettings()), **(chain or {})}
    pool_raw = {**asdict(PoolSettings()), **(pool or {})}
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "classroom_name": "测试课堂",
                "web_host": "127.0.0.1",
                "web_port": 8123,
                "storage_path": "./classroom.db",
                "admin_key": admin_key,
                "chain": chain_raw,
                "pool": pool_raw,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def make_service(
    tmp_path: Path,
    *,
    chain: dict | None = None,
    pool: dict | None = None,
    admin_key: str = "TESTKEY",
) -> ClassroomService:
    defaults = {"initial_difficulty": 12, "difficulty_min": 4, "difficulty_max": 24}
    pool_defaults = {"initial_share_bits": 8, "share_bits_min": 4, "share_target_seconds": 30.0}
    path = write_config(
        tmp_path,
        chain={**defaults, **(chain or {})},
        pool={**pool_defaults, **(pool or {})},
        admin_key=admin_key,
    )
    return ClassroomService(path)


def make_chain(
    tmp_path: Path,
    *,
    chain: dict | None = None,
    db_name: str = "chain.db",
) -> Blockchain:
    settings = ChainSettings(**{**asdict(ChainSettings()), "initial_difficulty": 12, **(chain or {})})
    store = SQLiteStore(tmp_path / db_name)
    return Blockchain(settings, store)


def new_player(nickname: str = "alice") -> Player:
    private_key = generate_private_key()
    public_key = public_key_from_private(private_key)
    return Player(
        nickname=nickname,
        private_key=private_key,
        public_key=public_key,
        address=address_from_public_key(public_key),
        token="",
    )


def make_player(service: ClassroomService, nickname: str = "alice") -> Player:
    player = new_player(nickname)
    begin = service.begin_login(nickname)
    signature = sign_message(player.private_key, login_message(nickname, begin["nonce"]))
    result = service.finish_login(
        nickname, begin["nonce"], player.public_key, signature
    )
    return Player(**{**player.__dict__, "token": result["token"]})


def signed_tx(player: Player, tx_type: str, **fields) -> dict:
    tx = {
        "type": tx_type,
        "sender": player.address,
        "timestamp": int(fields.pop("timestamp", time.time())),
        "public_key": player.public_key,
        **fields,
    }
    tx["tx_id"] = compute_tx_id(tx)
    tx["signature"] = sign_message(player.private_key, tx_bytes(tx))
    return tx


def make_coinbase(chain: Blockchain, height: int, payouts: list[dict], timestamp: int | None = None) -> dict:
    fees = 0
    coinbase = {
        "type": "coinbase",
        "height": height,
        "timestamp": int(timestamp if timestamp is not None else time.time()),
        "subsidy": chain.subsidy(height),
        "fees": fees,
        "finder": payouts[0]["receiver"],
        "payouts": payouts,
    }
    coinbase["tx_id"] = compute_tx_id(coinbase)
    return coinbase


def mine_direct(
    chain: Blockchain,
    finder: str,
    *,
    transactions: list[dict] | None = None,
    timestamp: int | None = None,
    nonce_start: int = 0,
) -> dict:
    """测试用挖矿：按链上规则拼一个区块并暴力搜索 nonce。"""
    transfers = list(transactions or [])
    height = chain.height + 1
    difficulty = chain.difficulty_for(height)
    fees = sum(int(tx["fee"]) for tx in transfers)
    subsidy = chain.subsidy(height)
    coinbase = make_coinbase(
        chain,
        height,
        [{"receiver": finder, "amount": subsidy + fees}],
        timestamp=timestamp,
    )
    coinbase["fees"] = fees
    coinbase["tx_id"] = compute_tx_id(coinbase)
    header = {
        "version": 1,
        "prev_hash": chain.tip_hash,
        "merkle_root": "",
        "timestamp": int(timestamp if timestamp is not None else time.time()),
        "difficulty": difficulty,
        "miner_tag": 1,
        "nonce": nonce_start,
    }
    block = {"header": header, "transactions": [coinbase, *transfers]}
    header["merkle_root"] = merkle_root([tx["tx_id"] for tx in block["transactions"]])
    nonce = nonce_start
    while True:
        header["nonce"] = nonce
        if meets_bits(block_hash(header), difficulty):
            return block
        nonce += 1


def remine(block: dict) -> dict:
    """改写区块内容后重新搜索 nonce，让工作量重新达标，用于验证其它规则。"""
    header = block["header"]
    nonce = 0
    while True:
        header["nonce"] = nonce
        if meets_bits(block_hash(header), int(header["difficulty"])):
            return block
        nonce += 1


def rebuild_block_body(block: dict) -> dict:
    """交易改动后重算 coinbase 交易号与梅克尔根。"""
    coinbase = block["transactions"][0]
    coinbase["tx_id"] = compute_tx_id({key: value for key, value in coinbase.items() if key != "tx_id"})
    block["header"]["merkle_root"] = merkle_root([tx["tx_id"] for tx in block["transactions"]])
    return block


def mine_share(service: ClassroomService, player: Player, *, max_tries: int = 20_000, start: int = 0) -> dict:
    """通过矿池接口提交份额，直到被接受（测试里通常一次就中）。"""
    job = service.issue_job(player.address)
    for nonce in range(start, start + max_tries):
        result = service.submit_share(player.address, job["job_id"], nonce)
        if result.get("accepted"):
            return result
    raise AssertionError("没有在限定次数内找到份额，请调低测试难度")


def header_from_job(job: dict, nonce: int) -> dict:
    """按文档里的 84 字节区块头布局组装 header（模拟学生自己写的矿工）。"""
    return {
        "version": int(job["version"]),
        "prev_hash": job["prev_hash"],
        "merkle_root": job["merkle_root"],
        "timestamp": int(job["timestamp"]),
        "difficulty": int(job["difficulty"]),
        "miner_tag": int(job["miner_tag"]),
        "nonce": int(nonce),
    }


def find_nonce(job: dict, bits: int, *, start: int = 0, max_tries: int = 2_000_000) -> int:
    from app.core.block import block_hash, meets_bits

    for nonce in range(start, start + max_tries):
        if meets_bits(block_hash(header_from_job(job, nonce)), bits):
            return nonce
    raise AssertionError("在限定次数内没有找到满足难度的 nonce")

