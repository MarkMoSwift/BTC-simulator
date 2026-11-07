/* 课堂投影大屏：两块排行榜 + 榜单底部的区块、红包、市场与拍卖动态。

   大屏只读，用匿名 WebSocket 接收服务端每秒推送的课堂快照。 */
(function () {
  "use strict";

  const { setText, setHtml, deferWhileSelected, Feed, formatCoins, formatHashrate, formatTime, formatCountdown, auctionPrice, escapeHtml, shortAddress, notify } = window.Sim;

  const BOARD_ROWS = 10;
  const STRIP_ROWS = 4;
  const BANNER_MS = 6000;
  const MOVE_MS = 1200;

  const $ = (id) => document.getElementById(id);

  const state = {
    chain: null,
    blocks: [],
    names: {},
    announcements: "",
  };

  const moveTimers = new WeakMap();
  const feed = new Feed(handleEvent, handleConnection, "/ws/screen");

  function handleEvent(event) {
    const data = event.data || {};
    if (event.type === "hello" || event.type === "tick") {
      state.tick = data;
      renderAll();
    } else if (event.type === "block") {
      announceBlock(data);
      prependBlock(data);
    } else if (event.type === "chain_changed") {
      state.blocks = [];
      if (data.reorganized) notify("主链已重组，榜单按当前主链更新", "info");
    } else if (event.type === "announcement") {
      renderAnnouncement({ message: data.message });
      if (data.message) {
        notify(`老师通知：${data.message}`, "info");
      }
    } else if (event.type === "reset") {
      state.blocks = [];
      renderAll();
    }
  }

  function handleConnection(online) {
    setText($("screen-sub"), online ? "" : "与大屏的连接已断开，正在重连…");
    if (online && state.tick) {
      renderSubtitle();
    }
  }

  function renderAll() {
    const tick = state.tick;
    if (!tick) {
      return;
    }
    state.chain = tick.chain;
    state.blocks = tick.recent_blocks || [];
    state.names = Object.fromEntries(tick.leaderboard.rows.map((row) => [row.address, row.nickname]));
    setText($("metric-height"), tick.chain.height.toLocaleString());
    setText($("metric-difficulty"), `${tick.chain.difficulty} 位`);
    setText($("metric-hashrate"), formatHashrate(tick.leaderboard.network_hashrate));
    setText($("metric-online"), `${tick.online} / ${tick.leaderboard.rows.length}`);
    renderSubtitle();
    renderBoard($("board-hashrate"), tick.leaderboard.by_hashrate, "hashrate");
    renderBoard($("board-mined"), tick.leaderboard.by_mined, "mined");
    setText($("hashrate-note"), "在线状态与算力综合本节点观测及联网节点通报；挖矿收益以链上记录为准");
    renderPackets(tick.packets.active);
    renderMarket(tick.market);
    renderAuctions(tick.auctions.active);
    renderBlocks();
    renderAnnouncement(tick.announcement);
  }

  function renderSubtitle() {
    const chain = state.chain;
    if (!chain) {
      return;
    }
    setText($("screen-sub"),
      `链上已有 ${chain.height} 个区块 · 下一块奖励 ${formatCoins(chain.subsidy)} 币 · ` +
      `目标出块 ${chain.target_block_seconds} 秒`);
  }

  /* ---------- 排行榜 ---------- */

  function renderBoard(list, rows, mode) {
    if (deferWhileSelected(list, () => renderBoard(list, rows, mode))) return;
    if (!rows.length) {
      if (!list.querySelector(".board-empty")) {
        setHtml(list, `<li class="board-empty">还没有同学加入，用浏览器打开课堂网址即可开始挖矿</li>`);
      }
      return;
    }
    const empty = list.querySelector(".board-empty");
    if (empty) {
      empty.remove();
    }
    const visible = rows.slice(0, BOARD_ROWS);
    const keep = new Set();
    visible.forEach((row, index) => {
      const place = index + 1;
      keep.add(row.address);
      let node = list.querySelector(`[data-address="${row.address}"]`);
      if (!node) {
        node = document.createElement("li");
        node.className = "board-row";
        node.dataset.address = row.address;
        node.innerHTML =
          '<span class="board-rank"></span>' +
          '<span class="board-who"><span class="board-name"></span><span class="board-meta"></span></span>' +
          '<span class="board-value"></span>';
      }
      const previous = Number(node.dataset.place || 0);
      node.dataset.place = String(place);
      node.classList.remove("place-1", "place-2", "place-3", "is-offline");
      if (place <= 3) {
        node.classList.add(`place-${place}`);
      }
      list.appendChild(node);
      setText(node.querySelector(".board-rank"), String(place));
      setHtml(node.querySelector(".board-name"),
        `${escapeHtml(row.nickname)}<span class="board-state${row.online ? " is-online" : ""}"></span>`);
      if (mode === "hashrate") {
        setText(node.querySelector(".board-value"), formatHashrate(row.hashrate));
        setText(node.querySelector(".board-meta"),
          `${row.blocks_found} 块 · 累计 ${formatCoins(row.mined, 2)} 币`);
      } else {
        setText(node.querySelector(".board-value"), `${formatCoins(row.mined, 2)} 币`);
        setText(node.querySelector(".board-meta"),
          `${formatHashrate(row.hashrate)} · 挖到 ${row.blocks_found} 块`);
      }
      if (!row.online) {
        node.classList.add("is-offline");
      }
      if (previous && previous !== place) {
        flash(node, place < previous ? "is-up" : "is-down");
      }
    });
    for (const node of [...list.children]) {
      if (node.dataset.address && !keep.has(node.dataset.address)) {
        node.remove();
      }
    }
  }

  function flash(node, className) {
    node.classList.remove("is-up", "is-down");
    void node.offsetWidth;
    node.classList.add(className);
    clearTimeout(moveTimers.get(node));
    moveTimers.set(node, setTimeout(() => node.classList.remove(className), MOVE_MS));
  }

  /* ---------- 区块、红包、藏品 ---------- */

  function prependBlock(block) {
    state.blocks.unshift(block);
    state.blocks = state.blocks.slice(0, STRIP_ROWS);
    renderBlocks();
  }

  function renderBlocks() {
    const list = $("strip-blocks");
    const rows = state.blocks.slice(0, STRIP_ROWS).map(
      (block) => `<li>
        <strong>#${block.height}</strong>
        <span>${escapeHtml(nicknameOf(block.finder))}</span>
        <span class="strip-muted">难度 ${block.difficulty} · ${formatTime(block.timestamp)}</span>
      </li>`,
    );
    setHtml(list, rows.join("") || `<li class="strip-empty">链上还没有区块</li>`);
  }

  function renderPackets(packets) {
    const rows = packets.slice(0, STRIP_ROWS).map((packet) => {
      const left = packet.count - packet.claimed_count;
      const note = packet.note ? ` · ${escapeHtml(packet.note)}` : "";
      return `<li>
        <strong>${formatCoins(packet.total, 2)} 币</strong>
        <span>${escapeHtml(nicknameOf(packet.sender))} 发的红包</span>
        <span class="strip-muted">还剩 ${left} 个${note}</span>
      </li>`;
    });
    setHtml($("strip-packets"), rows.join("") || `<li class="strip-empty">暂时没有红包，挖到币的同学可以发一个</li>`);
  }

  function renderMarket(listings) {
    const rows = listings.slice(0, STRIP_ROWS).map(
      (item) => `<li>
        <strong>${escapeHtml(item.name)}</strong>
        <span>${formatCoins(item.price, 2)} 币</span>
        <span class="strip-muted">卖家 ${escapeHtml(item.seller_name)}</span>
      </li>`,
    );
    setHtml($("strip-market"), rows.join("") || `<li class="strip-empty">市场上还没有挂单的藏品</li>`);
  }

  function renderAuctions(auctions) {
    const rows = auctions.slice(0, STRIP_ROWS).map(
      (auction) => `<li>
        <strong>${escapeHtml(auction.name)}</strong>
        <span>${formatCoins(auctionPrice(auction), 2)} 币</span>
        <span class="strip-muted">${
          auction.ends_height ? `第 ${auction.ends_height} 块结算` :
            (auction.seconds_left > 0 ? `剩 ${formatCountdown(auction.seconds_left)}` : "等待结算")
        }</span>
      </li>`,
    );
    setHtml($("strip-auctions"), rows.join("") || `<li class="strip-empty">拍卖场还空着</li>`);
  }

  function renderAnnouncement(announcement) {
    const box = $("screen-announcement");
    if (!announcement || !announcement.message) {
      box.hidden = true;
      return;
    }
    setText(box, `老师通知：${announcement.message}`);
    box.hidden = false;
  }

  let bannerTimer = null;

  function announceBlock(block) {
    const banner = $("block-banner");
    banner.innerHTML =
      `<span class="banner-tag">新区块</span>` +
      `<strong>#${block.height}</strong>` +
      `<span>${escapeHtml(nicknameOf(block.finder))} 挖到</span>` +
      `<span class="banner-muted">奖励 ${formatCoins(block.subsidy + block.fees, 2)} 币 · ` +
      `${block.tx_count} 笔交易 · 难度 ${block.difficulty}</span>`;
    banner.hidden = false;
    banner.classList.remove("is-shown");
    void banner.offsetWidth;
    banner.classList.add("is-shown");
    clearTimeout(bannerTimer);
    bannerTimer = setTimeout(() => {
      banner.hidden = true;
      banner.classList.remove("is-shown");
    }, BANNER_MS);
  }

  function nicknameOf(address) {
    return state.names[address] || shortAddress(address);
  }

  /* ---------- 启动 ---------- */

  function tickClock() {
    const chain = state.chain;
    if (!chain) {
      return;
    }
    if (chain.height === 0) {
      setText($("metric-latest"), "等待第一个区块");
      return;
    }
    const seconds = Math.max(Math.floor(Date.now() / 1000) - chain.tip_timestamp, 0);
    setText($("metric-latest"), seconds < 60 ? `${seconds} 秒前` : `${Math.floor(seconds / 60)} 分钟前`);
  }

  async function boot() {
    try {
      const publicState = await new Sim.API().get("/api/public");
      setText($("screen-classroom"), publicState.classroom);
      state.chain = publicState.chain;
      state.blocks = publicState.recent_blocks || [];
      renderSubtitle();
      renderBlocks();
      renderAnnouncement(publicState.announcement);
    } catch (problem) {
      setText($("screen-sub"), `读取课堂信息失败：${problem.message}`);
    }
    feed.connect("");
    setInterval(tickClock, 1000);
  }

  boot();
})();
