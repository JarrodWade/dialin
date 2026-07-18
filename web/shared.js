/* dialin — shared JS utilities
 * Expects DOM elements: #api-base, #toast; optional #user-id (legacy), #clerk-auth-root (Clerk).
 * Optional window.DIALIN_CONFIG from dialin-config.js; optional localStorage dialin.clerkPk.
 */

let _toastEl;

function _initSharedRefs() {
  if (_toastEl) return;
  _toastEl = document.getElementById("toast");
}

function toast(msg, isError = false) {
  _initSharedRefs();
  _toastEl.textContent = msg;
  _toastEl.classList.toggle("error", isError);
  _toastEl.classList.add("show");
  setTimeout(() => _toastEl.classList.remove("show"), 2800);
}

/** True when Clerk publishable key is configured (JWT auth path). */
function clerkConfigured() {
  const cfg = typeof window !== "undefined" ? window.DIALIN_CONFIG : null;
  const fromCfg = (cfg && cfg.clerkPublishableKey) || "";
  const fromLs = (typeof localStorage !== "undefined" && localStorage.getItem("dialin.clerkPk")) || "";
  return Boolean(String(fromCfg || fromLs).trim());
}

/** True when Clerk is on and there is no usable session (including while Clerk boots). */
function needsSignIn() {
  if (!clerkConfigured()) return false;
  return !window.__clerk?.session;
}

/** Friendly copy for list panels before login — never surface raw 401/Unauthorized. */
function signedOutEmptyMessage(kind) {
  const map = {
    coffees: "Sign in to see your coffees.",
    gear: "Sign in to see your gear.",
    brews: "Sign in to see your brews.",
    places: "Sign in to see your cafés and roasters.",
    visits: "Sign in to see your visits.",
    default: "Sign in to load your journal.",
  };
  return map[kind] || map.default;
}

/**
 * Fill a list panel with calm signed-out copy (Sign in lives in the banner + header).
 */
function renderSignedOutEmpty(parent, kind) {
  if (!parent) return;
  parent.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "empty signed-out-empty";
  wrap.textContent = signedOutEmptyMessage(kind);
  parent.appendChild(wrap);
}

/** Normalize list-panel load failures so auth errors stay calm. */
function journalLoadErrorMessage(err, kind) {
  const status = err?.status;
  const raw = String(err?.message || "");
  if (status === 401 || status === 403 || /unauthorized|forbidden/i.test(raw)) {
    return signedOutEmptyMessage(kind);
  }
  return raw || "Couldn’t load this list.";
}

function isAuthLoadError(err) {
  const status = err?.status;
  const raw = String(err?.message || "");
  return status === 401 || status === 403 || /unauthorized|forbidden/i.test(raw);
}

/** Render auth gate or a plain empty error into a list panel. */
function renderJournalLoadError(parent, err, kind) {
  if (!parent) return;
  if (isAuthLoadError(err)) {
    renderSignedOutEmpty(parent, kind);
    return;
  }
  parent.innerHTML = `<div class="empty">${escapeHtml(journalLoadErrorMessage(err, kind))}</div>`;
}

/** Dialin paper/ink theme for Clerk modal + UserButton (hex for Clerk color-mix compat). */
function dialinClerkAppearance() {
  return {
    variables: {
      colorPrimary: "#6b3f1d",
      colorForeground: "#2b1d12",
      colorBackground: "#fbf6ec",
      colorMutedForeground: "#8a7762",
      colorDanger: "#a83a3a",
      colorInputBackground: "#fbf6ec",
      colorInputForeground: "#2b1d12",
      borderRadius: "8px",
      fontFamily: 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
    },
    elements: {
      card: {
        backgroundColor: "#fbf6ec",
        borderColor: "#d8c8b0",
        boxShadow: "0 12px 40px rgba(43, 29, 18, 0.12)",
      },
      headerTitle: { color: "#2b1d12", fontFamily: 'ui-serif, Georgia, "Times New Roman", serif' },
      headerSubtitle: { color: "#8a7762" },
      formButtonPrimary: {
        backgroundColor: "#6b3f1d",
        color: "#fbf6ec",
        "&:hover": { backgroundColor: "#b1582b" },
      },
      footerActionLink: { color: "#6b3f1d" },
      identityPreviewEditButton: { color: "#6b3f1d" },
      userButtonPopoverCard: {
        backgroundColor: "#fbf6ec",
        borderColor: "#d8c8b0",
      },
    },
  };
}

/** Open Clerk sign-in modal (no-op when Clerk is not ready). */
function openDialinSignIn() {
  const clerk = window.__clerk;
  if (!clerk || typeof clerk.openSignIn !== "function") {
    toast("Sign in is still loading — try again in a moment", true);
    return;
  }
  const href = () => window.location.href;
  clerk.openSignIn({
    appearance: dialinClerkAppearance(),
    afterSignInUrl: href(),
    afterSignUpUrl: href(),
  });
}

