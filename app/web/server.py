"""HTTP 与 WebSocket 接口。"""
from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import ConfigError, SESSION_VERSION, VERSION
from app.core.block import BlockError
from app.core.state import RuleError
from app.core.transaction import TxError
from app.runtime import ClassroomError, ClassroomService

STATIC_DIR = Path(__file__).parent / "static"
LOOPBACK = {"127.0.0.1", "::1"}


def is_local_wallet_host(request: Request) -> bool:
    return (
        request.client is not None
        and request.client.host in LOOPBACK
        and request.url.hostname in LOOPBACK | {"localhost"}
        and not any(name in request.headers for name in ("forwarded", "x-forwarded-for", "x-forwarded-host"))
    )


def require_local_wallet_request(request: Request) -> None:
    if (not is_local_wallet_host(request)
            or request.headers.get("origin") != f"{request.url.scheme}://{request.url.netloc}"
            or request.headers.get("x-local-wallet") != "1"):
        raise HTTPException(status_code=404)


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


class PeerBody(BaseModel):
    address: str = Field(min_length=1, max_length=2048)


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
        try:
            await service.start()
            yield
        finally:
            await service.shutdown()

    version = SESSION_VERSION if service.chain.session else VERSION
    app = FastAPI(title="区块链科普模拟器", version=version, docs_url=None, redoc_url=None, lifespan=lifespan)
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

    def local_request(request: Request) -> None:
        if service.wallet is None:
            raise HTTPException(status_code=404)
        require_local_wallet_request(request)

    def local_owner(request: Request, token: str) -> None:
        local_request(request)
        if service.student(token)["address"] != service.wallet.address:
            raise HTTPException(status_code=403)

    # ---------- 页面 ----------
    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/bigscreen")
    async def bigscreen() -> FileResponse:
        return FileResponse(STATIC_DIR / "bigscreen.html")

    # ---------- 公共信息 ----------
    def original_path(image_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", image_id):
            raise HTTPException(status_code=404)
        return service.store.db_path.parent / "originals" / image_id

    def image_type(data: bytes) -> str:
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return "image/webp"
        raise HTTPException(status_code=400, detail="原图支持 JPEG、PNG、GIF 或 WebP")

    @app.post("/api/originals/{image_id}")
    async def upload_original(image_id: str, request: Request,
                              token: str = Depends(student_token)) -> dict[str, bool]:
        player = service.student(token)
        path = original_path(image_id)
        # Attach only to an accepted mint belonging to this wallet.
        pending = list(service.mempool.transactions.values())
        heights = {nft["mint_height"] for nft in service.chain.state.nfts.values()
                   if nft.get("thumbnail_id") == image_id}
        for height in heights:
            detail = service.store.block_detail(height)
            if detail:
                pending.extend(detail["transactions"])
        allowed = False
        for tx in pending:
            if tx.get("type") != "nft_mint" or tx.get("sender") != player["address"]:
                continue
            raw = base64.b64decode(tx.get("thumbnail", ""))
            digest = hashlib.sha256(hashlib.sha256(raw).digest()).hexdigest()
            if digest == image_id:
                allowed = True
                break
        if not allowed:
            raise HTTPException(status_code=400, detail="需要先提交当前钱包的照片铸造交易")
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > 8 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="原图不能超过 8 MiB")
        image_type(chunks)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(chunks)
        return {"saved": True}

    @app.api_route("/api/originals/{image_id}", methods=["GET", "HEAD"])
    async def original(image_id: str) -> Response:
        path = original_path(image_id)
        if not path.is_file():
            raise HTTPException(status_code=404)
        with path.open("rb") as source:
            mime = image_type(source.read(16))
        return FileResponse(path, media_type=mime,
                            headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; sandbox"})

    @app.get("/api/public")
    async def public_state() -> dict[str, Any]:
        return {**service.public_state(), **{k: v for k, v in service.live_tick().items() if k != "chain"}}

    @app.get("/api/blocks")
    async def blocks(limit: int = 20, offset: int = 0, before: int | None = None,
                     tip: str | None = None) -> dict[str, Any]:
        limit = min(max(limit, 1), 100)
        offset = max(offset, 0)
        if tip is not None and tip not in service.chain.hashes:
            return {"blocks": [], "height": service.chain.height, "stale": True}
        if before is not None:
            offset = max(service.chain.height - before + 1, 0)
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

    @app.get("/api/thumbnails/{image_id}")
    async def thumbnail(image_id: str) -> Response:
        image = service.chain.state.thumbnails.get(image_id)
        if image is None:
            raise HTTPException(status_code=404)
        return Response(image, media_type="image/jpeg", headers={"X-Content-Type-Options": "nosniff"})

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

    @app.post("/api/local/wallet")
    async def local_wallet(request: Request) -> dict[str, Any]:
        local_request(request)
        miner = service.local_miner
        return {**service.local_login(), "running": bool(miner and miner.running),
                "name": miner.name if miner else "本机矿工"}

    @app.post("/api/local/wallet/status")
    async def local_wallet_status(request: Request, token: str = Depends(student_token)) -> dict[str, Any]:
        local_owner(request, token)
        miner = service.local_miner
        return {"running": bool(miner and miner.running), "name": miner.name if miner else "本机矿工"}

    @app.post("/api/local/peers")
    async def local_peer(request: Request, body: PeerBody,
                         token: str = Depends(student_token)) -> dict[str, Any]:
        local_owner(request, token)
        try:
            return service.connect_peer(body.address)
        except ConfigError as exc:
            raise ClassroomError(str(exc)) from exc

    @app.post("/api/local/wallet/transaction")
    async def local_transaction(request: Request, body: TransactionBody,
                                token: str = Depends(student_token)) -> dict[str, Any]:
        local_owner(request, token)
        return service.local_transaction(body.tx)

    @app.post("/api/local/wallet/mining/{action}")
    async def local_mining(request: Request, action: str,
                           token: str = Depends(student_token)) -> dict[str, Any]:
        local_owner(request, token)
        miner = service.local_miner
        if miner is None or action not in {"start", "stop"}:
            raise HTTPException(status_code=404)
        try:
            if action == "start":
                await miner.start()
            else:
                await miner.stop()
        except ValueError as exc:
            raise ClassroomError(str(exc)) from exc
        return {"running": miner.running, "name": miner.name}

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

    @app.get("/api/results.csv")
    async def classroom_export() -> PlainTextResponse:
        if service.chain.session is None:
            raise HTTPException(status_code=404)
        return PlainTextResponse(
            service.export_results(), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="mining-results.csv"'},
        )

    # ---------- 实时推送 ----------
    @app.websocket("/p2p")
    async def websocket_peer(socket: WebSocket) -> None:
        if service.chain.session is None:
            await socket.close(code=1008)
            return
        await socket.accept()
        try:
            await service.peers.exchange(socket.send_text, socket.receive_text)
        except WebSocketDisconnect:
            return
        except (ValueError, TypeError, KeyError, TimeoutError):
            await socket.close(code=1008)
        else:
            await socket.close()

    @app.websocket("/ws")
    async def websocket_events(socket: WebSocket, token: str | None = Query(default=None)) -> None:
        await socket.accept()
        identity = None
        try:
            session = service.session(token)
            role = session["role"]
            if role == "student" and session.get("client_kind") != "native":
                player = service.student(token)
                origin = ""
                try:
                    source = urlsplit(socket.headers.get("origin", ""))
                    if (source.scheme in {"http", "https"} and source.hostname
                            and source.username is None and source.password is None
                            and source.path in {"", "/"} and not source.query and not source.fragment):
                        origin = f"{source.scheme}://{source.netloc}"
                except ValueError:
                    pass
                identity = {"kind": "browser", "nickname": player["nickname"],
                            "address": player["address"], "source_origin": origin}
        except ClassroomError:
            role = "guest"
        await service.hub.register(socket, identity)
        try:
            await socket.send_json({"type": "hello", "data": {"role": role, **service.live_tick()}})
            while True:
                await socket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            service.hub.unregister(socket)

    @app.websocket("/ws/screen")
    async def websocket_screen(socket: WebSocket) -> None:
        await socket.accept()
        await socket.send_json({"type": "hello", "data": service.screen_tick()})
        await service.screen_hub.register(socket)
        try:
            while True:
                await socket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            service.screen_hub.unregister(socket)

    return app


def _error_response(status: int, message: str) -> Any:
    return JSONResponse(status_code=status, content={"detail": message})
