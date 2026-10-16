/* 学生控制台：登录、挖矿、钱包、红包、藏品市场。

   私钥在这台设备上生成并保存在 localStorage，服务器只收到公钥和签名。
   矿工程序默认用服务器上的 miner.js，也可以载入本机改过的版本。 */
(function () {
  "use strict";

  const { API, Feed, canonicalJson, formatCoins, parseCoins, formatHashrate, formatTime, escapeHtml, shortAddress, notify } = window.Sim;

  const STORAGE_PRIVATE_KEY = "btc-sim.private-key";
  const STORAGE_NICKNAME = "btc-sim.nickname";
  const STORAGE_TOKEN = "btc-sim.token";
  const STORAGE_MINER = "btc-sim.custom-miner";
  const JOB_POLL_MS = 4000;
  const RATE_WINDOW_MS = 4000;
  const LOG_LIMIT = 12;
  const MINER_MAX_BYTES = 262144;

  const $ = (id) => document.getElementById(id);

  const state = {
    privateKey: "",
    publicKey: "",
    address: "",
    token: "",
    nickname: "",
    job: null,
    workerJobKey: "",
    workerJobId: "",
    me: null,
    tick: null,
    blocks: [],
    worker: null,
    workerUrl: "",
    customMiner: false,
    mining: false,
    lastRejection: "",
    rate: 0,
    totalHashes: 0,
    rateSamples: [],
    leaderboardTab: "hashrate",
    log: [],
  };

  const api = new API();
  const feed = new Feed(handleEvent, handleConnection);

  /* ---------- 钱包与登录 ---------- */

  function loadWallet() {
    let privateKey = localStorage.getItem(STORAGE_PRIVATE_KEY);
    if (!privateKey) {
      privateKey = SimCrypto.generatePrivateKey();
      localStorage.setItem(STORAGE_PRIVATE_KEY, privateKey);
    }
    state.privateKey = privateKey;
    state.publicKey = SimCrypto.publicKeyFromPrivate(privateKey);
    state.address = SimCrypto.addressFromPublicKey(state.publicKey);
    $("login-address").textContent = shortAddress(state.address);
    $("wallet-address").textContent = state.address;
  }

  function loginMessage(nickname, nonce) {
    return `btc-sim-login|${nickname}|${nonce}`;
  }

  async function login(nickname) {
    const begin = await api.post("/api/auth/begin", { nickname });
    const signature = SimCrypto.sign(state.privateKey, loginMessage(nickname, begin.nonce));
    return api.post("/api/auth/finish", {
      nickname,
      nonce: begin.nonce,
      public_key: state.publicKey,
      signature,
    });
  }

  async function handleLoginSubmit(event) {
    event.preventDefault();
    const nickname = $("login-nickname").value.trim();
    const error = $("login-error");
    error.hidden = true;
    try {
      const result = await login(nickname);
      state.token = result.token;
      state.nickname = result.player.nickname;
      api.token = result.token;
      localStorage.setItem(STORAGE_NICKNAME, state.nickname);
      localStorage.setItem(STORAGE_TOKEN, result.token);
      enterConsole(result.player);
    } catch (problem) {
      error.textContent = problem.message;
      error.hidden = false;
    }
  }

  function logout() {
    localStorage.removeItem(STORAGE_TOKEN);
    localStorage.removeItem(STORAGE_NICKNAME);
    location.reload();
  }

  async function restoreSession() {
    const token = localStorage.getItem(STORAGE_TOKEN);
    const nickname = localStorage.getItem(STORAGE_NICKNAME);
    if (!token || !nickname) {
      return false;
    }
    api.token = token;
    try {
      const me = await api.get("/api/me");
      state.token = token;
      state.nickname = nickname;
      enterConsole(me);
      return true;
    } catch (problem) {
      localStorage.removeItem(STORAGE_TOKEN);
      api.token = "";
      return false;
    }
  }

  function enterConsole(player) {
    $("login-overlay").hidden = true;
    $("identity").hidden = false;
    $("logout").hidden = false;
    $("me-nickname").textContent = state.nickname;
    state.me = player;
    renderWallet();
    feed.connect(state.token);
    loadPublic();
    loadRecentBlocks();
    pollJob();
    setInterval(pollJob, JOB_POLL_MS);
    setInterval(refreshMe, 10000);
    startMining();
  }

  /* ---------- 实时推送 ---------- */

  function handleConnection(online) {
    $("connection").classList.toggle("is-online", online);
    $("connection-text").textContent = online ? "已连接课堂网络" : "连接断开，正在重连";
  }

  function handleEvent(event) {
    const data = event.data || {};
    if (event.type === "hello" || event.type === "tick") {
      state.tick = data;
      renderLive();
    } else if (event.type === "block") {
      handleBlock(data);
    } else if (event.type === "announcement") {
      renderAnnouncement(data);
    } else if (event.type === "reset") {
      state.blocks = [];
      notify("老师已经开启新一局，链重新开始", "info");
      refreshMe();
      pollJob();
    } else if (event.type === "players" || event.type === "settings") {
      refreshMe();
    }
  }

  function handleBlock(block) {
    state.blocks.unshift(block);
    state.blocks = state.blocks.slice(0, 8);
    renderBlocks();
    const mine = block.finder === state.address;
    pushLog(
      `区块 #${block.height} 由 ${mine ? "你" : shortAddress(block.finder)} 挖到，奖励 ${formatCoins(block.subsidy + block.fees)} 币`,
      "block",
    );
    if (mine) {
      notify(`你挖到了区块 #${block.height}！`, "success");
    }
    refreshMe();
    /* 别人出块后当前作业立即作废，马上换新作业，别把算力浪费在旧链尖上。 */
    pollJob();
  }

  /* ---------- 挖矿 ---------- */

  function ensureWorker() {
    if (!state.worker) {
      state.worker = createWorker();
      state.worker.onmessage = handleWorkerMessage;
      state.worker.onerror = handleWorkerError;
    }
    return state.worker;
  }

  function createWorker() {
    const source = localStorage.getItem(STORAGE_MINER);
    state.customMiner = Boolean(source);
    if (!source) {
      return new Worker("/static/miner.js");
    }
    const cryptoUrl = new URL("/static/crypto.js", location.href).href;
    const blob = new Blob([`importScripts(${JSON.stringify(cryptoUrl)});\n${source}`], {
      type: "text/javascript",
    });
    state.workerUrl = URL.createObjectURL(blob);
    return new Worker(state.workerUrl);
  }

  function disposeWorker() {
    if (state.worker) {
      state.worker.terminate();
      state.worker = null;
    }
    if (state.workerUrl) {
      URL.revokeObjectURL(state.workerUrl);
      state.workerUrl = "";
    }
  }

  function handleWorkerError(event) {
    event.preventDefault();
    state.mining = false;
    state.rate = 0;
    state.rateSamples = [];
    updateMineButton();
    renderMiningStats();
    pushLog(`矿工脚本出错：${event.message || "无法运行"}`, "error");
    notify("矿工脚本出错了，可以点“用回默认”换回内置矿工", "error");
  }

  async function handleMinerFile(event) {
    const file = event.target.files[0];
    event.target.value = "";
    if (!file) {
      return;
    }
    try {
      const source = await file.text();
      if (!source.trim()) {
        throw new Error("这个文件是空的");
      }
      if (source.length > MINER_MAX_BYTES) {
        throw new Error("矿工文件不能超过 256 KB");
      }
      if (/importScripts\s*\(\s*["'][^"']*crypto\.js["']\s*\)/.test(source)) {
        throw new Error("平台已经加载好 crypto.js，删掉这行 importScripts 再载入");
      }
      localStorage.setItem(STORAGE_MINER, source);
      restartMiner(`已载入 ${file.name}`);
    } catch (problem) {
      notify(problem.message, "error");
    }
  }

  function resetMiner() {
    localStorage.removeItem(STORAGE_MINER);
    restartMiner("已换回默认矿工");
  }

  function restartMiner(message) {
    disposeWorker();
    state.totalHashes = 0;
    state.rate = 0;
    state.rateSamples = [];
    state.workerJobKey = "";
    state.workerJobId = "";
    renderMiningStats();
    ensureWorker();
    pushLog(message);
    if (state.mining) {
      pollJob();
    }
  }

  function startMining() {
    state.mining = true;
    /* 暂停期间 Worker 里的作业已经作废，恢复时要重新下发一次。 */
    state.workerJobKey = "";
    state.workerJobId = "";
    updateMineButton();
    ensureWorker();
    pushLog("开始挖矿，正在向服务器要作业…");
    pollJob();
  }

  function stopMining() {
    state.mining = false;
    updateMineButton();
    if (state.worker) {
      state.worker.postMessage({ type: "stop" });
    }
    state.rate = 0;
    state.rateSamples = [];
    renderMiningStats();
    pushLog("已暂停挖矿");
  }

  function updateMineButton() {
    $("mine-toggle-label").textContent = state.mining ? "暂停挖矿" : "开始挖矿";
    $("mine-toggle").classList.toggle("is-running", state.mining);
    $("mine-toggle").querySelector("use").setAttribute("href", state.mining ? "#i-pause" : "#i-play");
  }

  async function pollJob() {
    if (!state.token) {
      return;
    }
    try {
      adoptJob(await api.post("/api/mining/job"));
    } catch (problem) {
      pushLog(`获取作业失败：${problem.message}`, "error");
    }
  }

  function adoptJob(job) {
    state.job = job;
    renderJob();
    renderMiningStats();
    const key = `${job.job_id}:${job.share_bits}`;
    if (!state.mining || key === state.workerJobKey) {
      return;
    }
    state.workerJobKey = key;
    state.workerJobId = job.job_id;
    ensureWorker().postMessage({ type: "job", job });
  }

  function handleWorkerMessage(event) {
    const message = event.data;
    if (message.type === "progress") {
      state.totalHashes += message.hashes;
      updateRate(state.totalHashes);
      renderMiningStats();
    } else if (message.type === "share") {
      submitShare(message);
    }
  }

  function updateRate(hashes) {
    const now = performance.now();
    state.rateSamples.push({ at: now, hashes });
    while (state.rateSamples.length > 2 && now - state.rateSamples[0].at > RATE_WINDOW_MS) {
      state.rateSamples.shift();
    }
    const first = state.rateSamples[0];
    const span = Math.max(now - first.at, 500);
    state.rate = ((hashes - first.hashes) / span) * 1000;
  }

  async function submitShare(share) {
    if (!state.workerJobId) {
      return;
    }
    try {
      const result = await api.post("/api/mining/share", { job_id: state.workerJobId, nonce: share.nonce });
      if (!result.accepted) {
        const reason = String(result.reason);
        if (reason.includes("作业")) {
          pollJob();
        }
        if (reason !== state.lastRejection) {
          state.lastRejection = reason;
          pushLog(`份额被拒绝：${reason}`, "error");
        }
        return;
      }
      state.lastRejection = "";
      pushLog(`提交份额成功：+${result.work} 工作量（${result.share_bits} 位难度）`);
      if (result.job) {
        adoptJob(result.job);
      }
      refreshMe();
    } catch (problem) {
      pushLog(`提交份额失败：${problem.message}`, "error");
    }
  }

  /* ---------- 数据刷新 ---------- */

  async function refreshMe() {
    if (!state.token) {
      return;
    }
    try {
      state.me = await api.get("/api/me");
      renderWallet();
      renderNfts();
      renderPackets();
      renderMiningStats();
    } catch (problem) {
      if (String(problem.message).includes("登录")) {
        logout();
      }
    }
  }

  async function loadRecentBlocks() {
    try {
      const data = await api.get("/api/blocks?limit=8");
      state.blocks = data.blocks;
      renderBlocks();
    } catch (problem) {
      pushLog(`读取区块失败：${problem.message}`, "error");
    }
  }

  async function loadPublic() {
    try {
      const data = await api.get("/api/public");
      $("classroom-name").textContent = data.classroom;
      renderAnnouncement(data.announcement);
    } catch (problem) {
      pushLog(`读取课堂信息失败：${problem.message}`, "error");
    }
  }

  /* ---------- 交易 ---------- */

  function buildTransaction(type, fields) {
    const body = {
      type,
      sender: state.address,
      timestamp: Math.floor(Date.now() / 1000),
      public_key: state.publicKey,
      ...fields,
    };
    const message = canonicalJson(body);
    return {
      ...body,
      tx_id: SimCrypto.bytesToHex(SimCrypto.sha256d(SimCrypto.utf8Bytes(message))),
      signature: SimCrypto.sign(state.privateKey, message),
    };
  }

  async function submitTransaction(type, fields) {
    const tx = buildTransaction(type, fields);
    await api.post("/api/transactions", { tx });
    pushLog(`交易已进入内存池，等待打包：${type}`);
    notify("交易已提交，等下一个区块确认", "info");
  }

  function minFee() {
    return state.tick && state.tick.chain ? state.tick.chain.min_fee_sat : 10000;
  }

  function resolveReceiver(text) {
    const value = text.trim();
    if (/^sim[0-9a-f]{40}$/.test(value)) {
      return value;
    }
    const rows = state.tick && state.tick.leaderboard ? state.tick.leaderboard.rows : [];
    const hit = rows.find((row) => row.nickname === value);
    if (!hit) {
      throw new Error("没有找到这个昵称，检查一下拼写，或者直接填钱包地址");
    }
    return hit.address;
  }

  async function handleTransfer(event) {
    event.preventDefault();
    try {
      const receiver = resolveReceiver($("transfer-receiver").value);
      const amount = parseCoins($("transfer-amount").value);
      const fee = parseCoins($("transfer-fee").value || formatCoins(minFee(), 8));
      const note = $("transfer-note").value.trim();
      await submitTransaction("transfer", { receiver, amount, fee, ...(note ? { note } : {}) });
      $("transfer-amount").value = "";
      $("transfer-note").value = "";
      await refreshMe();
    } catch (problem) {
      notify(problem.message, "error");
    }
  }

  async function handlePacketCreate(event) {
    event.preventDefault();
    try {
      const total = parseCoins($("packet-total").value);
      const count = Number($("packet-count").value);
      const note = $("packet-note").value.trim();
      await submitTransaction("packet_create", {
        total,
        count,
        fee: minFee(),
        ...(note ? { note } : {}),
      });
      $("packet-total").value = "";
      $("packet-note").value = "";
      await refreshMe();
    } catch (problem) {
      notify(problem.message, "error");
    }
  }

  async function handleMint(event) {
    event.preventDefault();
    try {
      await submitTransaction("nft_mint", {
        name: $("mint-name").value.trim(),
        seed: SimCrypto.randomHex(16),
        fee: minFee(),
      });
      $("mint-name").value = "";
      await refreshMe();
    } catch (problem) {
      notify(problem.message, "error");
    }
  }

  async function claimPacket(packetId) {
    try {
      const ticket = await api.post(`/api/packets/${packetId}/claim-ticket`);
      await submitTransaction("packet_claim", {
        packet_id: packetId,
        index: ticket.index,
        fee: minFee(),
      });
      notify(`抢到第 ${ticket.index + 1} 个红包，金额 ${formatCoins(ticket.amount)} 币，等待打包`, "success");
      await refreshMe();
    } catch (problem) {
      notify(problem.message, "error");
    }
  }

  async function listNft(tokenId, priceText) {
    try {
      await submitTransaction("nft_list", { token_id: tokenId, price: parseCoins(priceText), fee: minFee() });
      await refreshMe();
    } catch (problem) {
      notify(problem.message, "error");
    }
  }

  async function simpleNftAction(type, tokenId) {
    try {
      await submitTransaction(type, { token_id: tokenId, fee: minFee() });
      await refreshMe();
    } catch (problem) {
      notify(problem.message, "error");
    }
  }

  /* ---------- 渲染 ---------- */

  function renderLive() {
    const chain = state.tick.chain;
    $("chain-summary").textContent = `区块高度 ${chain.height} · 出块难度 ${chain.difficulty} 位 · 下一块奖励 ${formatCoins(chain.subsidy)} 币`;
    renderAnnouncement(state.tick.announcement);
    renderLeaderboard();
    renderPackets();
    renderNfts();
    renderJob();
    if (!$("transfer-fee").value) {
      $("transfer-fee").value = formatCoins(minFee(), 8);
    }
  }

  function renderAnnouncement(announcement) {
    const box = $("announcement");
    if (!announcement || !announcement.message) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    box.textContent = `老师通知：${announcement.message}`;
  }

  function renderJob() {
    if (!state.job) {
      $("job-line").textContent = "正在获取挖矿作业…";
      return;
    }
    const job = state.job;
    $("job-line").textContent =
      `当前作业：第 ${job.height} 块 · 出块难度 ${job.difficulty} 位 · 交易 ${job.tx_count} 笔 · ` +
      `本块分给 ${job.payout_count} 人 · 你的份额难度 ${job.share_bits} 位`;
  }

  function renderMiningStats() {
    $("stat-hashrate").textContent = formatHashrate(state.rate);
    const mining = state.me ? state.me.mining : null;
    $("stat-work").textContent = (mining ? mining.stage_work : 0).toLocaleString();
    $("stat-shares").textContent = mining ? mining.shares : 0;
    $("stat-share-bits").textContent = mining ? `${mining.share_bits} 位` : "--";
    $("stat-blocks").textContent = mining ? mining.blocks_found : 0;
    $("stat-miner").textContent = state.customMiner ? "自定义矿工" : "默认矿工";
    $("reset-miner").hidden = !state.customMiner;
  }

  function renderWallet() {
    if (!state.me) {
      return;
    }
    $("wallet-balance").textContent = formatCoins(state.me.balance);
    $("wallet-mined").textContent = formatCoins(state.me.mined);
    $("wallet-address").textContent = state.me.address;
    const rows = state.me.transactions.map((tx) => {
      const outgoing = tx.sender === state.me.address;
      const kind = tx.type === "transfer" ? (outgoing ? "转出" : "转入") : labelOfType(tx.type);
      const text = tx.amount ? `${kind} ${formatCoins(tx.amount)} 币` : kind;
      return `<li class="row-item">
        <span class="tag">${escapeHtml(labelOfType(tx.type))}</span>
        <span class="grow">${escapeHtml(text)}</span>
        <span class="muted small">区块 #${tx.block_height} · ${formatTime(tx.timestamp)}</span>
      </li>`;
    });
    $("tx-list").innerHTML = rows.join("") || `<li class="empty">还没有链上交易</li>`;
  }

  function renderPackets() {
    if (!state.tick) {
      return;
    }
    const claimed = new Set((state.me ? state.me.packets_claimed : []).map((item) => item.packet_id));
    const active = state.tick.packets.active;
    const rows = active.map((packet) => {
      const mine = packet.sender === state.address;
      const already = claimed.has(packet.packet_id);
      const left = packet.count - packet.claimed_count;
      let action = `<button type="button" class="ghost" data-action="claim" data-packet="${packet.packet_id}">抢红包</button>`;
      if (mine) {
        action = `<span class="muted small">我发的</span>`;
      } else if (already) {
        action = `<span class="muted small">已领过</span>`;
      }
      return `<li class="row-item">
        <span class="tag tag-gold">红包</span>
        <span class="grow">
          <strong>${formatCoins(packet.total)} 币</strong>
          <span class="muted small">剩 ${left}/${packet.count} 个 · 来自 ${escapeHtml(nicknameOf(packet.sender))}</span>
          ${packet.note ? `<em class="note">${escapeHtml(packet.note)}</em>` : ""}
        </span>
        ${action}
      </li>`;
    });
    $("packet-list").innerHTML = rows.join("") || `<li class="empty">当前没有可抢的红包，挖到币的同学可以发一个</li>`;
  }

  let renderedNftList = "";

  function renderNfts() {
    if (!state.tick) {
      return;
    }
    const mine = state.me ? state.me.nfts : [];
    const rows = mine.map((nft) => {
      const listed = nft.price !== null;
      const action = listed
        ? `<span class="muted small">在售 ${formatCoins(nft.price)} 币</span>
           <button type="button" class="ghost" data-action="unlist" data-token="${nft.token_id}">下架</button>`
        : `<input class="price-input" type="text" inputmode="decimal" placeholder="价格" data-price="${nft.token_id}">
           <button type="button" class="ghost" data-action="list" data-token="${nft.token_id}">上架</button>`;
      return `<li class="row-item">
        <span class="tag tag-blue">我的藏品</span>
        <span class="grow">
          <strong>${escapeHtml(nft.name)}</strong>
          <span class="muted small">${nft.token_id.slice(0, 12)}… · 第 ${nft.mint_height} 块铸造</span>
        </span>
        ${action}
      </li>`;
    });

    const listings = state.tick.market.map((item) => {
      const mineListing = item.seller === state.address;
      const action = mineListing
        ? `<span class="muted small">我的挂单</span>`
        : `<button type="button" class="ghost" data-action="buy" data-token="${item.token_id}">买下</button>`;
      return `<li class="row-item">
        <span class="tag">在售</span>
        <span class="grow">
          <strong>${escapeHtml(item.name)}</strong>
          <span class="muted small">卖家 ${escapeHtml(item.seller_name)} · ${formatCoins(item.price)} 币</span>
        </span>
        ${action}
      </li>`;
    });

    const html =
      rows.join("") + listings.join("") ||
      `<li class="empty">还没有藏品，先铸造一件属于自己的纪念品</li>`;
    /* 每秒重建列表会清掉学生正在输入的价格，内容没变就不碰 DOM。 */
    if (html !== renderedNftList) {
      renderedNftList = html;
      $("nft-list").innerHTML = html;
    }
  }

  function renderLeaderboard() {
    if (!state.tick) {
      return;
    }
    const key = state.leaderboardTab === "hashrate" ? "by_hashrate" : "by_mined";
    const rows = state.tick.leaderboard[key];
    const body = $("leaderboard").querySelector("tbody");
    body.innerHTML = rows
      .map((row, index) => {
        const me = row.address === state.address ? ' class="is-me"' : "";
        const place = index + 1;
        const rank = place <= 3 ? `<span class="rank rank-${place}">${place}</span>` : place;
        return `<tr${me}>
          <td class="rank">${rank}</td>
          <td>${escapeHtml(row.nickname)}</td>
          <td>${formatHashrate(row.hashrate)}</td>
          <td>${formatCoins(row.mined)} 币</td>
          <td>${row.blocks_found}</td>
          <td>${row.online ? '<span class="online">在线</span>' : '<span class="muted">离线</span>'}</td>
        </tr>`;
      })
      .join("") || `<tr><td colspan="6" class="empty">还没有同学加入</td></tr>`;
  }

  function renderBlocks() {
    const rows = state.blocks.map(
      (block) => `<li class="row-item">
        <span class="tag">#${block.height}</span>
        <span class="grow mono">${String(block.hash).slice(0, 18)}…</span>
        <span class="muted small">难度 ${block.difficulty} · ${formatTime(block.timestamp)}</span>
      </li>`,
    );
    $("block-list").innerHTML = rows.join("") || `<li class="empty">链上还没有区块</li>`;
  }

  function pushLog(message, kind) {
    state.log.unshift({ message, kind: kind || "info", at: new Date() });
    state.log = state.log.slice(0, LOG_LIMIT);
    $("share-log").innerHTML = state.log
      .map(
        (item) => `<li class="log-${item.kind}">
          <span class="muted small">${item.at.toLocaleTimeString("zh-CN", { hour12: false })}</span>
          ${escapeHtml(item.message)}
        </li>`,
      )
      .join("");
  }

  function nicknameOf(address) {
    const rows = state.tick && state.tick.leaderboard ? state.tick.leaderboard.rows : [];
    const hit = rows.find((row) => row.address === address);
    return hit ? hit.nickname : shortAddress(address);
  }

  function labelOfType(type) {
    return {
      transfer: "转账",
      nft_mint: "铸造",
      nft_list: "上架",
      nft_unlist: "下架",
      nft_buy: "购买",
      packet_create: "发红包",
      packet_claim: "领红包",
      packet_refund: "退红包",
      coinbase: "挖矿奖励",
    }[type] || type;
  }

  /* ---------- 事件绑定 ---------- */

  function bindEvents() {
    $("login-form").addEventListener("submit", handleLoginSubmit);
    $("logout").addEventListener("click", logout);
    $("mine-toggle").addEventListener("click", () => (state.mining ? stopMining() : startMining()));
    $("transfer-form").addEventListener("submit", handleTransfer);
    $("packet-form").addEventListener("submit", handlePacketCreate);
    $("mint-form").addEventListener("submit", handleMint);
    $("copy-address").addEventListener("click", copyAddress);
    $("load-miner").addEventListener("click", () => $("miner-file").click());
    $("miner-file").addEventListener("change", handleMinerFile);
    $("reset-miner").addEventListener("click", resetMiner);

    $("leaderboard-tabs").addEventListener("click", (event) => {
      const tab = event.target.closest(".tab");
      if (!tab) {
        return;
      }
      state.leaderboardTab = tab.dataset.tab;
      for (const button of $("leaderboard-tabs").querySelectorAll(".tab")) {
        button.classList.toggle("is-active", button === tab);
      }
      renderLeaderboard();
    });

    $("packet-list").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-action='claim']");
      if (button) {
        claimPacket(button.dataset.packet);
      }
    });

    $("nft-list").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-action]");
      if (!button) {
        return;
      }
      const tokenId = button.dataset.token;
      if (button.dataset.action === "list") {
        const input = $("nft-list").querySelector(`input[data-price="${tokenId}"]`);
        listNft(tokenId, input ? input.value : "");
      } else if (button.dataset.action === "unlist") {
        simpleNftAction("nft_unlist", tokenId);
      } else if (button.dataset.action === "buy") {
        simpleNftAction("nft_buy", tokenId);
      }
    });
  }

  async function copyAddress() {
    try {
      await navigator.clipboard.writeText(state.address);
      notify("钱包地址已复制", "success");
    } catch (problem) {
      notify(`复制失败，请手动选择地址：${state.address}`, "error");
    }
  }

  function boot() {
    loadWallet();
    bindEvents();
    restoreSession();
  }

  boot();
})();