const _DEV_CHROME_KEY = "dialin.devChrome";

/** Show/hide API + Refresh strip; gear only when Clerk is configured. */
function initDevChrome(opts = {}) {
  const strip = document.getElementById("dev-chrome");
  const toggle = document.getElementById("dev-chrome-toggle");
  if (!strip) return;

  function apply(open) {
    strip.hidden = !open;
    strip.classList.toggle("is-open", open);
    if (toggle) {
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      toggle.classList.toggle("is-active", open);
    }
  }

  if (!clerkConfigured() || opts.forceLegacy) {
    if (toggle) toggle.hidden = true;
    apply(true);
    return;
  }

  if (toggle) {
    toggle.hidden = false;
    const saved = sessionStorage.getItem(_DEV_CHROME_KEY) === "1";
    apply(saved);
    toggle.onclick = () => {
      const next = strip.hidden;
      sessionStorage.setItem(_DEV_CHROME_KEY, next ? "1" : "0");
      apply(next);
    };
  } else {
    apply(false);
  }
}

/** Terms / Clear chat overflow menu. */
function initHeaderMore() {
  const btn = document.getElementById("header-more-btn");
  const menu = document.getElementById("header-more-menu");
  if (!btn || !menu) return;

  function close() {
    menu.hidden = true;
    btn.setAttribute("aria-expanded", "false");
  }

  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = menu.hidden;
    menu.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  });
  menu.addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", close);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") close();
  });
}

/**
 * Toggle signed-out body chrome: banner, hide prefs/+add, soft-disable chat/ask.
 * Call after auth ready / sign-in / sign-out.
 */
function syncAuthUiChrome() {
  const gated = needsSignIn();
  document.body.classList.toggle("signed-out", gated);

  const banner = document.getElementById("signed-out-banner");
  if (banner) banner.hidden = !gated;

  const msg = document.getElementById("msg");
  const send = document.getElementById("send");
  if (msg) {
    if (!msg.dataset.ph) msg.dataset.ph = msg.getAttribute("placeholder") || "";
    msg.disabled = gated;
    msg.placeholder = gated ? "Sign in to chat about your brews…" : msg.dataset.ph;
  }
  if (send) send.disabled = gated;

  const ask = document.getElementById("ask-input");
  if (ask) {
    if (!ask.dataset.ph) ask.dataset.ph = ask.getAttribute("placeholder") || "";
    ask.disabled = gated;
    ask.placeholder = gated ? "Sign in to ask, scout cafés, or get For You picks…" : ask.dataset.ph;
  }
  ["ask-send", "for-you-send", "cafes-send"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.disabled = gated;
  });
}

function _wireSignedOutBannerOnce() {
  const banner = document.getElementById("signed-out-banner");
  if (!banner || banner.dataset.wired) return;
  banner.dataset.wired = "1";
  banner.querySelectorAll("[data-open-signin]").forEach((btn) => {
    btn.addEventListener("click", () => openDialinSignIn());
  });
}

/** @type {any} Clerk browser SDK instance when Clerk auth is enabled */
window.__clerk = window.__clerk || null;

function userId() {
  if (clerkConfigured()) {
    const id = window.__clerk?.user?.id;
    if (!id) {
      toast("Sign in to continue", true);
      throw new Error("not signed in");
    }
    return id;
  }
  const v = (document.getElementById("user-id")?.value || "").trim();
  if (!v) { toast("Set a user id first", true); throw new Error("no user id"); }
  return v;
}

/** Rolling chat window (messages, not exchanges). Match Lambda ``CHAT_HISTORY_TURN_LIMIT``. */
function chatHistoryTurnLimit() {
  const raw = window.DIALIN_CONFIG && window.DIALIN_CONFIG.chatHistoryTurnLimit;
  const n = parseInt(raw, 10);
  return Number.isFinite(n) && n > 0 ? n : 24;
}

/** Trim chat history arrays to the same limit the API keeps. */
function trimChatHistory(history) {
  const list = Array.isArray(history) ? history : [];
  const cap = chatHistoryTurnLimit();
  return list.slice(-cap);
}

/** Stable per-user suffix for localStorage (Clerk sub or legacy user id). Does not throw. */
function storageUserKey() {
  if (clerkConfigured()) {
    return window.__clerk?.user?.id || "_signed_out";
  }
  return (document.getElementById("user-id")?.value || "").trim() || "_";
}

/** Append ?userId= only for legacy mode (no Clerk). */
function withLegacyUserQuery(path, extraQuery = "") {
  if (clerkConfigured()) {
    return extraQuery ? `${path}?${extraQuery}` : path;
  }
  const uid = `userId=${encodeURIComponent(userId())}`;
  if (!extraQuery) return path.includes("?") ? `${path}&${uid}` : `${path}?${uid}`;
  return path.includes("?") ? `${path}&${uid}&${extraQuery}` : `${path}?${uid}&${extraQuery}`;
}

