const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

let latestStatus = null;
let latestClassroom = null;
let toastTimer = null;
let blocksOffset = 0;
const blocksLimit = 30;
const isAdministrator = new URLSearchParams(window.location.search).get("administrator") === "true";
const labTasks = [
  ["wallet", "生成钱包地址", "接收页能看到本节点默认地址"],
  ["join", "准备入网信息", "入网页能复制 P2P 地址给同学"],
  ["peer", "连接至少 1 个节点", "节点页或教师页能看到连接记录"],
  ["mine", "挖出第一个区块", "区块高度大于 0，余额获得 coinbase 奖励"],
  ["tx", "观察内存池交易", "发送交易后先看到内存池数量变化"],
  ["sync", "同步课堂链", "多个节点高度接近，教师页无参数异常"],
];

function formatBTC(value) {
  return `${Number(value || 0).toFixed(8).replace(/\.?0+$/, "")} BTC`;
}

function shortHash(value, size = 10) {
  if (!value) return "--";
  if (value.length <= size * 2 + 3) return value;
  return `${value.slice(0, size)}...${value.slice(-size)}`;
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

function formatTime(epoch) {
  if (!epoch) return "--";
  return new Date(epoch * 1000).toLocaleString();
}

function formatDuration(seconds) {
  const value = Number(seconds || 0);
  if (value < 60) return `${value} 秒`;
  if (value % 60 === 0) return `${value / 60} 分钟`;
  return `${value} 秒`;
}

function setText(selector, value) {
  const element = $(selector);
  if (element) element.textContent = value;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("visible"), 2800);
}

async function copyText(value, label = "内容") {
  try {
    await navigator.clipboard.writeText(value || "");
    showToast(`${label}已复制`);
  } catch (_err) {
    showToast("复制失败");
  }
}

function setupAdminVisibility() {
  $$(".admin-only").forEach((element) => {
    element.hidden = !isAdministrator;
  });
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch (_err) {
      detail = response.statusText;
    }
    throw new Error(detail);
  }
  return response.json();
}

function renderStatus(status) {
  latestStatus = status;
  const isMining = Boolean(status.mining.is_mining);
  const miningStateClass = isMining
    ? "running"
    : status.mining.status === "暂停"
      ? "paused"
      : "idle";
  const nonceText = status.mining.nonce ? status.mining.nonce : "--";
  const hashText = status.mining.hash || "--";

  $("#nodeName").textContent = status.node_name || "节点控制台";
  $("#balance").textContent = formatBTC(status.balance);
  $("#availableBalance").textContent = `可用 ${formatBTC(status.available_balance)}`;
  $("#height").textContent = status.height;
  $("#tipHash").textContent = shortHash(status.tip_hash, 8);
  $("#tipHashFull").textContent = status.tip_hash || "--";
  $("#peerCount").textContent = `${status.peers.inbound} / ${status.peers.outbound}`;
  $("#peerDetail").textContent = `入站 ${status.peers.inbound}，出站 ${status.peers.outbound}`;
  $("#miningStatus").textContent = status.mining.status;
  const policy = status.difficulty_policy || {};
  const targetText = policy.auto ? ` · 目标 ${formatDuration(policy.target_block_seconds)}/块` : "";
  $("#difficulty").textContent = `难度 ${status.difficulty}${targetText}`;
  $("#miningDifficulty").textContent = status.difficulty;
  if ($("#difficultyInput") && document.activeElement !== $("#difficultyInput")) {
    $("#difficultyInput").value = status.difficulty;
  }
  $("#mempoolCount").textContent = `${status.mempool.count} 笔`;
  $("#mempoolBytes").textContent = formatBytes(status.mempool.bytes);
  $("#version").textContent = `v${status.version}`;
  $("#lastBlockTime").textContent = `上一区块 ${formatTime(status.last_block_time)}`;
  $("#miningBadge").textContent = status.mining.status;
  $("#miningBadge").className = `status-badge ${miningStateClass}`;
  $("#miningHint").textContent = isMining
    ? `正在尝试满足 ${"0".repeat(status.difficulty)}... 前缀的区块 hash，目标 ${formatDuration(policy.target_block_seconds || 60)} 出一块。`
    : status.mining.hash
      ? "挖矿已暂停。下面保留的是最近一次 nonce/hash，链头 hash 表示最新已确认区块。"
      : "点击开始挖矿后，节点会在后台生成候选区块并刷新 nonce。";
  $("#nonceLabel").textContent = isMining ? "当前 Nonce" : "最近 Nonce";
  $("#hashLabel").textContent = isMining ? "候选 Hash" : "最近尝试 Hash";
  $("#nonce").textContent = nonceText;
  $("#currentHash").textContent = hashText;
  $("#currentHash").title = hashText;
  $("#walletAddress").textContent = status.wallet.address || "--";
  $("#walletAddressShort").textContent = status.wallet.address || "--";
  $("#walletAddressShort").title = status.wallet.address || "";
  $("#tipHashFull").title = status.tip_hash || "";
  $("#walletName").textContent = status.wallet.name || "default";
  $("#receiveBalance").textContent = formatBTC(status.balance);
  $("#receivePeers").textContent = `${status.peers.total} 个节点`;
  renderJoinInfo(status);
  renderLabGuide(status);
  $("#startMiningBtn").disabled = isMining;
  $("#stopMiningBtn").disabled = !isMining;
  renderLogs(status.logs || []);
}

