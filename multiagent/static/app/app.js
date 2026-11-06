// Hash router. Each view is `async (root, params, query) => cleanup?`.
import { agentsView } from "./views/agents.js";
import { agentFormView } from "./views/agentForm.js";
import { toolsView } from "./views/tools.js";
import { toolFormView } from "./views/toolForm.js";
import { chatView } from "./views/chat.js";
import { voiceView } from "./views/voice.js";
import { loginView } from "./views/login.js";
import { accountView } from "./views/account.js";
import { currentUser, html, logout, mount } from "./lib.js";

const routes = [
  ["agents", agentsView],
  ["agents/new", agentFormView],
  ["agents/:name/edit", agentFormView],
  ["agents/:name/chat", chatView],
  ["agents/:name/voice", voiceView],
  ["tools", toolsView],
  ["tools/new", toolFormView],
  ["login", loginView],
  ["account", accountView],
].map(([pattern, view]) => ({ parts: pattern.split("/"), view }));

const root = document.getElementById("view");
let cleanup = null;
let renderId = 0;

function match(path) {
  const segs = path.split("/").filter(Boolean);
  for (const r of routes) {
    if (r.parts.length !== segs.length) continue;
    const params = {};
    if (r.parts.every((p, i) => (p.startsWith(":") ? (params[p.slice(1)] = decodeURIComponent(segs[i]), true) : p === segs[i]))) {
      return { view: r.view, params };
    }
  }
  return null;
}

async function render() {
  const id = ++renderId;
  const [path, qs] = location.hash.replace(/^#\/?/, "").split("?");
  if (!path) { location.replace("#/agents"); return; }
  if (cleanup) { try { cleanup(); } catch { /* ignore */ } cleanup = null; }

  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", path.startsWith(a.dataset.nav)));
  const m = match(path);
  if (!m) {
    mount(root, html`<div class="empty"><h3>Page not found</h3><a class="btn" href="#/agents">Go to agents</a></div>`);
    return;
  }
  mount(root, html`<div class="loading">Loading…</div>`);
  if (path !== "login") {
    try {
      await currentUser();  // not logged in → api() sends us to #/login
    } catch {
      return;
    }
    if (id !== renderId) return;
  }
  try {
    const result = await m.view(root, m.params, new URLSearchParams(qs || ""));
    if (id === renderId) cleanup = result || null; else if (result) result();  // navigated away meanwhile
  } catch (err) {
    if (id === renderId) {
      mount(root, html`<div class="banner error">${err.message}</div>
        <p><a href="#/agents">← Back to agents</a></p>`);
    }
  }
  if (id === renderId) root.focus({ preventScroll: true });
}

// Sidebar: who is logged in
async function showUser() {
  const box = document.getElementById("user-box");
  try {
    const me = await currentUser();
    box.querySelector("#user-name").textContent = me.username;
    box.querySelector("#user-role").textContent = me.role;
    box.hidden = false;
  } catch {
    box.hidden = true;
  }
}
document.getElementById("logout").addEventListener("click", logout);
window.addEventListener("userchange", showUser);
window.addEventListener("hashchange", () => { if (!location.hash.startsWith("#/login")) showUser(); });

window.addEventListener("hashchange", render);
render();
if (!location.hash.startsWith("#/login")) showUser();