/** Merge device IANA timezone for POST /chat so the server aligns "today/last Sunday" with the user's locale. */
function withClientTimezone(obj) {
  let tz = "";
  try {
    tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch {
    /* ignore */
  }
  const trimmed = tz.trim();
  const base = typeof obj === "object" && obj ? obj : {};
  return trimmed ? { ...base, clientTimezone: trimmed } : base;
}

/** Plain-object form of authedJsonBody: legacy mode adds userId; Clerk mode relies on Authorization only. */
function authedJsonPayload(obj) {
  if (clerkConfigured()) return obj;
  return { ...obj, userId: userId() };
}

/** JSON body: legacy mode adds userId; Clerk mode relies on Authorization only. */
function authedJsonBody(obj) {
  return JSON.stringify(authedJsonPayload(obj));
}

function apiBase() {
  const v = (document.getElementById("api-base")?.value || "").trim().replace(/\/$/, "");
  if (!v) { toast("Set the API URL first", true); throw new Error("no api base"); }
  return v;
}

/** Wait for Clerk to attach a Session after sign-in / reload (SDK can lag behind `user`). */
async function _awaitClerkSession(maxMs = 4000) {
  const c = window.__clerk;
  if (!c) return;
  const deadline = Date.now() + maxMs;
  while (Date.now() < deadline) {
    if (c.session) return;
    if (c.loaded && !c.user) return;
    await new Promise((r) => setTimeout(r, 50));
  }
}

async function _authHeaders() {
  const h = {};
  if (!clerkConfigured() || !window.__clerk) return h;
  const c = window.__clerk;
  if (!c.user) return h;
  await _awaitClerkSession(4000);
  const sess = c.session;
  if (!sess) {
    console.warn("Clerk: no session after wait — API call will be unauthenticated");
    return h;
  }
  try {
    if (typeof c.getToken === "function") {
      const t = await c.getToken();
      if (t) {
        h.authorization = `Bearer ${t}`;
        return h;
      }
    }
    const t = await sess.getToken();
    if (t) h.authorization = `Bearer ${t}`;
    else console.warn("Clerk getToken() returned null");
  } catch (e) {
    console.warn("Clerk getToken() failed:", e);
  }
  return h;
}

async function api(path, opts = {}) {
  const url = apiBase() + path;
  const auth = await _authHeaders();
  const res = await fetch(url, {
    ...opts,
    headers: { "content-type": "application/json", ...auth, ...(opts.headers || {}) },
  });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
  if (!res.ok) {
    let msg = data?.error || data?.message || res.statusText;
    if (res.status === 401 || res.status === 403) {
      if (!clerkConfigured()) {
        msg =
          "Unauthorized — API expects Clerk sign-in. Set clerkPublishableKey in web/dialin-config.js " +
          "(copy from dialin-config.example.js + your Clerk dashboard) or localStorage dialin.clerkPk.";
      } else if (!auth.authorization) {
        msg = "Sign in to continue.";
      } else {
        msg = msg || "Session expired — sign out and sign in again.";
      }
    }
    const err = new Error(msg);
    err.status = res.status;
    err.body = data;
    throw err;
  }
  return data;
}

/**
 * SSE helper against the streaming Lambda Function URL (``streamApiBase``).
 * Returns false when streaming is not configured so callers can fall back.
 *
 * ``handlers``: onStatus / onDelta / onDone / onError — see stream_server.py.
 */
async function streamSse(path, payload, handlers = {}) {
  const base = ((window.DIALIN_CONFIG && window.DIALIN_CONFIG.streamApiBase) || "").trim().replace(/\/$/, "");
  if (!base) {
    console.info("[dialin] streamApiBase not set — using buffered API");
    return false;
  }

  const auth = await _authHeaders();
  const url = base + path;
  console.info("[dialin] streaming via", url);
  const res = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json", ...auth },
    body: JSON.stringify(payload),
  });
  if (!res.ok || !res.body) {
    let msg = res.statusText;
    try {
      const data = await res.json();
      msg = data?.error || msg;
    } catch {
      /* ignore — use statusText */
    }
    const err = new Error(msg || `stream request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      let event = "message";
      const dataLines = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (!dataLines.length) continue;
      let data;
      try {
        data = JSON.parse(dataLines.join("\n"));
      } catch {
        continue;
      }
      if (event === "status") handlers.onStatus?.(data);
      else if (event === "delta") handlers.onDelta?.(data.text || "");
      else if (event === "done") handlers.onDone?.(data);
      else if (event === "error") handlers.onError?.(data);
    }
  }
  return true;
}

/** SSE variant of POST /chat. */
async function streamChat(payload, handlers = {}) {
  return streamSse("/chat/stream", payload, handlers);
}

/** SSE variant of POST /recommendations/beans. */
async function streamRecommendBeans(payload, handlers = {}) {
  return streamSse("/recommendations/beans/stream", payload, handlers);
}

/** SSE variant of POST /recommendations/cafes. */
async function streamRecommendCafes(payload, handlers = {}) {
  return streamSse("/recommendations/cafes/stream", payload, handlers);
}

/** Default rotating copy while a chat / ask turn is in flight. */
const DEFAULT_THINKING_PHRASES = [
  "brewing an answer…",
  "checking your journal…",
  "thinking it through…",
  "dialing it in…",
];

/**
 * Render an animated thinking status into ``el`` (chat bubble or explore ask-reply).
 * Returns ``{ stop, setLabel }`` — call ``stop()`` when the wait ends.
 */
function startThinkingStatus(el, phrases = DEFAULT_THINKING_PHRASES) {
  if (!el) return { stop() {}, setLabel() {} };
  const list = Array.isArray(phrases) && phrases.length ? phrases : DEFAULT_THINKING_PHRASES;
  let i = 0;
  let manual = false;
  el.innerHTML =
    '<span class="thinking-status-row">' +
    '<span class="thinking-text"></span>' +
    '<span class="thinking-dots"><span></span><span></span><span></span></span>' +
    "</span>";
  const textEl = el.querySelector(".thinking-text");
  textEl.textContent = list[0];
  const timer = setInterval(() => {
    if (manual) return;
    i = (i + 1) % list.length;
    textEl.textContent = list[i];
  }, 2200);
  return {
    stop: () => clearInterval(timer),
    setLabel(label) {
      manual = true;
      if (textEl) textEl.textContent = label;
    },
  };
}

/**
 * Load Clerk: compact header (Sign in button + UserButton). Sign-in opens in a modal
 * so we never remount the full form on every Clerk listener tick (that broke inputs).
 * Dispatches "dialin:auth-ready" when done.
 */
async function initDialinAuth() {
  const root = document.getElementById("clerk-auth-root");
  const legacyRow = document.getElementById("legacy-user-row");
  const pk = (
    (window.DIALIN_CONFIG && window.DIALIN_CONFIG.clerkPublishableKey) ||
    (typeof localStorage !== "undefined" && localStorage.getItem("dialin.clerkPk")) ||
    ""
  ).trim();

  initDevChrome();
  initHeaderMore();
  _wireSignedOutBannerOnce();

  if (!pk) {
    if (legacyRow) legacyRow.style.display = "";
    if (root) root.style.display = "none";
    document.body.classList.remove("signed-out");
    syncAuthUiChrome();
    window.dispatchEvent(new CustomEvent("dialin:auth-ready", { detail: { mode: "legacy" } }));
    return;
  }

  if (legacyRow) legacyRow.style.display = "none";
  let signInBtn = null;
  let userMountEl = null;
  if (root) {
    root.style.display = "flex";
    root.innerHTML =
      "<button type=\"button\" id=\"clerk-sign-in-btn\" class=\"clerk-sign-in-btn\">Sign in</button>" +
      "<div id=\"clerk-user-mount\" class=\"clerk-user-mount\"></div>";
    signInBtn = document.getElementById("clerk-sign-in-btn");
    userMountEl = document.getElementById("clerk-user-mount");
  }

  const authRedirect = () => window.location.href;
  const appearance = dialinClerkAppearance();

  try {
    const mod = await import("https://cdn.jsdelivr.net/npm/@clerk/clerk-js@5.46.0/+esm");
    const Clerk = mod.default || mod.Clerk;
    const clerk = new Clerk(pk);
    await clerk.load({ appearance });
    window.__clerk = clerk;

    let signedIn = !!clerk.user;
    let userButtonMounted = false;

    function notifySessionReady() {
      if (!clerk.session) return;
      const api = document.getElementById("api-base")?.value?.trim();
      if (api) window.dispatchEvent(new CustomEvent("dialin:clerk-session-ready"));
    }

    function unmountUserButton() {
      if (!userButtonMounted || !userMountEl) return;
      try {
        clerk.unmountUserButton(userMountEl);
      } catch (_) {
        /* already unmounted */
      }
      userButtonMounted = false;
    }

    function syncAuthChrome() {
      if (!signInBtn || !userMountEl) return;
      if (clerk.user) {
        signInBtn.hidden = true;
        userMountEl.hidden = false;
        if (!userButtonMounted) {
          clerk.mountUserButton(userMountEl, {
            afterSignOutUrl: authRedirect(),
            appearance,
          });
          userButtonMounted = true;
        }
      } else {
        unmountUserButton();
        userMountEl.hidden = true;
        signInBtn.hidden = false;
      }
    }

    if (signInBtn) {
      signInBtn.addEventListener("click", () => openDialinSignIn());
    }

    function onClerkResourceUpdate() {
      const nowSignedIn = !!clerk.user;
      if (nowSignedIn !== signedIn) {
        const wasSignedIn = signedIn;
        signedIn = nowSignedIn;
        syncAuthChrome();
        if (nowSignedIn && !wasSignedIn) {
          window.dispatchEvent(new CustomEvent("dialin:signed-in"));
        } else if (!nowSignedIn && wasSignedIn) {
          window.dispatchEvent(new CustomEvent("dialin:signed-out"));
        }
        syncAuthUiChrome();
      }
      notifySessionReady();
    }

    syncAuthChrome();
    notifySessionReady();
    clerk.addListener(onClerkResourceUpdate);
    syncAuthUiChrome();

    window.dispatchEvent(
      new CustomEvent("dialin:auth-ready", { detail: { mode: "clerk", signedIn: !!clerk.user } })
    );
  } catch (e) {
    console.error(e);
    toast("Clerk failed to load — check publishable key", true);
    if (legacyRow) legacyRow.style.display = "";
    if (root) root.style.display = "none";
    initDevChrome({ forceLegacy: true });
    document.body.classList.remove("signed-out");
    syncAuthUiChrome();
    window.dispatchEvent(new CustomEvent("dialin:auth-ready", { detail: { mode: "legacy", error: String(e) } }));
  }
}

function escapeHtml(str) {
  return String(str ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/**
 * Render a bot reply with lightweight markdown into `parent`.
 * Handles: **bold**, *italic*, `code`, paragraphs (blank lines), line breaks.
 * Builds DOM nodes — never sets innerHTML — so it is XSS-safe.
 */
function renderMarkdown(parent, text) {
  const s = (text ?? "").replace(/\r\n/g, "\n");
  const paragraphs = s.split(/\n{2,}/);
  paragraphs.forEach((para, pi) => {
    const p = document.createElement("p");
    const lines = para.split("\n");
    lines.forEach((line, li) => {
      if (li > 0) p.appendChild(document.createElement("br"));
      _appendInline(p, line);
    });
    parent.appendChild(p);
  });
}

function _appendInline(parent, text) {
  // Match **bold**, *italic*, `code` — in that order of precedence
  const re = /\*\*([^*\n]+)\*\*|\*([^*\n]+)\*|`([^`\n]+)`/g;
  let last = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
    let el;
    if      (m[1] != null) { el = document.createElement("strong"); el.textContent = m[1]; }
    else if (m[2] != null) { el = document.createElement("em");     el.textContent = m[2]; }
    else                   { el = document.createElement("code");   el.textContent = m[3]; }
    parent.appendChild(el);
    last = m.index + m[0].length;
  }
  if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
}

const _DATE_ONLY_ISO = /^\d{4}-\d{2}-\d{2}$/;

/**
 * `YYYY-MM-DD` is a calendar date; ES `Date` parses it as UTC midnight, which renders as the prior
 * calendar day in US timezones. Full ISO timestamps parse as documented.
 */
function _parseCalendarOrInstant(iso) {
  const s = String(iso ?? "").trim();
  if (_DATE_ONLY_ISO.test(s)) {
    const y = Number(s.slice(0, 4));
    const mo = Number(s.slice(5, 7));
    const d = Number(s.slice(8, 10));
    return new Date(y, mo - 1, d);
  }
  return new Date(iso);
}

/** Today's date in local time as YYYY-MM-DD (for `<input type="date">`). */
function todayLocalYYYYMMDD() {
  const n = new Date();
  const y = n.getFullYear();
  const m = String(n.getMonth() + 1).padStart(2, "0");
  const day = String(n.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function fmtDate(iso) {
  if (!iso) return "";
  try {
    const d = _parseCalendarOrInstant(iso);
    if (Number.isNaN(d.getTime())) return String(iso).slice(0, 10);
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  } catch {
    return String(iso).slice(0, 10);
  }
}

function fmtTime(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  } catch { return iso; }
}

function daysSince(dateStr) {
  if (!dateStr) return null;
  const d = _parseCalendarOrInstant(dateStr);
  if (Number.isNaN(d.getTime())) return null;
  const today = new Date();
  const start = Date.UTC(d.getFullYear(), d.getMonth(), d.getDate());
  const now = Date.UTC(today.getFullYear(), today.getMonth(), today.getDate());
  return Math.floor((now - start) / 86400000);
}

/* Sync api-base + user-id inputs with localStorage; boot Clerk when configured. */
function initSharedInputs(onApiChange, onUserChange) {
  const apiInput  = document.getElementById("api-base");
  const userInput = document.getElementById("user-id");

  if (typeof window.DIALIN_CONFIG === "undefined") window.DIALIN_CONFIG = {};
  const savedPk = localStorage.getItem("dialin.clerkPk");
  if (savedPk && !window.DIALIN_CONFIG.clerkPublishableKey) {
    window.DIALIN_CONFIG.clerkPublishableKey = savedPk.trim();
  }

  const cfgApi = (window.DIALIN_CONFIG.apiBase || "").trim();
  apiInput.value = localStorage.getItem("dialin.apiBase") || cfgApi || "";
  if (userInput) {
    userInput.value = localStorage.getItem("dialin.userId") || "jarrod";
    userInput.addEventListener("input", () => localStorage.setItem("dialin.userId", userInput.value.trim()));
    userInput.addEventListener("change", () => { if (onUserChange) onUserChange(); });
  }

  apiInput.addEventListener("input", () => localStorage.setItem("dialin.apiBase", apiInput.value.trim()));

  /** Debounced: load data once API URL is set and (legacy OR Clerk has a session). */
  let _dataLoadTimer = null;
  function scheduleDataLoad() {
    clearTimeout(_dataLoadTimer);
    _dataLoadTimer = setTimeout(() => {
      _dataLoadTimer = null;
      if (!apiInput.value.trim()) return;
      if (clerkConfigured() && !window.__clerk?.session) return;
      if (onApiChange) onApiChange();
    }, 120);
  }

  /** Refresh panels for signed-out / missing-API states (no journal fetches). */
  function scheduleAuthIdle() {
    clearTimeout(_dataLoadTimer);
    _dataLoadTimer = setTimeout(() => {
      _dataLoadTimer = null;
      if (onApiChange) onApiChange();
    }, 40);
  }

  apiInput.addEventListener("change", scheduleDataLoad);

  window.addEventListener("dialin:signed-in", () => {
    syncAuthUiChrome();
    scheduleDataLoad();
  });
  window.addEventListener("dialin:clerk-session-ready", scheduleDataLoad);
  window.addEventListener("dialin:signed-out", () => {
    syncAuthUiChrome();
    scheduleAuthIdle();
  });
  window.addEventListener("dialin:auth-ready", (e) => {
    syncAuthUiChrome();
    if (e.detail?.mode === "clerk" && e.detail?.signedIn) scheduleDataLoad();
    else if (e.detail?.mode === "clerk") scheduleAuthIdle();
  });

  initDialinAuth().then(() => {
    if (!clerkConfigured()) {
      scheduleDataLoad();
      return;
    }
    if (!window.__clerk?.session) {
      console.info(
        "[dialin] Clerk: no session yet — journal API calls are skipped until you sign in (then you will see GET /coffees etc. in Network)."
      );
      scheduleAuthIdle();
      return;
    }
    scheduleDataLoad();
  });
}

/* Modal open/close helpers */
function openModal(el)  { el.classList.add("open"); }
function closeModal(el) { el.classList.remove("open"); }

/* Wire all [data-close] buttons and modal-bg background clicks.
 * onClose(bg) is called with the modal element that was closed.
 */
function initModalClose(onClose) {
  document.querySelectorAll("[data-close]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const bg = btn.closest(".modal-bg");
      closeModal(bg);
      if (onClose) onClose(bg);
    });
  });
  document.querySelectorAll(".modal-bg").forEach((bg) => {
    bg.addEventListener("click", (e) => {
      if (e.target !== bg) return;
      closeModal(bg);
      if (onClose) onClose(bg);
    });
  });
}

