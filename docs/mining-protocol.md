# 挖矿协议

课堂网络只有 HTTP 和 WebSocket 两种接口，没有隐藏状态。任何语言只要按本文实现，就能
接入同一场比赛；[`examples/python_miner.py`](../examples/python_miner.py) 是一份可运行的参考实现。

假设服务器地址是 `http://192.168.1.23:8000`，下面用 `$BASE` 表示。

## 0. 一分钟总览

1. 本地生成 P-256 密钥对 → 得到钱包地址（私钥不出本机）。
2. `POST /api/auth/begin` 拿一次性 nonce，签名后 `POST /api/auth/finish` 换会话令牌。
3. `POST /api/mining/job` 取挖矿作业。
4. 拼 84 字节区块头，从 `0` 开始改 nonce，算 `sha256d`。
5. 哈希值 ≤ `share_target` 就 `POST /api/mining/share` 提交；≤ `block_target` 时这个区块归你。
6. 服务端重新验算哈希，通过后按工作量记账，下一阶段分奖励。

## 1. 钱包与地址

| 项目 | 规则 |
| --- | --- |
| 曲线 | NIST P-256（secp256r1） |
| 私钥 | 32 字节随机数，十六进制表示 |
| 公钥 | 64 字节 `x || y`，十六进制表示（不带 `04` 前缀） |
| 地址 | `"sim" + sha256(公钥的 64 字节)[:20].hex()`，共 43 个字符 |
| 签名 | 对 `sha256(message)` 做 ECDSA，输出 64 字节 `r || s` 的十六进制 |

登录消息固定为：

```text
btc-sim-login|<昵称>|<nonce>
```

昵称限 1-12 个字符，可用汉字、`A-Za-z0-9_` 与短横线。

## 2. 登录

```http
POST $BASE/api/auth/begin
Content-Type: application/json

{"nickname": "小明"}
```

```json
{"nickname": "小明", "nonce": "4f1c9a7d2b8e5031", "registered": false}
```

签名 `btc-sim-login|小明|4f1c9a7d2b8e5031` 后：

```http
POST $BASE/api/auth/finish
Content-Type: application/json

{
  "nickname": "小明",
  "nonce": "4f1c9a7d2b8e5031",
  "public_key": "…128 位十六进制…",
  "signature": "…128 位十六进制…"
}
```

返回：

```json
{"token": "会话令牌", "player": {"nickname": "小明", "address": "sim…", "balance": 0}}
```

之后所有请求都带 `Authorization: Bearer <token>`。会话有效期 12 小时。
昵称第一次登录时和当前公钥绑定，换一把私钥用同一个昵称会被拒绝，教师可以在控制台移除旧昵称。

## 3. 取作业

```http
POST $BASE/api/mining/job
Authorization: Bearer <token>
Content-Type: application/json

{}
```

| 字段 | 含义 |
| --- | --- |
| `job_id` | 作业编号，提交份额时必须带上；换作业后旧编号会被拒绝 |
| `height` | 这份作业在挖第几个区块 |
| `version` | 区块头版本，固定为 1 |
| `prev_hash` | 上一个区块的哈希（64 位十六进制） |
| `merkle_root` | 这份作业的梅克尔根，已经固定 |
| `tx_ids` | 参与梅克尔根的交易号，`tx_ids[0]` 是 coinbase |
| `timestamp` | 区块时间戳（秒，写进区块头的是 32 位小端） |
| `difficulty` | 出块难度：哈希前导零位数 |
| `miner_tag` | 由你的地址推导出的 32 位标记，已经在区块头里 |
| `share_bits` | 你的份额难度，比出块难度低，服务端按你的速度自动调整 |
| `share_target` | 份额目标值（64 位十六进制） |
| `block_target` | 出块目标值（64 位十六进制） |
| `subsidy` | 本块基础奖励（聪） |
| `fees` | 本块包含的手续费总额（聪） |
| `tx_count` | 本块包含的普通交易笔数 |
| `payout_count` | coinbase 的收款人数 |
| `issued_at` | 作业下发时间（秒） |

## 4. 组装 84 字节区块头

区块头是定长 84 字节，整数一律 **小端序**：

| 偏移 | 长度 | 字段 | 编码 |
| --- | --- | --- | --- |
| 0 | 4 | version | uint32 LE |
| 4 | 32 | prev_hash | 十六进制解码后的原始字节 |
| 36 | 32 | merkle_root | 十六进制解码后的原始字节 |
| 68 | 4 | timestamp | uint32 LE |
| 72 | 4 | difficulty | uint32 LE |
| 76 | 4 | miner_tag | uint32 LE |
| 80 | 4 | nonce | uint32 LE |

