from __future__ import annotations

import argparse
import copy
import json
import re
import socket
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import DEFAULT_CONFIG
from app.network.address import (
    configured_listen_hosts,
    format_host_port,
    format_url_host,
    parse_host_port,
)


def parse_peer(value: str) -> list[Any]:
    try:
        host, port = parse_host_port(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "peer must use host:port, for example 192.168.1.23:7464 or [2001:db8::10]:7464"
        ) from exc
    return [host, port]


def sanitize_name(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip()).strip("-")
    return cleaned or "node"


def detect_lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def detect_lan_ipv6() -> str | None:
    if not socket.has_ipv6:
        return None
    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    try:
        sock.connect(("2001:4860:4860::8888", 80, 0, 0))
        return str(sock.getsockname()[0])
    except OSError:
        return None
    finally:
        sock.close()


def prompt(default: str, label: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def build_config(
    *,
    node_name: str,
    listen_ip: str,
    listen_port: int,
    web_host: str,
    web_port: int,
    peers: list[list[Any]],
    enable_ipv6: bool = True,
    advertise_ip: str | None = None,
    advertise_ipv6: str | None = None,
    storage_path: str | None = None,
) -> dict[str, Any]:
    config = copy.deepcopy(DEFAULT_CONFIG)
    safe_name = sanitize_name(node_name)
    config["node_name"] = safe_name
    config["listen_ip"] = listen_ip
    config["enable_ipv6"] = bool(enable_ipv6)
    config["advertise_ip"] = advertise_ip or None
    config["advertise_ipv6"] = advertise_ipv6 or None
    config["listen_port"] = int(listen_port)
    config["web_host"] = web_host
    config["web_port"] = int(web_port)
    config["servers"] = peers
    config["storage"]["path"] = storage_path or f"./data/{safe_name}.db"
    return config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Initialize a BTC simulator node config for local or LAN networks."
    )
    parser.add_argument("--config", default=None, help="Output config path, for example config_alice.json")
    parser.add_argument("--name", default=None, help="Node name shown in the Web console and P2P HELLO")
    parser.add_argument("--network-id", default=None, help="Classroom network id shared by all nodes")
    parser.add_argument("--listen-ip", default="0.0.0.0", help="P2P bind address")
    parser.add_argument(
        "--ipv6",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable IPv6 P2P listening; use --no-ipv6 to disable it",
    )
    parser.add_argument("--advertise-ip", default=None, help="IPv4 address shared with other nodes")
    parser.add_argument("--advertise-ipv6", default=None, help="IPv6 address shared with other nodes")
    parser.add_argument("--listen-port", type=int, default=7464, help="P2P port")
    parser.add_argument("--web-host", default="127.0.0.1", help="Web console bind address")
    parser.add_argument("--web-port", type=int, default=8000, help="Web console port")
    parser.add_argument("--storage", default=None, help="SQLite database path")
    parser.add_argument(
        "--peer",
        action="append",
        default=[],
        type=parse_peer,
        help="Seed peer in host:port format. Repeat for multiple peers.",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite an existing config file")
    parser.add_argument("--no-prompt", action="store_true", help="Do not ask interactive questions")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    lan_ip = detect_lan_ip()
    lan_ipv6 = detect_lan_ipv6()
    default_name = sanitize_name(socket.gethostname())

    if args.no_prompt:
        node_name = sanitize_name(args.name or default_name)
        network_id = args.network_id
        config_path = Path(args.config or f"config_{node_name}.json")
        listen_ip = args.listen_ip
        enable_ipv6 = args.ipv6
        advertise_ip = args.advertise_ip
        advertise_ipv6 = args.advertise_ipv6
        listen_port = args.listen_port
        web_host = args.web_host
        web_port = args.web_port
        peers = args.peer
    else:
        print("BTC Simulator node initialization")
        print(f"Detected LAN IP: {lan_ip}")
        if lan_ipv6:
            print(f"Detected LAN IPv6: {lan_ipv6}")
        node_name = sanitize_name(prompt(args.name or default_name, "Node name"))
        network_id = prompt(args.network_id or DEFAULT_CONFIG["network_id"], "Network ID")
        default_config = args.config or f"config_{node_name}.json"
        config_path = Path(prompt(default_config, "Config file"))
        listen_ip = prompt(args.listen_ip, "P2P bind IP")
        enable_ipv6 = args.ipv6
        advertise_ip = prompt(args.advertise_ip or lan_ip, "Advertised IPv4")
        advertise_ipv6 = prompt(args.advertise_ipv6 or lan_ipv6 or "", "Advertised IPv6")
        listen_port = int(prompt(str(args.listen_port), "P2P port"))
        web_host = prompt(args.web_host, "Web bind IP")
        web_port = int(prompt(str(args.web_port), "Web port"))
        peers = list(args.peer)
        seed = prompt("", "Seed peer host:port, empty if this is the first node")
        if seed:
            peers.append(parse_peer(seed))

    if config_path.exists() and not args.force:
        raise SystemExit(f"{config_path} already exists. Use --force to overwrite it.")

    config = build_config(
        node_name=node_name,
        listen_ip=listen_ip,
        listen_port=listen_port,
        web_host=web_host,
        web_port=web_port,
        peers=peers,
        enable_ipv6=enable_ipv6,
        advertise_ip=advertise_ip,
        advertise_ipv6=advertise_ipv6,
        storage_path=args.storage,
    )
    if network_id:
        config["network_id"] = network_id
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    shared_ipv4 = config.get("advertise_ip") or lan_ip
    shared_ipv6 = config.get("advertise_ipv6") or lan_ipv6
    if web_host == "0.0.0.0":
        local_web_host = "127.0.0.1"
        display_host = shared_ipv4
    elif web_host == "::":
        local_web_host = "::1"
        display_host = shared_ipv6 or "::1"
    else:
        local_web_host = web_host
        display_host = web_host
    print()
    print(f"Config written: {config_path}")
    print("Start this node with:")
    print(f"  python main.py --config {config_path}")
    print()
    print("Open the Web console:")
    print(f"  Local: http://{format_url_host(local_web_host)}:{web_port}")
    print(f"  LAN:   http://{format_url_host(display_host)}:{web_port}")
    print()
    print("Share this P2P address with other nodes:")
    advertised_hosts: list[str] = []
    for host in configured_listen_hosts(config):
        if host == "0.0.0.0":
            advertised_host = shared_ipv4
        elif host == "::":
            advertised_host = shared_ipv6
        else:
            advertised_host = host
        if advertised_host and advertised_host not in advertised_hosts:
            advertised_hosts.append(advertised_host)
    for host in advertised_hosts:
        family = "IPv6" if ":" in host else "IPv4"
        print(f"  {family}: {format_host_port(host, listen_port)}")
    if peers:
        joined = ", ".join(format_host_port(host, port) for host, port in peers)
        print(f"Seed peers: {joined}")


if __name__ == "__main__":
    main()
