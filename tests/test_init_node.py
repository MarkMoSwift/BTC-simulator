from __future__ import annotations

import argparse

from scripts.init_node import build_config, parse_peer, sanitize_name


def test_parse_peer_accepts_host_port():
    assert parse_peer("192.168.1.23:7464") == ["192.168.1.23", 7464]


def test_parse_peer_rejects_bad_port():
    try:
        parse_peer("192.168.1.23:not-a-port")
    except argparse.ArgumentTypeError as exc:
        assert "host:port" in str(exc)
    else:
        raise AssertionError("bad peer should be rejected")


def test_build_config_for_lan_node():
    config = build_config(
        node_name="Alice Node",
        listen_ip="0.0.0.0",
        listen_port=7464,
        web_host="0.0.0.0",
        web_port=8000,
        peers=[["192.168.1.10", 7464]],
    )

    assert config["node_name"] == "Alice-Node"
    assert config["network_id"] == "btc-sim-classroom"
    assert config["listen_ip"] == "0.0.0.0"
    assert config["web_host"] == "0.0.0.0"
    assert config["servers"] == [["192.168.1.10", 7464]]
    assert config["storage"]["path"] == "./data/Alice-Node.db"


def test_sanitize_name_has_fallback():
    assert sanitize_name("   ") == "node"
