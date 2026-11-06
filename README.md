# 区块链科普模拟器

参与者可以运行独立 Python 节点，或用浏览器连接可达节点。节点验证、同步区块和交易，大屏显示实时算力榜与累计挖矿榜。

## 启动课堂

需要 Python 3.10 或更新版本。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py --init-config
python main.py --create-session invite.json --entry http://192.168.1.23:8000
```

入口填写当前节点可达的实际地址。其他节点收到同一邀请文件后运行以下命令。

```powershell
python main.py --join-session invite.json
```

以后使用 `python main.py` 启动已选择的场次。没有场次信息时程序提示创建或导入邀请。每节新课堂创建新邀请，同一课堂使用同一份邀请。

## 两种参与方式

运行节点后打开 `http://localhost:8000`，页面自动使用节点的持久钱包。点击「开始挖矿」后，节点在后台计算，关闭网页后仍会继续。钱包文件位于配置文件同目录，以 `.wallet.key` 结尾，应妥善保存。

纯浏览器参与者访问可达节点的网址，输入昵称或者姓名，点击「开始挖矿」使用浏览器矿工。本机也可以访问 `http://localhost:8000/?browser=1`。

换网页节点前下载加密钱包备份，在核对过的新入口导入后登录，可以沿用原地址。备份加解密需要 HTTPS 或 localhost，请保存备份密码。浏览器私钥保存在对应网站来源的本地存储。

## 本地配置

`config.example.json` 是公开模板。默认读取当前工作目录的 `config.json`。它缺失时兼容同目录已有的 `config.local.json`。可用 `--config` 或环境变量 `BTC_SIM_CONFIG` 指定其他文件。

`node_name` 可人工填写，空白、null 或缺省时自动生成并保存。`classroom_name` 仅用于显示。邀请文件提供 `network_id`、`session_id` 和 `session_start_time`。

```json
{
  "classroom_name": "区块链科普课堂",
  "node_name": "",
  "peers": [],
  "web_host": "0.0.0.0",
  "web_port": 8000,
  "reset_chain_on_start": true,
  "tls": {"certificate_dir": ""}
}
```

`peers` 缺省等同于空列表。裸 IP 使用 HTTP 的 8000 端口，HTTPS 或其他端口请写完整 URL。节点只主动连接明确提供的地址。区块和交易通过已有连接转发。

`reset_chain_on_start` 默认 true，重启清理当前场次的本地链和派生记录，再同步同伴。钱包独立保存。继续课堂使用原邀请，新比赛创建新邀请。

相对数据及证书目录以配置文件所在目录解析。设置证书目录后，其中应包含 `cert.pem` 和 `key.pem`，程序启用 HTTPS/WSS。缺省使用 HTTP/WS，显式证书配置无效时启动失败。

本地配置、钱包和数据受 Git 忽略规则保护。提交者仍应检查已跟踪文件和强制添加的内容。

## 课堂功能

每块基础奖励为 50 币，加上区块手续费，全部归出块钱包。分叉按累计有效工作量选择主链，平手时保留当前分支。3.x 场次与旧版链隔离。

支持转账、红包、照片藏品、一口价与拍卖。每批铸造默认 1 件、最多 20 件，只收一次交易费。缩略图随区块同步。红包和拍卖按区块高度自动结算，默认手续费 0.1 币，可修改为 0。

任意节点的 `/bigscreen` 提供大屏，访问时计算榜单。累计挖矿榜反映当前主链奖励，算力及在线钱包数表示本节点观测。一个钱包使用多个计算程序仍对应一个身份。只读记录从 `/api/results.csv` 下载。

课堂规则由节点共同验证，运行中的场次不提供教师修改共识规则的权限。

## 开发验证

```powershell
pip install -r requirements-dev.txt
python -m pytest -q
```

实验要求见 [课堂实验](docs/lab.md)，提交安排以教师发布的要求为准。
