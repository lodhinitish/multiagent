// Shared helpers: safe HTML templating, API client, toasts.

class Raw { constructor(s) { this.s = s; } toString() { return this.s; } }

export const raw = (s) => new Raw(s);

export function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function fmt(v) {
  if (v instanceof Raw) return v.s;
  if (Array.isArray(v)) return v.map(fmt).join("");
  if (v === false || v === null || v === undefined) return "";
  return esc(v);
}

/** Tagged template: every interpolated value is escaped unless wrapped in raw() or produced by html``. */
export function html(strings, ...vals) {
  let out = "";
  strings.forEach((s, i) => { out += s + (i < vals.length ? fmt(vals[i]) : ""); });
  return new Raw(out);
}

export function mount(el, content) { el.innerHTML = String(content); }

function errorText(detail, status) {
  if (Array.isArray(detail)) {
    return detail.map((d) => `${(d.loc || []).filter((x) => x !== "body").join(".") || "request"}: ${d.msg}`).join("; ");
  }
  return typeof detail === "string" ? detail : `Request failed (${status})`;
}

export async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body !== undefined ? { "content-type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (res.status === 401 && !path.startsWith("/auth/login")) {
    toLogin();
    throw new Error("Your session has ended. Please log in again.");
  }
  if (!res.ok) throw new Error(errorText(data?.detail, res.status));
  return data;
}

// ---------- session ----------
let me = null;  // the logged-in user, cached

/** The logged-in user ({username, role}), or a redirect to the login page. */
export async function currentUser() {
  if (!me) me = await api("/auth/me");
  return me;
}
export function setCurrentUser(user) { me = user; window.dispatchEvent(new Event("userchange")); }

/** Go to the login page, coming back to the current page afterwards. */
export function toLogin() {
  me = null;
  const here = location.hash.replace(/^#\/?/, "");
  if (here.startsWith("login")) return;
  location.hash = `#/login${here ? `?next=${encodeURIComponent(here)}` : ""}`;
}

export async function logout() {
  await fetch("/auth/logout", { method: "POST" }).catch(() => {});
  me = null;
  location.hash = "#/login";
}

/** WebSocket close code the server uses for "not logged in". */
export const WS_UNAUTHORIZED = 4401;

export function toast(message, kind = "info", ms = 4200) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  document.getElementById("toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? ms * 1.6 : ms);
}

export function wsUrl(path) {
  return `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${path}`;
}

export const enc = encodeURIComponent;

export function go(hash) { location.hash = hash; }

/** One-line, human description of a streamed trace event. */
export function describeEvent(e) {
  const d = e.data || {};
  const who = e.depth ? `${"  ".repeat(e.depth)}↳ ${e.agent}` : e.agent;
  if (e.event === "tool_call") {
    const sub = d.name?.startsWith("ask_");
    const args = sub ? `"${(d.arguments?.task || "").slice(0, 120)}"` : JSON.stringify(d.arguments ?? {});
    return { text: `${who}: ${sub ? "delegates to " + d.name.slice(4) : "calls " + d.name} ${args}`, error: false };
  }
  if (e.event === "tool_result") {
    const body = String(d.content ?? "").replace(/\s+/g, " ").slice(0, 160);
    return { text: `${who}: ${d.name} → ${body}`, error: !!d.is_error };
  }
  if (e.event === "llm_response") {
    const n = (d.tool_calls || []).length;
    return { text: `${who}: step ${d.step}${n ? ` · ${n} tool call${n > 1 ? "s" : ""}` : " · final answer"}`, error: false };
  }
  return { text: `${who}: ${e.event}`, error: e.event === "error" };
}

export const MODEL_SUGGESTIONS = {
  anthropic: ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5", "claude-sonnet-4-5"],
  openai: ["gpt-5.6-luna", "gpt-5.4-mini", "gpt-5.4"],
};
