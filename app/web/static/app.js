/* 学生控制台：登录、挖矿、钱包、红包、藏品市场。

   私钥在这台设备上生成并保存在 localStorage，服务器只收到公钥和签名。 */
(function () {
  "use strict";

  const { setText, setHtml, deferWhileSelected, API, Feed, canonicalJson, formatCoins, parseCoins, formatHashrate, formatTime, formatCountdown, auctionPrice, escapeHtml, shortAddress, notify } = window.Sim;

  const STORAGE_PRIVATE_KEY = "btc-sim.private-key";
  const STORAGE_NICKNAME = "btc-sim.nickname";
  const STORAGE_TOKEN = "btc-sim.token";
  const STORAGE_MINER = "btc-sim.custom-miner";
  const STORAGE_MINING_INTENT = "btc-sim.browser-mining-intent";
  const JOB_POLL_MS = 4000;
  const RATE_WINDOW_MS = 4000;
  const LOG_LIMIT = 200;
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

    workerShareBits: 0,
    me: null,
    tick: null,
    blocks: [],
    blockPages: 1,
    loadingBlocks: false,
    blocksRefreshPending: false,
    worker: null,
    workerUrl: "",
    customMiner: false,
    mining: false,
    localWallet: false,
    localMinerName: "",
    lastRejection: "",
    rate: 0,
    totalHashes: 0,
    rateSamples: [],
    leaderboardTab: "hashrate",
    log: [],
  };

  const api = new API();
  const feed = new Feed(handleEvent, handleConnection);
  let sessionRecovery = null;
  let pageRecovery = null;
  let consoleEntered = false;
  let jobPollGeneration = 0;
  let pollTimer = null;
  let meTimer = null;
  api.recoverSession = recoverSession;

  async function recoverSession(rejectedToken) {
    if (rejectedToken && state.token && rejectedToken !== state.token) return;
    if (sessionRecovery) return sessionRecovery;
    sessionRecovery = (async () => {
      const nickname = state.nickname || localStorage.getItem(STORAGE_NICKNAME);
      if (!state.localWallet && (!nickname || !state.privateKey)) throw new Error("请先输入昵称进入课堂");
      const result = state.localWallet ? await localPost("/api/local/wallet") : await login(nickname);
      state.token = api.token = result.token;
      state.nickname = result.player.nickname;
      state.me = result.player;
      if (!state.localWallet) {
        localStorage.setItem(STORAGE_TOKEN, result.token);
        localStorage.setItem(STORAGE_NICKNAME, state.nickname);
      }
      if (consoleEntered) {
        setText($("me-nickname"), state.nickname);
        feed.connect(state.token);
      }
    })();
    try { await sessionRecovery; }
    finally { sessionRecovery = null; }
  }

  async function recoverPage() {
    if (document.hidden || pageRecovery) return pageRecovery;
    pageRecovery = (async () => {
      if (!consoleEntered) {
        if (localStorage.getItem(STORAGE_TOKEN)) await restoreSession();
        return;
      }
      try {
        const result = await api.get("/api/me");
        state.me = result;
        feed.connect(state.token);
        const previousDomain = state.transactionDomain;
        await loadPublic();
        if (!state.localWallet && previousDomain && previousDomain !== state.transactionDomain && state.mining) stopMining();
        if (!state.localWallet && state.mining) {
          disposeWorker();
          state.workerJobKey = "";
          state.workerJobId = "";
          try { ensureWorker(); }
          catch (problem) {
            state.mining = false;
            rememberBrowserMining(false);
            updateMineButton();
            throw problem;
          }
        }
        await refreshMe();
        await pollJob();
      } catch (problem) {
        pushLog(`恢复连接失败：${problem.message}，联网后将重试`, "error");
        notify("暂未恢复连接，请检查网络后重试", "error");
      }
    })();
    try { await pageRecovery; }
    finally { pageRecovery = null; }
  }

  async function localPost(path, body) {
    const response = await fetch(path, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Local-Wallet": "1",
                "Authorization": `Bearer ${state.token}`},
      body: JSON.stringify(body || {}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "本机钱包请求失败");
    return data;
  }

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
    setText($("login-address"), shortAddress(state.address));
    $("wallet-address").value = state.address;
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
    const button = event.currentTarget.querySelector('button[type="submit"]');
    if (button.disabled) return;
    button.disabled = true;
    setText(button, "进入中…");
    try {
      const result = await login(nickname);
      state.token = result.token;
      state.nickname = result.player.nickname;
      api.token = result.token;
      localStorage.setItem(STORAGE_NICKNAME, state.nickname);
      localStorage.setItem(STORAGE_TOKEN, result.token);
      await enterConsole(result.player);
    } catch (problem) {
      setText(error, problem.message);
      error.hidden = false;
      $("login-overlay").hidden = false;
    } finally {
      button.disabled = false;
      setText(button, "进入");
    }
  }

  function logout() {
    rememberBrowserMining(false);
    localStorage.removeItem(STORAGE_TOKEN);
    localStorage.removeItem(STORAGE_NICKNAME);
    location.reload();
  }

  async function restoreSession() {
    const token = localStorage.getItem(STORAGE_TOKEN);
    const nickname = localStorage.getItem(STORAGE_NICKNAME);
    if (!token || !nickname) {
      rememberBrowserMining(false);
      return false;
    }
    api.token = token;
    state.token = token;
    state.nickname = nickname;
    try {
      const me = await api.get("/api/me");
      state.token = api.token;
      state.nickname = me.nickname;
      await enterConsole(me);
      return true;
    } catch (problem) {
      $("login-nickname").value = nickname;
      setText($("login-error"), `暂未恢复登录：${problem.message}。可以点击进入重试。`);
      $("login-error").hidden = false;
      return false;
    }
  }

  async function enterConsole(player) {
    consoleEntered = true;
    $("login-overlay").hidden = true;
    $("identity").hidden = false;
    $("topbar-wallet").hidden = false;
    $("logout").hidden = false;
    setText($("me-nickname"), state.nickname);
    $("me-nickname").title = state.nickname;
    state.me = player;
    renderWallet();
    feed.connect(state.token);
    await loadPublic();
    loadRecentBlocks();
    pollJob();
    clearInterval(pollTimer);
    clearInterval(meTimer);
    pollTimer = setInterval(pollJob, JOB_POLL_MS);
    meTimer = setInterval(refreshMe, 10000);
    if (state.localWallet) {
      $("peer-form").hidden = false;
      $("wallet-backup-form").hidden = true;
      $("logout").hidden = true;
      updateMineButton();
      renderMiningStats();
    } else {
      resumeBrowserMining();
    }
  }

  /* ---------- 实时推送 ---------- */

  function handleConnection(online) {
    $("connection").classList.toggle("is-online", online);
    setText($("connection-text"), online ? "已连接课堂网络" : "连接断开，正在重连");
    if (!state.localWallet && state.tick) renderNodes();
  }

  function handleEvent(event) {
    const data = event.data || {};
    if (event.type === "hello" && data.role === "guest" && state.token) {
      recoverSession(state.token).catch(problem => pushLog(`恢复登录失败：${problem.message}`, "error"));
    }
    if (event.type === "hello" || event.type === "tick") {
      state.tick = data;
      renderLive();
    } else if (event.type === "block") {
      handleBlock(data);
    } else if (event.type === "chain_changed") {
      loadRecentBlocks();
      refreshMe();
      pollJob();
      if (data.reorganized) {
        pushLog("主链发生重组，余额、藏品和待确认交易已重新计算", "reorg");
        notify("主链已重组，请查看更新后的余额和交易确认", "info");
      }
    } else if (event.type === "mining_job_changed") {
      pollJob();
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
    if (state.transactionDomain) loadRecentBlocks();

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
    disposeWorker();
    state.workerJobKey = "";
    state.workerJobId = "";
    state.mining = false;
    rememberBrowserMining(false);
    state.rate = 0;
    state.rateSamples = [];
    updateMineButton();
    renderMiningStats();
    pushLog(`矿工脚本出错：${event.message || "无法运行"}`, "error");
    notify("矿工脚本运行失败", "error");
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
    state.workerShareBits = 0;
    renderMiningStats();
    ensureWorker();
    pushLog(message);
    if (state.mining) {
      pollJob();
    }
  }

  function startMining() {
    try { ensureWorker(); }
    catch (problem) {
      state.mining = false;
      updateMineButton();
      pushLog(`无法启动矿工：${problem.message}`, "error");
      notify(`无法启动矿工：${problem.message}`, "error");
      return;
    }
    state.mining = true;

    state.workerJobKey = "";
    state.workerJobId = "";
    state.workerShareBits = 0;
    updateMineButton();
    rememberBrowserMining(true);
    pushLog("开始挖矿，正在向服务器要作业…");
    pollJob();
  }

  function stopMining() {
    state.mining = false;
    rememberBrowserMining(false);
    updateMineButton();
    if (state.worker) {
      state.worker.postMessage({ type: "stop" });
    }
    state.rate = 0;
    state.rateSamples = [];
    renderMiningStats();
    pushLog("已暂停挖矿");
  }

  function rememberBrowserMining(running, suspended = false) {
    if (state.localWallet) return;
    try {
      if (running && state.address && state.transactionDomain) {
        sessionStorage.setItem(STORAGE_MINING_INTENT, JSON.stringify({
          address: state.address, domain: state.transactionDomain, suspended,
        }));
      } else sessionStorage.removeItem(STORAGE_MINING_INTENT);
    } catch { /* Storage restrictions leave manual mining available. */ }
  }

  function resumeBrowserMining() {
    if (state.localWallet || state.mining) return false;
    try {
      const saved = JSON.parse(sessionStorage.getItem(STORAGE_MINING_INTENT) || "null");
      const navigation = performance.getEntriesByType("navigation")[0];
      if ((navigation?.type === "reload" || saved?.suspended) && saved?.address === state.address
          && state.transactionDomain && saved.domain === state.transactionDomain) {
        startMining();
        pushLog("刷新后已恢复挖矿");
        return true;
      }
      rememberBrowserMining(false);
    } catch {
      rememberBrowserMining(false);
    }
    return false;
  }

  function updateMineButton() {
    setText($("mine-toggle-label"), state.mining ? "暂停挖矿" : "开始挖矿");
    $("mine-toggle").classList.toggle("is-running", state.mining);
    $("mine-toggle").querySelector("use").setAttribute("href", state.mining ? "#i-pause" : "#i-play");
  }

  async function pollJob() {
    if (!state.token) {
      return;
    }
    const generation = ++jobPollGeneration;
    try {
      const job = await api.post("/api/mining/job");
      if (generation === jobPollGeneration) adoptJob(job);
    } catch (problem) {
      pushLog(`获取作业失败：${problem.message}`, "error");
    }
  }

  function adoptJob(job) {
    state.job = job;
    renderJob();
    renderMiningStats();
    const key = `${job.job_id}:${job.share_bits}`;
    if (state.localWallet || !state.mining || key === state.workerJobKey) {
      return;
    }
    state.workerJobKey = key;
    state.workerJobId = job.job_id;
    state.workerShareBits = job.share_bits;
    state.workerGeneration = (state.workerGeneration || 0) + 1;
    state.workerRequestId = String(state.workerGeneration);
    ensureWorker().postMessage({ type: "job", job: { ...job, request_id: state.workerRequestId } });
  }

  function handleWorkerMessage(event) {
    const message = event.data;
    if (!state.mining || message.request_id !== state.workerRequestId) {
      return;
    }
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
      const result = await api.post("/api/mining/share", {
        job_id: share.job_id,
        nonce: share.nonce,
        share_bits: share.share_bits,
      });
      if (!result.accepted) {
        const reason = String(result.reason);
        if (reason.includes("作业") || reason === "工作量样本目标已更新") {
          if (share.job_id === state.workerJobId) await pollJob();
          return;
        }
        if (reason !== state.lastRejection) {
          state.lastRejection = reason;
          pushLog(`工作量样本未通过：${reason}`, "error");
        }
        return;
      }
      state.lastRejection = "";
      pushLog(`工作量样本已验证：+${result.work}（${result.share_bits} 位目标）`);
      if (result.job) {
        adoptJob(result.job);
      }
      refreshMe();
    } catch (problem) {
      pushLog(`提交工作量失败：${problem.message}`, "error");
    }
  }

  /* ---------- 数据刷新 ---------- */

  async function refreshMe() {
    if (!state.token) {
      return;
    }
    try {
      state.me = await api.get("/api/me");
      if (state.nickname !== state.me.nickname) {
        state.nickname = state.me.nickname;
        setText($("me-nickname"), state.nickname);
        $("me-nickname").title = state.nickname;
        if (!state.localWallet) localStorage.setItem(STORAGE_NICKNAME, state.nickname);
      }
      if (state.localWallet) {
        const miner = await localPost("/api/local/wallet/status");
        state.mining = miner.running;
        state.localMinerName = miner.name;
        updateMineButton();
      }
      renderWallet();
      renderNfts();
      renderPackets();
      renderMiningStats();
    } catch (problem) {
      pushLog(`刷新钱包失败：${problem.message}`, "error");
    }
  }

  async function loadRecentBlocks(more = false) {
    if (state.loadingBlocks) {
      if (!more) state.blocksRefreshPending = true;
      return;
    }
    state.loadingBlocks = true;
    $("load-more-blocks").disabled = true;
    try {
      const pages = state.blockPages + (more ? 1 : 0);
      // Anchor pagination to the first snapshot height, even if new blocks arrive.
      const first = await api.get("/api/blocks?limit=20");
      const blocks = [...first.blocks];
      let stale = false;
      for (let page = 1; page < pages && blocks.length && blocks.at(-1).height > 0; page++) {
        const data = await api.get(`/api/blocks?limit=20&before=${blocks.at(-1).height}&tip=${blocks[0].hash}`);
        if (data.stale) { stale = true; break; }
        blocks.push(...data.blocks);
      }
      if (stale) { state.blocksRefreshPending = true; return; }
      state.blockPages = pages;
      state.blocks = blocks;
      renderBlocks();
    } catch (problem) {
      pushLog(`读取区块失败：${problem.message}`, "error");
    } finally {
      state.loadingBlocks = false;
      $("load-more-blocks").disabled = false;
      if (state.blocksRefreshPending) {
        state.blocksRefreshPending = false;
        setTimeout(() => loadRecentBlocks(), 500);
      }
    }
  }

  async function loadPublic() {
    try {
      const data = await api.get("/api/public");
      state.transactionDomain = data.transaction_domain;
      setText($("classroom-name"), data.classroom);
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
    if (state.transactionDomain) body.domain = state.transactionDomain;
    const message = canonicalJson(body);
    return {
      ...body,
      tx_id: SimCrypto.bytesToHex(SimCrypto.sha256d(SimCrypto.utf8Bytes(message))),
      signature: SimCrypto.sign(state.privateKey, message),
    };
  }

  async function submitTransaction(type, fields) {
    if (state.localWallet) {
      await localPost("/api/local/wallet/transaction", {tx: {type, ...fields}});
      notify("交易已提交，等下一个区块确认", "info");
      return;
    }
    const tx = buildTransaction(type, fields);
    await api.post("/api/transactions", { tx });
    pushLog(`交易已进入内存池，等待打包：${type}`);
    notify("交易已提交，等下一个区块确认", "info");
  }

  function economyFee(category) {
    return parseCoins($(`${category}-fee`).value || "0.1");
  }

  function resolveReceiver(text) {
    const value = text.trim();
    if (/^sim[0-9a-f]{40}$/.test(value)) {
      return value;
    }
    const hit = Object.entries(state.tick?.names || {}).find(([, name]) => name === value);
    if (!hit) {
      throw new Error("没有找到这个昵称，检查一下拼写，或者直接填钱包地址");
    }
    return hit[0];
  }

  async function handleTransfer(event) {
    event.preventDefault();
    try {
      const receiver = resolveReceiver($("transfer-receiver").value);
      const amount = parseCoins($("transfer-amount").value.trim() || "1.5");
      const fee = parseCoins($("transfer-fee").value || "0.1");
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
      const total = parseCoins($("packet-total").value.trim() || "5");
      const count = Number($("packet-count").value || "5");
      const note = $("packet-note").value.trim();
      await submitTransaction("packet_create", {
        total,
        count,
        fee: economyFee("packet"),
        ...(note ? { note } : {}),
      });
      $("packet-total").value = "";
      $("packet-note").value = "";
      await refreshMe();
    } catch (problem) {
      notify(problem.message, "error");
    }
  }

  async function photoThumbnail(file) {
    if (!file) throw new Error("请选择照片");
    const source = await createImageBitmap(file);
    try {
      const canvas = document.createElement("canvas");
      const scale = Math.min(1, 256 / Math.max(source.width, source.height));
      canvas.width = Math.max(1, Math.round(source.width * scale));
      canvas.height = Math.max(1, Math.round(source.height * scale));
      const context = canvas.getContext("2d", {alpha: false});
      context.fillStyle = "white";
      context.fillRect(0, 0, canvas.width, canvas.height);
      context.drawImage(source, 0, 0, canvas.width, canvas.height);
      for (const quality of [0.85, 0.65, 0.45, 0.25, 0.12]) {
        const url = canvas.toDataURL("image/jpeg", quality);
        if (atob(url.split(",")[1]).length <= 16384) return url;
      }
      throw new Error("照片无法压缩到 16 KiB，请更换照片");
    } finally {
      source.close();
    }
  }

  async function handleMint(event) {
    event.preventDefault();
    try {
      const file = $("mint-photo").files[0];
      if (file && file.size > 8 * 1024 * 1024) throw new Error("原图不能超过 8 MiB");
      if (file && !["image/jpeg", "image/png", "image/gif", "image/webp"].includes(file.type)) throw new Error("照片支持 JPEG、PNG、GIF 或 WebP");
      const photo = state.transactionDomain ? {
        thumbnail: (await photoThumbnail(file)).split(",")[1],
        quantity: Number($("mint-quantity").value || "1"),
      } : {};
      await submitTransaction("nft_mint", {
        name: $("mint-name").value.trim(),
        seed: SimCrypto.randomHex(16),
        fee: economyFee("nft"),
        ...photo,
      });
      if (file && photo.thumbnail) {
        const bytes = Uint8Array.from(atob(photo.thumbnail), character => character.charCodeAt(0));
        const id = SimCrypto.bytesToHex(SimCrypto.sha256d(bytes));
        try {
          const response = await fetch(`/api/originals/${id}`, {
            method: "POST", headers: {Authorization: `Bearer ${state.token}`, "Content-Type": file.type}, body: file,
          });
          if (!response.ok) throw new Error("原图保存失败");
        } catch {
          notify("铸造交易已提交，但原图未保存；链上缩略图不受影响", "info");
        }
      }
      $("mint-name").value = "";
      $("mint-photo").value = "";
      updatePhotoButton();
      $("mint-quantity").value = "1";
      $("mint-preview").hidden = true;
      $("mint-preview").removeAttribute("src");
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
        fee: economyFee("packet"),
      });
      notify(`抢到第 ${ticket.index + 1} 个红包，金额 ${formatCoins(ticket.amount)} 币，等待打包`, "success");
      await refreshMe();
    } catch (problem) {
      notify(problem.message, "error");
    }
  }

  async function listNft(tokenId, priceText) {
    try {
      await submitTransaction("nft_list", {
        token_id: tokenId, price: listingPrice(priceText, "一口价"), fee: economyFee("nft"),
      });
      await refreshMe();
    } catch (problem) {
      notify(problem.message, "error");
    }
  }

  function listingPrice(text, label) {
    const value = text.trim() || "1";
    if (!/^\d+(\.\d{1,8})?$/.test(value)) {
      throw new Error(`${label}请输入数字，最多 8 位小数，例如 1.5`);
    }
    const amount = parseCoins(value);
    if (!Number.isSafeInteger(amount) || amount <= 0) throw new Error(`${label}须为有效的正数`);
    return amount;
  }

  async function simpleNftAction(type, tokenId) {
    try {
      await submitTransaction(type, { token_id: tokenId, fee: economyFee("nft") });
      await refreshMe();
    } catch (problem) {
      notify(problem.message, "error");
    }
  }

  async function startAuction(tokenId, priceText, durationText) {
    try {
      await submitTransaction("nft_auction_create", {
        token_id: tokenId,
        reserve_price: listingPrice(priceText, "起拍价"),
        duration: Number(durationText),
        fee: economyFee("auction"),
      });
      await refreshMe();
    } catch (problem) {
      notify(problem.message, "error");
    }
  }

  async function placeBid(auctionId, priceText) {
    try {
      await submitTransaction("nft_bid", {
        auction_id: auctionId,
        amount: parseCoins(priceText.trim() || formatCoins(state.tick.auctions.active.find(item => item.auction_id === auctionId).minimum_bid, 8)),
        fee: economyFee("auction"),
      });
      const input = $("auction-list").querySelector(`input[data-bid-price="${auctionId}"]`);
      if (input) {
        input.value = "";
      }
      await refreshMe();
    } catch (problem) {
      notify(problem.message, "error");
    }
  }

  async function simpleAuctionAction(type, auctionId, fee) {
    try {
      await submitTransaction(type, { auction_id: auctionId, fee: fee ?? economyFee("auction") });
      await refreshMe();
    } catch (problem) {
      notify(problem.message, "error");
    }
  }

  /* ---------- 渲染 ---------- */

  function renderLive() {
    const chain = state.tick.chain;
    const {count, bytes} = state.tick.mempool;
    const size = bytes < 1024 ? `${bytes} B` : bytes < 1024 ** 2 ? `${(bytes / 1024).toFixed(1)} KiB`
      : bytes < 1024 ** 3 ? `${(bytes / 1024 ** 2).toFixed(1)} MiB` : `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
    setText($("chain-summary"), `区块高度 ${chain.height} · 基准出块难度 ${chain.difficulty} 位 · 下一块奖励 ${formatCoins(chain.subsidy)} 币 · 内存池 ${count} 笔 / ${size}`);
    setText($("stat-mempool"), `${count} 笔`);
    setText($("stat-mempool-bytes"), size);
    $("stat-mempool").parentElement.title = `内存池 ${count} 笔 / ${bytes.toLocaleString()} B`;
    fitMiningStats();
    renderAnnouncement(state.tick.announcement);
    renderLeaderboard();
    renderPackets();
    renderNfts();
    renderAuctions();
    renderJob();
    renderNodes();
  }

  function renderAnnouncement(announcement) {
    const box = $("announcement");
    if (!announcement || !announcement.message) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    setText(box, `老师通知：${announcement.message}`);
  }

  function renderJob() {
    if (!state.job) {
      setText($("job-line"), "正在获取挖矿作业…");
      return;
    }
    const job = state.job;
    const chain = state.tick?.chain;
    const relaxed = chain && job.height === chain.height + 1 && job.difficulty < chain.difficulty;
    setText($("job-line"),
      `当前作业：第 ${job.height} 块 · 出块前导 ${job.difficulty} 位${relaxed ? `（等待较久，按规则从 ${chain.difficulty} 位临时放宽）` : ""} · 奖励 ${chain ? formatCoins(chain.subsidy) : "--"} 币 + 手续费`);
    setText($("stat-share-bits"), `${job.difficulty} 位`);
  }

  function renderMiningStats() {
    const mining = state.me ? state.me.mining : null;
    setText($("stat-hashrate"), formatHashrate(state.localWallet ? mining?.hashrate : state.rate));
    const work = mining ? mining.stage_work : 0;
    setText($("stat-work"), compactWork(work));
    $("stat-work").title = work.toLocaleString();
    setText($("stat-shares"), mining ? mining.shares : 0);
    setText($("stat-share-bits"), state.job ? `${state.job.difficulty} 位` : "--");
    setText($("stat-blocks"), mining ? mining.blocks_found : 0);
    const browserRow = $("browser-miner-row");
    const browserWorking = state.mining && !state.localWallet;
    browserRow.classList.toggle("is-working", browserWorking);
    browserRow.classList.toggle("is-idle", !browserWorking);
    setText($("browser-miner-status"), browserWorking ? "工作中" : "未工作");
    $("native-miner-row").hidden = !state.localWallet;
    $("native-miner-row").classList.toggle("is-working", state.localWallet && state.mining);
    $("native-miner-row").classList.toggle("is-idle", !state.mining);
    setText($("native-miner-name"), state.localMinerName);
    setText($("native-miner-status"), state.mining ? "工作中" : "未工作");
    if (window.SimMiningDriver) window.SimMiningDriver.render();
    $("reset-miner").hidden = true;
    fitMiningStats();
  }

  function fitMiningStats() {
    const stats = document.querySelector(".panel-mining .stats");
    stats.classList.remove("stats-wide");
    const values = stats.querySelectorAll(".stat:not([hidden]) strong, .stat:not([hidden]) small, .stat-label");
    if ([...values].some(value => value.scrollWidth > value.clientWidth + 1)) {
      stats.classList.add("stats-wide");
    }
  }

  function renderWallet() {
    if (!state.me) {
      return;
    }
    setText($("wallet-balance"), formatCoins(state.me.balance));
    setText($("wallet-mined"), formatCoins(state.me.mined));
    $("wallet-address").value = state.me.address;
    const rows = state.me.transactions.map((tx) => {
      const outgoing = tx.sender === state.me.address;
      const kind = tx.type === "transfer" ? (outgoing ? "转出" : "转入") : labelOfType(tx.type);
      const text = tx.type === "coinbase" ? `${formatCoins(tx.amount || 0)} 币`
        : tx.amount ? `${kind} ${formatCoins(tx.amount)} 币` : kind;
      return `<li class="row-item">
        <span class="tag">${escapeHtml(labelOfType(tx.type))}</span>
        <span class="grow">${escapeHtml(text)}</span>
        <span class="muted small">区块 #${tx.block_height} · <span title="确认数 = 当前主链高度 − 所在区块高度 + 1；与节点数量无关">${tx.confirmations} 次确认</span> · ${formatTime(tx.timestamp)}</span>
      </li>`;
    });
    setHtml($("tx-list"), rows.join("") || `<li class="empty">还没有链上交易</li>`);
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
    setHtml($("packet-list"), rows.join("") || `<li class="empty">当前没有可抢的红包，挖到币的同学可以发一个</li>`);
  }

  let renderedNftList = "";

  function compactWork(value) {
    if (value < 10000) return value.toLocaleString();
    const units = ["", "k", "M", "G", "T", "P", "E"];
    let index = Math.min(Math.floor(Math.log10(value) / 3), units.length - 1);
    let scaled = Number((value / 1000 ** index).toPrecision(4));
    if (scaled >= 1000 && index < units.length - 1) { index++; scaled /= 1000; }
    return scaled.toPrecision(4) + units[index];
  }

  function nftIdentity(item, label) {
    const id = item.token_id || "";
    return `<div class="nft-identity"><span class="tag tag-blue">${escapeHtml(label)}</span>
      <button type="button" class="contract-copy" data-copy-contract="${escapeHtml(id)}" title="${escapeHtml(id)}">${escapeHtml(id.slice(0, 12))}…</button>
      ${item.mint_height != null ? `<span class="muted small">铸造：第 ${item.mint_height} 块</span>` : ""}</div>`;
  }

  function nftArt(item) {
    return `<div class="nft-art">${thumbnailHtml(item)}<strong class="nft-name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</strong></div>`;
  }

  function thumbnailHtml(item) {
    if (!item.thumbnail_id || !/^[a-f0-9]{64}$/.test(item.thumbnail_id)) return "";
    const url = `/api/thumbnails/${item.thumbnail_id}`;
    return `<button type="button" class="nft-picture" data-image="${item.thumbnail_id}" title="查看图片并下载"><img src="${url}" alt="${escapeHtml(item.name || "缩略图")}" width="64" loading="lazy"></button>`;
  }

  function renderNfts() {
    if (deferWhileSelected($("nft-list"), renderNfts)) return;
    if (!state.tick) {
      return;
    }
    const auctions = state.tick.auctions.active;
    const mine = state.me ? state.me.nfts : [];
    const rows = mine.map((nft) => {
      const auction = nft.auction_id ? auctions.find((item) => item.auction_id === nft.auction_id) : null;
      if (auction) {
        return `<li class="row-item nft-row">
          ${nftIdentity(nft, "拍卖中")}${nftArt(nft)}<span class="grow">
            <span class="muted small">当前价 ${formatCoins(auctionPrice(auction))} 币 · ${auction.bid_count} 次出价</span>
          </span>
          <span class="muted small">${
            auction.highest_bid === null ? "还没有人出价" : `领先 ${escapeHtml(auction.highest_bidder_name)}`
          }</span>
        </li>`;
      }
      if (nft.price !== null) {
        return `<li class="row-item nft-row">
          ${nftIdentity(nft, "我的藏品")}${nftArt(nft)}<span class="grow">
            <span class="muted small">在售 ${formatCoins(nft.price)} 币</span>
          </span>
          <button type="button" class="ghost" data-action="unlist" data-token="${nft.token_id}">下架</button>
        </li>`;
      }
      return `<li class="row-item nft-row">
        ${nftIdentity(nft, "我的藏品")}${nftArt(nft)}
        <div class="nft-controls">
          <label>一口价<input class="price-input" type="text" inputmode="decimal" placeholder="1" data-price="${nft.token_id}"></label>
          <button type="button" class="primary" data-action="list" data-token="${nft.token_id}">上架</button>
          <label>起拍价<input class="price-input" type="text" inputmode="decimal" placeholder="1" data-reserve="${nft.token_id}"></label>
          <label>${state.transactionDomain ? "限时块数" : "限时"}<select class="duration-select" data-duration="${nft.token_id}">
            ${state.transactionDomain ? `<option value="2">2 块</option>
            <option value="4">4 块</option>
            <option value="10" selected>10 块</option>
            <option value="20">20 块</option>` : `<option value="60">1 分钟</option>
            <option value="180">3 分钟</option>
            <option value="300" selected>5 分钟</option>
            <option value="600">10 分钟</option>`}
          </select></label>
          <button type="button" class="primary" data-action="auction" data-token="${nft.token_id}">开拍</button>
        </div>
      </li>`;
    });

    const listings = state.tick.market.map((item) => {
      const mineListing = item.seller === state.address;
      const action = mineListing
        ? `<span class="muted small">我的挂单</span>`
        : `<button type="button" class="ghost" data-action="buy" data-token="${item.token_id}">买下</button>`;
      return `<li class="row-item nft-row">
        ${nftIdentity(item, "在售")}${nftArt(item)}<span class="grow">
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
      const list = $("nft-list");
      const selector = "input[data-price], input[data-reserve], select[data-duration]";
      const fieldKey = (field) => field.dataset.price ? `price:${field.dataset.price}`
        : field.dataset.reserve ? `reserve:${field.dataset.reserve}` : `duration:${field.dataset.duration}`;
      const drafts = new Map(Array.from(list.querySelectorAll(selector), (field) => [fieldKey(field), field.value]));
      renderedNftList = html;
      list.innerHTML = html;
      for (const field of list.querySelectorAll(selector)) {
        if (drafts.has(fieldKey(field))) field.value = drafts.get(fieldKey(field));
      }
    }
  }

  function renderAuctions() {
    if (deferWhileSelected($("auction-list"), renderAuctions)) return;
    if (!state.tick) {
      return;
    }
    const list = $("auction-list");
    const auctions = state.tick.auctions.active;
    const keep = new Set();
    for (const auction of auctions) {
      keep.add(auction.auction_id);
      let node = list.querySelector(`li[data-auction="${auction.auction_id}"]`);
      if (!node) {
        node = document.createElement("li");
        node.className = "row-item row-item-stack";
        node.dataset.auction = auction.auction_id;
        list.appendChild(node);
      }
      paintAuction(node, auction);
    }
    for (const node of [...list.children]) {
      if (node.dataset.auction && !keep.has(node.dataset.auction)) {
        node.remove();
      }
    }
    const empty = list.querySelector(".empty");
    if (auctions.length && empty) {
      empty.remove();
    } else if (!auctions.length && !empty) {
      list.innerHTML = `<li class="empty">拍卖场还空着，选一件藏品开拍试试</li>`;
    }
  }

  /* 每秒重画整行会清掉学生正在输入的出价：行内容变了才重画，倒计时单独更新。 */
  function paintAuction(node, auction) {
    const shape = [
      auction.name,
      auction.seller,
      auction.highest_bid,
      auction.highest_bidder,
      auction.bid_count,
      auction.seconds_left > 0,
      auction.seconds_left > 15,
    ].join("|");
    if (node.dataset.shape !== shape) {
      const previous = node.querySelector("input[data-bid-price]");
      const typed = previous ? previous.value : "";
      node.dataset.shape = shape;
      node.innerHTML = auctionRowHtml(auction);
      const input = node.querySelector("input[data-bid-price]");
      if (input && typed) {
        input.value = typed;
      }
    }
    const timer = node.querySelector(".auction-timer");
    setText(timer, auction.ends_height ? `第 ${auction.ends_height} 块结算 · 剩余 ${auction.blocks_left} 块` :
      (auction.seconds_left > 0 ? `剩余 ${formatCountdown(auction.seconds_left)}` : "已结束，等待结算"));
  }

  function auctionRowHtml(auction) {
    const mine = auction.seller === state.address;
    const leading = auction.highest_bidder === state.address;
    const ended = !auction.ends_height && auction.seconds_left <= 0;
    let action = `<input class="price-input" type="text" inputmode="decimal"
        data-bid-price="${auction.auction_id}" placeholder="${formatCoins(auction.minimum_bid, 8)}">
      <button type="button" class="ghost" data-action="bid" data-auction="${auction.auction_id}">出价</button>`;
    if (mine) {
      action =
        auction.highest_bid === null
          ? `<button type="button" class="ghost" data-action="auction-cancel" data-auction="${auction.auction_id}">取消拍卖</button>`
          : `<span class="muted small">等同学出更高价</span>`;
    } else if (leading) {
      action = `<span class="muted small">你已领先，等别人出价</span>`;
    }
    if (ended) {
      action =
        mine || leading
          ? `<button type="button" class="primary" data-action="settle" data-auction="${auction.auction_id}">${
              mine ? "收款交货" : "领取藏品"
            }</button>`
          : `<span class="muted small">等待结算</span>`;
    }
    const rush =
      !mine && !leading && auction.seconds_left > 0 && auction.seconds_left <= 15
        ? `<span class="muted small">最后 15 秒，出价可能赶不上打包</span>`
        : "";
    return `<div class="nft-row">
        ${nftIdentity(auction, "拍卖")}${nftArt(auction)}<div class="auction-info">
          <span class="muted small">卖家 ${escapeHtml(auction.seller_name)} · 当前价 ${formatCoins(auctionPrice(auction))} 币 · ${
            auction.bid_count
          } 次出价</span>
        <span class="auction-timer muted small"></span>
        <span class="muted small">最低出价 ${formatCoins(auction.minimum_bid, 8)} 币</span>
        ${rush}<div class="auction-actions">${action}</div></div>
      </div>`;
  }

  function renderLeaderboard() {
    if (!state.tick) {
      return;
    }
    $("leaderboard-panel").hidden = !state.tick.leaderboard;
    if (!state.tick.leaderboard) return;
    const key = state.leaderboardTab === "hashrate" ? "by_hashrate" : "by_mined";
    const rows = state.tick.leaderboard[key];
    const body = $("leaderboard").querySelector("tbody");
    setHtml(body, rows
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
      .join("") || `<tr><td colspan="6" class="empty">还没有同学加入</td></tr>`);
  }

  function renderBlocks() {
    const rows = state.blocks.map(
      (block) => `<li class="row-item block-row">
        <time class="muted mono">${formatTime(block.timestamp)}</time>
        <span class="tag">#${block.height}</span>
        <button type="button" class="contract-copy block-hash" data-copy-block="${escapeHtml(block.hash)}" title="${escapeHtml(block.hash)}">${escapeHtml(block.hash)}</button>
        <span class="muted block-difficulty">难度 ${block.difficulty}</span>
      </li>`,
    );
    setHtml($("block-list"), rows.join("") || `<li class="empty">链上还没有区块</li>`);
    $("load-more-blocks").hidden = !state.blocks.length || state.blocks.at(-1).height === 0;
  }

  function renderNodes() {
    const nodes = state.tick?.nodes;
    const browsers = nodes?.browsers || [];
    $("peer-form").hidden = !state.localWallet;
    $("web-participants-title").hidden = !state.localWallet;
    if (!state.localWallet) {
      $("configured-peers").hidden = true;
      $("node-log").hidden = true;
      setText($("node-status"), `当前网页连接的节点 · 区块高度 ${state.tick?.chain.height ?? "--"}`);
      setHtml($("node-participants"), `<li class="row-item provider-participant"><div class="participant-main"><strong>${escapeHtml(nodes?.node_name || "未命名")}</strong><span class="mono">${escapeHtml(location.host)}</span></div><div class="participant-details"><span>主动</span><span>${$("connection").classList.contains("is-online") ? "已连接" : "连接中"}</span><span>来源网页</span></div></li>`);
      return;
    }
    $("configured-peers").hidden = false;
    $("node-log").hidden = false;
    setText($("node-status"), `独立节点连接 ${nodes?.connected ?? 0} 个 · 本节点连接的浏览器钱包 ${browsers.length} 个 · 区块高度 ${state.tick?.chain.height ?? "--"}`);
    setHtml($("node-participants"), browsers.map(row => `<li class="row-item browser-participant">
      <strong title="${escapeHtml(row.address)}">${escapeHtml(row.nickname || shortAddress(row.address))}</strong>
      <span class="mono">${escapeHtml(row.source_ips?.join("、") || "地址未上报")}</span>
      <span>被动</span><span>已连接</span><span>来源网页</span></li>`).join("") || '<li class="empty">暂无网页参与者</li>');
    if (!nodes) return;
    renderConfiguredPeers(nodes.peers || []);
    setHtml($("node-log"), [...nodes.events].reverse().map(item =>
      `<li><time class="muted small">${formatTime(item.at)}</time><span>${escapeHtml(item.message)}</span></li>`).join("") || '<li>暂时没有节点连接记录</li>');
  }

  function renderConfiguredPeers(peers) {
    const list = $("configured-peers");
    if (deferWhileSelected(list, () => renderConfiguredPeers(peers))) return;
    if (!peers.length) {
      setHtml(list, '<li class="empty">尚未添加独立节点</li>');
      return;
    }
    list.querySelector(".empty")?.remove();
    const existing = new Map([...list.children].map(row => [row.dataset.peerKey, row]));
    const peerStates = {connecting: "正在连接", connected: "已连接", retrying: "等待重试", paused: "已断开 · 不再重试"};
    peers.forEach((peer, index) => {
      const key = `${peer.direction || "outbound"}|${peer.address}`;
      let row = existing.get(key);
      existing.delete(key);
      if (!row) {
        row = document.createElement("li");
        row.className = "row-item configured-peer";
        row.dataset.peerKey = key;
        row.innerHTML = '<strong class="peer-name"></strong><span class="peer-address mono"></span><span class="peer-direction muted small"></span><span class="peer-status muted small"></span><div class="peer-actions"></div>';
      }
      const inbound = peer.direction === "inbound";
      const address = String(peer.address).replace(/^\[::ffff:(\d+\.\d+\.\d+\.\d+)\]:(\d+)$/i, "$1:$2");
      setText(row.querySelector(".peer-name"), peer.name || "未命名");
      row.querySelector(".peer-name").title = peer.name || "未命名";
      setText(row.querySelector(".peer-address"), address);
      row.querySelector(".peer-address").title = address;
      setText(row.querySelector(".peer-direction"), inbound ? "被动" : "主动");
      setText(row.querySelector(".peer-status"), `${peerStates[peer.status] || "未连接"}${inbound ? "（来源地址）" : ""}`);
      row.querySelector(".peer-status").title = inbound ? "对方发起本次连接的源 IP 和临时端口，不是对方的服务监听端口" : "连接的节点服务地址";
      const actions = row.querySelector(".peer-actions");
      const controllable = state.localWallet && !inbound;
      if (controllable && !actions.children.length) {
        actions.innerHTML = '<button type="button" class="ghost"></button><button type="button" class="ghost" data-peer-action="delete">删除</button>';
      }
      actions.hidden = !controllable;
      if (controllable) {
        const button = actions.firstElementChild;
        button.dataset.peerAction = peer.status === "paused" ? "connect" : "disconnect";
        setText(button, peer.status === "paused" ? "连接" : "断开");
        for (const action of actions.children) action.dataset.address = peer.address;
      }
      if (list.children[index] !== row) list.insertBefore(row, list.children[index] || null);
    });
    for (const row of existing.values()) row.remove();
  }

  function updatePhotoButton() {
    const selected = $("mint-photo").files.length > 0;
    setText($("photo-toggle"), selected ? "−" : "+");
    $("photo-toggle").title = selected ? "删除照片" : "上传照片";
    $("photo-toggle").setAttribute("aria-label", $("photo-toggle").title);
  }

  async function copyText(text, success) {
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
      else throw new Error("clipboard unavailable");
    } catch {
      const input = document.createElement("textarea");
      input.value = text;
      input.style.position = "fixed";
      input.style.opacity = "0";
      document.body.appendChild(input);
      input.select();
      const copied = document.execCommand("copy");
      input.remove();
      if (!copied) { notify(`复制失败，请手动复制：${text}`, "info"); return; }
    }
    notify(success, "success");
  }

  function pushLog(message, kind) {
    const category = kind === "reorg" ? "重组" : kind === "error" ? "提示" : kind === "block" || message.startsWith("区块") ? "出块"
      : message.includes("交易") ? "交易" : message.includes("作业") ? "作业" : "算力";
    state.log.unshift({ message, category, kind: kind || "info", at: new Date() });
    state.log = state.log.slice(0, LOG_LIMIT);
    setHtml($("share-log"), state.log
      .map(
        (item) => `<li class="log-${item.kind}">
          <time class="muted small">${item.at.toLocaleTimeString("zh-CN", { hour12: false })}</time>
          <span class="log-category">${item.category}</span><span class="log-message">${escapeHtml(item.message)}</span>
        </li>`,
      )
      .join(""));
  }

  function nicknameOf(address) {
    return state.tick?.names?.[address] || shortAddress(address);
  }

  function labelOfType(type) {
    return {
      transfer: "转账",
      nft_mint: "铸造",
      nft_list: "上架",
      nft_unlist: "下架",
      nft_buy: "购买",
      nft_auction_create: "开拍",
      nft_auction_cancel: "取消拍卖",
      nft_auction_settle: "拍卖成交",
      nft_bid: "拍卖出价",
      packet_create: "发红包",
      packet_claim: "领红包",
      packet_refund: "退红包",
      coinbase: "挖矿奖励",
    }[type] || type;
  }

  /* ---------- 事件绑定 ---------- */

  async function importWallet(file, password) {
    if (!file) return;
    try {
      if (file.size > 4096) throw new Error("钱包备份文件过大");
      const secret = await SimWalletBackup.decrypt(JSON.parse(await file.text()), password);
      if (!confirm("导入后将切换到备份中的钱包。请确认已备份当前钱包。")) return;
      stopMining();
      localStorage.setItem(STORAGE_PRIVATE_KEY, secret);
      localStorage.removeItem(STORAGE_TOKEN);
      localStorage.removeItem(STORAGE_NICKNAME);
      location.reload();
    } catch (problem) {
      notify(problem.message, "error");
    }
  }

  function bindEvents() {
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) rememberBrowserMining(state.mining, true);
      else recoverPage();
    });
    document.addEventListener("resume", recoverPage);
    window.addEventListener("pageshow", event => { if (event.persisted) recoverPage(); });
    window.addEventListener("online", recoverPage);
    new ResizeObserver(fitMiningStats).observe(document.querySelector(".panel-mining .stats"));
    document.fonts.ready.then(fitMiningStats);
    $("load-more-blocks").addEventListener("click", () => loadRecentBlocks(true));
    $("close-image").addEventListener("click", () => $("image-viewer").close());
    document.addEventListener("click", async event => {
      const block = event.target.closest("[data-copy-block]");
      if (block) await copyText(block.dataset.copyBlock, "区块哈希复制成功");
      const contract = event.target.closest("[data-copy-contract]");
      if (contract) await copyText(contract.dataset.copyContract, "合约地址复制成功");
      const picture = event.target.closest("[data-image]");
      if (!picture) return;
      const dialog = $("image-viewer");
      const id = picture.dataset.image;
      let url = `/api/thumbnails/${id}`;
      let original = false;
      let extension = "jpg";
      try {
        const response = await fetch(`/api/originals/${id}`, {method: "HEAD"});
        if (response.ok) {
          url = `/api/originals/${id}`;
          original = true;
          extension = {"image/png":"png", "image/gif":"gif", "image/webp":"webp"}[response.headers.get("Content-Type")] || "jpg";
        }
      } catch { /* The on-chain thumbnail remains available. */ }
      $("full-image").src = url;
      $("download-image").href = url;
      $("download-image").download = `collectible.${extension}`;
      setText($("image-caption"), original ? "原图" : "此节点没有原图，显示链上缩略图");
      if (!dialog.open) dialog.showModal();
    });
    $("photo-toggle").addEventListener("click", () => {
      if (!$("mint-photo").files.length) { $("mint-photo").click(); return; }
      $("mint-photo").value = "";
      $("mint-preview").hidden = true;
      $("mint-preview").removeAttribute("src");
      updatePhotoButton();
    });
    $("peer-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        await localPost("/api/local/peers", {address: $("peer-address").value.trim()});
        notify("已添加节点，正在尝试连接", "info");
      } catch (problem) {
        notify(problem.message, "error");
      }
    });
    $("configured-peers").addEventListener("click", async (event) => {
      const button = event.target.closest("button[data-peer-action]");
      if (!button || button.disabled) return;
      button.disabled = true;
      const action = button.dataset.peerAction;
      try {
        await localPost(action === "connect" ? "/api/local/peers" : `/api/local/peers/${action}`, {address: button.dataset.address});
        notify(action === "delete" ? "已删除节点" : action === "disconnect" ? "已断开，停止自动重试；地址已保留" : "正在尝试连接", "info");
      } catch (problem) {
        notify(problem.message, "error");
      } finally {
        button.disabled = false;
      }
    });
    $("login-wallet-import").addEventListener("click", () => $("login-wallet-file").click());
    $("login-wallet-file").addEventListener("change", async (event) => {
      const file = event.target.files[0];
      event.target.value = "";
      await importWallet(file, $("login-backup-password").value);
    });
    $("wallet-backup-form").addEventListener("submit", (event) => event.preventDefault());
    $("wallet-export").addEventListener("click", async () => {
      try {
        const backup = await SimWalletBackup.encrypt(state.privateKey, $("wallet-backup-password").value);
        const url = URL.createObjectURL(new Blob([JSON.stringify(backup)], {type: "application/json"}));
        const link = document.createElement("a");
        link.href = url;
        link.download = "classroom-wallet.json";
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        $("wallet-backup-password").value = "";
      } catch (problem) {
        notify(problem.message, "error");
      }
    });
    $("wallet-import").addEventListener("click", () => $("wallet-backup-file").click());
    $("wallet-backup-file").addEventListener("change", async (event) => {
      const file = event.target.files[0];
      event.target.value = "";
      await importWallet(file, $("wallet-backup-password").value);
    });
    window.addEventListener("btc-sim-stop-browser-miner", () => {
      if (state.mining) stopMining();
    });
    window.addEventListener("btc-sim-start-browser-miner", () => {
      if (state.token && !state.mining) startMining();
    });
    $("login-form").addEventListener("submit", handleLoginSubmit);
    $("logout").addEventListener("click", logout);
    $("mine-toggle").addEventListener("click", async () => {
      if (!state.localWallet) {
        if (state.mining) stopMining();
        else startMining();
        return;
      }
      try {
        const action = state.mining ? "stop" : "start";
        const result = await localPost(`/api/local/wallet/mining/${action}`);
        state.mining = result.running;
        state.localMinerName = result.name;
        updateMineButton();
        renderMiningStats();
      } catch (problem) {
        notify(problem.message, "error");
      }
    });
    $("transfer-form").addEventListener("submit", handleTransfer);
    $("packet-form").addEventListener("submit", handlePacketCreate);
    $("mint-form").addEventListener("submit", handleMint);
    $("mint-photo").addEventListener("change", async (event) => {
      updatePhotoButton();
      const file = event.target.files[0];
      const preview = $("mint-preview");
      preview.hidden = true;
      preview.removeAttribute("src");
      if (!file) return;
      try {
        const source = await photoThumbnail(file);
        if (event.target.files[0] === file) {
          preview.src = source;
          preview.hidden = false;
        }
      } catch (problem) {
        notify(problem.message, "error");
      }
    });
    $("wallet-address").addEventListener("click", copyAddress);
    $("wallet-address").addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); copyAddress(); }
    });
    $("identity").addEventListener("click", () => copyText(state.nickname, "名称复制成功"));
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
      } else if (button.dataset.action === "auction") {
        const input = $("nft-list").querySelector(`input[data-reserve="${tokenId}"]`);
        const duration = $("nft-list").querySelector(`select[data-duration="${tokenId}"]`);
        startAuction(tokenId, input ? input.value : "", duration ? duration.value : "300");
      } else if (button.dataset.action === "unlist") {
        simpleNftAction("nft_unlist", tokenId);
      } else if (button.dataset.action === "buy") {
        simpleNftAction("nft_buy", tokenId);
      }
    });

    $("auction-list").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-action]");
      if (!button) {
        return;
      }
      const auctionId = button.dataset.auction;
      if (button.dataset.action === "bid") {
        const input = $("auction-list").querySelector(`input[data-bid-price="${auctionId}"]`);
        placeBid(auctionId, input ? input.value : "");
      } else if (button.dataset.action === "auction-cancel") {
        simpleAuctionAction("nft_auction_cancel", auctionId);
      } else if (button.dataset.action === "settle") {
        simpleAuctionAction("nft_auction_settle", auctionId, 0);
      }
    });
  }

  async function copyAddress() {
    try {
      await navigator.clipboard.writeText(state.address);
    } catch {
      const input = $("wallet-address");
      input.focus();
      input.select();
      if (!document.execCommand("copy")) {
        notify("已选中地址，请按 Ctrl+C 或使用系统复制菜单", "info");
        return;
      }
    }
    notify("钱包地址已复制", "success");
  }

  async function boot() {
    bindEvents();
    if (["localhost", "127.0.0.1", "[::1]"].includes(location.hostname)
        && !new URLSearchParams(location.search).has("browser")) {
      let local;
      try {
        local = await localPost("/api/local/wallet");
      } catch {
        // A browser-only session has no native wallet endpoint.
      }
      if (local) {
        state.localWallet = true;
        state.token = api.token = local.token;
        state.address = local.player.address;
        state.nickname = local.player.nickname;
        state.mining = local.running;
        state.localMinerName = local.name;
        $("wallet-address").value = state.address;
        await enterConsole(local.player);
        return;
      }
    }
    try {
      loadWallet();
      await restoreSession();
    } catch (problem) {
      setText($("login-error"), `页面初始化失败：${problem.message}`);
      $("login-error").hidden = false;
      $("login-overlay").hidden = false;
    }
  }

  boot();
})();
