import { MODEL_SUGGESTIONS, api, enc, go, html, mount, toast } from "../lib.js";

const DEFAULTS = {
  name: "", description: "", system_prompt: "", provider: "anthropic", model: MODEL_SUGGESTIONS.anthropic[0],
  tools: [], sub_agents: [], max_steps: 10, temperature: null, max_tokens: 4096,
};

// Create (#/agents/new[?parent=X]) and edit (#/agents/:name/edit).
export async function agentFormView(root, params, query) {
  const editing = params.name;
  const parent = query.get("parent");
  const [providers, tools, agents, existing] = await Promise.all([
    api("/providers"), api("/tools"), api("/agents"), editing ? api(`/agents/${enc(editing)}`) : null,
  ]);
  const a = existing || { ...DEFAULTS, provider: providers.includes("anthropic") ? "anthropic" : providers[0] };
  // Can't pick yourself, or the parent you're being created under (that would be a cycle).
  const others = agents.filter((x) => x.name !== editing && x.name !== parent);
  const back = parent ? `#/agents/${enc(parent)}/edit` : "#/agents";

  const toolRow = (t) => html`
    <label><input type="checkbox" name="tools" value="${t.name}" ${a.tools.includes(t.name) ? "checked" : ""}>
      <span style="min-width:0"><span class="row" style="gap:6px"><span class="name">${t.name}</span>
        <span class="badge ${t.kind}">${t.kind}</span></span>
        <span class="desc">${t.description}</span></span></label>`;
  const subRow = (s) => html`
    <label><input type="checkbox" name="sub_agents" value="${s.name}" ${a.sub_agents.includes(s.name) ? "checked" : ""}>
      <span style="min-width:0"><span class="row" style="gap:6px"><span class="name">${s.name}</span>
        <span class="badge provider">${s.provider}</span></span>
        <span class="desc">${s.description || "No description. Add one so parent agents know when to delegate here."}</span></span></label>`;

  mount(root, html`
    <a class="crumb" href="${back}">← ${parent ? `Back to ${parent}` : "Agents"}</a>
    <div class="page-head"><div>
      <h1>${editing ? `Edit ${editing}` : parent ? "New sub-agent" : "New agent"}</h1>
      ${parent ? html`<p>When it's saved, this agent is added as a sub-agent of <strong>${parent}</strong>.</p>` : ""}
    </div></div>
    <form class="form" novalidate>
      <section class="panel">
        <h2>Basics</h2>
        <div class="grid-2">
          <label class="field">Name
            <input name="name" required pattern="[a-zA-Z][a-zA-Z0-9_\\-]{0,47}" value="${a.name}" ${editing ? "disabled" : ""} placeholder="e.g. researcher" autocomplete="off">
            <small>Letters, digits, - and _. It can't be changed later.</small>
          </label>
          <label class="field">Description
            <input name="description" value="${a.description}" placeholder="e.g. Fetches and summarises web pages">
            <small>Parent agents read this to decide when to delegate here.</small>
          </label>
        </div>
        <label class="field">System prompt
          <textarea name="system_prompt" rows="6" required placeholder="You are a helpful research assistant…">${a.system_prompt}</textarea>
        </label>
      </section>

      <section class="panel">
        <h2>Model</h2>
        <div class="grid-2">
          <label class="field">Provider
            <select name="provider">${providers.map((p) => html`<option ${p === a.provider ? "selected" : ""}>${p}</option>`)}</select>
          </label>
          <label class="field">Model
            <input name="model" list="model-list" value="${a.model}" required autocomplete="off">
            <datalist id="model-list"></datalist>
          </label>
        </div>
        <div class="grid-3">
          <label class="field">Max steps <input name="max_steps" type="number" min="1" max="100" value="${a.max_steps}"></label>
          <label class="field">Temperature <input name="temperature" type="number" min="0" max="2" step="0.1" value="${a.temperature ?? ""}" placeholder="provider default"></label>
          <label class="field">Max tokens <input name="max_tokens" type="number" min="1" value="${a.max_tokens}"></label>
        </div>
      </section>

      <section class="panel">
        <div class="row"><h2 style="margin:0">Tools</h2><span class="spacer"></span>
          <a class="btn small" href="#/tools/new${editing ? `?agent=${enc(editing)}` : ""}">+ New HTTP tool</a></div>
        ${tools.length ? html`<div class="picker">${tools.map(toolRow)}</div>` : html`<span class="none">No tools registered.</span>`}
      </section>

      <section class="panel">
        <div class="row"><h2 style="margin:0">Sub-agents</h2><span class="spacer"></span>
          ${editing ? html`<a class="btn small" href="#/agents/new?parent=${enc(editing)}">+ Create new sub-agent</a>` : ""}</div>
        <span class="hint">This agent can hand tasks to the agents you tick. Each one appears to it as a tool named <code>ask_&lt;name&gt;</code>.</span>
        ${others.length ? html`<div class="picker">${others.map(subRow)}</div>`
          : html`<span class="none">No other agents yet. Create one first, or save this one and use "+ Sub-agent".</span>`}
      </section>

      <div id="form-error"></div>
      <div class="form-actions">
        <a class="btn" href="${back}">Cancel</a>
        <button class="btn primary" type="submit">${editing ? "Save changes" : "Create agent"}</button>
      </div>
    </form>`);

  const form = root.querySelector("form");
  const providerSel = form.elements.provider;
  const modelInput = form.elements.model;
  const fillModels = () => {
    root.querySelector("#model-list").innerHTML = (MODEL_SUGGESTIONS[providerSel.value] || [])
      .map((m) => `<option value="${m}"></option>`).join("");
  };
  providerSel.addEventListener("change", () => {
    const all = Object.values(MODEL_SUGGESTIONS).flat();
    if (!modelInput.value || all.includes(modelInput.value)) modelInput.value = (MODEL_SUGGESTIONS[providerSel.value] || [""])[0];
    fillModels();
  });
  fillModels();

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = form.elements;
    const checked = (name) => [...form.querySelectorAll(`input[name=${name}]:checked`)].map((i) => i.value);
    const body = {
      description: f.description.value.trim(),
      system_prompt: f.system_prompt.value.trim(),
      provider: f.provider.value,
      model: f.model.value.trim(),
      tools: checked("tools"),
      sub_agents: checked("sub_agents"),
      max_steps: Number(f.max_steps.value) || 10,
      temperature: f.temperature.value === "" ? null : Number(f.temperature.value),
      max_tokens: Number(f.max_tokens.value) || 4096,
    };
    const name = editing || f.name.value.trim();
    const errBox = root.querySelector("#form-error");
    const fail = (msg) => { errBox.innerHTML = String(html`<div class="banner error">${msg}</div>`); };
    if (!name) return fail("Name is required.");
    if (!body.system_prompt) return fail("System prompt is required.");
    if (!body.model) return fail("Model is required.");

    const submit = form.querySelector("button[type=submit]");
    submit.disabled = true;
    try {
      if (editing) {
        await api(`/agents/${enc(name)}`, { method: "PUT", body });
      } else {
        await api("/agents", { method: "POST", body: { name, ...body } });
        if (parent) {
          try {
            await api(`/agents/${enc(parent)}/sub_agents/${enc(name)}`, { method: "POST" });
          } catch (err) {
            toast(`Created ${name}, but couldn't attach it to ${parent}: ${err.message}`, "error");
            go(`#/agents/${enc(name)}/edit`);
            return;
          }
        }
      }
      toast(editing ? `Saved ${name}` : parent ? `Created ${name} as a sub-agent of ${parent}` : `Created ${name}`, "ok");
      go(parent ? `#/agents/${enc(parent)}/edit` : "#/agents");
    } catch (err) {
      fail(err.message);
      submit.disabled = false;
    }
  });
}
