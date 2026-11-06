import { api, enc, html, mount, toast } from "../lib.js";

function typeOf(schema) {
  if (!schema) return "any";
  if (schema.type) return schema.type === "array" ? `${typeOf(schema.items)}[]` : schema.type;
  if (schema.enum) return schema.enum.map((v) => JSON.stringify(v)).join(" | ");
  if (schema.anyOf) return schema.anyOf.map(typeOf).filter((t) => t !== "null").join(" | ") + (schema.anyOf.some((s) => s.type === "null") ? "?" : "");
  return "any";
}

function paramsTable(parameters) {
  const props = parameters?.properties || {};
  const required = new Set(parameters?.required || []);
  const names = Object.keys(props);
  if (!names.length) return html`<span class="none">No parameters</span>`;
  return html`<table><thead><tr><th>Parameter</th><th>Type</th><th>Description</th></tr></thead><tbody>
    ${names.map((n) => html`<tr>
      <td class="mono">${n}${required.has(n) ? html`<span title="required" style="color:var(--danger)">*</span>` : ""}</td>
      <td class="mono">${typeOf(props[n])}</td>
      <td>${props[n].description || ""}${props[n].default !== undefined && props[n].default !== null ? html` <span class="none">(default ${JSON.stringify(props[n].default)})</span>` : ""}</td>
    </tr>`)}
  </tbody></table>`;
}

export async function toolsView(root, _params, query) {
  const tools = await api("/tools");
  let filter = query.get("kind") || "all";
  let search = "";

  const card = (t) => html`
    <article class="card" data-name="${t.name.toLowerCase()} ${t.description.toLowerCase()}" data-kind="${t.kind}">
      <div class="card-head">
        <div class="row" style="gap:8px;min-width:0"><h3>${t.name}</h3><span class="badge ${t.kind}">${t.kind}</span></div>
        ${t.kind === "http" ? html`<button class="btn small ghost danger" data-delete="${t.name}" aria-label="Delete ${t.name}">Delete</button>` : ""}
      </div>
      <p>${t.description}</p>
      ${t.http ? html`<div class="mono" style="word-break:break-all"><strong>${t.http.method}</strong> ${t.http.url}</div>
        ${Object.keys(t.http.headers).length ? html`<div class="chips">${Object.keys(t.http.headers).map((h) => html`<span class="chip">${h}: ••••</span>`)}</div>` : ""}` : ""}
      ${paramsTable(t.parameters)}
      <dl class="kv"><dt>Used by</dt><dd>${t.used_by.length
        ? html`<div class="chips">${t.used_by.map((n) => html`<a class="chip" href="#/agents/${enc(n)}/edit">${n}</a>`)}</div>`
        : html`<span class="none">No agents</span>`}</dd></dl>
    </article>`;

  mount(root, html`
    <div class="page-head">
      <div><h1>Tools</h1>
        <p>Python tools are defined in code and loaded at startup (<code>TOOL_MODULES</code>). HTTP tools are created here and call an endpoint of yours.</p></div>
      <a class="btn primary" href="#/tools/new">+ New HTTP tool</a>
    </div>
    <div class="toolbar">
      <input type="search" placeholder="Search tools…" aria-label="Search tools">
      <div class="seg" role="group" aria-label="Filter by kind">
        ${["all", "python", "http"].map((k) => html`<button type="button" data-kind="${k}" class="${k === filter ? "on" : ""}">${k === "all" ? "All" : k === "http" ? "HTTP" : "Python"}</button>`)}
      </div>
    </div>
    ${tools.length ? html`<div class="cards" id="tool-cards">${tools.map(card)}</div>` : html`<div class="empty"><h3>No tools</h3></div>`}
    <div class="empty" id="no-match" hidden><h3>No tools match</h3></div>`);

  const apply = () => {
    let shown = 0;
    root.querySelectorAll("#tool-cards .card").forEach((c) => {
      const ok = (filter === "all" || c.dataset.kind === filter) && c.dataset.name.includes(search);
      c.hidden = !ok; shown += ok;
    });
    root.querySelector("#no-match").hidden = shown > 0 || !tools.length;
  };
  root.querySelector("input[type=search]").addEventListener("input", (e) => { search = e.target.value.trim().toLowerCase(); apply(); });
  root.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => {
    filter = b.dataset.kind;
    root.querySelectorAll(".seg button").forEach((x) => x.classList.toggle("on", x === b));
    apply();
  }));
  apply();

  root.querySelectorAll("[data-delete]").forEach((btn) => btn.addEventListener("click", async () => {
    const name = btn.dataset.delete;
    if (!confirm(`Delete HTTP tool "${name}"?`)) return;
    try {
      await api(`/tools/${enc(name)}`, { method: "DELETE" });
      toast(`Deleted ${name}`, "ok");
      toolsView(root, {}, new URLSearchParams({ kind: filter }));
    } catch (err) { toast(err.message, "error"); }
  }));
}
