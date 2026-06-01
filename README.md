# BTC 模拟器 v0.1.0

这是一个教学型 BTC 模拟软件，不是真实 Bitcoin 钱包，不连接主网或测试网，也不兼容真实 BTC 地址。MVP 使用账户余额模型、SQLite 本地存储、ECDSA secp256k1 签名、TCP JSON Lines P2P 和本地 Web 控制台。

私钥在 MVP 中以明文保存到 SQLite，仅用于本地模拟教学，不能用于真实资产。

## 安装

```bash
python -m pip install -r requirements.txt
```

## 启动单节点

```bash
python main.py --config config.json
```

打开：

```text
http://127.0.0.1:8000
```

首次启动会自动创建配置、SQLite 数据库、默认钱包和创世块。

## 启动三个本地节点

分别开三个终端：

```bash
python main.py --config config_node1.json
python main.py --config config_node2.json
python main.py --config config_node3.json
```

Web 控制台：

```text
server1 http://127.0.0.1:8000  P2P 127.0.0.1:7464
server2 http://127.0.0.1:8001  P2P 127.0.0.1:7465
server3 http://127.0.0.1:8002  P2P 127.0.0.1:7466
```

## 初始化局域网节点

如果多台电脑在同一个局域网内，可以让每个人初始化自己的节点并加入网络。第一台电脑可以作为种子节点，后续节点只需要知道种子节点的局域网 IP 和 P2P 端口。

### 1. 第一台电脑创建种子节点

```bash
python scripts/init_node.py --name seed --config config_seed.json --no-prompt
python main.py --config config_seed.json
```

脚本会生成一个配置文件，默认：

```text
Web 控制台: 0.0.0.0:8000
P2P 节点:   0.0.0.0:7464
数据库:     ./data/seed.db
```

本机打开：

```text
http://127.0.0.1:8000
```

局域网内其他电脑打开：

```text
http://种子节点局域网IP:8000
```

把种子节点的 P2P 地址告诉其他人，例如：

```text
192.168.1.23:7464
```

如果同一堂课需要隔离成不同网络，可以指定 `network_id`：

```bash
python scripts/init_node.py --name seed --network-id class-a --config config_seed.json --no-prompt
```

### 2. 其他电脑加入网络

在另一台电脑上执行：

```bash
python scripts/init_node.py --name alice --config config_alice.json --peer 192.168.1.23:7464 --no-prompt
python main.py --config config_alice.json
```

启动后节点会自动连接 seed peer，并通过 `HELLO/PEERS/GET_BLOCKS/BLOCKS` 加入网络、同步区块。也可以在 Web 控制台“节点”页手动添加其他节点：

```text
IP:   192.168.1.23
Port: 7464
```

### 3. 同一台电脑跑多个节点

同一台电脑上端口不能重复，需要分别指定不同端口：

```bash
python scripts/init_node.py --name node1 --config config_node1.json --listen-port 7464 --web-port 8000 --no-prompt
python scripts/init_node.py --name node2 --config config_node2.json --listen-port 7465 --web-port 8001 --peer 127.0.0.1:7464 --no-prompt
```

不同电脑上可以使用相同端口，因为它们的 IP 不同。

### 4. 局域网注意事项

- P2P 连接使用 `listen_ip/listen_port`，默认 `0.0.0.0:7464`。
- Web 控制台使用 `web_host/web_port`，初始化脚本默认 `0.0.0.0:8000`，方便局域网访问。
- 如果连接失败，检查系统防火墙是否允许 Python 入站，或是否放行了 `7464` 和 `8000`。
- 连接其他电脑时不要填 `127.0.0.1`，要填对方的局域网 IP，例如 `192.168.1.23`。
- “入网”页会显示本机 Web 地址、P2P 地址、Network ID 和参数 Hash，可直接复制给其他同学。
- 本项目没有登录鉴权，只建议在可信局域网内演示，不要暴露到公网。

