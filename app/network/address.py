from __future__ import annotations

import ipaddress
from typing import Any


def normalize_host(host: Any) -> str:
    value = str(host or "").strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return value


def is_ipv6_host(host: Any) -> bool:
    value = normalize_host(host)
    if not value:
        return False
    address = value.split("%", 1)[0]
    try:
        return ipaddress.ip_address(address).version == 6
    except ValueError:
        return ":" in value


def is_loopback_or_unspecified(host: Any) -> bool:
    value = normalize_host(host)
    if value == "localhost":
        return True
    address = value.split("%", 1)[0]
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        parsed = parsed.ipv4_mapped
    return parsed.is_loopback or parsed.is_unspecified


def format_host_port(host: Any, port: int) -> str:
    value = normalize_host(host)
    if is_ipv6_host(value):
        return f"[{value}]:{int(port)}"
    return f"{value}:{int(port)}"


def format_url_host(host: Any) -> str:
    value = normalize_host(host)
    if is_ipv6_host(value):
        return f"[{value.replace('%', '%25')}]"
    return value


def parse_host_port(value: str) -> tuple[str, int]:
    text = value.strip()
    if text.startswith("["):
        closing = text.find("]")
        if closing < 0 or text[closing + 1:closing + 2] != ":":
            raise ValueError("IPv6 peer must use [address]:port")
        host = text[1:closing]
        port_text = text[closing + 2:]
    else:
        host, separator, port_text = text.rpartition(":")
        if not separator:
            raise ValueError("peer must use host:port")

    host = normalize_host(host)
    if not host or not port_text.isdigit():
        raise ValueError("peer must use host:port")
    port = int(port_text)
    if port < 1 or port > 65535:
        raise ValueError("peer port must be between 1 and 65535")
    return host, port


def configured_listen_hosts(config: dict[str, Any]) -> list[str]:
    host = normalize_host(config.get("listen_ip", "0.0.0.0"))
    if not bool(config.get("enable_ipv6", True)):
        return [host]
    if host == "0.0.0.0":
        return ["::", "0.0.0.0"]
    if host == "127.0.0.1":
        return ["::1", "127.0.0.1"]
    if host == "::":
        return ["::", "0.0.0.0"]
    if host == "::1":
        return ["::1", "127.0.0.1"]
    return [host]
