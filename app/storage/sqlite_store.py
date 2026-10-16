"""SQLite 存储：区块、交易索引、内存池、学生账号与挖矿统计。"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from app.core.state import TransactionIndex


class SQLiteStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(self.db_path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    # ---------- 区块 ----------
    def insert_block(
        self,
        height: int,
        block_hash: str,
        block: dict[str, Any],
        index: dict[str, TransactionIndex],
    ) -> None:
        header = block["header"]
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO blocks (height, hash, prev_hash, merkle_root, timestamp,
                                    difficulty, miner_tag, nonce, tx_count, block_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(height),
                    block_hash,
                    header["prev_hash"],
                    header["merkle_root"],
                    int(header["timestamp"]),
                    int(header["difficulty"]),
                    int(header["miner_tag"]),
                    int(header["nonce"]),
                    len(block["transactions"]),
                    json.dumps(block, ensure_ascii=True, sort_keys=True),
                ),
            )
            for tx in block["transactions"]:
                record = index[tx["tx_id"]]
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO transactions
                    (tx_id, block_height, type, sender, receiver, amount, fee, timestamp, tx_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tx["tx_id"],
                        int(height),
                        tx["type"],
                        tx.get("sender") or tx.get("finder"),
                        record.counterparty or "",
                        int(record.amount),
                        int(tx.get("fee", 0)),
                        int(tx["timestamp"]),
                        json.dumps(tx, ensure_ascii=True, sort_keys=True),
                    ),
                )
                self.connection.executemany(
                    """
                    INSERT OR IGNORE INTO transaction_participants (tx_id, address)
                    VALUES (?, ?)
                    """,
                    [(tx["tx_id"], address) for address in sorted(record.addresses)],
                )

    def reset_chain(self) -> None:
        with self.lock, self.connection:
            self.connection.execute("DELETE FROM blocks")
            self.connection.execute("DELETE FROM transactions")
            self.connection.execute("DELETE FROM transaction_participants")
            self.connection.execute("DELETE FROM mempool")

    def tip_height(self) -> int | None:
        with self.lock:
            row = self.connection.execute("SELECT MAX(height) AS height FROM blocks").fetchone()
        return None if row is None or row["height"] is None else int(row["height"])

    def get_block(self, identifier: str | int) -> dict[str, Any] | None:
        with self.lock:
            if isinstance(identifier, int) or str(identifier).isdigit():
                row = self.connection.execute(
                    "SELECT * FROM blocks WHERE height = ?", (int(identifier),)
                ).fetchone()
            else:
                row = self.connection.execute(
                    "SELECT * FROM blocks WHERE hash = ?", (str(identifier),)
                ).fetchone()
        return dict(row) if row else None

    def block_detail(self, identifier: str | int) -> dict[str, Any] | None:
        row = self.get_block(identifier)
        if row is None:
            return None
        block = json.loads(row["block_json"])
        return {
            "height": row["height"],
            "hash": row["hash"],
            "prev_hash": row["prev_hash"],
            "merkle_root": row["merkle_root"],
            "timestamp": row["timestamp"],
            "difficulty": row["difficulty"],
            "miner_tag": row["miner_tag"],
            "nonce": row["nonce"],
            "tx_count": row["tx_count"],
            "transactions": block["transactions"],
        }

    def list_blocks_descending(self, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT height, hash, prev_hash, timestamp, difficulty, tx_count, nonce, miner_tag
                FROM blocks ORDER BY height DESC LIMIT ? OFFSET ?
                """,
                (int(limit), int(offset)),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_blocks_ascending(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT block_json FROM blocks ORDER BY height ASC"
            ).fetchall()
        return [json.loads(row["block_json"]) for row in rows]

    # ---------- 交易 ----------
    def get_transaction(self, tx_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM transactions WHERE tx_id = ?", (tx_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_transactions(
        self,
        *,
        address: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM transactions"
        params: list[Any] = []
        if address:
            sql += " WHERE tx_id IN (SELECT tx_id FROM transaction_participants WHERE address = ?)"
            params.append(address)
        sql += " ORDER BY block_height DESC, rowid DESC LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
        with self.lock:
            rows = self.connection.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    # ---------- 内存池 ----------
    def add_mempool(self, tx: dict[str, Any], received_at: int) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO mempool (tx_id, fee, received_at, tx_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    tx["tx_id"],
                    int(tx["fee"]),
                    int(received_at),
                    json.dumps(tx, ensure_ascii=True, sort_keys=True),
                ),
            )

    def list_mempool(self) -> list[tuple[dict[str, Any], int]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT tx_json, received_at FROM mempool ORDER BY received_at ASC"
            ).fetchall()
        return [(json.loads(row["tx_json"]), int(row["received_at"])) for row in rows]

    def remove_mempool(self, tx_ids: list[str]) -> None:
        if not tx_ids:
            return
        with self.lock, self.connection:
            self.connection.executemany(
                "DELETE FROM mempool WHERE tx_id = ?", [(tx_id,) for tx_id in tx_ids]
            )

    def clear_mempool(self) -> None:
        with self.lock, self.connection:
            self.connection.execute("DELETE FROM mempool")

    # ---------- 学生账号 ----------
    def upsert_player(self, nickname: str, address: str, public_key: str, created_at: int) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO players (nickname, address, public_key, created_at, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(nickname) DO UPDATE SET
                    address = excluded.address,
                    public_key = excluded.public_key,
                    last_seen = excluded.last_seen
                """,
                (nickname, address, public_key, int(created_at), int(created_at)),
            )

    def get_player(self, nickname: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM players WHERE nickname = ?", (nickname,)
            ).fetchone()
        return dict(row) if row else None

    def get_player_by_address(self, address: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM players WHERE address = ?", (address,)
            ).fetchone()
        return dict(row) if row else None

    def list_players(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM players ORDER BY created_at ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def touch_player(self, nickname: str, seen_at: int) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE players SET last_seen = ? WHERE nickname = ?", (int(seen_at), nickname)
            )

    def set_player_banned(self, nickname: str, banned: bool) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE players SET banned = ? WHERE nickname = ?", (1 if banned else 0, nickname)
            )

    def record_mining_stats(
        self,
        address: str,
        *,
        work: int,
        shares: int,
        best_share_bits: int,
        blocks_found: int = 0,
    ) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                UPDATE players
                SET total_work = total_work + ?,
                    total_shares = total_shares + ?,
                    best_share_bits = MAX(best_share_bits, ?),
                    blocks_found = blocks_found + ?
                WHERE address = ?
                """,
                (int(work), int(shares), int(best_share_bits), int(blocks_found), address),
            )

    def delete_player(self, nickname: str) -> None:
        with self.lock, self.connection:
            self.connection.execute("DELETE FROM players WHERE nickname = ?", (nickname,))

    def reset_mining_stats(self) -> None:
        """新开一局时清零累计工作量，保留学生的昵称与钱包绑定。"""
        with self.lock, self.connection:
            self.connection.execute(
                """
                UPDATE players
                SET total_work = 0, total_shares = 0, best_share_bits = 0, blocks_found = 0
                """
            )

    # ---------- 统计 ----------
    def stats(self) -> dict[str, Any]:
        with self.lock:
            blocks = self.connection.execute("SELECT COUNT(*) AS n FROM blocks").fetchone()["n"]
            transactions = self.connection.execute(
                "SELECT COUNT(*) AS n FROM transactions"
            ).fetchone()["n"]
            players = self.connection.execute("SELECT COUNT(*) AS n FROM players").fetchone()["n"]
        return {"blocks": int(blocks), "transactions": int(transactions), "players": int(players)}

    def _init_schema(self) -> None:
        with self.lock, self.connection:
            self.connection.executescript(
                """
                PRAGMA journal_mode = WAL;

                CREATE TABLE IF NOT EXISTS blocks (
                    height INTEGER PRIMARY KEY,
                    hash TEXT NOT NULL UNIQUE,
                    prev_hash TEXT NOT NULL,
                    merkle_root TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    difficulty INTEGER NOT NULL,
                    miner_tag INTEGER NOT NULL,
                    nonce INTEGER NOT NULL,
                    tx_count INTEGER NOT NULL,
                    block_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS transactions (
                    tx_id TEXT PRIMARY KEY,
                    block_height INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    sender TEXT,
                    receiver TEXT,
                    amount INTEGER NOT NULL,
                    fee INTEGER NOT NULL,
                    timestamp INTEGER NOT NULL,
                    tx_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_transactions_height ON transactions (block_height DESC);

                CREATE TABLE IF NOT EXISTS transaction_participants (
                    tx_id TEXT NOT NULL,
                    address TEXT NOT NULL,
                    PRIMARY KEY (tx_id, address)
                );
                CREATE INDEX IF NOT EXISTS idx_transaction_participants_address
                    ON transaction_participants (address);

                CREATE TABLE IF NOT EXISTS mempool (
                    tx_id TEXT PRIMARY KEY,
                    fee INTEGER NOT NULL,
                    received_at INTEGER NOT NULL,
                    tx_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS players (
                    nickname TEXT PRIMARY KEY,
                    address TEXT NOT NULL UNIQUE,
                    public_key TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL,
                    banned INTEGER NOT NULL DEFAULT 0,
                    total_work INTEGER NOT NULL DEFAULT 0,
                    total_shares INTEGER NOT NULL DEFAULT 0,
                    best_share_bits INTEGER NOT NULL DEFAULT 0,
                    blocks_found INTEGER NOT NULL DEFAULT 0
                );
                """
            )