function renderJoinInfo(status) {
  const network = status.network || {};
  setText("#joinWebUrl", network.web_url || "--");
  setText("#joinP2pAddress", network.p2p_address || "--");
  setText("#joinNetworkId", network.network_id || "--");
  setText("#joinParamsHash", shortHash(network.chain_params_hash || "", 12));
  const qrText = network.p2p_address || "";
  setText("#joinQrText", qrText || "--");
  drawJoinCode(qrText);
}

function drawJoinCode(value) {
  const canvas = $("#joinQr");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const size = 29;
  const scale = Math.floor(canvas.width / size);
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#111715";

  function square(x, y, w) {
    ctx.fillRect(x * scale, y * scale, w * scale, w * scale);
  }
  function finder(x, y) {
    square(x, y, 7);
    ctx.fillStyle = "#ffffff";
    square(x + 1, y + 1, 5);
    ctx.fillStyle = "#111715";
    square(x + 2, y + 2, 3);
  }

  finder(1, 1);
  finder(size - 8, 1);
  finder(1, size - 8);

  let seed = 2166136261;
  for (const ch of value || "btc-simulator") {
    seed ^= ch.charCodeAt(0);
    seed = Math.imul(seed, 16777619);
  }
  for (let y = 1; y < size - 1; y += 1) {
    for (let x = 1; x < size - 1; x += 1) {
      const inFinder =
        (x < 9 && y < 9) ||
        (x > size - 10 && y < 9) ||
        (x < 9 && y > size - 10);
      if (inFinder) continue;
      seed ^= seed << 13;
      seed ^= seed >>> 17;
      seed ^= seed << 5;
      if ((seed >>> 0) % 5 < 2) square(x, y, 1);
    }
  }
}

function renderLogs(logs) {
  const root = $("#logs");
  root.innerHTML = "";
  if (!logs.length) {
    const empty = document.createElement("div");
    empty.className = "log-item";
    empty.innerHTML = `<span class="log-time">--</span><span>暂无日志</span>`;
    root.appendChild(empty);
    return;
  }
  for (const item of logs) {
    const row = document.createElement("div");
    row.className = "log-item";
    row.innerHTML = `<span class="log-time">${new Date(item.time * 1000).toLocaleTimeString()}</span><span>${item.message}</span>`;
    root.appendChild(row);
  }
}

async function refreshStatus() {
  const status = await api("/api/status");
  renderStatus(status);
}

