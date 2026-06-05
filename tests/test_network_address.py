from __future__ import annotations

import asyncio
import copy

from app.config import DEFAULT_CONFIG
from app.network.address import (
    configured_listen_hosts,
    format_host_port,
    format_url_host,
    normalize_host,
    parse_host_port,
)
from app.runtime import NodeService


def test_ipv6_address_formatting_and_parsing():
    assert normalize_host("[2001:db8::10]") == "2001:db8::10"
    assert format_host_port("2001:db8::10", 7464) == "[2001:db8::10]:7464"
    assert format_url_host("fe80::10%en0") == "[fe80::10%25en0]"
    assert parse_host_port("[2001:db8::10]:7464") == ("2001:db8::10", 7464)


def test_default_wildcard_listens_on_ipv6_and_ipv4():
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["listen_ip"] = "0.0.0.0"
    config["enable_ipv6"] = True

    assert configured_listen_hosts(config) == ["::", "0.0.0.0"]

    config["enable_ipv6"] = False
    assert configured_listen_hosts(config) == ["0.0.0.0"]


def test_network_info_formats_ipv6_web_and_p2p_addresses(tmp_path):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["listen_ip"] = "::1"
    config["web_host"] = "::1"
    config["servers"] = []
    config["storage"]["path"] = str(tmp_path / "ipv6-node.db")
    service = NodeService(config)

    info = service.network_info()

    assert info["enable_ipv6"] is True
    assert info["web_url"] == "http://[::1]:8000"
    assert info["p2p_address"] == "[::1]:7464"
    assert "[::1]:7464" in info["p2p_addresses"]
    asyncio.run(service.shutdown())


def test_connect_peer_removes_ipv6_brackets(tmp_path, monkeypatch):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["servers"] = []
    config["storage"]["path"] = str(tmp_path / "ipv6-peer.db")
    service = NodeService(config)
    captured: dict[str, object] = {}

    async def fake_open_connection(host, port, **kwargs):
        captured.update(host=host, port=port, kwargs=kwargs)
        raise OSError("test connection stopped")

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    accepted, _message = asyncio.run(service.connect_peer("[2001:db8::20]", 7464))

    assert not accepted
    assert captured["host"] == "2001:db8::20"
    assert captured["port"] == 7464
    asyncio.run(service.shutdown())


def test_network_info_uses_advertised_addresses(tmp_path):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["advertise_ip"] = "10.20.30.40"
    config["advertise_ipv6"] = "2001:db8::40"
    config["servers"] = []
    config["storage"]["path"] = str(tmp_path / "advertised-node.db")
    service = NodeService(config)

    info = service.network_info()

    assert info["lan_ip"] == "10.20.30.40"
    assert info["lan_ipv6"] == "2001:db8::40"
    assert "10.20.30.40:7464" in info["p2p_addresses"]
    assert "[2001:db8::40]:7464" in info["p2p_addresses"]
    asyncio.run(service.shutdown())
