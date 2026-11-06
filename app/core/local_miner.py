"""默认本机计算进程内的有界挖矿循环。"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from app.core.block import block_hash, meets_target

BATCH_SIZE = 400
BATCH_INTERVAL = 0.01
NONCE_LIMIT = 1 << 32


def search(job: dict[str, Any], start: int, stop: int) -> tuple[int | None, int | None]:
    header = {**job}
    sample = None
    for nonce in range(start, stop):
        header["nonce"] = nonce
        digest = block_hash(header)
        if meets_target(digest, job["block_target"]):
            return nonce, sample
        if sample is None and meets_target(digest, job["share_target"]):
            sample = nonce
    return None, sample


class LocalMiner:
    name = "默认本机 CPU 矿工"

    def __init__(self, next_job: Callable[[], dict[str, Any]], submit: Callable[..., dict[str, Any]]):
        self.next_job = next_job
        self.submit = submit
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        if not self.running:
            self.task = asyncio.create_task(self._run(), name="local-wallet-miner")

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def _run(self) -> None:
        previous = ""
        cursor = 0
        sample_at = 0.0
        while True:
            job = self.next_job()
            if not job:
                await asyncio.sleep(0.25)
                continue
            if job["job_id"] != previous:
                previous = job["job_id"]
                cursor = 0
            if cursor == NONCE_LIMIT:
                await asyncio.sleep(0.25)
                continue
            stop = min(cursor + BATCH_SIZE, NONCE_LIMIT)
            winner, sample = await asyncio.to_thread(search, job, cursor, stop)
            cursor = stop
            now = time.monotonic()
            if winner is not None:
                self.submit(job, winner)
            elif sample is not None and now >= sample_at:
                result = self.submit(job, sample)
                if result.get("accepted"):
                    sample_at = now + 1
            await asyncio.sleep(BATCH_INTERVAL)
