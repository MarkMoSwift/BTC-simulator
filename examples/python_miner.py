"""参考矿工：只用公开 HTTP 协议接入课堂网络，不导入本项目代码。

先启动课堂服务器，然后：
    pip install ecdsa
    python examples/python_miner.py --base http://127.0.0.1:8000 --nickname Python矿工
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import time
import urllib.error
import urllib.request

from ecdsa import NIST256p, SigningKey
from ecdsa.util import sigencode_string

NONCE_LIMIT = 1 << 32


def sha256d(payload: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()


def request(base: str, path: str, payload: dict | None = None, token: str | None = None) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if body else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(f"{base}{path}", body, headers), timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


class Wallet:
    """浏览器里的钱包做同样的事：本地生成私钥，服务器只看到公钥和签名。"""

    def __init__(self) -> None:
        self.signing_key = SigningKey.generate(curve=NIST256p)
        self.public_key = self.signing_key.get_verifying_key().to_string().hex()
        self.address = "sim" + hashlib.sha256(bytes.fromhex(self.public_key)).hexdigest()[:40]

    def sign(self, message: str) -> str:
        digest = hashlib.sha256(message.encode("utf-8")).digest()
        signature = self.signing_key.sign_digest_deterministic(
            digest, hashfunc=hashlib.sha256, sigencode=sigencode_string
        )
        return signature.hex()


def login(base: str, nickname: str, wallet: Wallet) -> str:
    begin = request(base, "/api/auth/begin", {"nickname": nickname})
    result = request(
        base,
        "/api/auth/finish",
        {
            "nickname": nickname,
            "nonce": begin["nonce"],
            "public_key": wallet.public_key,
            "signature": wallet.sign(f"btc-sim-login|{nickname}|{begin['nonce']}"),
        },
    )
    return result["token"]


def merkle_root(tx_ids: list[str]) -> bytes:
    layer = [bytes.fromhex(tx_id) for tx_id in tx_ids]
    if not layer:
        return bytes(32)
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [sha256d(layer[index] + layer[index + 1]) for index in range(0, len(layer), 2)]
    return layer[0]


def header_prefix(job: dict) -> bytes:
    """区块头前 80 字节：nonce 以外的部分在整份作业里是固定的。"""
    return (
        struct.pack("<I", job["version"])
        + bytes.fromhex(job["prev_hash"])
        + merkle_root(job["tx_ids"])
        + struct.pack("<I", job["timestamp"])
        + struct.pack("<I", job["difficulty"])
        + struct.pack("<I", job["miner_tag"])
    )


class Session:
    def __init__(self, base: str, token: str) -> None:
        self.base = base
        self.token = token
        self.hashes = 0
        self.shares = 0
        self.blocks = 0
        self.prefix = b""
        self.share_target = 0
        self.job: dict = {}

    def adopt(self, job: dict) -> None:
        self.job = job
        self.prefix = header_prefix(job)
        self.retarget(job["share_bits"], int(job["share_target"], 16))

    def retarget(self, bits: int, target: int | None = None) -> None:
        """服务端会按提交速度自动调份额难度，跟着它走才不会一直交无效份额。"""
        self.job["share_bits"] = bits
        self.share_target = target if target is not None else (1 << (256 - bits)) - 1

    def refresh(self) -> None:
        self.adopt(request(self.base, "/api/mining/job", {}, self.token))

    def submit(self, nonce: int) -> None:
        reply = request(
            self.base, "/api/mining/share", {"job_id": self.job["job_id"], "nonce": nonce}, self.token
        )
        if not reply["accepted"]:
            print(f"份额被拒绝：{reply['reason']}")
            if "作业" in str(reply["reason"]):
                self.refresh()
            return
        self.retarget(reply["next_share_bits"])
        self.shares += 1
        print(f"份额 {self.shares}：nonce={nonce} 难度={reply['share_bits']} 位 工作量={reply['work']}")
        if reply.get("block"):
            self.blocks += 1
            print(f"  挖到区块 #{reply['block']['height']}，本块分到 {reply['block']['payout']} 聪")
        if reply.get("job"):
            self.adopt(reply["job"])

    def mine(self, seconds: float) -> float:
        self.refresh()
        started = time.time()
        nonce = 0
        while time.time() - started < seconds:
            digest = sha256d(self.prefix + struct.pack("<I", nonce))
            self.hashes += 1
            if int.from_bytes(digest, "big") <= self.share_target:
                self.submit(nonce)
            nonce = (nonce + 1) % NONCE_LIMIT
        return max(time.time() - started, 0.001)


def main() -> None:
    parser = argparse.ArgumentParser(description="按公开协议接入课堂网络的参考矿工")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="课堂服务器地址")
    parser.add_argument("--nickname", default="Python矿工", help="课堂昵称")
    parser.add_argument("--seconds", type=float, default=30.0, help="挖矿时长（秒）")
    args = parser.parse_args()

    wallet = Wallet()
    try:
        token = login(args.base, args.nickname, wallet)
    except urllib.error.HTTPError as exc:
        print(f"登录失败（HTTP {exc.code}）：{exc.read().decode('utf-8')}")
        raise SystemExit(1)
    print(f"登录成功：{args.nickname} {wallet.address}")

    session = Session(args.base, token)
    try:
        elapsed = session.mine(args.seconds)
    except KeyboardInterrupt:
        elapsed = args.seconds
    balance = request(args.base, "/api/me", None, token)["balance"]
    print(
        f"共计算 {session.hashes} 个哈希（{session.hashes / elapsed:.0f} H/s），"
        f"提交 {session.shares} 份，出块 {session.blocks} 个，余额 {balance} 聪"
    )


if __name__ == "__main__":
    main()
