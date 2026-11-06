import { api, enc, html, mount, wsUrl, toLogin, WS_UNAUTHORIZED } from "../lib.js";
import { agentHeader, conversation, setConn } from "./conversation.js";

// #/agents/:name/chat — text to text over /ws/agents/{name}/chat
export async function chatView(root, { name }) {
  const agent = await api(`/agents/${enc(name)}`);
  mount(root, html`
    <div class="chat">
      ${agentHeader(agent, "chat")}
      <div class="messages" id="messages"></div>
      <form class="composer">
        <textarea id="input" rows="1" placeholder="Message ${agent.name}…  (Enter to send, Shift+Enter for a new line)" aria-label="Message" disabled></textarea>
        <button class="btn primary" type="submit" id="send" disabled>Send</button>
      </form>
    </div>`);

  const convo = conversation(root.querySelector("#messages"), `Start a conversation with ${agent.name}. The agent remembers earlier messages until you start a new conversation.`);
  const input = root.querySelector("#input");
  const send = root.querySelector("#send");
  const resetBtn = root.querySelector("#reset");
  let ws = null;
  let closedByUs = false;

  const setReady = (ready) => {
    input.disabled = !ready;
    send.disabled = !ready || convo.busy;
    resetBtn.disabled = !ready || convo.busy;
  };

  function connect() {
    setConn(root, "", "connecting…");
    const sock = new WebSocket(wsUrl(`/ws/agents/${enc(name)}/chat?trace=true`));
    ws = sock;
    sock.onmessage = (e) => {
      if (sock !== ws) return;
      const m = JSON.parse(e.data);
      if (m.type === "ready") { setConn(root, "on", "connected"); setReady(true); input.focus(); }
      else if (m.type === "event") convo.event(m);
      else if (m.type === "response") { convo.reply(m, agent.name); setReady(true); input.focus(); }
      else if (m.type === "error") { convo.error(m.message); setReady(ws.readyState === WebSocket.OPEN); }
      else if (m.type === "reset_ok") convo.reset();
    };
    sock.onclose = (ev) => {
      if (sock !== ws) return;
      if (ev.code === WS_UNAUTHORIZED) { toLogin(); return; }
      setReady(false);
      if (closedByUs) return;
      if (convo.busy) convo.error("Connection lost while the agent was working.");
      setConn(root, "off", "disconnected");
      if (root.querySelector("#reconnect")) return;
      root.querySelector("#conn").insertAdjacentHTML("afterend", '<button class="btn small" id="reconnect">Reconnect</button>');
      root.querySelector("#reconnect").onclick = (ev) => { ev.target.remove(); connect(); };
    };
  }

  function submit() {
    const text = input.value.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN || convo.busy) return;
    convo.user(text);
    convo.startPending();
    ws.send(JSON.stringify({ type: "message", text }));
    input.value = "";
    autosize();
    setReady(true);
  }

  const autosize = () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 180) + "px"; };
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
  });
  root.querySelector(".composer").addEventListener("submit", (e) => { e.preventDefault(); submit(); });
  resetBtn.addEventListener("click", () => ws?.send(JSON.stringify({ type: "reset" })));

  connect();
  return () => { closedByUs = true; ws?.close(); };
}
