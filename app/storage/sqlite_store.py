from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from app.core.transaction import estimate_transaction_bytes


class SQLiteStore:
    """Small synchronized SQLite wrapper for the simulator.

    MVP stores private keys in plaintext to keep the teaching flow simple.
    Never reuse this code for real assets.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def init_schema(self) -> None:
        with self.lock, self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS wallet_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    address TEXT NOT NULL UNIQUE,
                    public_key TEXT NOT NULL,
                    private_key_plain_for_mvp TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    is_default INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS blocks (
                    height INTEGER PRIMARY KEY,
                    hash TEXT NOT NULL UNIQUE,
                    prev_hash TEXT NOT NULL,
                    merkle_root TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    difficulty INTEGER NOT NULL,
                    target TEXT,
                    nonce INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    block_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS transactions (
                    tx_id TEXT PRIMARY KEY,
                    block_hash TEXT NOT NULL,
                    type TEXT NOT NULL,
                    sender TEXT,
                    receiver TEXT NOT NULL,
                    amount REAL NOT NULL,
                    fee REAL NOT NULL,
                    timestamp INTEGER NOT NULL,
                    tx_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS mempool (
                    tx_id TEXT PRIMARY KEY,
                    sender TEXT NOT NULL,
                    receiver TEXT NOT NULL,
                    amount REAL NOT NULL,
                    fee REAL NOT NULL,
                    timestamp INTEGER NOT NULL,
                    tx_json TEXT NOT NULL,
                    received_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS peers (
                    ip TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    name TEXT,
                    address TEXT,
                    direction TEXT,
                    status TEXT,
                    last_seen INTEGER,
                    network_id TEXT,
                    chain_params_hash TEXT,
                    height INTEGER,
                    difficulty INTEGER,
                    target TEXT,
                    mining_status TEXT,
                    web_port INTEGER,
                    mismatch_reason TEXT,
                    PRIMARY KEY (ip, port)
                );
                """
            )
            self._ensure_columns(
                "blocks",
                {
                    "target": "TEXT",
                },
            )
            self._ensure_columns(
                "peers",
                {
                    "network_id": "TEXT",
                    "chain_params_hash": "TEXT",
                    "height": "INTEGER",
                    "difficulty": "INTEGER",
                    "target": "TEXT",
                    "mining_status": "TEXT",
                    "web_port": "INTEGER",
                    "mismatch_reason": "TEXT",
                },
            )

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = {
            str(row["name"])
            for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, definition in columns.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def save_wallet(self, wallet: Any, make_default: bool = True) -> None:
        with self.lock, self.conn:
            if make_default:
                self.conn.execute("UPDATE wallet_keys SET is_default = 0")
            self.conn.execute(
                """
                INSERT OR REPLACE INTO wallet_keys
                (name, address, public_key, private_key_plain_for_mvp, created_at, is_default)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    wallet.name,
                    wallet.address,
                    wallet.public_key,
                    wallet.private_key,
                    wallet.created_at,
                    1 if make_default else int(wallet.is_default),
                ),
            )

    def get_default_wallet(self) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                """
                SELECT name, address, public_key, private_key_plain_for_mvp AS private_key,
                       created_at, is_default
                FROM wallet_keys
                WHERE is_default = 1
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            return dict(row) if row else None

    def list_wallets(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT name, address, public_key, private_key_plain_for_mvp AS private_key,
                       created_at, is_default
                FROM wallet_keys
                ORDER BY id DESC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def insert_block(self, height: int, block_hash: str, block: dict[str, Any]) -> None:
        header = block["header"]
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO blocks
                (height, hash, prev_hash, merkle_root, timestamp, difficulty, target, nonce, version, block_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    height,
                    block_hash,
                    header["prev_hash"],
                    header["merkle_root"],
                    header["timestamp"],
                    header["difficulty"],
                    header.get("target"),
                    header["nonce"],
                    header["version"],
                    json.dumps(block, ensure_ascii=True, sort_keys=True),
                ),
            )
            for tx in block.get("transactions", []):
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO transactions
                    (tx_id, block_hash, type, sender, receiver, amount, fee, timestamp, tx_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tx["tx_id"],
                        block_hash,
                        tx["type"],
                        tx.get("sender"),
                        tx["receiver"],
                        float(tx["amount"]),
                        float(tx.get("fee", 0.0)),
                        int(tx["timestamp"]),
                        json.dumps(tx, ensure_ascii=True, sort_keys=True),
                    ),
                )

    def get_tip(self) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM blocks ORDER BY height DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def get_block_by_hash(self, block_hash: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM blocks WHERE hash = ?", (block_hash,)
            ).fetchone()
            return dict(row) if row else None

    def get_block_by_height(self, height: int) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM blocks WHERE height = ?", (int(height),)
            ).fetchone()
            return dict(row) if row else None

    def has_block_hash(self, block_hash: str) -> bool:
        return self.get_block_by_hash(block_hash) is not None

    def get_block_json_by_hash(self, block_hash: str) -> dict[str, Any] | None:
        row = self.get_block_by_hash(block_hash)
        return json.loads(row["block_json"]) if row else None

    def list_block_summaries(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT
                    b.height,
                    b.hash,
                    b.prev_hash,
                    b.merkle_root,
                    b.timestamp,
                    b.difficulty,
                    b.target,
                    b.nonce,
                    b.version,
                    COUNT(t.tx_id) AS tx_count,
                    COALESCE(SUM(t.fee), 0) AS total_fees
                FROM blocks b
                LEFT JOIN transactions t ON t.block_hash = b.hash
                GROUP BY b.height
                ORDER BY b.height DESC
                LIMIT ? OFFSET ?
                """,
                (int(limit), int(offset)),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_block_detail(self, identifier: str | int) -> dict[str, Any] | None:
        if isinstance(identifier, int) or str(identifier).isdigit():
            row = self.get_block_by_height(int(identifier))
        else:
            row = self.get_block_by_hash(str(identifier))
        if not row:
            return None

        block = json.loads(row["block_json"])
        with self.lock:
            tx_rows = self.conn.execute(
                """
                SELECT tx_json
                FROM transactions
                WHERE block_hash = ?
                ORDER BY rowid ASC
                """,
                (row["hash"],),
            ).fetchall()
        transactions = [json.loads(tx_row["tx_json"]) for tx_row in tx_rows]
        if len(transactions) != len(block.get("transactions", [])):
            transactions = block.get("transactions", [])

        return {
            **dict(row),
            "transactions": transactions,
            "tx_count": len(transactions),
            "total_fees": round(
                sum(float(tx.get("fee", 0.0)) for tx in transactions if tx.get("type") != "coinbase"),
                8,
            ),
            "block": block,
        }

    def get_blocks_from_height(self, from_height: int, limit: int = 10000) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT block_json FROM blocks WHERE height >= ? ORDER BY height ASC LIMIT ?",
                (int(from_height), int(limit)),
            ).fetchall()
            return [json.loads(row["block_json"]) for row in rows]

    def replace_chain(self, blocks: list[dict[str, Any]], block_hashes: list[str]) -> None:
        if len(blocks) != len(block_hashes):
            raise ValueError("blocks and hashes length mismatch")
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM blocks")
            self.conn.execute("DELETE FROM transactions")
            self.conn.execute("DELETE FROM mempool")
            for height, (block, block_hash) in enumerate(zip(blocks, block_hashes)):
                self.insert_block(height, block_hash, block)

    def is_tx_confirmed(self, tx_id: str) -> bool:
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM transactions WHERE tx_id = ? LIMIT 1", (tx_id,)
            ).fetchone()
            return row is not None

    def confirmed_balance(self, address: str) -> float:
        with self.lock:
            income = self.conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS value FROM transactions WHERE receiver = ?",
                (address,),
            ).fetchone()["value"]
            spend = self.conn.execute(
                """
                SELECT COALESCE(SUM(amount + fee), 0) AS value
                FROM transactions
                WHERE sender = ?
                """,
                (address,),
            ).fetchone()["value"]
            return round(float(income) - float(spend), 8)

    def add_mempool_transaction(self, tx: dict[str, Any], received_at: int) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO mempool
                (tx_id, sender, receiver, amount, fee, timestamp, tx_json, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tx["tx_id"],
                    tx["sender"],
                    tx["receiver"],
                    float(tx["amount"]),
                    float(tx["fee"]),
                    int(tx["timestamp"]),
                    json.dumps(tx, ensure_ascii=True, sort_keys=True),
                    int(received_at),
                ),
            )

    def is_tx_in_mempool(self, tx_id: str) -> bool:
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM mempool WHERE tx_id = ? LIMIT 1", (tx_id,)
            ).fetchone()
            return row is not None

    def list_mempool_transactions(self, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT tx_json FROM mempool ORDER BY fee DESC, timestamp ASC"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (int(limit),)
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
            return [json.loads(row["tx_json"]) for row in rows]

    def pending_outgoing(self, address: str, exclude_tx_id: str | None = None) -> float:
        sql = "SELECT COALESCE(SUM(amount + fee), 0) AS value FROM mempool WHERE sender = ?"
        params: list[Any] = [address]
        if exclude_tx_id:
            sql += " AND tx_id != ?"
            params.append(exclude_tx_id)
        with self.lock:
            row = self.conn.execute(sql, tuple(params)).fetchone()
            return round(float(row["value"]), 8)

    def mempool_stats(self) -> dict[str, int]:
        txs = self.list_mempool_transactions()
        return {
            "count": len(txs),
            "bytes": sum(estimate_transaction_bytes(tx) for tx in txs),
        }

    def remove_mempool_transactions(self, tx_ids: list[str]) -> None:
        if not tx_ids:
            return
        with self.lock, self.conn:
            self.conn.executemany("DELETE FROM mempool WHERE tx_id = ?", [(tx_id,) for tx_id in tx_ids])

    def upsert_peer(
        self,
        ip: str,
        port: int,
        name: str | None,
        address: str | None,
        direction: str | None,
        status: str,
        last_seen: int,
        network_id: str | None = None,
        chain_params_hash: str | None = None,
        height: int | None = None,
        difficulty: int | None = None,
        target: str | None = None,
        mining_status: str | None = None,
        web_port: int | None = None,
        mismatch_reason: str | None = None,
    ) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO peers (
                    ip, port, name, address, direction, status, last_seen,
                    network_id, chain_params_hash, height, difficulty,
                    target, mining_status, web_port, mismatch_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ip, port) DO UPDATE SET
                    name = excluded.name,
                    address = excluded.address,
                    direction = excluded.direction,
                    status = excluded.status,
                    last_seen = excluded.last_seen,
                    network_id = excluded.network_id,
                    chain_params_hash = excluded.chain_params_hash,
                    height = excluded.height,
                    difficulty = excluded.difficulty,
                    target = excluded.target,
                    mining_status = excluded.mining_status,
                    web_port = excluded.web_port,
                    mismatch_reason = excluded.mismatch_reason
                """,
                (
                    ip,
                    int(port),
                    name,
                    address,
                    direction,
                    status,
                    int(last_seen),
                    network_id,
                    chain_params_hash,
                    height,
                    difficulty,
                    target,
                    mining_status,
                    web_port,
                    mismatch_reason,
                ),
            )

    def list_peers(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT ip, port, name, address, direction, status, last_seen,
                       network_id, chain_params_hash, height, difficulty,
                       target, mining_status, web_port, mismatch_reason
                FROM peers
                ORDER BY ip, port
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def delete_peer(self, ip: str, port: int) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "DELETE FROM peers WHERE ip = ? AND port = ?",
                (ip, int(port)),
            )