区块哈希 = `sha256(sha256(header))` 的 32 字节，按 **大端** 解释成 256 位整数，越小越难。

```python
def header_prefix(job):            # nonce 之外的 80 字节，一份作业只算一次
    return (struct.pack("<I", job["version"])
            + bytes.fromhex(job["prev_hash"])
            + bytes.fromhex(job["merkle_root"])
            + struct.pack("<I", job["timestamp"])
            + struct.pack("<I", job["difficulty"])
            + struct.pack("<I", job["miner_tag"]))

digest = sha256d(header_prefix(job) + struct.pack("<I", nonce))
```

`tx_ids` 和 `merkle_root` 都给了：用 `merkle_root` 最快，用 `tx_ids` 自己复算可以验证
「为什么改一笔交易会改动整个区块头」。

### 目标值与难度

难度 `d` 表示哈希的前导零位数，目标值就是 `(1 << (256 - d)) - 1`：

```text
difficulty = 24  →  target = 000000ffffffff…ffff
哈希 ≤ target 才算达标，平均需要 2^24 次尝试
```

`share_target` 一定不小于 `block_target`：先达到 `share_target` 就有份额，
达到 `block_target` 才算挖到这个区块。

## 5. 提交份额

```http
POST $BASE/api/mining/share
Authorization: Bearer <token>
Content-Type: application/json

{"job_id": "…", "nonce": 123456}
```

`nonce` 必须是 0 到 4294967295 之间的整数。服务端会用你的 `miner_tag`、`prev_hash` 和
当前作业重新拼区块头并验算哈希，所以伪造 nonce 或改难度都不会通过。

普通结果：

```json
{
  "accepted": true,
  "reason": null,
  "share_bits": 14,
  "next_share_bits": 14,
  "work": 16384,
  "hash": "0000…",
  "hashrate": 91234.5,
  "block": null,
  "balance": 1200000
}
```

挖到区块时会多出这些字段：

```json
{
  "block": {
    "height": 42, "hash": "…", "difficulty": 20, "finder": "sim…",
    "payout": 51234567, "subsidy": 5000000000, "fees": 20000,
    "payouts": [{"receiver": "sim…", "amount": 4800000000}], "tx_count": 3
  },
  "job": { "…下一份作业…" }
}
```

收到 `job` 字段要立刻切换作业；收到 `block` 表示这个区块已经上链。

`share_bits` 是这一份份额的难度，`next_share_bits` 是服务端根据你最近的提交速度算出的
下一次该用的难度：份额交得太密会往上涨，太稀会往下掉。把它当提示用，重新取一份作业，
或者自己按 `target = (1 << (256 - bits)) - 1` 换算出新目标。

### 奖励怎么分

区块头必须在开挖前就固定，所以 coinbase 的分配名单不能等"谁挖到"再决定。规则是：

1. 每份作业携带 **上一阶段封盘** 的工作量快照，按快照比例分配 `subsidy + fees`。
2. 你现在提交的份额进入下一阶段，区块产生后的下一份作业才会按新快照分钱。
3. 发现区块的人额外拿 `finder_bonus_percent`（默认 5%）的奖励，其余按工作量分。
4. 余数按小数部分补足，最后保证每个收款人至少 1 聪。

## 6. 浏览器自定义矿工的 Worker 协议

不想装任何东西时，可以在学生控制台点「自定义矿工」载入一个 `.js` 文件。控制台会先
加载 `/static/crypto.js`（暴露为 `SimCrypto`），再运行你的代码，所以你不需要自己写
`importScripts`：如果文件是从默认矿工复制的，把第一行 `importScripts("crypto.js")`
删掉，控制台会明确提示这一条。要引入别的工具脚本请用绝对地址。

| 方向 | 消息 | 说明 |
| --- | --- | --- |
| 页面 → Worker | `{type: "job", job}` | 新作业，收到后从头开始搜索 |
| 页面 → Worker | `{type: "stop"}` | 暂停挖矿 |
| Worker → 页面 | `{type: "share", nonce}` | 提交份额，页面会带上当前 `job_id` 发给服务器 |
| Worker → 页面 | `{type: "progress", hashes}` | 自上次上报以来算过的哈希数，用来显示实时算力 |