/* Preferences chip + modal — shared between both pages */
function initPrefsChip(onLoad) {
  const chip     = document.getElementById("prefs-chip");
  const chipText = document.getElementById("prefs-chip-text");
  const modal    = document.getElementById("prefs-modal");
  const form     = document.getElementById("prefs-form");

  /* Load and display preferences */
  async function loadPreferences() {
    if (!document.getElementById("api-base").value.trim()) return;
    if (needsSignIn()) {
      if (chipText) chipText.textContent = "Sign in to set preferences →";
      return;
    }
    try {
      const data = await api(withLegacyUserQuery("/profile"));
      const p = data.profile || {};
      const parts = [];
      if (p.preferredRoastLevel)   parts.push(p.preferredRoastLevel + " roasts");
      if (p.experimentalPreference === "seek") parts.push("seeks experimental lots");
      else if (p.experimentalPreference === "open") parts.push("open to experimental lots");
      if (p.discoveryChannels?.length) parts.push(p.discoveryChannels.slice(0, 2).join(", "));
      if (p.preferredOrigins?.length) parts.push(p.preferredOrigins.join(", "));
      if (p.homeCity)              parts.push("from " + p.homeCity);
      chipText.textContent = parts.length ? parts.join(" · ") : "Set your taste preferences →";
      if (onLoad) onLoad(p);
      return p;
    } catch { return {}; }
  }

  /* Chip-input group helpers */
  function getChips(field) {
    return [...document.querySelectorAll(`.chip-input-group[data-field="${field}"] .chip`)]
      .map(c => c.dataset.value).filter(Boolean);
  }
  function addChip(groupEl, value) {
    if (!value.trim()) return;
    const chip = document.createElement("span");
    chip.className = "chip"; chip.dataset.value = value.trim();
    chip.innerHTML = `${escapeHtml(value.trim())} <button type="button" aria-label="remove">×</button>`;
    chip.querySelector("button").addEventListener("click", () => chip.remove());
    groupEl.insertBefore(chip, groupEl.querySelector(".chip-input"));
  }
  function populateChips(field, values) {
    const group = document.querySelector(`.chip-input-group[data-field="${field}"]`);
    if (!group) return;
    group.querySelectorAll(".chip").forEach(c => c.remove());
    (values || []).forEach(v => addChip(group, v));
  }

  document.querySelectorAll(".chip-input").forEach((input) => {
    input.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== ",") return;
      e.preventDefault();
      addChip(input.closest(".chip-input-group"), input.value);
      input.value = "";
    });
  });

  chip.addEventListener("click", async () => {
    const p = await loadPreferences();
    form.elements.homeCity.value = p.homeCity || "";
    form.elements.preferredRoastLevel.value = p.preferredRoastLevel || "";
    if (form.elements.experimentalPreference) form.elements.experimentalPreference.value = p.experimentalPreference || "";
    ["discoveryChannels", "preferredOrigins", "preferredProcesses", "favoriteRoasters", "favoriteCafes", "dislikedNotes"].forEach(f => populateChips(f, p[f]));
    form.elements.notes.value = p.notes || "";
    openModal(modal);
  });

  document.getElementById("prefs-submit").addEventListener("click", async () => {
    const fd = new FormData(form);
    const body = {};
    const chipFields = ["discoveryChannels", "preferredOrigins", "preferredProcesses", "favoriteRoasters", "favoriteCafes", "dislikedNotes"];
    chipFields.forEach(f => { body[f] = getChips(f); });
    if (fd.get("homeCity"))             body.homeCity             = fd.get("homeCity").trim();
    if (fd.get("preferredRoastLevel"))  body.preferredRoastLevel  = fd.get("preferredRoastLevel");
    if (fd.get("notes"))                body.notes                = fd.get("notes").trim();
    if (form.elements.experimentalPreference) body.experimentalPreference = (fd.get("experimentalPreference") || "").trim();
    try {
      await api("/profile", { method: "PATCH", body: authedJsonBody(body) });
      toast("Preferences saved");
      closeModal(modal);
      await loadPreferences();
    } catch (err) { toast(err.message, true); }
  });

  return loadPreferences;
}

