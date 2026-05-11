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

/** Append ?userId= only for legacy mode (no Clerk). */
function withLegacyUserQuery(path, extraQuery = "") {
  if (clerkConfigured()) {
    return extraQuery ? `${path}?${extraQuery}` : path;
  }
  const uid = `userId=${encodeURIComponent(userId())}`;
  if (!extraQuery) return path.includes("?") ? `${path}&${uid}` : `${path}?${uid}`;
  return path.includes("?") ? `${path}&${uid}&${extraQuery}` : `${path}?${uid}&${extraQuery}`;
}

/** JSON body: legacy mode adds userId; Clerk mode relies on Authorization only. */
function authedJsonBody(obj) {
  if (clerkConfigured()) return JSON.stringify(obj);
  return JSON.stringify({ ...obj, userId: userId() });
}

function apiBase() {
  const v = (document.getElementById("api-base")?.value || "").trim().replace(/\/$/, "");
  if (!v) { toast("Set the API URL first", true); throw new Error("no api base"); }
  return v;
}

async function _authHeaders() {
  const h = {};
  if (!clerkConfigured() || !window.__clerk?.session) return h;
  try {
    const t = await window.__clerk.session.getToken();
    if (t) h.authorization = `Bearer ${t}`;
  } catch (_) { /* session not ready */ }
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
    const err = new Error(data?.error || data?.message || res.statusText);
    err.status = res.status;
    err.body = data;
    throw err;
  }
  return data;
}

/**
 * Load Clerk, mount sign-in / user button into #clerk-auth-root, hide #legacy-user-row when configured.
 * Dispatches window event "dialin:auth-ready" when done (signed in or legacy path).
 */
async function initDialinAuth() {
  const root = document.getElementById("clerk-auth-root");
  const legacyRow = document.getElementById("legacy-user-row");
  const pk = (
    (window.DIALIN_CONFIG && window.DIALIN_CONFIG.clerkPublishableKey) ||
    (typeof localStorage !== "undefined" && localStorage.getItem("dialin.clerkPk")) ||
    ""
  ).trim();

  if (!pk) {
    if (legacyRow) legacyRow.style.display = "";
    if (root) root.style.display = "none";
    window.dispatchEvent(new CustomEvent("dialin:auth-ready", { detail: { mode: "legacy" } }));
    return;
  }

  if (legacyRow) legacyRow.style.display = "none";
  if (root) {
    root.style.display = "flex";
    root.innerHTML = "<div id=\"clerk-mount\" class=\"clerk-mount\"></div>";
  }

  try {
    const mod = await import("https://esm.sh/@clerk/clerk-js@5.46.0");
    const Clerk = mod.default || mod.Clerk;
    const clerk = new Clerk(pk);
    await clerk.load();
    window.__clerk = clerk;

    const mountEl = document.getElementById("clerk-mount");
    function renderAuth() {
      if (!mountEl) return;
      mountEl.innerHTML = "";
      if (clerk.user) {
        clerk.mountUserButton(mountEl);
      } else {
        clerk.mountSignIn(mountEl, { routing: "hash" });
      }
    }
    renderAuth();
    clerk.addListener(renderAuth);

    window.dispatchEvent(new CustomEvent("dialin:auth-ready", { detail: { mode: "clerk" } }));
  } catch (e) {
    console.error(e);
    toast("Clerk failed to load — check publishable key", true);
    if (legacyRow) legacyRow.style.display = "";
    if (root) root.style.display = "none";
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

function fmtDate(iso) {
  if (!iso) return "";
  try { return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric" }); }
  catch { return iso.slice(0, 10); }
}

function fmtTime(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  } catch { return iso; }
}

function daysSince(dateStr) {
  if (!dateStr) return null;
  return Math.floor((Date.now() - new Date(dateStr).getTime()) / 86400000);
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

  apiInput.value = localStorage.getItem("dialin.apiBase") || "";
  if (userInput) {
    userInput.value = localStorage.getItem("dialin.userId") || "jarrod";
    userInput.addEventListener("input", () => localStorage.setItem("dialin.userId", userInput.value.trim()));
    userInput.addEventListener("change", () => { if (onUserChange) onUserChange(); });
  }

  apiInput.addEventListener("input", () => localStorage.setItem("dialin.apiBase", apiInput.value.trim()));
  apiInput.addEventListener("change", () => { if (onApiChange) onApiChange(); });

  initDialinAuth().then(() => {
    if (onApiChange) onApiChange();
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
    try {
      const data = await api(withLegacyUserQuery("/profile"));
      const p = data.profile || {};
      const parts = [];
      if (p.preferredRoastLevel)   parts.push(p.preferredRoastLevel);
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
    ["preferredOrigins", "preferredProcesses", "favoriteRoasters", "favoriteCafes", "dislikedNotes"].forEach(f => populateChips(f, p[f]));
    form.elements.notes.value = p.notes || "";
    openModal(modal);
  });

  document.getElementById("prefs-submit").addEventListener("click", async () => {
    const fd = new FormData(form);
    const body = {};
    const chipFields = ["preferredOrigins", "preferredProcesses", "favoriteRoasters", "favoriteCafes", "dislikedNotes"];
    chipFields.forEach(f => { body[f] = getChips(f); });
    if (fd.get("homeCity"))             body.homeCity             = fd.get("homeCity").trim();
    if (fd.get("preferredRoastLevel"))  body.preferredRoastLevel  = fd.get("preferredRoastLevel");
    if (fd.get("notes"))                body.notes                = fd.get("notes").trim();
    try {
      await api("/profile", { method: "PATCH", body: authedJsonBody(body) });
      toast("Preferences saved");
      closeModal(modal);
      await loadPreferences();
    } catch (err) { toast(err.message, true); }
  });

  return loadPreferences;
}
