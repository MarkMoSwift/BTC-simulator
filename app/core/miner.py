from __future__ import annotations

import asyncio
import inspect
from typing import Any, Awaitable, Callable

from app.core.block import compute_block_hash, hash_meets_difficulty, hash_meets_target
from app.core.blockchain import Blockchain
from app.core.mempool import Mempool

LogFn = Callable[[str], None]
BlockCallback = Callable[[dict[str, Any], str], Awaitable[None] | None]


class Miner:
    def __init__(
        self,
        blockchain: Blockchain,
        mempool: Mempool,
        log: LogFn | None = None,
        on_mined_block: BlockCallback | None = None,
    ):
        self.blockchain = blockchain
        self.mempool = mempool
        self.log = log or (lambda _message: None)
        self.on_mined_block = on_mined_block
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self.current_nonce = 0
        self.current_hash = ""
        self.status = "未挖矿"

    @property
    def is_mining(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        if self.is_mining:
            return False
        self._stop_event = asyncio.Event()
        self.status = "正在挖矿"
        self._task = asyncio.create_task(self._mine_forever(), name="btc-sim-miner")
        return True

    async def stop(self) -> bool:
        if not self.is_mining:
            self.status = "暂停"
            return False
        self._stop_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=3)
            except asyncio.TimeoutError:
                self._task.cancel()
        self.status = "暂停"
        self.log("Mining paused")
        return True

    async def _emit_mined(self, block: dict[str, Any], block_hash: str) -> None:
        if not self.on_mined_block:
            return
        result = self.on_mined_block(block, block_hash)
        if inspect.isawaitable(result):
            await result

    async def _mine_forever(self) -> None:
        wallet = self.blockchain.store.get_default_wallet()
        if not wallet:
            self.status = "未挖矿"
            self.log("Mining failed: no wallet")
            return

        self.status = "正在挖矿"
        self.log("Mining started")
        max_transfers = max(int(self.blockchain.config["max_block_transactions"]) - 1, 0)

        try:
            while not self._stop_event.is_set():
                start_tip = self.blockchain.tip_hash()
                transfers = self.blockchain.select_transactions_for_block(max_transfers)
                block = self.blockchain.create_candidate_block(wallet["address"], transfers)
                difficulty = int(block["header"]["difficulty"])
                target = block["header"].get("target")
                nonce = 0
                while not self._stop_event.is_set():
                    if self.blockchain.tip_hash() != start_tip:
                        break
                    block["header"]["nonce"] = nonce
                    block_hash = compute_block_hash(block)
                    await asyncio.sleep(0.001)
                    self.current_nonce = nonce
                    self.current_hash = block_hash
                    meets_work = (
                        hash_meets_target(block_hash, target)
                        if target is not None
                        else hash_meets_difficulty(block_hash, difficulty)
                    )
                    if meets_work:
                        accepted, message = self.blockchain.add_block(block, source="miner")
                        if accepted:
                            self.log(
                                f"Mining success: nonce {nonce}, hash {block_hash[:16]}, "
                                f"txs {len(block['transactions'])}"
                            )
                            await self._emit_mined(block, block_hash)
                        else:
                            self.log(f"Mined block discarded: {message}")
                        await asyncio.sleep(0)
                        break
                    nonce += 1
                    if nonce % 1000 == 0:
                        await asyncio.sleep(0)
        finally:
            self.status = "暂停" if self._stop_event.is_set() else "未挖矿"