## 课堂功能

Web 控制台内置三个课堂辅助页：

- “入网”：显示本机 Web 控制台地址、P2P 地址、Network ID、参数 Hash，并提供复制按钮和入网码。
- “实验”：给学生提供任务清单，覆盖钱包、入网、连接节点、挖矿、内存池、同步等关键概念。
- “教师”：显示本机和已知学生节点的高度、难度、挖矿状态、连接状态和参数 Hash。

节点连接时会检查：

```text
network_id
chain_params_hash
```

`chain_params_hash` 由初始难度、自动难度规则、目标出块时间、挖矿奖励、区块交易上限等共识参数计算得到。若某台电脑的参数不同，教师页会显示“参数不匹配”，该节点不会加入当前课堂网络。

## 使用流程

1. 打开节点 Web 控制台。
2. 在“接收”页复制某个节点地址。
3. 在另一个节点“发送”页填写接收方地址、金额和手续费。
4. 交易验证通过后进入本地内存池并广播到已连接节点。
5. 在“挖矿”页点击“开始挖矿”。
6. 挖到新区块后，coinbase 奖励默认 `50 BTC + 区块手续费`，区块会保存到 SQLite 并广播。
7. 在“浏览器”页可以查看历史区块、区块头和区块内交易。
8. 在“入网”页复制 P2P 地址给其他同学。
9. 在“实验”页按任务清单完成课堂实验。
10. 在“教师”页查看全班节点状态。

## 配置

核心字段：

```json
{
  "network_id": "btc-sim-classroom",
  "listen_port": 7464,
  "web_port": 8000,
  "difficulty": 5,
  "auto_difficulty": true,
  "target_block_seconds": 60,
  "difficulty_adjustment_interval": 10,
  "mining_reward": 50.0,
  "servers": [["127.0.0.1", 7464], ["127.0.0.1", 7465]],
  "storage": {"type": "sqlite", "path": "./data/blockchain.db"}
}
```

`difficulty` 表示初始区块 hash 需要满足的前导 `0` 个数。默认开启 `auto_difficulty`，目标出块时间为 `target_block_seconds = 60`，即约 1 分钟一块。模拟器每 `difficulty_adjustment_interval = 10` 个区块评估一次最近出块速度：太快则难度 +1，太慢则难度 -1，单次最多调整 `difficulty_max_step`。

真实 BTC 是每 2016 个区块、约 14 天调整一次。这里把窗口缩短到 10 个区块，是为了本地教学演示能看见调整效果。同一个模拟网络里的所有节点必须配置成相同的难度规则，否则会拒绝彼此挖出的新区块。

初始难度也可以在管理员模式下调整：访问 `/?administrator=true` 后，“挖矿”页会显示难度设置。保存后会写回当前节点的配置文件；如果节点正在挖矿，系统会先暂停挖矿，避免当前候选区块继续按旧规则计算。多节点演示时请把每个节点设置成相同规则。

管理员模式下也会显示“重置到创世块”按钮。该操作会保留钱包和节点记录，清空本节点的历史区块、交易和内存池，只留下创世块。需要重置整个本地网络时，请分别在每个节点执行一次。

## 查看数据库

```bash
sqlite3 data/blockchain.db
```

常用查询：

```sql
SELECT height, hash, timestamp FROM blocks ORDER BY height;
SELECT type, sender, receiver, amount, fee FROM transactions;
SELECT tx_id, sender, receiver, amount, fee FROM mempool;
```

## 当前限制

- 不实现真实 Bitcoin 主网协议。
- 不实现真实 BTC 地址格式。
- 不实现 UTXO、Script、找零、SPV。
- 不实现真实 BTC 的 nBits/target 难度编码、复杂分叉重组或拜占庭攻击防御。
- 钱包私钥不加密。
- Web API 只服务本地教学演示，未做生产级认证授权。