`SimCrypto` 提供 `sha256`、`sha256d`、`hexToBytes`、`bytesToHex`、`utf8Bytes`、
`toUint32LE`、`concatBytes` 等工具，默认矿工 `app/web/static/miner.js` 就是按这套协议写的。

## 7. 会被拒绝的常见情况

| 返回的 `reason` | 原因 |
| --- | --- |
| 作业已经更新，请重新获取 | `job_id` 过期，重新取作业 |
| 这个 nonce 已经提交过 | 同一作业同一 nonce 不能重复提交 |
| 哈希没有达到份额难度 | 算出的哈希大于 `share_target` |
| 提交过于频繁 | 每秒放行的份额有上限（默认 8 份），多出来的会被挡下；份额难度调高后自然就降下来了 |
| 登录状态已过期 | 令牌超过 12 小时，重新登录 |
| 这个昵称已被教师停用 | 教师停用了账号 |

## 8. 自己发起交易

学生端能点的每一个按钮都是一笔签名交易，接口只有一个：

```http
POST $BASE/api/transactions
Authorization: Bearer <token>
Content-Type: application/json

{"tx": {"…交易体…"}}
```

| 字段 | 含义 |
| --- | --- |
| `type` | 交易类型，见下表 |
| `sender` | 你的地址 |
| `timestamp` | 发起时间（秒），和服务器时间差太多会被拒收 |
| `public_key` | 你的公钥，必须和 `sender` 匹配 |
| `fee` | 手续费（聪），一般不低于课堂设置的 `min_fee_sat` |

`tx_id` 是交易体（不含 `tx_id` 和 `signature`）规范 JSON 的 `sha256d`，签名也对这份规范
JSON 做 ECDSA。规范 JSON 指按键排序、无空格、非 ASCII 一律转义的文本，实现见
`app/utils/canonical.py`。

| 类型 | 额外字段 |
| --- | --- |
| `transfer` | `receiver`、`amount`，可选 `note` |
| `packet_create` | `total`、`count`，可选 `note` |
| `packet_claim` | `packet_id`、`index` |
| `packet_refund` | `packet_id` |
| `nft_mint` | `name`、`seed`（16 位十六进制） |
| `nft_list` | `token_id`、`price` |
| `nft_unlist` | `token_id` |
| `nft_buy` | `token_id` |
| `nft_auction_create` | `token_id`、`reserve_price`、`duration`（秒，1-600） |
| `nft_bid` | `auction_id`、`amount` |
| `nft_auction_cancel` | `auction_id` |
| `nft_auction_settle` | `auction_id`，手续费必须填 `0` |

### 藏品拍卖的规则

1. 开拍交易上链那一刻开始计时，`duration` 秒内接受出价。
2. 第一次出价不能低于 `reserve_price`，之后每次至少比当前最高价高 10%。
3. 出价即托管：钱在出价时就从余额扣走，被别人超过时自动原路退回，手续费不退。
4. 结束前 30 秒内有人出价，结束时间顺延到出价后 30 秒，卡点偷袭拿不到便宜。
5. 到点后任何人发一笔 `nft_auction_settle` 都能结算：托管的钱付给卖家，藏品过户给最高出价者。
6. 没人出价时卖家可以 `nft_auction_cancel` 收回藏品，有人出价后就不能取消。

## 9. 其它只读接口

| 接口 | 用途 |
| --- | --- |
| `GET /api/public` | 课堂快照：高度、难度、排行榜、红包、市场与拍卖 |
| `GET /api/blocks?limit=20` | 最近区块列表 |
| `GET /api/blocks/{高度或哈希}` | 区块明细（含全部交易） |
| `GET /api/transactions/{tx_id}` | 交易明细，未确认时返回 `pending` |
| `GET /api/mempool` | 内存池中的待打包交易 |
| `GET /api/packets` | 红包状态 |
| `GET /api/market` | 藏品挂单 |
| `WS /ws?token=…` | 实时推送：`tick`、`share`、`block`、`announcement` 等 |

实时推送每秒一条 `tick`，内容与 `/api/public` 相同，适合做自己的看板。

`auctions` 字段分两部分：`active` 是进行中的拍卖（`auction_id`、`token_id`、`name`、
`seller`、`reserve_price`、`minimum_bid`、`highest_bid`、`highest_bidder`、`bid_count`、
`ends_at`、`seconds_left`）；`history` 是已结束的拍卖（`outcome` 为 `sold` 或
`cancelled`，成交时还有 `winner`、`amount`、`closed_at`）。`/api/me` 里每件藏品带
`auction_id`，不为 `null` 就说明这件藏品正在拍卖。
