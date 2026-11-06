import { api, enc, go, html, mount, toast } from "../lib.js";

const TYPES = ["string", "integer", "number", "boolean"];

const paramRow = (p = { name: "", type: "string", description: "", required: true }) => html`
  <div class="line param">
    <input data-f="name" placeholder="name" value="${p.name}" aria-label="Parameter name" autocomplete="off">
    <select data-f="type" aria-label="Parameter type">${TYPES.map((t) => html`<option ${t === p.type ? "selected" : ""}>${t}</option>`)}</select>
    <input data-f="description" placeholder="what the model should pass" value="${p.description}" aria-label="Parameter description">
    <label class="check"><input type="checkbox" data-f="required" ${p.required ? "checked" : ""}> required</label>
    <button type="button" class="btn small ghost danger" data-remove aria-label="Remove parameter">✕</button>
  </div>`;

const headerRow = () => html`
  <div class="line header">
    <input data-f="key" placeholder="Header, e.g. Authorization" aria-label="Header name" autocomplete="off">
    <input data-f="value" type="password" placeholder="value, e.g. Bearer …" aria-label="Header value" autocomplete="off">
    <button type="button" class="btn small ghost danger" data-remove aria-label="Remove header">✕</button>
  </div>`;

// #/tools/new[?agent=X]  (when opened from an agent's form, the new tool gets attached to that agent)
export async function toolFormView(root, _params, query) {
  const agent = query.get("agent");
  const back = agent ? `#/agents/${enc(agent)}/edit` : "#/tools";

  mount(root, html`
    <a class="crumb" href="${back}">← ${agent ? `Back to ${agent}` : "Tools"}</a>
    <div class="page-head"><div>
      <h1>New HTTP tool</h1>
      <p>When an agent uses this tool, the server calls your URL with the arguments the model supplies.
        <code>{param}</code> placeholders in the URL are filled in. The other parameters go in the query string for GET and DELETE, or in a JSON body otherwise.
        ${agent ? html`<br>It will be attached to <strong>${agent}</strong>.` : ""}</p>
    </div></div>
    <form class="form" novalidate>
      <section class="panel">
        <h2>Basics</h2>
        <label class="field">Name
          <input name="name" placeholder="e.g. get_weather" autocomplete="off">
          <small>Letters, digits, - and _ (the model sees this name).</small></label>
        <label class="field">Description
          <textarea name="description" rows="3" placeholder="Get the current weather for a city. Use when the user asks about weather."></textarea>
          <small>The model uses this to decide when to call the tool, so be specific.</small></label>
      </section>
      <section class="panel">
        <h2>Request</h2>
        <div class="row" style="flex-wrap:nowrap">
          <select name="method" style="width:auto" aria-label="HTTP method">${["GET", "POST", "PUT", "PATCH", "DELETE"].map((m) => html`<option>${m}</option>`)}</select>
          <input name="url" placeholder="https://api.example.com/weather/{city}" aria-label="URL">
        </div>
        <div class="grid-2">
          <label class="field">Timeout (seconds) <input name="timeout" type="number" min="1" max="120" value="20"></label>
          <label class="field">Max response characters <input name="max_chars" type="number" min="100" max="100000" value="5000"></label>
        </div>
      </section>
      <section class="panel">
        <div class="row"><h2 style="margin:0">Parameters</h2><span class="spacer"></span>
          <button type="button" class="btn small" id="add-param">+ Add parameter</button></div>
        <div class="rows" id="params">${paramRow()}</div>
      </section>
      <section class="panel">
        <div class="row"><h2 style="margin:0">Headers</h2><span class="spacer"></span>
          <button type="button" class="btn small" id="add-header">+ Add header</button></div>
        <span class="hint">Sent with every request, for example an API key. The values are stored on the server and never shown again.</span>
        <div class="rows" id="headers"></div>
      </section>
      <div id="form-error"></div>
      <div class="form-actions">
        <a class="btn" href="${back}">Cancel</a>
        <button class="btn primary" type="submit">Create tool</button>
      </div>
    </form>`);

  const form = root.querySelector("form");
  const addRow = (container, row) => container.insertAdjacentHTML("beforeend", String(row));
  root.querySelector("#add-param").addEventListener("click", () => addRow(root.querySelector("#params"), paramRow()));
  root.querySelector("#add-header").addEventListener("click", () => addRow(root.querySelector("#headers"), headerRow()));
  form.addEventListener("click", (e) => { if (e.target.closest("[data-remove]")) e.target.closest(".line").remove(); });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = form.elements;
    const val = (line, k) => line.querySelector(`[data-f=${k}]`);
    const parameters = [...root.querySelectorAll("#params .line")]
      .map((l) => ({ name: val(l, "name").value.trim(), type: val(l, "type").value,
                     description: val(l, "description").value.trim(), required: val(l, "required").checked }))
      .filter((p) => p.name);
    const headers = Object.fromEntries([...root.querySelectorAll("#headers .line")]
      .map((l) => [val(l, "key").value.trim(), val(l, "value").value]).filter(([k]) => k));
    const body = {
      name: f.name.value.trim(), description: f.description.value.trim(), method: f.method.value,
      url: f.url.value.trim(), headers, parameters,
      timeout: Number(f.timeout.value) || 20, max_chars: Number(f.max_chars.value) || 5000,
    };
    const errBox = root.querySelector("#form-error");
    const fail = (msg) => { errBox.innerHTML = String(html`<div class="banner error">${msg}</div>`); };
    if (!body.name || !body.description || !body.url) return fail("Name, description and URL are required.");

    const submit = form.querySelector("button[type=submit]");
    submit.disabled = true;
    try {
      await api("/tools", { method: "POST", body });
      if (agent) {
        try {
          await api(`/agents/${enc(agent)}/tools/${enc(body.name)}`, { method: "POST" });
        } catch (err) { toast(`Tool created, but couldn't attach it to ${agent}: ${err.message}`, "error"); }
      }
      toast(agent ? `Created ${body.name} and attached it to ${agent}` : `Created ${body.name}`, "ok");
      go(back);
    } catch (err) {
      fail(err.message);
      submit.disabled = false;
    }
  });
}
