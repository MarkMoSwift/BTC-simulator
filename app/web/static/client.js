/* 三个页面共用的前端工具箱：规范化 JSON、接口调用、实时推送、单位换算。 */
(function (global) {
  "use strict";

  const SAT_PER_COIN = 100000000;

  /* 与服务器 app/utils/canonical.py 一致：键排序、无空格、非 ASCII 转义。
     交易号与签名都建立在这段字节上，任何一点不同都会导致签名验证失败。 */
  function canonicalJson(value) {
    if (value === null) {
      return "null";
    }
    if (typeof value === "boolean") {
      return value ? "true" : "false";
    }
    if (typeof value === "number") {
      return JSON.stringify(value);
    }
    if (typeof value === "string") {
      return jsonString(value);
    }
    if (Array.isArray(value)) {
      return "[" + value.map(canonicalJson).join(",") + "]";
    }
    const keys = Object.keys(value).sort();
    return "{" + keys.map((key) => jsonString(key) + ":" + canonicalJson(value[key])).join(",") + "}";
  }

  function jsonString(text) {
    let out = '"';
    for (const character of text) {
      const code = character.codePointAt(0);
      if (character === '"') {
        out += '\\"';
      } else if (character === "\\") {
        out += "\\\\";
      } else if (character === "\n") {
        out += "\\n";
      } else if (character === "\r") {
        out += "\\r";
      } else if (character === "\t") {
        out += "\\t";
      } else if (character === "\b") {
        out += "\\b";
      } else if (character === "\f") {
        out += "\\f";
      } else if (code < 0x20 || code > 0x7e) {
        if (code > 0xffff) {
          const offset = code - 0x10000;
          out += "\\u" + (0xd800 + (offset >> 10)).toString(16).padStart(4, "0");
          out += "\\u" + (0xdc00 + (offset & 0x3ff)).toString(16).padStart(4, "0");
        } else {
          out += "\\u" + code.toString(16).padStart(4, "0");
        }
      } else {
        out += character;
      }
    }
    return out + '"';
  }

  function formatCoins(sat, digits = 4) {
    const value = Math.trunc(sat || 0);
    const sign = value < 0 ? "-" : "";
    const amount = Math.abs(value);
    const whole = Math.floor(amount / SAT_PER_COIN);
    const fraction = String(amount % SAT_PER_COIN).padStart(8, "0").slice(0, digits).replace(/0+$/, "");
    return fraction ? `${sign}${whole}.${fraction}` : `${sign}${whole}`;
  }

  function parseCoins(text) {
    const raw = String(text).trim();
    if (!/^\d+(\.\d{1,8})?$/.test(raw)) {
      throw new Error("金额格式不正确，最多 8 位小数");
    }
    const [whole, fraction = ""] = raw.split(".");
    return Number(whole) * SAT_PER_COIN + Number(fraction.padEnd(8, "0"));
  }

  function formatHashrate(value) {
    const rate = Number(value) || 0;
    if (rate >= 1000000) {
      return `${(rate / 1000000).toFixed(2)} MH/s`;
    }
    if (rate >= 1000) {
      return `${(rate / 1000).toFixed(1)} kH/s`;
    }
    return `${rate.toFixed(0)} H/s`;
  }

  function formatTime(seconds) {
    if (!seconds) {
      return "--:--:--";
    }
    return new Date(Number(seconds) * 1000).toLocaleTimeString("zh-CN", { hour12: false });
  }

  function formatDuration(seconds) {
    const total = Math.max(Math.floor(Number(seconds) || 0), 0);
    const minutes = Math.floor(total / 60);
    return minutes > 0 ? `${minutes} 分 ${total % 60} 秒` : `${total} 秒`;
  }

  function formatCountdown(seconds) {
    const total = Math.max(Math.floor(Number(seconds) || 0), 0);
    return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
  }

  function auctionPrice(auction) {
    return auction.highest_bid === null ? auction.reserve_price : auction.highest_bid;
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[character]);
  }

  function shortAddress(address) {
    const text = String(address || "");
    return text.length > 14 ? `${text.slice(0, 8)}...${text.slice(-4)}` : text;
  }

  class API {
    constructor(token) {
      this.token = token || "";
      this.recoverSession = null;
    }

    headers(extra) {
      const headers = { ...(extra || {}) };
      if (this.token) {
        headers.Authorization = `Bearer ${this.token}`;
      }
      return headers;
    }

    async request(path, options, retryAuth = true) {
      const config = { ...(options || {}) };
      config.headers = this.headers(config.body ? { "Content-Type": "application/json" } : {});
      const rejectedToken = this.token;
      const controller = new AbortController();
      if (!config.signal) config.signal = controller.signal;
      const timeout = global.setTimeout(() => controller.abort(), 15000);
      let response, data;
      try {
        response = await fetch(path, config);
        const text = await response.text();
        data = text ? JSON.parse(text) : {};
      } finally {
        global.clearTimeout(timeout);
      }
      if (!response.ok) {
        const error = new Error(data.detail || `请求失败（HTTP ${response.status}）`);
        error.status = response.status;
        const expired = response.status === 401 || ["请先登录", "登录状态已过期，请重新登录"].includes(data.detail);
        if (expired && retryAuth && rejectedToken && this.recoverSession && !path.startsWith("/api/auth/")) {
          await this.recoverSession(rejectedToken);
          return this.request(path, options, false);
        }
        throw error;
      }
      return data;
    }

    get(path) {
      return this.request(path);
    }

    post(path, body) {
      return this.request(path, { method: "POST", body: JSON.stringify(body === undefined ? {} : body) });
    }
  }

  class Feed {
    constructor(onEvent, onStatus, path = "/ws") {
      this.onEvent = onEvent;
      this.onStatus = onStatus || (() => {});
      this.socket = null;
      this.closed = false;
      this.retry = null;
      this.path = path;
    }

    connect(token) {
      this.close();
      this.closed = false;
      const scheme = global.location.protocol === "https:" ? "wss" : "ws";
      const query = token ? `?token=${encodeURIComponent(token)}` : "";
      const socket = this.socket = new WebSocket(`${scheme}://${global.location.host}${this.path}${query}`);
      socket.onopen = () => { if (this.socket === socket) this.onStatus(true); };
      socket.onmessage = (message) => {
        if (this.socket !== socket) return;
        try {
          this.onEvent(JSON.parse(message.data));
        } catch (error) {
          console.warn("无法解析推送消息", error);
        }
      };
      socket.onclose = () => {
        if (this.socket !== socket) return;
        this.onStatus(false);
        if (!this.closed) {
          this.retry = global.setTimeout(() => this.connect(token), 2000);
        }
      };
      socket.onerror = () => socket.close();
    }

    close() {
      this.closed = true;
      global.clearTimeout(this.retry);
      if (this.socket) {
        const socket = this.socket;
        this.socket = null;
        socket.close();
      }
    }
  }

  // 复制实时信息时保留选区，释放选区后只补画最新状态。
  const pendingRenders = new Map();
  const renderedHtml = new WeakMap();
  let selectionStart = null;

  const flushRenders = () => {
    for (const [element, render] of [...pendingRenders]) {
      if (!element.isConnected) pendingRenders.delete(element);
      else if (!deferWhileSelected(element, render)) render();
    }
  };
  document.addEventListener("pointerdown", event => {
    if (event.button === 0) selectionStart = event.target;
  });
  const finishSelectionDrag = () => { selectionStart = null; flushRenders(); };
  document.addEventListener("pointerup", finishSelectionDrag);
  document.addEventListener("pointercancel", finishSelectionDrag);
  global.addEventListener("blur", finishSelectionDrag);

  function deferWhileSelected(element, render) {
    if (selectionStart && (element.contains(selectionStart) || selectionStart.contains(element))) {
      pendingRenders.set(element, render);
      return true;
    }
    const selection = global.getSelection();
    if (selection && !selection.isCollapsed) {
      for (let index = 0; index < selection.rangeCount; index += 1) {
        if (selection.getRangeAt(index).intersectsNode(element)) {
          pendingRenders.set(element, render);
          return true;
        }
      }
    }
    pendingRenders.delete(element);
    return false;
  }

  document.addEventListener("selectionchange", flushRenders);

  function setText(element, value) {
    const text = String(value);
    if (element.textContent === text) {
      pendingRenders.delete(element);
      return;
    }
    if (!deferWhileSelected(element, () => setText(element, text))) element.textContent = text;
  }

  function setHtml(element, html) {
    const previous = renderedHtml.get(element);
    const current = element.innerHTML;
    if (current === html || (previous?.html === html && previous.dom === current)) {
      pendingRenders.delete(element);
      return;
    }
    if (deferWhileSelected(element, () => setHtml(element, html))) return;
    element.innerHTML = html;
    renderedHtml.set(element, {html, dom: element.innerHTML});
  }

  function notify(message, kind = "info") {
    const area = document.getElementById("toasts");
    if (!area) {
      return;
    }
    const item = document.createElement("div");
    item.className = `toast toast-${kind}`;
    item.textContent = message;
    area.appendChild(item);
    global.setTimeout(() => item.classList.add("toast-out"), 3200);
    global.setTimeout(() => item.remove(), 3800);
  }

  global.Sim = {
    SAT_PER_COIN,
    canonicalJson,
    formatCoins,
    parseCoins,
    formatHashrate,
    formatTime,
    formatDuration,
    formatCountdown,
    auctionPrice,
    escapeHtml,
    shortAddress,
    notify,
    setText,
    setHtml,
    deferWhileSelected,
    API,
    Feed,
  };
})(window);
