/* ===========================================================================
   Trae Token Dashboard — Shared client logic
   ---------------------------------------------------------------------------
   Vanilla JS, no build step. Loaded by the overview page.
   Exposes utility functions + theme toggle + toast helper on window.App.

   Responsibilities:
   - Number / text formatting (formatTokens, formatInt, formatDate, ...)
   - Aggregations (aggregateByDay, aggregateByModel)
   - Thin fetch wrapper (apiGet, fetchStatus, fetchAccounts, fetchHistory)
   - Theme (initTheme, applyTheme, toggleTheme) + localStorage persistence
   - Inline SVG sparkline renderer
   - Toast + URL helpers
   =========================================================================== */
(function (global) {
  "use strict";

  // ----- Constants -------------------------------------------------------
  const CACHE_KEY = "trae-dashboard-cache-v1";
  const THEME_KEY = "trae-theme";
  // No background polling — the dashboard only refreshes when the user
  // clicks the refresh button (which calls POST /api/refresh). Set to 0
  // to disable legacy callers that imported this constant.
  const AUTO_REFRESH_MS = 0;

  // ----- Number / text helpers -------------------------------------------

  /**
   * Format a token count with K/M suffix.
   *   999   -> "999"
   *   1234  -> "1.2K"
   *   1_500_000 -> "1.5M"
   */
  function formatTokens(n) {
    n = Number(n) || 0;
    if (n < 1000) return String(n);
    if (n < 1_000_000) {
      const v = n / 1000;
      return (v < 10 ? v.toFixed(2) : v < 100 ? v.toFixed(1) : v.toFixed(0)) + "K";
    }
    const v = n / 1_000_000;
    return (v < 10 ? v.toFixed(2) : v < 100 ? v.toFixed(1) : v.toFixed(0)) + "M";
  }

  /** Format a number with thousands separators (en-US). */
  function formatInt(n) {
    n = Number(n) || 0;
    return n.toLocaleString("en-US");
  }

  /** Format a YYYY-MM-DD string as "M月D日" (Chinese short form). */
  function formatDate(iso) {
    if (!iso) return "";
    const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (!m) return iso;
    return `${parseInt(m[2], 10)}月${parseInt(m[3], 10)}日`;
  }

  /** Format HH:MM from an ISO timestamp (local time zone). */
  function formatTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "—";
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `${hh}:${mm}`;
  }

  /**
   * Format a relative time string ("刚刚 / N 分钟前 / N 小时前 / N 天前").
   * Accepts either an ISO string, a Date, or a unix timestamp (number).
   */
  function formatRelativeTime(input) {
    if (input == null) return "—";
    let d;
    if (input instanceof Date) d = input;
    else if (typeof input === "number") d = new Date(input);
    else d = new Date(input);
    if (isNaN(d.getTime())) return "—";
    const now = Date.now();
    const diff = Math.max(0, now - d.getTime());
    const sec = Math.floor(diff / 1000);
    if (sec < 45) return "刚刚";
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min} 分钟前`;
    const hr = Math.floor(min / 60);
    if (hr < 24) return `${hr} 小时前`;
    const day = Math.floor(hr / 24);
    if (day < 30) return `${day} 天前`;
    return d.toLocaleDateString("zh-CN");
  }

  /**
   * Format countdown from now to endIso into "{d} 天 {h} 时 {m} 分".
   * Returns null if endIso is missing/unparseable or already past.
   */
  function formatCountdown(endIso) {
    const d = parseDateLoose(endIso);
    if (!d) return null;
    const diff = d.getTime() - Date.now();
    if (diff <= 0) return null;
    const totalMin = Math.floor(diff / 60000);
    const days = Math.floor(totalMin / 1440);
    const hours = Math.floor((totalMin % 1440) / 60);
    const mins = totalMin % 60;
    return days + " 天 " + hours + " 时 " + mins + " 分";
  }

  /** Format a percentage with up to 1 decimal. */
  function formatPercent(pct, digits) {
    const n = Number(pct) || 0;
    const d = digits == null ? 1 : digits;
    return n.toFixed(d) + "%";
  }

  /** First character of a string (used for avatar). */
  function initial(text) {
    if (!text) return "?";
    return String(text).trim().charAt(0).toUpperCase();
  }

  /** Deterministic 32-bit hash of a string (avatar hue bucket). */
  function hashEmail(s) {
    let h = 0;
    const str = String(s || "");
    for (let i = 0; i < str.length; i++) {
      h = ((h << 5) - h) + str.charCodeAt(i);
      h |= 0;
    }
    return Math.abs(h);
  }

  /** Color-blind safe palette for sparkline per-row hues (cycles). */
  const HUE_TABLE = [
    220, 260, 340, 30, 180, 280, 10, 200, 320, 100,
    240, 60, 150, 290, 0, 270, 190, 50, 310, 130,
  ];
  function hueFor(key) {
    return HUE_TABLE[hashEmail(key) % HUE_TABLE.length];
  }

  // ----- Date helpers ----------------------------------------------------

  /**
   * Parse a YYYY-MM-DD or full ISO string as a local Date. Returns null
   * on failure.
   */
  function parseDateLoose(s) {
    if (!s) return null;
    const d = new Date(s);
    if (isNaN(d.getTime())) return null;
    return d;
  }

  /**
   * Format a YYYY-MM-DD-ish timestamp as a short Chinese cycle range label,
   * e.g. "2026-06-10 ~ 2026-06-29".
   * Accepts ISO strings with or without time/timezone.
   */
  function formatCycleRange(startIso, endIso) {
    const a = parseDateLoose(startIso);
    const b = parseDateLoose(endIso);
    const fmt = (d) => {
      if (!d) return "—";
      const y = d.getFullYear();
      const m = String(d.getMonth() + 1).padStart(2, "0");
      const day = String(d.getDate()).padStart(2, "0");
      return `${y}-${m}-${day}`;
    };
    if (!a && !b) return "周期 —";
    if (!a) return "周期 ~ " + fmt(b);
    if (!b) return "周期 " + fmt(a) + " ~";
    return "周期 " + fmt(a) + " ~ " + fmt(b);
  }

  /**
   * Number of days remaining from `now` to `endIso` (>= 0).
   * Returns null if endIso is missing/unparseable.
   */
  function daysUntil(endIso) {
    const d = parseDateLoose(endIso);
    if (!d) return null;
    const diff = d.getTime() - Date.now();
    if (diff <= 0) return 0;
    return Math.ceil(diff / 86400000);
  }

  // ----- Aggregations ----------------------------------------------------

  /**
   * Aggregate history rows by date -> Map("YYYY-MM-DD" -> { in, out }).
   * Returns array of { date, in, out } sorted ascending by date.
   */
  function aggregateByDay(history) {
    const buckets = new Map();
    for (const row of history || []) {
      const date = row.date;
      if (!buckets.has(date)) buckets.set(date, { date, in: 0, out: 0 });
      const b = buckets.get(date);
      b.in += Number(row.input_tokens) || 0;
      b.out += Number(row.output_tokens) || 0;
    }
    return Array.from(buckets.values()).sort((a, b) => a.date.localeCompare(b.date));
  }

  /**
   * Aggregate history rows by model_name.
   * Returns array sorted by total tokens desc.
   */
  function aggregateByModel(history) {
    const buckets = new Map();
    for (const row of history || []) {
      const key = row.model_name || "unknown";
      if (!buckets.has(key)) {
        buckets.set(key, {
          model_name: row.model_name || "unknown",
          model_type: row.model_type || "-",
          model_source: row.model_source || "-",
          input_tokens: 0,
          output_tokens: 0,
          calls: 0,
        });
      }
      const b = buckets.get(key);
      b.input_tokens += Number(row.input_tokens) || 0;
      b.output_tokens += Number(row.output_tokens) || 0;
      b.calls += 1;
    }
    const total = Array.from(buckets.values()).reduce(
      (s, m) => s + m.input_tokens + m.output_tokens,
      0
    );
    const rows = Array.from(buckets.values()).map((m) => ({
      ...m,
      pct: total > 0 ? ((m.input_tokens + m.output_tokens) / total) * 100 : 0,
    }));
    rows.sort((a, b) => b.input_tokens + b.output_tokens - (a.input_tokens + a.output_tokens));
    return rows;
  }

  // ----- API -------------------------------------------------------------

  /**
   * Thin fetch wrapper. On network/HTTP error throws Error(friendly msg).
   */
  async function apiGet(url) {
    let response;
    try {
      response = await fetch(url, { headers: { Accept: "application/json" } });
    } catch (err) {
      throw new Error(`网络错误: ${err.message || err}`);
    }
    if (!response.ok) {
      let msg = `HTTP ${response.status}`;
      try {
        const body = await response.json();
        if (body && body.detail) msg = `${msg} ${body.detail}`;
      } catch (_) {
        /* ignore */
      }
      throw new Error(msg);
    }
    try {
      return await response.json();
    } catch (err) {
      throw new Error("响应不是合法 JSON");
    }
  }

  async function fetchHealth() {
    try {
      return await apiGet("/api/health");
    } catch (err) {
      return { ok: false, error: err.message };
    }
  }

  /**
   * GET /api/status — dashboard health + (when available) cycle metadata.
   * Returns the raw payload; helpers like `enrichStatus` derive quota info
   * from the response.
   */
  async function fetchStatus() {
    return apiGet("/api/status");
  }

  async function postRefresh() {
    // POST /api/refresh triggers a synchronous fetch cycle on the server.
    // The server hits the Trae API once with ALL configured accounts and
    // returns when the cycle is done.
    const resp = await fetch("/api/refresh", { method: "POST" });
    if (!resp.ok) {
      let detail = resp.statusText;
      try {
        const j = await resp.json();
        detail = j.detail || JSON.stringify(j);
      } catch (_) {}
      throw new Error(detail);
    }
    return resp.json();
  }

  /**
   * POST JSON to an API endpoint. Throws Error(friendly msg) on HTTP error.
   * The error message is the response body's `detail` field when present.
   */
  async function apiPost(url, body) {
    let response;
    try {
      response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(body || {}),
      });
    } catch (err) {
      throw new Error(`网络错误: ${err.message || err}`);
    }
    if (!response.ok) {
      throw await _toHttpError(response);
    }
    try {
      return await response.json();
    } catch (_) {
      return {};
    }
  }

  /** DELETE an API endpoint. Throws Error(friendly msg) on HTTP error. */
  async function apiDelete(url) {
    let response;
    try {
      response = await fetch(url, { method: "DELETE", headers: { Accept: "application/json" } });
    } catch (err) {
      throw new Error(`网络错误: ${err.message || err}`);
    }
    if (!response.ok) {
      throw await _toHttpError(response);
    }
    try {
      return await response.json();
    } catch (_) {
      return {};
    }
  }

  /**
   * PUT JSON to an API endpoint. Throws Error(friendly msg) on HTTP error.
   * The error message is the response body's `detail` field when present.
   */
  async function apiPut(url, body) {
    let response;
    try {
      response = await fetch(url, {
        method: "PUT",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(body || {}),
      });
    } catch (err) {
      throw new Error(`网络错误: ${err.message || err}`);
    }
    if (!response.ok) {
      throw await _toHttpError(response);
    }
    try {
      return await response.json();
    } catch (_) {
      return {};
    }
  }

  /**
   * POST JSON to an API endpoint and return the raw `Blob` body.
   * Used by `.eml` download (Content-Type: message/rfc822).
   * Throws Error(friendly msg) on HTTP error.
   */
  async function apiPostBlob(url, body) {
    let response;
    try {
      response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(body || {}),
      });
    } catch (err) {
      throw new Error(`网络错误: ${err.message || err}`);
    }
    if (!response.ok) {
      throw await _toHttpError(response);
    }
    return response.blob();
  }

  async function _toHttpError(response) {
    let msg = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (body && body.detail) msg = `${msg} ${body.detail}`;
    } catch (_) {
      /* ignore */
    }
    return new Error(msg);
  }

  // ----- Status enrichment helpers (T5) -----------------------------------

  /**
   * Format a token count using the dashboard's standard K/M suffix.
   * @param {number|null|undefined} n
   * @returns {string}
   */
  function consumedFmt(n) {
    return formatTokens(n || 0);
  }

  /**
   * Format a utilization percentage with 2 decimals, e.g. 24.69.
   * Falls back to "0.00" if pct is missing.
   * @param {number|null|undefined} pct
   * @returns {string}
   */
  function utilizationPct(pct) {
    const n = Number(pct);
    if (!isFinite(n)) return "0.00";
    return n.toFixed(2);
  }

  /**
   * Format the cycle window as "2026-06-10 ~ 2026-06-29".
   * Accepts ISO strings (with or without time component).
   * @param {string|null|undefined} startISO
   * @param {string|null|undefined} endISO
   * @returns {string}
   */
  function cycleLabel(startISO, endISO) {
    if (!startISO || !endISO) return "—";
    const s = String(startISO).substring(0, 10);
    const e = String(endISO).substring(0, 10);
    return `${s} ~ ${e}`;
  }

  /**
   * GET /api/accounts?cycle=true (or ?days=N) — list of accounts with cycle
   * totals. When the backend ships the cycle endpoint, this passes
   * `cycle=true`; until then it falls back to `days=30` so the dashboard
   * keeps working against the current backend.
   *
   * Normalizes the response so callers can always rely on the following
   * fields per account:
   *   { email, display_name, consumed, input_tokens, output_tokens, active_days }
   * Older backends return `total_in` / `total_out` instead of `consumed` /
   * `input_tokens` / `output_tokens`; we adapt in `normalizeAccount`.
   */
  async function fetchAccounts() {
    let rows;
    try {
      rows = await apiGet("/api/accounts?cycle=true");
    } catch (_) {
      // Backend hasn't shipped the cycle endpoint yet — fall back gracefully.
      rows = await apiGet("/api/accounts?days=30");
    }
    return (rows || []).map(normalizeAccount);
  }

  /**
   * Adapt one row from the v1 (total_in / total_out) shape to the unified
   * v2 (consumed / input_tokens / output_tokens) shape.
   */
  function normalizeAccount(row) {
    const input = Number(row.input_tokens != null ? row.input_tokens : row.total_in) || 0;
    const output = Number(row.output_tokens != null ? row.output_tokens : row.total_out) || 0;
    const consumed = Number(
      row.consumed != null ? row.consumed : input + output
    ) || 0;
    return {
      email: row.email,
      display_name: row.display_name,
      consumed: consumed,
      input_tokens: input,
      output_tokens: output,
      active_days: Number(row.active_days) || 0,
      // Pass-through fields used by the per-account table:
      //   - models: per-model breakdown for the consumed-cell tooltip
      //   - per_account_quota / quota_used_pct / model_count: row chips
      // Older backends may not return these; guard with defaults.
      models: Array.isArray(row.models) ? row.models : [],
      model_count: Number(row.model_count) || 0,
      per_account_quota: Number(row.per_account_quota) || 0,
      quota_used_pct: Number(row.quota_used_pct) || 0,
    };
  }

  async function fetchHistory(email, days) {
    return apiGet(
      `/api/accounts/${encodeURIComponent(email)}/history?days=${encodeURIComponent(days)}`
    );
  }

  /**
   * Merge a /api/status payload (which may or may not include cycle/quota
   * fields, depending on backend version) with a derived fallback computed
   * from the account list.
   *
   * Returns:
   *   {
   *     total_accounts, accounts_with_data,
   *     cycle_start, cycle_end,
   *     per_account_quota, total_quota, total_consumed, total_remaining,
   *     utilization_pct,
   *     ...all original fields (last_fetched_at, seconds_since_fetch, db_path)
   *   }
   */
  function enrichStatus(status, accounts) {
    const accs = accounts || [];
    const sumIn = accs.reduce((s, a) => s + (a.input_tokens || 0), 0);
    const sumOut = accs.reduce((s, a) => s + (a.output_tokens || 0), 0);
    const sumConsumed = accs.reduce((s, a) => s + (a.consumed || 0), 0);
    const accountsWithData = accs.filter(
      (a) => (a.input_tokens || 0) > 0 || (a.output_tokens || 0) > 0
    ).length;

    const total_consumed = Number(status && status.total_consumed) || sumConsumed || sumIn + sumOut;
    const per_account_quota = Number(status && status.per_account_quota) || 50_000_000;
    // total_quota = per_account_quota * accounts_with_data (or status value)
    const total_quota = Number(status && status.total_quota) ||
      per_account_quota * Math.max(accountsWithData, accs.length);
    const total_remaining = Number(status && status.total_remaining) ||
      Math.max(0, total_quota - total_consumed);

    const utilization_pct = Number(status && status.utilization_pct) ||
      (total_quota > 0 ? (total_consumed / total_quota) * 100 : 0);

    return Object.assign({}, status || {}, {
      total_consumed: total_consumed,
      per_account_quota: per_account_quota,
      total_quota: total_quota,
      total_remaining: total_remaining,
      utilization_pct: utilization_pct,
      cycle_start: (status && status.cycle_start) || null,
      cycle_end: (status && status.cycle_end) || null,
      nextResetAt: (status && status.nextResetAt) || null,
    });
  }

  // ----- Cache (localStorage) -------------------------------------------

  function cacheGet(key) {
    try {
      const raw = localStorage.getItem(CACHE_KEY);
      if (!raw) return null;
      const obj = JSON.parse(raw);
      return obj[key] || null;
    } catch (_) {
      return null;
    }
  }

  function cacheSet(key, value) {
    try {
      const raw = localStorage.getItem(CACHE_KEY);
      const obj = raw ? JSON.parse(raw) : {};
      obj[key] = { value, ts: Date.now() };
      localStorage.setItem(CACHE_KEY, JSON.stringify(obj));
    } catch (_) {
      /* quota / privacy mode — ignore */
    }
  }

  // ----- Theme ----------------------------------------------------------

  function applyTheme(theme) {
    const root = document.documentElement;
    if (theme === "dark" || theme === "light") {
      root.setAttribute("data-theme", theme);
    } else {
      root.removeAttribute("data-theme");
    }
  }

  function readStoredTheme() {
    try {
      const stored = localStorage.getItem(THEME_KEY);
      if (stored === "dark" || stored === "light") return stored;
    } catch (_) {
      /* ignore */
    }
    const current = document.documentElement.getAttribute("data-theme");
    if (current === "dark" || current === "light") return current;
    if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
      return "dark";
    }
    return "light";
  }

  function initTheme() {
    let theme;
    try {
      theme = localStorage.getItem(THEME_KEY);
    } catch (_) {
      theme = null;
    }
    if (theme !== "dark" && theme !== "light") {
      theme = readStoredTheme();
      try {
        localStorage.setItem(THEME_KEY, theme);
      } catch (_) {
        /* ignore */
      }
    }
    applyTheme(theme);
    return theme;
  }

  function toggleTheme() {
    const cur = readStoredTheme();
    const next = cur === "dark" ? "light" : "dark";
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch (_) {
      /* ignore */
    }
    applyTheme(next);
    return next;
  }


  // ----- Toast ----------------------------------------------------------

  function ensureToastContainer() {
    let c = document.querySelector(".toast-container");
    if (!c) {
      c = document.createElement("div");
      c.className = "toast-container";
      c.setAttribute("aria-live", "polite");
      c.setAttribute("aria-atomic", "false");
      document.body.appendChild(c);
    }
    return c;
  }

  function showToast(message, opts) {
    opts = opts || {};
    const duration = opts.duration || 4000;
    const variant = opts.variant || "error";
    const container = ensureToastContainer();
    const el = document.createElement("div");
    el.className = "toast toast--" + variant;
    el.setAttribute("role", variant === "error" ? "alert" : "status");
    el.textContent = message;
    container.appendChild(el);

    let timer = setTimeout(() => dismiss(el), duration);
    el.addEventListener("mouseenter", () => clearTimeout(timer));
    el.addEventListener("mouseleave", () => {
      timer = setTimeout(() => dismiss(el), Math.min(2000, duration));
    });
  }

  function dismiss(el) {
    el.style.transition = "opacity 200ms";
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 250);
  }

  // ----- URL helpers -----------------------------------------------------

  function getUrlParam(name, fallback) {
    const p = new URLSearchParams(window.location.search);
    const v = p.get(name);
    return v === null || v === "" ? fallback : v;
  }

  // ----- Global error safety net -----------------------------------------

  window.addEventListener("unhandledrejection", (e) => {
    console.error("[trae-dashboard] unhandled rejection:", e.reason);
    const msg = e.reason && e.reason.message ? e.reason.message : "请求失败";
    showToast(msg, { variant: "error" });
    e.preventDefault();
  });

  // ----- Public API ------------------------------------------------------

  global.App = {
    // formatting
    formatTokens,
    formatInt,
    formatDate,
    formatTime,
    formatRelativeTime,
    formatPercent,
    initial,
    hashEmail,
    hueFor,
    // date helpers
    formatCycleRange,
    daysUntil,
    formatCountdown,
    // aggregation
    aggregateByDay,
    aggregateByModel,
    // api
    apiGet,
    apiPost,
    apiPut,
    apiPostBlob,
    apiDelete,
    fetchHealth,
    fetchStatus,
    fetchAccounts,
    postRefresh,
    fetchHistory,
    // status helpers
    consumedFmt,
    utilizationPct,
    cycleLabel,
    enrichStatus,
    normalizeAccount,
    // cache
    cacheGet,
    cacheSet,
    // theme
    applyTheme,
    initTheme,
    toggleTheme,
    // toast / url
    showToast,
    getUrlParam,
    // constant
    AUTO_REFRESH_MS,
  };
})(window);