/* ── coffee glossary modal (GET /glossary) ─────────────────────────────── */

let _glossaryJsonCache = null;
let _glossaryEntriesCached = null;

async function fetchGlossaryJson() {
  if (_glossaryJsonCache) return _glossaryJsonCache;
  const data = await api("/glossary", { method: "GET" });
  _glossaryJsonCache = data;
  _glossaryEntriesCached = Array.isArray(data.entries) ? data.entries : [];
  return data;
}

function ensureGlossaryModal() {
  let modal = document.getElementById("glossary-modal");
  if (modal) return modal;
  modal = document.createElement("div");
  modal.id = "glossary-modal";
  modal.className = "modal-bg glossary-modal-bg";
  modal.innerHTML = `
    <div class="modal glossary-modal" role="dialog" aria-labelledby="glossary-title">
      <h3 id="glossary-title">Coffee terms</h3>
      <p class="glossary-hint">Curated drinks, regions, and gear — same list the bot uses. <small>For threads and shop specifics, ask in chat (incl. Reddit search).</small></p>
      <input type="search" id="glossary-search" placeholder="Filter… e.g. WDT, flat white, kopitiam" autocomplete="off" />
      <div id="glossary-list" class="glossary-list"></div>
      <div class="modal-actions"><button type="button" data-glossary-close>Close</button></div>
    </div>`;
  document.body.appendChild(modal);
  const inner = modal.querySelector(".glossary-modal");
  inner.addEventListener("click", (e) => e.stopPropagation());
  modal.addEventListener("click", () => closeModal(modal));
  modal.querySelector("[data-glossary-close]").addEventListener("click", () => closeModal(modal));
  return modal;
}