async function refreshPeers() {
  const data = await api("/api/peers");
  const body = $("#peersTable");
  body.innerHTML = "";
  if (!data.peers.length) {
    body.innerHTML = `<tr><td colspan="8">暂无节点</td></tr>`;
    return;
  }
  data.peers.forEach((peer, index) => {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>${index + 1}</td>
      <td>${peer.ip || ""}</td>
      <td>${peer.port || ""}</td>
      <td>${peer.name || ""}</td>
      <td class="mono">${shortHash(peer.address || "", 8)}</td>
      <td>${peer.status || ""}</td>
      <td>${peer.direction || ""}</td>
      <td>${formatTime(peer.last_seen)}</td>
    `;
    body.appendChild(row);
  });
}

async function refreshClassroom() {
  const data = await api("/api/classroom");
  latestClassroom = data;
  renderClassroom(data);
  renderLabGuide(latestStatus);
}

function renderClassroom(data) {
  const nodes = data.nodes || [];
  const heights = nodes.map((node) => Number(node.height || 0));
  const mining = nodes.filter((node) => String(node.mining_status || "").includes("挖矿"));
  setText("#teacherNodeCount", nodes.length);
  setText("#teacherMaxHeight", heights.length ? Math.max(...heights) : 0);
  setText("#teacherMiningCount", mining.length);
  setText("#teacherMismatchCount", data.mismatch_count || 0);

  const alert = $("#classroomAlert");
  if (alert) {
    alert.hidden = !data.mismatch_count;
    alert.textContent = data.mismatch_count
      ? `检测到 ${data.mismatch_count} 个节点网络参数不一致，请检查 network_id 或难度规则。`
      : "";
  }

  const body = $("#classroomTable");
  body.innerHTML = "";
  if (!nodes.length) {
    body.innerHTML = `<tr><td colspan="8">暂无节点</td></tr>`;
    return;
  }
  nodes.forEach((node) => {
    const row = document.createElement("tr");
    const status = node.status || "";
    if (status === "参数不匹配" || node.mismatch_reason) row.className = "warning-row";
    row.innerHTML = `
      <td>${node.name || node.node_name || ""}</td>
      <td class="mono">${node.ip || ""}:${node.port || ""}</td>
      <td>${node.height ?? "--"}</td>
      <td>${node.difficulty ?? "--"}</td>
      <td>${node.mining_status || "--"}</td>
      <td>${status || "--"}</td>
      <td class="mono">${shortHash(node.chain_params_hash || "", 8)}</td>
      <td>${node.mismatch_reason || ""}</td>
    `;
    body.appendChild(row);
  });
}

function renderLabGuide(status) {
  const root = $("#labList");
  if (!root || !status) return;
  const classroom = latestClassroom || {};
  const taskStatus = {
    wallet: Boolean(status.wallet?.address),
    join: Boolean(status.network?.p2p_address),
    peer: Number(status.peers?.total || 0) > 0,
    mine: Number(status.height || 0) > 0 || Number(status.balance || 0) > 0,
    tx: Number(status.mempool?.count || 0) > 0,
    sync: Number(classroom.mismatch_count || 0) === 0 && Number(status.peers?.total || 0) > 0,
  };
  root.innerHTML = "";
  labTasks.forEach(([key, title, detail], index) => {
    const done = Boolean(taskStatus[key]);
    const item = document.createElement("div");
    item.className = `lab-item ${done ? "done" : ""}`;
    item.innerHTML = `
      <span class="lab-index">${index + 1}</span>
      <div>
        <strong>${title}</strong>
        <small>${detail}</small>
      </div>
      <span class="lab-state">${done ? "完成" : "待做"}</span>
    `;
    root.appendChild(item);
  });
}

function setupTabs() {
  $$(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      $$(".tab").forEach((item) => item.classList.remove("active"));
      $$(".tab-panel").forEach((item) => item.classList.remove("active"));
      tab.classList.add("active");
      $(`#tab-${tab.dataset.tab}`).classList.add("active");
      if (tab.dataset.tab === "nodes") refreshPeers().catch((err) => showToast(err.message));
      if (tab.dataset.tab === "explorer") refreshBlocks().catch((err) => showToast(err.message));
      if (tab.dataset.tab === "classroom") refreshClassroom().catch((err) => showToast(err.message));
      if (tab.dataset.tab === "lab") refreshClassroom().catch(() => {});
    });
  });
}

