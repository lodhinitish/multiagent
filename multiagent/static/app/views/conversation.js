// Message list shared by the text and voice screens.
import { describeEvent, html } from "../lib.js";

export function conversation(container, emptyHint) {
  let pending = null;  // { el, steps: [] } while the agent is working

  const scroll = () => { container.scrollTop = container.scrollHeight; };
  const clearEmpty = () => container.querySelector(".chat-empty")?.remove();
  const append = (content) => {
    clearEmpty();
    container.insertAdjacentHTML("beforeend", String(content));
    scroll();
    return container.lastElementChild;
  };
  const stepsBlock = (steps, open) => {
    if (!steps.length) return "";
    const calls = steps.filter((s) => s.text.includes(": calls ") || s.text.includes(": delegates to ")).length;
    return html`<details class="steps" ${open ? "open" : ""}>
      <summary>${steps.length} step${steps.length > 1 ? "s" : ""}${calls ? ` · ${calls} tool call${calls > 1 ? "s" : ""}` : ""}</summary>
      <ol>${steps.map((s) => html`<li class="${s.error ? "err" : ""}">${s.text}</li>`)}</ol></details>`;
  };

  const api = {
    reset() {
      container.innerHTML = String(html`<div class="chat-empty">${emptyHint}</div>`);
      pending = null;
    },
    user(text, label = "You") {
      const msg = html`<div class="msg you"><div class="bubble">${text}</div><div class="meta">${label}</div></div>`;
      if (pending) {  // voice: the transcript arrives after the "thinking" bubble is shown
        pending.el.insertAdjacentHTML("beforebegin", String(msg));
        scroll();
      } else {
        append(msg);
      }
    },
    startPending() {
      const el = append(html`<div class="msg agent"><div class="bubble typing" aria-label="Agent is working"><span></span><span></span><span></span></div><div class="live"></div></div>`);
      pending = { el, steps: [] };
    },
    event(e) {
      if (!pending) return;
      pending.steps.push(describeEvent(e));
      pending.el.querySelector(".live").innerHTML = String(stepsBlock(pending.steps, true));
      scroll();
    },
    reply(msg, agentName) {
      const steps = pending?.steps || [];
      pending?.el.remove();
      pending = null;
      const usage = msg.usage ? `${msg.usage.input_tokens + msg.usage.output_tokens} tokens` : "";
      const meta = [agentName, msg.stop_reason === "max_steps" ? "stopped at max steps" : "", usage].filter(Boolean).join(" · ");
      return append(html`<div class="msg agent"><div class="bubble">${msg.text || "(empty reply)"}</div>
        <div class="meta">${meta}</div>${stepsBlock(steps, false)}</div>`);
    },
    error(message) {
      pending?.el.remove();
      pending = null;
      append(html`<div class="msg agent error"><div class="bubble">${message}</div></div>`);
    },
    get busy() { return !!pending; },
  };
  api.reset();
  return api;
}

export function agentHeader(agent, mode, extra = "") {
  const enc = encodeURIComponent;
  return html`
    <div class="chat-head">
      <a class="btn small ghost" href="#/agents" aria-label="Back to agents">←</a>
      <div class="title"><strong>${agent.name}</strong>
        <span class="sub">${agent.provider} · ${agent.model}${agent.tools.length ? ` · ${agent.tools.length} tools` : ""}${agent.sub_agents.length ? ` · delegates to ${agent.sub_agents.join(", ")}` : ""}</span></div>
      <span class="conn" id="conn">connecting…</span>
      <span class="spacer"></span>
      ${extra}
      <button class="btn small" id="reset" disabled>New conversation</button>
      <a class="btn small" href="#/agents/${enc(agent.name)}/${mode === "chat" ? "voice" : "chat"}">${mode === "chat" ? "Switch to voice" : "Switch to text"}</a>
      <a class="btn small" href="#/agents/${enc(agent.name)}/edit">Edit</a>
    </div>`;
}

export function setConn(root, state, text) {
  const el = root.querySelector("#conn");
  el.className = `conn ${state}`;
  el.textContent = text;
}