function renderGlossaryList(entries, q) {
  const list = document.getElementById("glossary-list");
  const needle = (q || "").toLowerCase().trim();
  const filtered = !needle
    ? entries
    : entries.filter((ent) => {
        const aliasStr = (ent.aliases || []).join(" ");
        const seeStr = (ent.seeAlso || []).join(" ");
        const hay = `${ent.title || ""} ${ent.body || ""} ${aliasStr} ${seeStr}`.toLowerCase();
        return hay.includes(needle);
      });
  list.innerHTML = "";
  if (!filtered.length) {
    list.innerHTML = '<div class="glossary-empty">No matches.</div>';
    return;
  }
  const frag = document.createDocumentFragment();
  filtered.forEach((ent) => {
    const det = document.createElement("details");
    det.className = "glossary-entry";
    const sum = document.createElement("summary");
    sum.textContent = ent.title || "";
    det.appendChild(sum);
    const body = document.createElement("div");
    body.className = "glossary-body";
    body.textContent = ent.body || "";
    det.appendChild(body);
    if (ent.seeAlso && ent.seeAlso.length) {
      const see = document.createElement("div");
      see.className = "glossary-see";
      see.textContent = `See also: ${ent.seeAlso.join(", ")}`;
      det.appendChild(see);
    }
    frag.appendChild(det);
  });
  list.appendChild(frag);
}

