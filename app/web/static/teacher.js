/* 教师控制台：口令登录、课堂状态、学生管理、参数调整与成绩导出。

   口令只用来换取会话令牌，令牌放在 sessionStorage，关掉标签页就失效。 */
(function () {
  "use strict";

  const { API, Feed, SAT_PER_COIN, formatCoins, parseCoins, formatHashrate, formatTime, escapeHtml, notify } = window.Sim;

  const STORAGE_TOKEN = "btc-sim.teacher-token";
  const REFRESH_MS = 5000;

  const $ = (id) => document.getElementById(id);

  const state = {
    token: "",
    tick: null,
    overview: null,
    settingsFilled: false,
  };

  const api = new API();
  const feed = new Feed(handleEvent, handleConnection);

  /* ---------- 登录 ---------- */

  async function handleLoginSubmit(event) {
    event.preventDefault();
    const error = $("login-error");
    error.hidden = true;
    try {
      const result = await api.post("/api/auth/teacher", { key: $("login-key").value.trim() });
      sessionStorage.setItem(STORAGE_TOKEN, result.token);
      enterConsole(result.token);
    } catch (problem) {
      error.textContent = problem.message;
      error.hidden = false;
    }
  }

  function enterConsole(token) {
    state.token = token;
    api.token = token;
    $("login-overlay").hidden = true;
    $("logout").hidden = false;
    $("export").hidden = false;
    feed.connect(token);
    refreshOverview();
    setInterval(refreshOverview, REFRESH_MS);
  }

  function logout() {
    sessionStorage.removeItem(STORAGE_TOKEN);
    location.reload();
  }

  async function restoreSession() {
    const token = sessionStorage.getItem(STORAGE_TOKEN);
    if (!token) {
      return;
    }
    api.token = token;
    try {
      state.overview = await api.get("/api/teacher/overview");
      enterConsole(token);
    } catch (problem) {
      sessionStorage.removeItem(STORAGE_TOKEN);
      api.token = "";
      notify(problem.message, "error");
    }
  }

  /* ---------- 实时推送 ---------- */

  function handleConnection(online) {
    $("connection").classList.toggle("is-online", online);
    $("connection-text").textContent = online ? "已连接课堂网络" : "连接断开，正在重连";
  }

  function handleEvent(event) {
    if (event.type === "hello" || event.type === "tick") {
      state.tick = event.data;
      renderLive();
    } else if (event.type !== "share" && event.type !== "mempool") {
      refreshOverview();
    }
  }

  /* ---------- 数据刷新 ---------- */

  async function refreshOverview() {
    if (!state.token) {
      return;
    }
    try {
      state.overview = await api.get("/api/teacher/overview");
      renderOverview();
    } catch (problem) {
      if (String(problem.message).includes("登录") || String(problem.message).includes("权限")) {
        logout();
      }
    }
  }

  function renderOverview() {
    const overview = state.overview;
    if (!overview) {
      return;
    }
    $("classroom-name").textContent = `${overview.settings.classroom_name} · 教师控制台`;
    if (!state.settingsFilled) {
      state.settingsFilled = true;
      fillSettings(overview.settings);
    }
    renderPlayers(overview.players);
    renderEvents(overview.events);
    if (!state.tick) {
      renderLive();
    }
  }

  function liveSnapshot() {
    const overview = state.overview;
    return {
      chain: state.tick ? state.tick.chain : overview.chain,
      mempool: state.tick ? state.tick.mempool : overview.mempool,
      online: state.tick
        ? state.tick.online
        : overview.players.filter((row) => row.online).length,
      rows: state.tick ? state.tick.leaderboard.rows : overview.players,
      network_hashrate: state.tick
        ? state.tick.leaderboard.network_hashrate
        : overview.players.reduce((sum, row) => sum + row.hashrate, 0),
    };
  }

  function renderLive() {
    if (!state.overview) {
      return;
    }
    const live = liveSnapshot();
    const chain = live.chain;
    $("chain-summary").textContent =
      `区块高度 ${chain.height} · 出块难度 ${chain.difficulty} 位 · 下一块奖励 ${formatCoins(chain.subsidy)} 币`;
    $("round-state").textContent =
      chain.difficulty_override === null
        ? `难度自动调整：每 ${chain.retarget_interval_blocks} 块一次`
        : `难度固定为 ${chain.difficulty_override} 位`;
    $("stat-height").textContent = chain.height.toLocaleString();
    $("stat-difficulty").textContent = `${chain.difficulty} 位`;
    $("stat-hashrate").textContent = formatHashrate(live.network_hashrate);
    $("stat-online").textContent = `${live.online} / ${live.rows.length}`;
    $("stat-mempool").textContent = `${live.mempool.count} 笔`;
    $("stat-subsidy").textContent = `${formatCoins(chain.subsidy)} 币`;
    $("chain-line").textContent =
      `下一次难度调整在第 ${chain.next_retarget_height} 块 · ` +
      `奖励减半在第 ${chain.next_halving_height} 块 · ` +
      `手续费区间 ${formatCoins(chain.min_fee_sat, 8)} ~ ${formatCoins(chain.max_fee_sat, 8)} 币`;
  }

  function renderPlayers(players) {
    const rows = [...players].sort(
      (left, right) =>
        right.mined - left.mined ||
        right.hashrate - left.hashrate ||
        left.nickname.localeCompare(right.nickname),
    );
    $("player-count").textContent = `${rows.length} 人报名 · ${rows.filter((row) => row.online).length} 人在线`;
    $("players").querySelector("tbody").innerHTML =
      rows
        .map((row, index) => {
          const status = row.banned
            ? '<span class="tag tag-danger">已停用</span>'
            : row.online
              ? '<span class="online">在线</span>'
              : '<span class="muted">离线</span>';
          const nickname = escapeHtml(row.nickname);
          return `<tr>
            <td class="rank">${index + 1}</td>
            <td>${nickname}</td>
            <td>${formatCoins(row.balance)} 币</td>
            <td>${formatCoins(row.mined)} 币</td>
            <td>${formatHashrate(row.hashrate)}</td>
            <td>${row.blocks_found}</td>
            <td>${status}</td>
            <td class="action-cell">
              <button type="button" class="ghost" data-action="${row.banned ? "unban" : "ban"}" data-nickname="${nickname}">
                <svg class="icon"><use href="#${row.banned ? "i-player-on" : "i-player-off"}"/></svg>${row.banned ? "解除" : "停用"}
              </button>
              <button type="button" class="ghost danger" data-action="remove" data-nickname="${nickname}">
                <svg class="icon"><use href="#i-trash"/></svg>移除
              </button>
            </td>
          </tr>`;
        })
        .join("") || `<tr><td colspan="8" class="empty">还没有同学报名</td></tr>`;
  }

  function renderEvents(events) {
    $("event-list").innerHTML =
      events
        .map(
          (item) => `<li class="row-item">
            <span class="grow">${escapeHtml(item.message)}</span>
            <span class="muted small">${formatTime(item.at)}</span>
          </li>`,
        )
        .join("") || `<li class="empty">还没有课堂事件</li>`;
  }

  /* ---------- 学生管理 ---------- */

  async function handlePlayerAction(button) {
    const nickname = button.dataset.nickname;
    const action = button.dataset.action;
    try {
      if (action === "remove") {
        if (!confirm(`移除 ${nickname}？这个昵称会重新回到未注册状态。`)) {
          return;
        }
        await api.post(`/api/teacher/players/${encodeURIComponent(nickname)}/reset`);
        notify(`已移除 ${nickname}`, "info");
      } else {
        await api.post(`/api/teacher/players/${encodeURIComponent(nickname)}/ban`, {
          banned: action === "ban",
        });
        notify(action === "ban" ? `已停用 ${nickname} 的账号` : `已解除 ${nickname} 的停用`, "info");
      }
      await refreshOverview();
    } catch (problem) {
      notify(problem.message, "error");
    }
  }

  /* ---------- 课堂参数 ---------- */

  function fillSettings(settings) {
    const { chain, pool } = settings;
    $("setting-override").value = chain.difficulty_override === null ? "" : chain.difficulty_override;
    $("setting-difficulty-max").value = chain.difficulty_max;
    $("setting-target-seconds").value = chain.target_block_seconds;
    $("setting-retarget-blocks").value = chain.retarget_interval_blocks;
    $("setting-stall-seconds").value = chain.stall_seconds;
    $("setting-reward").value = formatCoins(chain.initial_reward_coins * SAT_PER_COIN, 8);
    $("setting-halving").value = chain.halving_interval_blocks;
    $("setting-bonus").value = chain.finder_bonus_percent;
    $("setting-min-fee").value = formatCoins(chain.min_fee_sat, 8);
    $("setting-share-seconds").value = pool.share_target_seconds;
    $("setting-nft-limit").value = chain.max_nfts_per_player;
    $("setting-packet-minutes").value = Math.round(chain.packet_expiry_seconds / 60);
  }

  function integerField(id, label) {
    const value = Number($(id).value);
    if (!Number.isInteger(value)) {
      throw new Error(`${label}要填整数`);
    }
    return value;
  }

  function decimalField(id, label) {
    const value = Number($(id).value);
    if (!Number.isFinite(value)) {
      throw new Error(`${label}要填数字`);
    }
    return value;
  }

  function settingsPatch() {
    const override = $("setting-override").value.trim();
    const minFee = parseCoins($("setting-min-fee").value);
    if (minFee > SAT_PER_COIN) {
      throw new Error("手续费下限最多 1 币");
    }
    return {
      chain: {
        difficulty_override: override === "" ? null : Number(override),
        difficulty_max: integerField("setting-difficulty-max", "自动难度上限"),
        target_block_seconds: integerField("setting-target-seconds", "目标出块时间"),
        retarget_interval_blocks: integerField("setting-retarget-blocks", "难度调整周期"),
        stall_seconds: integerField("setting-stall-seconds", "停滞降难度时间"),
        initial_reward_coins: integerField("setting-reward", "每块基础奖励"),
        halving_interval_blocks: integerField("setting-halving", "奖励减半周期"),
        finder_bonus_percent: integerField("setting-bonus", "发现者额外奖励"),
        min_fee_sat: minFee,
        max_nfts_per_player: integerField("setting-nft-limit", "每人藏品上限"),
        packet_expiry_seconds: integerField("setting-packet-minutes", "红包有效期") * 60,
      },
      pool: {
        share_target_seconds: decimalField("setting-share-seconds", "份额目标时间"),
      },
    };
  }

  async function handleSettingsSubmit(event) {
    event.preventDefault();
    try {
      const settings = await api.post("/api/teacher/settings", settingsPatch());
      fillSettings(settings);
      notify("课堂参数已生效", "success");
      await refreshOverview();
    } catch (problem) {
      notify(problem.message, "error");
    }
  }

  /* ---------- 课堂操作 ---------- */

  async function resetRound() {
    if (!confirm("开始新一局？链上余额、藏品、红包和累计挖矿都会清零，学生保留昵称和钱包。")) {
      return;
    }
    try {
      await api.post("/api/teacher/reset");
      notify("新一局已经开始", "success");
      await refreshOverview();
    } catch (problem) {
      notify(problem.message, "error");
    }
  }

  async function exportResults() {
    try {
      const response = await fetch("/api/teacher/export.csv", {
        headers: { Authorization: `Bearer ${state.token}` },
      });
      if (!response.ok) {
        throw new Error(`导出失败（HTTP ${response.status}）`);
      }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = "mining-results.csv";
      link.click();
      URL.revokeObjectURL(url);
    } catch (problem) {
      notify(problem.message, "error");
    }
  }

  async function handleAnnounce(event) {
    event.preventDefault();
    try {
      await api.post("/api/teacher/announce", { message: $("announce-message").value.trim() });
      $("announce-message").value = "";
      notify("通知已发到学生控制台和大屏", "success");
      await refreshOverview();
    } catch (problem) {
      notify(problem.message, "error");
    }
  }

  /* ---------- 事件绑定 ---------- */

  function bindEvents() {
    $("login-form").addEventListener("submit", handleLoginSubmit);
    $("logout").addEventListener("click", logout);
    $("announce-form").addEventListener("submit", handleAnnounce);
    $("settings-form").addEventListener("submit", handleSettingsSubmit);
    $("reload-settings").addEventListener("click", () => {
      if (state.overview) {
        fillSettings(state.overview.settings);
      }
    });
    $("reset-round").addEventListener("click", resetRound);
    $("export").addEventListener("click", exportResults);
    $("export-wide").addEventListener("click", exportResults);
    $("players").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-action]");
      if (button) {
        handlePlayerAction(button);
      }
    });
  }

  function boot() {
    bindEvents();
    restoreSession();
  }

  boot();
})();
