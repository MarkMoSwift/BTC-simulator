from __future__ import annotations

import argparse
import os
import sys

import uvicorn

from app.config import (
    is_default_node_name,
    load_config,
    random_node_name,
    save_config,
    sanitize_node_name,
)
from app.runtime import NodeService
from app.web.api import create_web_app

_DEVNULL_STREAMS = []


def ensure_stdio() -> None:
    """PyInstaller --noconsole can leave stdio as None; uvicorn expects streams."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
        _DEVNULL_STREAMS.append(sys.stdout)
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
        _DEVNULL_STREAMS.append(sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BTC simulator MVP web node")
    parser.add_argument("--config", default="config.json", help="Path to config JSON")
    parser.add_argument(
        "--node-name",
        default=None,
        help="Optional custom node name shown in the Web console and P2P HELLO",
    )
    return parser.parse_args()


def main() -> None:
    ensure_stdio()
    args = parse_args()
    config = load_config(args.config)
    if args.node_name:
        config["node_name"] = sanitize_node_name(args.node_name)
        save_config(config)
    elif is_default_node_name(config.get("node_name")):
        config["node_name"] = random_node_name()
        save_config(config)
    service = NodeService(config)
    app = create_web_app(service)
    uvicorn.run(
        app,
        host=config["web_host"],
        port=int(config["web_port"]),
        log_level="info",
    )


if __name__ == "__main__":
    main()
