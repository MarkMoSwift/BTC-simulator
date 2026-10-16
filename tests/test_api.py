from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.core.block import block_hash, meets_bits
from app.runtime import ClassroomService, login_message
from app.core.wallet import sign_message
from app.web.server import create_app
from tests.conftest import COIN, find_nonce, header_from_job, make_service, new_player, signed_tx


def bootstrap(tmp_path) -> tuple[TestClient, ClassroomService]:
    service = make_service(
        tmp_path,
        chain={"initial_difficulty": 8, "difficulty_max": 16},
        pool={"initial_share_bits": 8, "share_bits_min": 8, "job_refresh_seconds": 1},
    )
    return TestClient(create_app(service)), service


def login(client: TestClient, nickname: str):
    player = new_player(nickname)
    begin = client.post("/api/auth/begin", json={"nickname": nickname}).json()
    signature = sign_message(player.private_key, login_message(nickname, begin["nonce"]))
    response = client.post(
        "/api/auth/finish",
        json={
            "nickname": nickname,
            "nonce": begin["nonce"],
            "public_key": player.public_key,
            "signature": signature,
        },
    )
    assert response.status_code == 200, response.text
    return player, {"Authorization": f"Bearer {response.json()['token']}"}


def mine_a_block(client: TestClient, service: ClassroomService, headers: dict, *, tries: int = 20_000) -> dict:
    job = client.post("/api/mining/job", headers=headers).json()
    for nonce in range(tries):
        if not meets_bits(block_hash(header_from_job(job, nonce)), job["share_bits"]):
            continue
        result = client.post(
            "/api/mining/share", headers=headers, json={"job_id": job["job_id"], "nonce": nonce}
        ).json()
        assert result["accepted"], result
        if result["block"]:
            return result["block"]
    raise AssertionError("没有挖到区块")


def mine_pending(client: TestClient, service: ClassroomService, headers: dict) -> dict:
    service.pool.tick(time.time() + 5)
    return mine_a_block(client, service, headers)


def test_public_state_and_login(tmp_path) -> None:
    client, _service = bootstrap(tmp_path)
    with client:
        public = client.get("/api/public").json()
        assert public["classroom"] == "测试课堂"
        assert public["chain"]["height"] == 0

        player, headers = login(client, "小明")
        me = client.get("/api/me", headers=headers).json()
        assert me["nickname"] == "小明"
        assert me["address"] == player.address
        assert me["balance"] == 0

        assert client.get("/api/me").status_code == 400
        assert client.post("/api/me").status_code == 405


def test_mining_transfer_and_explorer(tmp_path) -> None:
    client, service = bootstrap(tmp_path)
    with client:
        alice, alice_headers = login(client, "alice")
        bob, _bob_headers = login(client, "bob")

        block = mine_a_block(client, service, alice_headers)
        assert block["height"] == 1
        me = client.get("/api/me", headers=alice_headers).json()
        assert me["balance"] == 50 * COIN
        assert me["mining"]["hashrate"] > 0

        tx = signed_tx(alice, "transfer", receiver=bob.address, amount=COIN, fee=10_000)
        response = client.post("/api/transactions", headers=alice_headers, json={"tx": tx})
        assert response.status_code == 200, response.text
        assert client.get("/api/mempool").json()["stats"]["count"] == 1

        mine_pending(client, service, alice_headers)
        me = client.get("/api/me", headers=alice_headers).json()
        assert me["balance"] == 99 * COIN
        detail = client.get("/api/blocks/2").json()
        assert [tx["tx_id"] for tx in detail["transactions"]][1] == tx["tx_id"]

        history = client.get("/api/transactions", params={"address": alice.address}).json()["transactions"]
        assert history[0]["tx_id"] == tx["tx_id"]
        assert history[0]["receiver"] == bob.address
        assert [row["type"] for row in history] == ["transfer", "coinbase", "coinbase"]

        tampered = {**tx, "amount": 2 * COIN}
        rejected = client.post("/api/transactions", headers=alice_headers, json={"tx": tampered})
        assert rejected.status_code == 400


