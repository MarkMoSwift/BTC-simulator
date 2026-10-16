"""HTTP 与 WebSocket 接口。"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import VERSION
from app.core.block import BlockError
from app.core.state import RuleError
from app.core.transaction import TxError
from app.runtime import ClassroomError, ClassroomService

STATIC_DIR = Path(__file__).parent / "static"


class BeginLoginBody(BaseModel):
    nickname: str


class FinishLoginBody(BaseModel):
    nickname: str
    nonce: str
    public_key: str
    signature: str


class AdminLoginBody(BaseModel):
    key: str


class TransactionBody(BaseModel):
    tx: dict[str, Any]


class ShareBody(BaseModel):
    job_id: str
    nonce: int = Field(ge=0, le=0xFFFFFFFF)
    share_bits: int = Field(ge=1, le=128)


class BanBody(BaseModel):
    banned: bool


class AnnounceBody(BaseModel):
    message: str


class SettingsBody(BaseModel):
    chain: dict[str, Any] | None = None
    pool: dict[str, Any] | None = None
    classroom_name: str | None = None


def create_app(service: ClassroomService) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await service.start()
        try:
            yield
        finally:
            await service.shutdown()

    app = FastAPI(title="区块链科普模拟器", version=VERSION, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(ClassroomError)
    async def _classroom_error(_request: Any, exc: ClassroomError) -> Any:
        return _error_response(400, str(exc))

    @app.exception_handler(TxError)
    async def _tx_error(_request: Any, exc: TxError) -> Any:
        return _error_response(400, str(exc))

    @app.exception_handler(RuleError)
    async def _rule_error(_request: Any, exc: RuleError) -> Any:
        return _error_response(400, str(exc))

    @app.exception_handler(BlockError)
    async def _block_error(_request: Any, exc: BlockError) -> Any:
        return _error_response(400, str(exc))

    def token_of(authorization: str | None) -> str | None:
        if not authorization:
            return None
        header = authorization.strip()
        if " " not in header:
            return header
        scheme, _, value = header.partition(" ")
        return value.strip() if scheme.lower() == "bearer" else header

    def student_token(authorization: str | None = Header(default=None)) -> str:
        return token_of(authorization) or ""

    def teacher_token(authorization: str | None = Header(default=None)) -> str:
        token = token_of(authorization) or ""
        service.teacher(token)
        return token

    # ---------- 页面 ----------
    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/bigscreen")
    async def bigscreen() -> FileResponse:
        return FileResponse(STATIC_DIR / "bigscreen.html")

    @app.get("/teacher")
    async def teacher_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "teacher.html")

    # ---------- 公共信息 ----------
    @app.get("/api/public")
    async def public_state() -> dict[str, Any]:
        return {**service.public_state(), **{k: v for k, v in service.live_tick().items() if k != "chain"}}

    @app.get("/api/blocks")
    async def blocks(limit: int = 20, offset: int = 0) -> dict[str, Any]:
        limit = min(max(limit, 1), 100)
        offset = max(offset, 0)
        return {
            "blocks": service.store.list_blocks_descending(limit=limit, offset=offset),
            "height": service.chain.height,
        }

    @app.get("/api/blocks/{identifier}")
    async def block_detail(identifier: str) -> dict[str, Any]:
        detail = service.store.block_detail(identifier)
        if detail is None:
            raise HTTPException(status_code=404, detail="没有找到这个区块")
        return detail

    @app.get("/api/transactions")
    async def transactions(address: str | None = None, limit: int = 20) -> dict[str, Any]:
        return {"transactions": service.store.list_transactions(address=address, limit=min(max(limit, 1), 100))}

    @app.get("/api/transactions/{tx_id}")
    async def transaction_detail(tx_id: str) -> dict[str, Any]:
        record = service.store.get_transaction(tx_id)
        if record is None:
            if tx_id in service.mempool.transactions:
                return {"status": "pending", "tx": service.mempool.transactions[tx_id]}
            raise HTTPException(status_code=404, detail="没有找到这笔交易")
        return {"status": "confirmed", **record}

    @app.get("/api/mempool")
    async def mempool() -> dict[str, Any]:
        return {"transactions": service.mempool.ordered(), "stats": service.mempool.stats()}

    @app.get("/api/packets")
    async def packets() -> dict[str, Any]:
        return service.packets_view()

    @app.get("/api/market")
    async def market() -> dict[str, Any]:
        return service.market_view()

    # ---------- 登录 ----------
    @app.post("/api/auth/begin")
    async def auth_begin(body: BeginLoginBody) -> dict[str, Any]:
        return service.begin_login(body.nickname)

    @app.post("/api/auth/finish")
    async def auth_finish(body: FinishLoginBody) -> dict[str, Any]:
        return service.finish_login(body.nickname, body.nonce, body.public_key, body.signature)

    @app.post("/api/auth/teacher")
    async def auth_teacher(body: AdminLoginBody) -> dict[str, Any]:
        return service.admin_login(body.key)

    # ---------- 学生 ----------
    @app.get("/api/me")
    async def me(token: str = Depends(student_token)) -> dict[str, Any]:
        player = service.student(token)
        return service.player_view_by_record(player)

    @app.post("/api/transactions")
    async def submit_transaction(body: TransactionBody, token: str = Depends(student_token)) -> dict[str, Any]:
        player = service.student(token)
        if body.tx.get("sender") != player["address"]:
            raise ClassroomError("只能提交自己签名的交易")
        return service.submit_transaction(body.tx)

    @app.post("/api/mining/job")
    async def mining_job(token: str = Depends(student_token)) -> dict[str, Any]:
        player = service.student(token)
        return service.issue_job(player["address"])

    @app.post("/api/mining/share")
    async def mining_share(body: ShareBody, token: str = Depends(student_token)) -> dict[str, Any]:
        player = service.student(token)
        return service.submit_share(player["address"], body.job_id, body.nonce, body.share_bits)

    @app.post("/api/packets/{packet_id}/claim-ticket")
    async def claim_ticket(packet_id: str, token: str = Depends(student_token)) -> dict[str, Any]:
        player = service.student(token)
        return service.reserve_claim(player["address"], packet_id)

    # ---------- 教师 ----------
    @app.get("/api/teacher/overview")
    async def teacher_overview(_token: str = Depends(teacher_token)) -> dict[str, Any]:
        return service.teacher_overview()

    @app.post("/api/teacher/settings")
    async def teacher_settings(body: SettingsBody, _token: str = Depends(teacher_token)) -> dict[str, Any]:
        patch: dict[str, Any] = {}
        if body.chain is not None:
            patch["chain"] = body.chain
        if body.pool is not None:
            patch["pool"] = body.pool
        if body.classroom_name is not None:
            patch["classroom_name"] = body.classroom_name
        return service.update_settings(patch)

    @app.post("/api/teacher/reset")
    async def teacher_reset(_token: str = Depends(teacher_token)) -> dict[str, Any]:
        return service.reset_chain()

    @app.post("/api/teacher/players/{nickname}/ban")
    async def teacher_ban(
        nickname: str, body: BanBody, _token: str = Depends(teacher_token)
    ) -> dict[str, Any]:
        return service.set_banned(nickname, body.banned)

    @app.post("/api/teacher/players/{nickname}/reset")
    async def teacher_reset_player(nickname: str, _token: str = Depends(teacher_token)) -> dict[str, Any]:
        return service.reset_player(nickname)

    @app.post("/api/teacher/announce")
    async def teacher_announce(body: AnnounceBody, _token: str = Depends(teacher_token)) -> dict[str, Any]:
        return service.announce(body.message)

    @app.get("/api/teacher/export.csv")
    async def teacher_export(_token: str = Depends(teacher_token)) -> PlainTextResponse:
        return PlainTextResponse(
            service.export_results(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="mining-results.csv"'},
        )

    # ---------- 实时推送 ----------
    @app.websocket("/ws")
    async def websocket_events(socket: WebSocket, token: str | None = Query(default=None)) -> None:
        await socket.accept()
        await service.hub.register(socket)
        try:
            role = service.session(token)["role"]
        except ClassroomError:
            role = "guest"
        await socket.send_json({"type": "hello", "data": {"role": role, **service.live_tick()}})
        try:
            while True:
                await socket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            service.hub.unregister(socket)

    return app


def _error_response(status: int, message: str) -> Any:
    return JSONResponse(status_code=status, content={"detail": message})
