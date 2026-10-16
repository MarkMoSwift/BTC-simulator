"""启动课堂模拟器：一台电脑当网络，学生用浏览器当矿机。"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace

import uvicorn

from app.config import VERSION, load_settings, save_settings
from app.runtime import ClassroomService
from app.web.server import create_app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="区块链科普模拟器")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--host", default=None, help="Web 监听地址，默认读配置")
    parser.add_argument("--port", default=None, type=int, help="Web 端口，默认读配置")
    parser.add_argument("--admin-key", default=None, help="覆盖教师口令")
    parser.add_argument("--quiet", action="store_true", help="不打印启动横幅")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = load_settings(args.config)
    if args.host or args.port or args.admin_key:
        settings = replace(
            settings,
            web_host=args.host or settings.web_host,
            web_port=args.port or settings.web_port,
            admin_key=args.admin_key or settings.admin_key,
        )
        save_settings(settings, args.config)

    service = ClassroomService(args.config)
    if not args.quiet:
        print_banner(service)
    uvicorn.run(create_app(service), host=settings.web_host, port=int(settings.web_port), log_level="info")


def print_banner(service: ClassroomService) -> None:
    settings = service.settings
    lines = [
        "",
        f"  区块链科普模拟器 v{VERSION}",
        f"  课堂名称：{settings.classroom_name}",
        "",
        f"  本机打开：  http://127.0.0.1:{settings.web_port}",
    ]
    for address in service.local_addresses():
        lines.append(f"  学生入口：  http://{address}:{settings.web_port}")
    lines.extend(
        [
            f"  投影大屏：  http://127.0.0.1:{settings.web_port}/bigscreen",
            f"  教师控制台：http://127.0.0.1:{settings.web_port}/teacher",
            f"  教师口令：  {settings.admin_key}",
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