function setupActions() {
  $("#refreshBtn").addEventListener("click", () => refreshStatus().catch((err) => showToast(err.message)));
  $("#refreshLogsBtn").addEventListener("click", () => refreshStatus().catch((err) => showToast(err.message)));
  $("#refreshPeersBtn").addEventListener("click", () => refreshPeers().catch((err) => showToast(err.message)));
  $("#refreshBlocksBtn").addEventListener("click", () => refreshBlocks().catch((err) => showToast(err.message)));
  $("#refreshClassroomBtn").addEventListener("click", () => refreshClassroom().catch((err) => showToast(err.message)));
  $("#refreshLabBtn").addEventListener("click", () => {
    refreshStatus().catch((err) => showToast(err.message));
    refreshClassroom().catch(() => {});
  });
  $("#syncClassroomBtn").addEventListener("click", syncBlocks);
  $("#copyP2pBtn").addEventListener("click", () => copyText(latestStatus?.network?.p2p_address, "P2P 地址"));
  $("#copyWebBtn").addEventListener("click", () => copyText(latestStatus?.network?.web_url, "Web 地址"));
  $("#copyJoinBundleBtn").addEventListener("click", () => {
    const network = latestStatus?.network || {};
    copyText(
      [
        `Web: ${network.web_url || ""}`,
        `P2P: ${network.p2p_address || ""}`,
        `Network ID: ${network.network_id || ""}`,
        `Params: ${network.chain_params_hash || ""}`,
      ].join("\n"),
      "入网信息",
    );
  });
  $("#prevBlocksBtn").addEventListener("click", () => {
    blocksOffset = Math.max(0, blocksOffset - blocksLimit);
    refreshBlocks().catch((err) => showToast(err.message));
  });
  $("#nextBlocksBtn").addEventListener("click", () => {
    blocksOffset += blocksLimit;
    refreshBlocks().catch((err) => showToast(err.message));
  });
  $("#blockSearchBtn").addEventListener("click", searchBlock);
  $("#blockSearchInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") searchBlock();
  });
  $("#syncTopBtn").addEventListener("click", syncBlocks);
  $("#syncNodesBtn").addEventListener("click", syncBlocks);
  $("#difficultyForm").addEventListener("submit", saveDifficulty);
  $("#resetChainBtn").addEventListener("click", resetChain);

  $("#startMiningBtn").addEventListener("click", async () => {
    try {
      await api("/api/mining/start", { method: "POST" });
      showToast("挖矿已启动");
      await refreshStatus();
    } catch (err) {
      showToast(err.message);
    }
  });

  $("#stopMiningBtn").addEventListener("click", async () => {
    try {
      await api("/api/mining/stop", { method: "POST" });
      showToast("挖矿已暂停");
      await refreshStatus();
    } catch (err) {
      showToast(err.message);
    }
  });

  $("#autoFeeBtn").addEventListener("click", () => {
    $("#feeInput").value = "0.01";
    showToast("手续费已填入 0.01 BTC");
  });

  $("#clearSendBtn").addEventListener("click", () => {
    $("#sendForm").reset();
    $("#feeInput").value = "0.01";
    $("#confirmationsInput").value = "10";
  });

  $("#copyAddressBtn").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(latestStatus?.wallet?.address || "");
      showToast("地址已复制");
    } catch (_err) {
      showToast("复制失败");
    }
  });

  $("#generateAddressBtn").addEventListener("click", async () => {
    try {
      await api("/api/wallet/generate", {
        method: "POST",
        body: JSON.stringify({ name: "default" }),
      });
      showToast("新地址已生成");
      await refreshStatus();
    } catch (err) {
      showToast(err.message);
    }
  });

  $("#sendForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = {
      receiver: $("#receiverInput").value.trim(),
      amount: Number($("#amountInput").value),
      fee: Number($("#feeInput").value),
      confirmations: Number($("#confirmationsInput").value || 10),
      note: $("#noteInput").value.trim() || null,
    };
    try {
      const result = await api("/api/transactions", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      showToast(`交易已进入内存池：${shortHash(result.tx_id, 8)}`);
      await refreshStatus();
    } catch (err) {
      showToast(err.message);
    }
  });

  $("#peerForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const result = await api("/api/peers", {
        method: "POST",
        body: JSON.stringify({
          ip: $("#peerIpInput").value.trim(),
          port: Number($("#peerPortInput").value),
        }),
      });
      showToast(result.message);
      await refreshPeers();
    } catch (err) {
      showToast(err.message);
    }
  });
}

async function syncBlocks() {
  try {
    const result = await api("/api/sync", { method: "POST" });
    showToast(`已向 ${result.requested_peers} 个节点请求同步`);
  } catch (err) {
    showToast(err.message);
  }
}

async function saveDifficulty(event) {
  event.preventDefault();
  const difficulty = Number($("#difficultyInput").value);
  if (!Number.isInteger(difficulty) || difficulty < 0 || difficulty > 12) {
    showToast("难度必须是 0 到 12 的整数");
    return;
  }
  try {
    const result = await api("/api/settings/difficulty", {
      method: "POST",
      body: JSON.stringify({ difficulty }),
    });
    const suffix = result.mining_stopped ? "，挖矿已暂停" : "";
    showToast(`难度已设置为 ${result.difficulty}${suffix}`);
    await refreshStatus();
    await refreshBlocks(false);
  } catch (err) {
    showToast(err.message);
  }
}

