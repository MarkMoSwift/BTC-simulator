"""启动课堂模拟器：一台电脑当网络，学生用浏览器当矿机。"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from dataclasses import replace
from pathlib import Path

import uvicorn

from app.config import (
    ConfigError, SESSION_VERSION, VERSION, load_settings, resolve_config_path, save_settings,
    settings_from_raw, tls_options,
)
from app.runtime import ClassroomService
from app.core.wallet import LocalWallet
from app.web.server import create_app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="区块链科普模拟器")
    parser.add_argument("--config", default=None, help="配置文件路径，也可设置 BTC_SIM_CONFIG")
    parser.add_argument("--init-config", action="store_true", help="生成或检查配置后退出，不启动服务器")
    parser.add_argument("--host", default=None, help="Web 监听地址，默认读配置")
    parser.add_argument("--port", default=None, type=int, help="Web 端口，默认读配置")
    parser.add_argument("--quiet", action="store_true", help="不打印启动横幅")
    session = parser.add_mutually_exclusive_group()
    session.add_argument("--create-session", metavar="INVITE", help="新建场次并写入邀请文件")
    session.add_argument("--join-session", metavar="INVITE", help="从邀请文件选择场次")
    parser.add_argument("--entry", action="append", default=[], help="写入邀请文件的显式节点地址，可重复")
    parser.add_argument("--import-wallet-key", metavar="FILE", help="从本机文件导入已有私钥")
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    config_path = resolve_config_path(args.config)
    settings = load_settings(config_path)
    if args.create_session:
        invitation = {
            "network_id": settings.network_id,
            "session_id": secrets.token_hex(16),
            "session_start_time": int(time.time()),
            "peers": args.entry,
        }
        invited = settings_from_raw(invitation)
        with Path(args.create_session).open("x", encoding="utf-8") as output:
            json.dump(invitation, output, ensure_ascii=False, indent=2)
        settings = replace(settings, session_id=invited.session_id, session_start_time=invited.session_start_time)
        save_settings(settings, config_path)
    if args.join_session:
        invited = settings_from_raw(json.loads(Path(args.join_session).read_text(encoding="utf-8-sig")))
        if invited.session() is None:
            raise ConfigError("邀请中缺少场次信息")
        settings = replace(settings, network_id=invited.network_id, session_id=invited.session_id,
                           session_start_time=invited.session_start_time,
                           peers=list(dict.fromkeys([*settings.peers, *invited.peers])))
        save_settings(settings, config_path)
    if args.host or args.port:
        settings = replace(
            settings,
            web_host=args.host or settings.web_host,
            web_port=args.port or settings.web_port,
        )
        save_settings(settings, config_path)
    if args.import_wallet_key:
        LocalWallet(config_path.with_suffix(".wallet.key"), Path(args.import_wallet_key))

    if args.init_config:
        print(f"配置已就绪：{config_path}", flush=True)
        return

    if settings.session() is None:
        print("请选择课堂场次后启动。新建场次使用 --create-session invite.json，"
              "加入已有场次使用 --join-session invite.json。", flush=True)
        return

    tls = tls_options(settings, config_path)
    service = ClassroomService(config_path)
    if not args.quiet:
        print_banner(service)
    uvicorn.run(create_app(service), host=settings.web_host, port=int(settings.web_port),
                log_level="info", proxy_headers=False, **tls)


def print_banner(service: ClassroomService) -> None:
    settings = service.settings
    scheme = "https" if settings.tls_certificate_dir else "http"
    version = SESSION_VERSION if service.chain.session else VERSION
    lines = [
        "",
        f"  区块链科普模拟器 v{version}",
        f"  课堂名称：{settings.classroom_name}",
        f"  节点名称：{settings.node_name}",
        "",
        f"  本机打开：  {scheme}://127.0.0.1:{settings.web_port}",
    ]
    for address in service.local_addresses():
        lines.append(f"  学生入口：  {scheme}://{address}:{settings.web_port}")
    lines.extend(
        [
            f"  投影大屏：  {scheme}://127.0.0.1:{settings.web_port}/bigscreen",
            "",
            "  学生只需在同一局域网内用浏览器打开入口网址，无需安装任何软件。",
            "  按 Ctrl+C 停止服务器。",
            "",
        ]
    )
    print("\n".join(lines), flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