async function openCoffeeGlossary() {
  const modal = ensureGlossaryModal();
  const listEl = document.getElementById("glossary-list");
  const searchEl = document.getElementById("glossary-search");
  openModal(modal);
  if (!_glossaryEntriesCached) {
    listEl.innerHTML = '<div class="glossary-loading">Loading…</div>';
    try {
      await fetchGlossaryJson();
    } catch (err) {
      listEl.innerHTML = `<div class="glossary-empty error">${escapeHtml(err.message || "Failed to load")}</div>`;
      return;
    }
  }
  if (!searchEl.dataset.wired) {
    searchEl.dataset.wired = "1";
    searchEl.addEventListener("input", () => renderGlossaryList(_glossaryEntriesCached, searchEl.value));
  }
  renderGlossaryList(_glossaryEntriesCached, searchEl.value);
}

/** Wire header button #glossary-btn to open the glossary modal. */
function initCoffeeGlossary(buttonId) {
  const btn = document.getElementById(buttonId || "glossary-btn");
  if (!btn) return;
  btn.addEventListener("click", () => { openCoffeeGlossary().catch((e) => toast(e.message, true)); });
}

// ---------------------------------------------------------------------------
// Chat feedback ("that wasn't quite right")
// ---------------------------------------------------------------------------

/**
 * Submit negative feedback for a bot response. Returns the created feedback object.
 * @param {string} userMessage - The user's message that prompted the response
 * @param {string} botMessage - The bot's response text
 * @param {string|null} comment - Optional freeform comment explaining what was wrong
 */
