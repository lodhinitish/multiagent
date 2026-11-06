import { api, enc, html, mount, toast } from "../lib.js";

export async function agentsView(root) {
  const agents = await api("/agents");
  const parentsOf = (name) => agents.filter((a) => a.sub_agents.includes(name)).map((a) => a.name);

  const chips = (items, hrefFn) => items.length
    ? html`<div class="chips">${items.map((n) => hrefFn ? html`<a class="chip" href="${hrefFn(n)}">${n}</a>` : html`<span class="chip">${n}</span>`)}</div>`
    : html`<span class="none">None</span>`;

  const card = (a) => {
    const parents = parentsOf(a.name);
    return html`
    <article class="card">
      <div class="card-head">
        <div style="min-width:0">
          <h3>${a.name}</h3>
          <div class="row" style="margin-top:6px;gap:6px">
            <span class="badge provider">${a.provider}</span>
            <span class="mono" style="color:var(--muted)">${a.model}</span>
          </div>
        </div>
        <button class="btn small ghost danger" data-delete="${a.name}" title="Delete ${a.name}" aria-label="Delete ${a.name}">Delete</button>
      </div>
      ${a.description ? html`<p>${a.description}</p>` : ""}
      <dl class="kv">
        <dt>Tools</dt><dd>${chips(a.tools)}</dd>
        <dt>Sub-agents</dt><dd>${chips(a.sub_agents, (n) => `#/agents/${enc(n)}/edit`)}</dd>
        ${parents.length ? html`<dt>Used by</dt><dd>${chips(parents, (n) => `#/agents/${enc(n)}/edit`)}</dd>` : ""}
      </dl>
      <div class="actions">
        <a class="btn small primary" href="#/agents/${enc(a.name)}/chat">Chat</a>
        <a class="btn small primary" href="#/agents/${enc(a.name)}/voice">Voice</a>
        <a class="btn small" href="#/agents/${enc(a.name)}/edit">Edit</a>
        <a class="btn small" href="#/agents/new?parent=${enc(a.name)}">+ Sub-agent</a>
      </div>
    </article>`;
  };

  mount(root, html`
    <div class="page-head">
      <div>
        <h1>Agents</h1>
        <p>${agents.length} agent${agents.length === 1 ? "" : "s"}. Each one has its own prompt, model, tools and sub-agents it can delegate to.</p>
      </div>
      <a class="btn primary" href="#/agents/new">+ New agent</a>
    </div>
    ${agents.length
      ? html`<div class="cards">${agents.map(card)}</div>`
      : html`<div class="empty"><h3>No agents yet</h3><div>Create an agent, give it tools, then chat with it by text or voice.</div>
          <a class="btn primary" href="#/agents/new">+ Create your first agent</a></div>`}
  `);

  root.querySelectorAll("[data-delete]").forEach((btn) => btn.addEventListener("click", async () => {
    const name = btn.dataset.delete;
    if (!confirm(`Delete agent "${name}"? This can't be undone.`)) return;
    try {
      await api(`/agents/${enc(name)}`, { method: "DELETE" });
      toast(`Deleted ${name}`, "ok");
      agentsView(root);
    } catch (err) { toast(err.message, "error"); }
  }));
}
