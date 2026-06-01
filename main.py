from __future__ import annotations

import argparse

import uvicorn

from app.config import load_config
from app.runtime import NodeService
from app.web.api import create_web_app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BTC simulator MVP web node")
    parser.add_argument("--config", default="config.json", help="Path to config JSON")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
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