async function resetChain() {
  const confirmed = window.confirm("确定要重置本节点区块链吗？这会清空历史区块、交易和内存池，只保留创世块。");
  if (!confirmed) return;
  try {
    const result = await api("/api/chain/reset", { method: "POST" });
    blocksOffset = 0;
    showToast(`已重置到创世块：${shortHash(result.tip_hash, 8)}`);
    await refreshStatus();
    await refreshBlocks();
  } catch (err) {
    showToast(err.message);
  }
}

async function refreshBlocks(selectFirst = true) {
  const data = await api(`/api/blocks?limit=${blocksLimit}&offset=${blocksOffset}`);
  renderBlocks(data.blocks || []);
  $("#prevBlocksBtn").disabled = blocksOffset === 0;
  $("#nextBlocksBtn").disabled = !data.blocks || data.blocks.length < blocksLimit;
  if (selectFirst && data.blocks && data.blocks.length) {
    await loadBlockDetail(data.blocks[0].height);
  }
}

function renderBlocks(blocks) {
  const body = $("#blocksTable");
  body.innerHTML = "";
  if (!blocks.length) {
    body.innerHTML = `<tr><td colspan="6">暂无区块</td></tr>`;
    clearBlockDetail();
    return;
  }

  blocks.forEach((block) => {
    const row = document.createElement("tr");
    row.className = "clickable-row";
    row.dataset.height = block.height;
    row.innerHTML = `
      <td>${block.height}</td>
      <td class="mono">${shortHash(block.hash, 9)}</td>
      <td>${block.tx_count}</td>
      <td>${block.difficulty}</td>
      <td>${block.nonce}</td>
      <td>${formatTime(block.timestamp)}</td>
    `;
    row.addEventListener("click", () => loadBlockDetail(block.height));
    body.appendChild(row);
  });
}

async function searchBlock() {
  const identifier = $("#blockSearchInput").value.trim();
  if (!identifier) {
    showToast("请输入区块高度或 hash");
    return;
  }
  try {
    await loadBlockDetail(identifier);
  } catch (err) {
    showToast(err.message);
  }
}

async function loadBlockDetail(identifier) {
  const detail = await api(`/api/blocks/${encodeURIComponent(identifier)}`);
  renderBlockDetail(detail);
  $$("#blocksTable tr").forEach((row) => {
    row.classList.toggle("active", Number(row.dataset.height) === Number(detail.height));
  });
}

function renderBlockDetail(detail) {
  $("#blockDetailTitle").textContent = `高度 ${detail.height}`;
  setText("#detailHash", detail.hash || "--");
  setText("#detailPrevHash", detail.prev_hash || "--");
  setText("#detailMerkle", detail.merkle_root || "--");
  setText("#detailTime", formatTime(detail.timestamp));
  setText("#detailTxCount", `${detail.tx_count} 笔`);
  setText("#detailDifficulty", detail.difficulty);
  setText("#detailNonce", detail.nonce);

  const txBody = $("#blockTxTable");
  txBody.innerHTML = "";
  const transactions = detail.transactions || [];
  if (!transactions.length) {
    txBody.innerHTML = `<tr><td colspan="6">创世块无交易</td></tr>`;
    return;
  }
  transactions.forEach((tx) => {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>${tx.type || ""}</td>
      <td class="mono">${shortHash(tx.tx_id || "", 8)}</td>
      <td class="mono">${shortHash(tx.sender || "", 8)}</td>
      <td class="mono">${shortHash(tx.receiver || "", 8)}</td>
      <td>${formatBTC(tx.amount)}</td>
      <td>${formatBTC(tx.fee)}</td>
    `;
    txBody.appendChild(row);
  });
}

function clearBlockDetail() {
  $("#blockDetailTitle").textContent = "未选择";
  ["#detailHash", "#detailPrevHash", "#detailMerkle", "#detailTime", "#detailTxCount", "#detailDifficulty", "#detailNonce"].forEach((selector) => {
    setText(selector, "--");
  });
  $("#blockTxTable").innerHTML = `<tr><td colspan="6">请选择一个区块</td></tr>`;
}

function connectEvents() {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${window.location.host}/ws/events`);
  socket.addEventListener("message", (event) => {
    renderStatus(JSON.parse(event.data));
  });
  socket.addEventListener("close", () => {
    setTimeout(connectEvents, 1500);
  });
}

setupTabs();
setupAdminVisibility();
setupActions();
refreshStatus().catch((err) => showToast(err.message));
refreshPeers().catch(() => {});
refreshBlocks().catch(() => {});
refreshClassroom().catch(() => {});
connectEvents();
