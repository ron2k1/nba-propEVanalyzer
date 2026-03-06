// Shared API utilities and formatters — used by all tab modules

export async function apiGet(path, { timeoutMs = 300_000 } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(path, { cache: "no-store", signal: ctrl.signal });
    return res.json();
  } catch (err) {
    if (err.name === 'AbortError') {
      return { success: false, error: `Request timed out after ${Math.round(timeoutMs / 60000)} min. The task may still be running on the server — check pipeline status before retrying.` };
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

export async function apiPost(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function fmt(value, digits = 2) {
  const num = Number(value);
  return Number.isFinite(num) ? num.toFixed(digits) : "n/a";
}

export function pct(value, digits = 1) {
  const num = Number(value);
  return Number.isFinite(num) ? `${(num * 100).toFixed(digits)}%` : "n/a";
}

export function pctAlready(value, digits = 2) {
  const num = Number(value);
  return Number.isFinite(num) ? `${num.toFixed(digits)}%` : "n/a";
}

export function statusPill(value) {
  const raw = String(value || "pending").toLowerCase();
  const cls = ["win", "loss", "push"].includes(raw) ? raw : "pending";
  return `<span class="status-pill ${cls}">${escapeHtml(raw.toUpperCase())}</span>`;
}

export function normalizeName(name) {
  return String(name || "")
    .toLowerCase()
    .replace(/[^a-z0-9 ]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

export function toUpperTrim(value) {
  return String(value ?? "").trim().toUpperCase();
}

export const PROP_MARKETS_PRESET = [
  "player_points", "player_rebounds", "player_assists",
  "player_threes", "player_blocks", "player_steals",
  "player_turnovers", "player_points_rebounds_assists",
  "player_points_rebounds", "player_points_assists",
  "player_rebounds_assists",
].join(",");

export const DEFAULT_BOOKMAKERS = "betmgm,draftkings,fanduel";

export function showError(el, message, data) {
  if (!el) return;
  const details = data ? `<pre>${escapeHtml(JSON.stringify(data, null, 2))}</pre>` : "";
  el.innerHTML = `<p class="error">${escapeHtml(message)}</p>${details}`;
}
