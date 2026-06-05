from __future__ import annotations

import asyncio
import copy

from app.config import DEFAULT_CONFIG, is_default_node_name, random_node_name
from app.core.transaction import create_transfer
from app.core.wallet import generate_wallet
from app.network.address import (
    configured_listen_hosts,
    format_host_port,
    format_url_host,
    normalize_host,
    parse_host_port,
)
from app.network.node import PeerConnection
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


def test_random_node_name_uses_large_random_space():
    names = {random_node_name() for _ in range(200)}

    assert len(names) == 200
    assert all(name.startswith("node-") for name in names)
    assert all(len(name) == 17 for name in names)
    assert all(name.replace("node-", "").isalnum() for name in names)


def test_only_literal_default_node_name_is_auto_generated():
    assert is_default_node_name("default")
    assert is_default_node_name(" DEFAULT ")
    assert not is_default_node_name("server1")
    assert not is_default_node_name("node")
    assert not is_default_node_name("node-teacher")


def test_peer_identity_conflicts_check_name_then_wallet_then_listen_address(tmp_path):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["node_name"] = "node-local"
    config["servers"] = []
    config["storage"]["path"] = str(tmp_path / "identity-peer.db")
    service = NodeService(config)
    wallet = service.default_wallet()

    same_name = PeerConnection(
        reader=object(),
        writer=object(),
        ip="192.168.1.50",
        port=7464,
        direction="outbound",
        name="node-local",
        address=wallet["address"],
        listen_port=7464,
    )
    assert service.p2p._identity_conflict_reason(same_name) == "same node name"

    same_wallet = PeerConnection(
        reader=object(),
        writer=object(),
        ip="192.168.1.51",
        port=7464,
        direction="outbound",
        name="node-remote",
        address=wallet["address"],
        listen_port=7464,
    )
    assert service.p2p._identity_conflict_reason(same_wallet) == "same wallet address"

    same_listen_address = PeerConnection(
        reader=object(),
        writer=object(),
        ip="127.0.0.1",
        port=7464,
        direction="outbound",
        name="node-remote",
        address="remote-wallet",
        listen_port=7464,
    )
    assert service.p2p._identity_conflict_reason(same_listen_address) == "same listen address"
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


def test_invalid_peer_transaction_records_security_event(tmp_path):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["servers"] = []
    config["storage"]["path"] = str(tmp_path / "bad-tx-peer.db")
    service = NodeService(config)
    attacker = generate_wallet("attacker")
    receiver = generate_wallet("receiver")
    tx = create_transfer(
        attacker.address,
        receiver.address,
        amount=1,
        fee=0,
        public_key=attacker.public_key,
        private_key=attacker.private_key,
    )
    tx["signature"] = "00"
    conn = PeerConnection(
        reader=object(),
        writer=object(),
        ip="192.168.1.60",
        port=7464,
        direction="inbound",
        name="attacker-node",
        address=attacker.address,
        listen_port=7464,
    )

    asyncio.run(service.p2p._handle_tx(conn, {"type": "TX", "tx": tx, "ttl": 1}))

    event = service.security_status()["events"][0]
    assert event["type"] == "invalid transaction"
    assert event["peer_name"] == "attacker-node"
    assert event["peer_ip"] == "192.168.1.60"
    assert event["tx_id"] == tx["tx_id"]
    assert "signature" in event["reason"]
    asyncio.run(service.shutdown())


def test_invalid_peer_block_records_security_event(tmp_path):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["servers"] = []
    config["storage"]["path"] = str(tmp_path / "bad-block-peer.db")
    service = NodeService(config)
    attacker = generate_wallet("attacker")
    block = service.blockchain.create_candidate_block(attacker.address, [])
    block["header"]["target"] = "0" * 64
    conn = PeerConnection(
        reader=object(),
        writer=object(),
        ip="192.168.1.61",
        port=7464,
        direction="inbound",
        name="bad-miner",
        address=attacker.address,
        listen_port=7464,
    )

    asyncio.run(service.p2p._handle_block(conn, {"type": "BLOCK", "block": block, "ttl": 1}))

    event = service.security_status()["events"][0]
    assert event["type"] == "invalid block"
    assert event["peer_name"] == "bad-miner"
    assert event["block_hash"]
    assert event["reason"]
    asyncio.run(service.shutdown())
