from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.runtime import NodeService


class SendTransactionRequest(BaseModel):
    receiver: str = Field(min_length=1)
    amount: float = Field(gt=0)
    fee: float = Field(ge=0)
    note: str | None = None


class WalletGenerateRequest(BaseModel):
    name: str = "default"


class PeerRequest(BaseModel):
    ip: str
    port: int


class DifficultyRequest(BaseModel):
    difficulty: int = Field(ge=0, le=255)


def create_web_app(service: NodeService) -> FastAPI:
    app = FastAPI(title="BTC Simulator", version=service.config["version"])
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.on_event("startup")
    async def _startup() -> None:
        await service.start()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await service.shutdown()

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return service.status()

    @app.get("/api/wallet")
    async def wallet() -> dict[str, Any]:
        return service.status()["wallet"]

    @app.post("/api/wallet/generate")
    async def generate_wallet(payload: WalletGenerateRequest) -> dict[str, Any]:
        return service.generate_new_wallet(payload.name)

    @app.post("/api/transactions")
    async def send_transaction(payload: SendTransactionRequest) -> dict[str, Any]:
        accepted, message, tx = await service.create_transaction(
            payload.receiver,
            payload.amount,
            payload.fee,
            payload.note,
        )
        if not accepted:
            raise HTTPException(status_code=400, detail=message)
        return {"tx_id": message, "tx": tx}

    @app.get("/api/mempool")
    async def mempool() -> dict[str, Any]:
        return {"transactions": service.mempool.ordered(), "stats": service.mempool.stats()}

    @app.get("/api/blocks")
    async def blocks(limit: int = 50, offset: int = 0) -> dict[str, Any]:
        return service.list_blocks(limit=limit, offset=offset)

    @app.get("/api/blocks/{identifier}")
    async def block_detail(identifier: str) -> dict[str, Any]:
        detail = service.block_detail(identifier)
        if detail is None:
            raise HTTPException(status_code=404, detail="block not found")
        return detail

    @app.post("/api/mining/start")
    async def start_mining() -> dict[str, Any]:
        started = await service.miner.start()
        return {"started": started, "status": service.miner.status}

    @app.post("/api/mining/stop")
    async def stop_mining() -> dict[str, Any]:
        stopped = await service.miner.stop()
        return {"stopped": stopped, "status": service.miner.status}

    @app.get("/api/peers")
    async def peers() -> dict[str, Any]:
        return {
            "connections": service.p2p.connection_counts(),
            "peers": service.store.list_peers(),
        }

    @app.get("/api/classroom")
    async def classroom() -> dict[str, Any]:
        return service.classroom_status()

    @app.get("/api/security-events")
    async def security_events(limit: int = 100) -> dict[str, Any]:
        return service.security_status(limit=limit)

    @app.post("/api/peers")
    async def connect_peer(payload: PeerRequest) -> dict[str, Any]:
        accepted, message = await service.connect_peer(payload.ip, payload.port)
        if not accepted and "already" not in message and "self" not in message:
            raise HTTPException(status_code=400, detail=message)
        return {"connected": accepted, "message": message}

    @app.post("/api/sync")
    async def sync() -> dict[str, Any]:
        return await service.sync_blocks()

    @app.post("/api/settings/difficulty")
    async def set_difficulty(payload: DifficultyRequest) -> dict[str, Any]:
        try:
            return await service.set_difficulty(payload.difficulty)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/chain/reset")
    async def reset_chain() -> dict[str, Any]:
        return await service.reset_chain()

    @app.websocket("/ws/events")
    async def websocket_events(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(service.status())
                await asyncio.sleep(1)
        except WebSocketDisconnect:
            return

    return app