def test_red_packet_flow(tmp_path) -> None:
    client, service = bootstrap(tmp_path)
    with client:
        alice, alice_headers = login(client, "alice")
        bob, bob_headers = login(client, "bob")
        mine_a_block(client, service, alice_headers)
        mine_a_block(client, service, bob_headers)

        create = signed_tx(alice, "packet_create", total=5 * COIN, count=2, note="新年快乐", fee=10_000)
        client.post("/api/transactions", headers=alice_headers, json={"tx": create})
        mine_pending(client, service, alice_headers)

        packets = client.get("/api/packets").json()
        assert len(packets["active"]) == 1
        packet = packets["active"][0]
        assert packet["note"] == "新年快乐"
        assert packet["claimed_count"] == 0

        ticket = client.post(f"/api/packets/{packet['packet_id']}/claim-ticket", headers=bob_headers).json()
        before = client.get("/api/me", headers=bob_headers).json()["balance"]
        claim = signed_tx(bob, "packet_claim", packet_id=packet["packet_id"], index=ticket["index"], fee=10_000)
        client.post("/api/transactions", headers=bob_headers, json={"tx": claim})
        mine_pending(client, service, alice_headers)

        packets = client.get("/api/packets").json()
        assert packets["active"][0]["claimed_count"] == 1
        assert packets["active"][0]["claims"][0]["amount"] == ticket["amount"]
        bob_balance = client.get("/api/me", headers=bob_headers).json()["balance"]
        assert bob_balance == before - 10_000 + ticket["amount"]

        bob_history = client.get("/api/me", headers=bob_headers).json()["transactions"]
        claim_row = next(row for row in bob_history if row["tx_id"] == claim["tx_id"])
        assert claim_row["amount"] == ticket["amount"]
        assert claim_row["receiver"] == alice.address

        again = client.post(f"/api/packets/{packet['packet_id']}/claim-ticket", headers=bob_headers)
        assert again.status_code == 400


def test_nft_market_flow(tmp_path) -> None:
    client, service = bootstrap(tmp_path)
    with client:
        alice, alice_headers = login(client, "alice")
        bob, bob_headers = login(client, "bob")
        mine_a_block(client, service, alice_headers)
        mine_a_block(client, service, bob_headers)

        mint = signed_tx(alice, "nft_mint", name="矿石徽章", seed="0123456789abcdef", fee=10_000)
        client.post("/api/transactions", headers=alice_headers, json={"tx": mint})
        mine_pending(client, service, alice_headers)

        token_id = mint["tx_id"]
        assert client.get("/api/me", headers=alice_headers).json()["nfts"][0]["token_id"] == token_id

        listing = signed_tx(alice, "nft_list", token_id=token_id, price=3 * COIN, fee=10_000)
        client.post("/api/transactions", headers=alice_headers, json={"tx": listing})
        mine_pending(client, service, alice_headers)
        market = client.get("/api/market").json()
        assert [item["token_id"] for item in market["listings"]] == [token_id]

        purchase = signed_tx(bob, "nft_buy", token_id=token_id, fee=10_000)
        client.post("/api/transactions", headers=bob_headers, json={"tx": purchase})
        mine_pending(client, service, alice_headers)

        assert client.get("/api/me", headers=bob_headers).json()["nfts"][0]["token_id"] == token_id
        assert client.get("/api/market").json()["listings"] == []

        alice_history = client.get("/api/me", headers=alice_headers).json()["transactions"]
        sale = next(row for row in alice_history if row["tx_id"] == purchase["tx_id"])
        assert sale["amount"] == 3 * COIN
        assert sale["receiver"] == alice.address