async function submitChatFeedback(userMessage, botMessage, comment) {
  return api("/chat/feedback", {
    method: "POST",
    body: authedJsonBody({ userMessage, botMessage, comment: comment || null }),
  });
}

/**
 * Attach feedback controls to a bot bubble element.
 * @param {HTMLElement} bubbleEl - The .bubble.bot element
 * @param {string} userMessage - The preceding user message text
 * @param {string} botMessage - The bot reply text
 */
function attachFeedbackControls(bubbleEl, userMessage, botMessage) {
  const wrap = document.createElement("div");
  wrap.className = "feedback-controls";
  const btn = document.createElement("button");
  btn.className = "feedback-btn";
  btn.textContent = "That wasn\u2019t quite right";
  btn.type = "button";
  wrap.appendChild(btn);

  btn.addEventListener("click", () => {
    if (wrap.querySelector(".feedback-form")) return;
    btn.style.display = "none";
    const form = document.createElement("div");
    form.className = "feedback-form";
    form.innerHTML =
      '<textarea class="feedback-comment" placeholder="What was off? (optional)" rows="2"></textarea>' +
      '<div class="feedback-actions">' +
      '<button type="button" class="feedback-submit">Submit</button>' +
      '<button type="button" class="feedback-cancel">Cancel</button>' +
      "</div>";
    wrap.appendChild(form);
    const textarea = form.querySelector(".feedback-comment");
    const submitBtn = form.querySelector(".feedback-submit");
    const cancelBtn = form.querySelector(".feedback-cancel");

    cancelBtn.addEventListener("click", () => { form.remove(); btn.style.display = ""; });
    submitBtn.addEventListener("click", async () => {
      submitBtn.disabled = true;
      submitBtn.textContent = "Sending…";
      try {
        await submitChatFeedback(userMessage, botMessage, textarea.value.trim());
        form.remove();
        btn.remove();
        const thanks = document.createElement("span");
        thanks.className = "feedback-thanks";
        thanks.textContent = "Thanks for the feedback";
        wrap.appendChild(thanks);
      } catch (err) {
        submitBtn.disabled = false;
        submitBtn.textContent = "Submit";
        toast("Could not send feedback: " + err.message, true);
      }
    });
  });

  bubbleEl.appendChild(wrap);
}
