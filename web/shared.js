/* dialin — shared JS utilities
 * Expects DOM elements: #api-base, #user-id, #toast
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

function userId() {
  const v = (document.getElementById("user-id")?.value || "").trim();
  if (!v) { toast("Set a user id first", true); throw new Error("no user id"); }
  return v;
}

function apiBase() {
  const v = (document.getElementById("api-base")?.value || "").trim().replace(/\/$/, "");
  if (!v) { toast("Set the API URL first", true); throw new Error("no api base"); }
  return v;
}

async function api(path, opts = {}) {
  const url = apiBase() + path;
  const res = await fetch(url, {
    ...opts,
    headers: { "content-type": "application/json", ...(opts.headers || {}) },
  });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
  if (!res.ok) throw new Error(data?.error || res.statusText);
  return data;
}

function escapeHtml(str) {
  return String(str ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
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

/* Sync api-base + user-id inputs with localStorage and notify on change */
function initSharedInputs(onApiChange, onUserChange) {
  const apiInput  = document.getElementById("api-base");
  const userInput = document.getElementById("user-id");

  apiInput.value  = localStorage.getItem("dialin.apiBase") || "";
  userInput.value = localStorage.getItem("dialin.userId")  || "jarrod";

  apiInput.addEventListener("input", () => localStorage.setItem("dialin.apiBase", apiInput.value.trim()));
  apiInput.addEventListener("change", () => { if (onApiChange) onApiChange(); });

  userInput.addEventListener("input", () => localStorage.setItem("dialin.userId", userInput.value.trim()));
  userInput.addEventListener("change", () => { if (onUserChange) onUserChange(); });
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
      const data = await api(`/profile?userId=${encodeURIComponent(userId())}`);
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
    const body = { userId: userId() };
    const chipFields = ["preferredOrigins", "preferredProcesses", "favoriteRoasters", "favoriteCafes", "dislikedNotes"];
    chipFields.forEach(f => { body[f] = getChips(f); });
    if (fd.get("homeCity"))             body.homeCity             = fd.get("homeCity").trim();
    if (fd.get("preferredRoastLevel"))  body.preferredRoastLevel  = fd.get("preferredRoastLevel");
    if (fd.get("notes"))                body.notes                = fd.get("notes").trim();
    try {
      await api("/profile", { method: "PATCH", body: JSON.stringify(body) });
      toast("Preferences saved");
      closeModal(modal);
      await loadPreferences();
    } catch (err) { toast(err.message, true); }
  });

  return loadPreferences;
}