def test_nft_auction_flow(tmp_path) -> None:
    client, service = bootstrap(tmp_path)
    with client:
        alice, alice_headers = login(client, "alice")
        bob, bob_headers = login(client, "bob")
        carol, carol_headers = login(client, "carol")
        for headers in (alice_headers, bob_headers, carol_headers):
            mine_a_block(client, service, headers)

        mint = signed_tx(alice, "nft_mint", name="课堂徽章", seed="0123456789abcdef", fee=10_000)
        client.post("/api/transactions", headers=alice_headers, json={"tx": mint})
        mine_pending(client, service, alice_headers)

        auction = signed_tx(
            alice,
            "nft_auction_create",
            token_id=mint["tx_id"],
            reserve_price=2 * COIN,
            duration=300,
            fee=10_000,
        )
        client.post("/api/transactions", headers=alice_headers, json={"tx": auction})
        mine_pending(client, service, alice_headers)
        assert client.get("/api/me", headers=alice_headers).json()["nfts"][0]["auction_id"] == auction["tx_id"]

        def place_bid(player, headers, amount: int):
            bid = signed_tx(player, "nft_bid", auction_id=auction["tx_id"], amount=amount, fee=10_000)
            response = client.post("/api/transactions", headers=headers, json={"tx": bid})
            assert response.status_code == 200, response.text
            return bid

        too_low = client.post(
            "/api/transactions",
            headers=carol_headers,
            json={"tx": signed_tx(carol, "nft_bid", auction_id=auction["tx_id"], amount=COIN, fee=10_000)},
        )
        assert too_low.status_code == 400
        assert "起拍价" in too_low.json()["detail"]

        bob_balance = client.get("/api/me", headers=bob_headers).json()["balance"]
        place_bid(bob, bob_headers, 2 * COIN)
        place_bid(carol, carol_headers, 3 * COIN)
        mine_pending(client, service, alice_headers)

        live = client.get("/api/public").json()
        active = live["auctions"]["active"][0]
        assert active["highest_bid"] == 3 * COIN
        assert active["highest_bidder"] == carol.address
        assert active["seconds_left"] > 0
        # 被超价的钱原路退回，只花掉一次手续费。
        assert client.get("/api/me", headers=bob_headers).json()["balance"] == bob_balance - 10_000

        service.chain.state.auctions[auction["tx_id"]]["ends_at"] = int(time.time()) - 300
        settlement = signed_tx(alice, "nft_auction_settle", auction_id=auction["tx_id"], fee=0)
        settled = client.post("/api/transactions", headers=alice_headers, json={"tx": settlement})
        assert settled.status_code == 200, settled.text
        mine_pending(client, service, carol_headers)

        assert client.get("/api/me", headers=carol_headers).json()["nfts"][0]["token_id"] == mint["tx_id"]
        seller_rows = client.get("/api/me", headers=alice_headers).json()["transactions"]
        payout = next(row for row in seller_rows if row["tx_id"] == settlement["tx_id"])
        assert payout["amount"] == 3 * COIN
        assert payout["receiver"] == carol.address
        closed = client.get("/api/public").json()["auctions"]
        assert closed["active"] == []
        assert closed["history"][0]["outcome"] == "sold"
        assert closed["history"][0]["winner"] == carol.address


def test_teacher_controls(tmp_path) -> None:
    client, service = bootstrap(tmp_path)
    with client:
        assert client.get("/teacher").status_code == 200
        assert client.get("/bigscreen").status_code == 200
        login(client, "alice")
        assert client.post("/api/auth/teacher", json={"key": "WRONG"}).status_code == 400
        token = client.post("/api/auth/teacher", json={"key": "TESTKEY"}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        overview = client.get("/api/teacher/overview", headers=headers).json()
        assert len(overview["players"]) == 1
        assert overview["settings"]["chain"]["target_block_seconds"] == 15

        updated = client.post(
            "/api/teacher/settings", headers=headers, json={"chain": {"target_block_seconds": 7}}
        )
        assert updated.status_code == 200, updated.text
        assert service.settings.chain.target_block_seconds == 7

        assert client.post("/api/teacher/announce", headers=headers, json={"message": "开始挖矿"}).status_code == 200
        export = client.get("/api/teacher/export.csv", headers=headers)
        assert export.status_code == 200
        assert "昵称" in export.text

        assert (
            client.post("/api/teacher/players/alice/ban", headers=headers, json={"banned": True}).status_code
            == 200
        )
        assert client.get("/api/teacher/overview", headers=headers).json()["players"][0]["banned"] is True
        assert (
            client.post("/api/teacher/players/alice/ban", headers=headers, json={"banned": False}).status_code
            == 200
        )

        assert client.post("/api/teacher/reset", headers=headers).status_code == 200
        assert client.get("/api/public").json()["chain"]["height"] == 0

        assert client.get("/api/teacher/overview").status_code == 400


