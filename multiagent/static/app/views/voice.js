import { api, enc, html, mount, raw, toast, wsUrl, toLogin, WS_UNAUTHORIZED } from "../lib.js";
import { agentHeader, conversation, setConn } from "./conversation.js";

const VOICES = ["alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer"];
const MODES = { auto: "Hands-free", hold: "Hold to talk" };
const MIC_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0M12 17v5M8 22h8"/></svg>';
const STOP_ICON = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';

// Hands-free tuning (milliseconds / RMS levels)
const TICK = 50;
const SPEECH_START = 200;    // this much continuous sound starts an utterance
const SILENCE_END = 1100;    // this much quiet after speech ends it
const IDLE_RESTART = 20000;  // drop audio we've been recording with nobody speaking
const MAX_UTTERANCE = 60000;
const BARGE_IN = 350;        // talking over the agent for this long stops its audio
const MIN_LEVEL = 0.015, BARGE_MIN_LEVEL = 0.05;

const load = (k, d) => { try { return localStorage.getItem(k) || d; } catch { return d; } };
const save = (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } };

function pickInputFormat() {
  const ok = (t) => window.MediaRecorder && MediaRecorder.isTypeSupported?.(t);
  if (ok("audio/webm")) return { fmt: "webm", mime: "audio/webm" };
  if (ok("audio/mp4")) return { fmt: "mp4", mime: "audio/mp4" };  // Safari
  return { fmt: "webm", mime: "" };
}

// #/agents/:name/voice — speech in, speech out over /ws/agents/{name}/voice
export async function voiceView(root, { name }) {
  const agent = await api(`/agents/${enc(name)}`);
  let voice = load("voice", "alloy");
  let mode = MODES[load("voiceMode", "auto")] ? load("voiceMode", "auto") : "auto";
  const pickers = html`
    <label class="row" style="gap:6px;flex-wrap:nowrap"><span class="hint">Mode</span>
      <select id="mode" style="width:auto">${Object.entries(MODES).map(([k, v]) => html`<option value="${k}" ${k === mode ? "selected" : ""}>${v}</option>`)}</select></label>
    <label class="row" style="gap:6px;flex-wrap:nowrap"><span class="hint">Voice</span>
      <select id="voice" style="width:auto">${VOICES.map((v) => html`<option ${v === voice ? "selected" : ""}>${v}</option>`)}</select></label>`;

  mount(root, html`
    <div class="chat">
      ${agentHeader(agent, "voice", pickers)}
      <div class="voice-stage">
        <button class="mic" id="mic" disabled>${raw(MIC_ICON)}</button>
        <div class="voice-status" id="status" role="status" aria-live="polite">Connecting…</div>
        <div class="voice-hint" id="hint"></div>
      </div>
      <div class="messages" id="messages"></div>
      <form class="composer">
        <textarea id="input" rows="1" placeholder="Or type a message. The reply is still spoken." aria-label="Message" disabled></textarea>
        <button class="btn primary" type="submit" id="send" disabled>Send</button>
        <button class="btn" type="button" id="stop" disabled>Stop audio</button>
      </form>
    </div>`);

  const $ = (id) => root.querySelector(`#${id}`);
  const convo = conversation($("messages"), `Talk to ${agent.name}. You'll see what you said and the agent's reply here, and hear the reply.`);
  const mic = $("mic"), status = (t) => { $("status").textContent = t; };
  const input = $("input"), send = $("send"), stopBtn = $("stop"), resetBtn = $("reset");
  const { fmt, mime } = pickInputFormat();
  const player = new Audio();
  let ws = null, closedByUs = false, audioChunks = [], connected = false, objectUrl = null;
  let hold = null;  // push-to-talk: { stream, rec } while the button is held
  let hf = null;    // hands-free session (see startHandsFree)

  const readyText = () => (mode === "auto"
    ? (hf ? "Listening… just start talking" : "Tap the microphone to start the conversation")
    : "Ready. Hold to talk.");
  const setMicLook = () => {
    const live = mode === "auto" && hf;
    mic.innerHTML = live ? STOP_ICON : MIC_ICON;
    mic.setAttribute("aria-label", mode === "hold" ? "Hold to talk" : live ? "End conversation" : "Start conversation");
    mic.classList.toggle("live", !!live);
    $("hint").textContent = mode === "hold"
      ? "Hold the button (or the Space bar) while you speak, then let go."
      : "Talk naturally. It sends when you pause, then listens again after the reply. Talk over the reply to interrupt.";
  };

  const refresh = () => {
    const idle = connected && !convo.busy;
    mic.disabled = !connected || (mode === "hold" && convo.busy && !hold);
    mic.classList.toggle("busy", mode === "hold" && convo.busy && !hold);
    input.disabled = !connected;
    send.disabled = !idle;
    resetBtn.disabled = !idle;
  };

  // ---------- recording one utterance ----------
  // Audio is buffered locally and only sent when the utterance is complete, so hands-free
  // mode can throw away stretches of silence without the server ever seeing them.
  function record(stream) {
    const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    const chunks = [];
    let bytes = 0;
    rec.ondataavailable = (e) => { if (e.data.size) { chunks.push(e.data); bytes += e.data.size; } };
    rec.start(250);
    return {
      // Stop recording; if `submit`, send it to the server. Resolves true if it was sent.
      finish: (submit) => new Promise((resolve) => {
        rec.onstop = async () => {
          if (!submit || bytes < 1000 || ws?.readyState !== 1) return resolve(false);
          for (const c of chunks) ws.send(await c.arrayBuffer());
          ws.send(JSON.stringify({ type: "end" }));
          resolve(true);
        };
        if (rec.state === "inactive") rec.onstop(); else rec.stop();
      }),
    };
  }
  function utteranceSent() {
    convo.startPending();
    status("Transcribing…");
    refresh();
  }
  const getMic = () => navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });

  // ---------- hold to talk ----------
  async function startHold() {
    if (!connected || hold || convo.busy) return;
    hold = { stream: null, rec: null };
    stopPlayback();
    try {
      hold.stream = await getMic();
    } catch (err) {
      hold = null;
      toast(`Microphone unavailable: ${err.message}`, "error");
      return;
    }
    if (!hold.pending) {  // still held after the permission prompt
      try {
        hold.rec = record(hold.stream);
      } catch (err) {
        hold.stream.getTracks().forEach((t) => t.stop());
        hold = null;
        toast(`Couldn't start recording: ${err.message}`, "error");
        return;
      }
      mic.classList.add("recording");
      status("Listening… release to send");
    }
    if (hold.pending) endHold();
  }
  async function endHold() {
    if (!hold) return;
    if (!hold.stream) { hold.pending = true; return; }  // released before permission resolved
    const { stream, rec } = hold;
    hold = null;
    mic.classList.remove("recording");
    const sent = rec ? await rec.finish(true) : false;
    stream.getTracks().forEach((t) => t.stop());
    if (sent) utteranceSent(); else { status("Too short. Hold the button while you speak."); refresh(); }
  }

  // ---------- hands-free ----------
  // States: listening (recording, waiting for speech) → waiting (agent working) →
  // speaking (reply playing; talking over it interrupts) → listening …
  async function startHandsFree() {
    if (!connected || hf) return;
    let stream;
    try {
      stream = await getMic();
    } catch (err) {
      toast(`Microphone unavailable: ${err.message}`, "error");
      return;
    }
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    ctx.createMediaStreamSource(stream).connect(analyser);
    hf = { stream, ctx, analyser, buf: new Float32Array(analyser.fftSize), floor: 0.01,
           state: "", rec: null, heard: false, voiced: 0, silent: 0, started: 0 };
    hf.timer = setInterval(tick, TICK);
    setMicLook();
    if (convo.busy) hf.state = "waiting"; else listen();
  }
  function stopHandsFree() {
    if (!hf) return;
    clearInterval(hf.timer);
    hf.rec?.finish(false);
    hf.stream.getTracks().forEach((t) => t.stop());
    hf.ctx.close();
    hf = null;
    mic.classList.remove("recording");
    mic.style.removeProperty("--level");
    setMicLook();
    if (!convo.busy) status(readyText());
  }
  function listen() {
    if (!hf) return;
    hf.rec?.finish(false);
    let rec;
    try {
      rec = record(hf.stream);
    } catch (err) {
      stopHandsFree();
      toast(`Couldn't start recording: ${err.message}`, "error");
      return;
    }
    Object.assign(hf, { state: "listening", rec, heard: false, voiced: 0, silent: 0, started: Date.now() });
    mic.classList.remove("recording");
    status("Listening… just start talking");
  }
  async function submitUtterance() {
    hf.state = "waiting";
    mic.classList.remove("recording");
    const rec = hf.rec;
    hf.rec = null;
    if (await rec.finish(true)) utteranceSent(); else listen();
  }
  function level() {
    hf.analyser.getFloatTimeDomainData(hf.buf);
    let sum = 0;
    for (const v of hf.buf) sum += v * v;
    return Math.sqrt(sum / hf.buf.length);
  }
  function tick() {
    if (!hf) return;
    const lv = level();
    const loud = lv > Math.max(MIN_LEVEL, hf.floor * 3);
    if (!loud) hf.floor = Math.min(0.05, hf.floor * 0.95 + lv * 0.05);  // follow the room's background noise
    mic.style.setProperty("--level", Math.min(1, lv * 8).toFixed(2));

    if (hf.state === "listening") {
      if (loud) {
        hf.voiced += TICK;
        hf.silent = 0;
        if (!hf.heard && hf.voiced >= SPEECH_START) {
          hf.heard = true;
          mic.classList.add("recording");
          status("Hearing you… pause when you're done");
        }
      } else {
        hf.silent += TICK;
        if (!hf.heard) hf.voiced = 0;
      }
      const age = Date.now() - hf.started;
      if (hf.heard && (hf.silent >= SILENCE_END || age >= MAX_UTTERANCE)) submitUtterance();
      else if (!hf.heard && age >= IDLE_RESTART) listen();  // don't keep a long recording of silence
    } else if (hf.state === "speaking") {
      if (lv > Math.max(BARGE_MIN_LEVEL, hf.floor * 6)) {
        hf.voiced += TICK;
        if (hf.voiced >= BARGE_IN) {  // the user is talking over the reply
          stopPlayback();
          listen();
          hf.heard = true;
          mic.classList.add("recording");
          status("Hearing you… pause when you're done");
        }
      } else {
        hf.voiced = 0;
      }
    }
  }
  // The agent's turn is over (reply played, or nothing to play): go back to listening.
  function turnOver() {
    if (hf) listen(); else status(readyText());
  }

  // ---------- connection & playback ----------
  function connect() {
    connected = false;
    setConn(root, "", "connecting…");
    const q = new URLSearchParams({ input_format: fmt, output_format: "mp3", voice, trace: "true" });
    const sock = new WebSocket(wsUrl(`/ws/agents/${enc(name)}/voice?${q}`));
    ws = sock;
    sock.binaryType = "arraybuffer";
    sock.onmessage = (e) => {
      if (sock !== ws) return;  // a socket we already replaced
      if (e.data instanceof ArrayBuffer) { audioChunks.push(e.data); return; }
      const m = JSON.parse(e.data);
      switch (m.type) {
        case "ready": connected = true; setConn(root, "on", "connected"); status(readyText()); break;
        case "transcript": if (m.text) { convo.user(m.text, "You (transcribed)"); status("Thinking…"); } break;
        case "event": convo.event(m); break;
        case "response":
          convo.reply(m, agent.name);
          if (m.text.trim()) status("Generating speech…"); else turnOver();
          break;
        case "audio_start": audioChunks = []; break;
        case "audio_end": play(); break;
        case "error": convo.error(m.message); turnOver(); break;
        case "reset_ok": convo.reset(); status(hf ? "New conversation. Listening…" : readyText()); break;
      }
      refresh();
    };
    sock.onclose = (ev) => {
      if (sock !== ws) return;
      if (ev.code === WS_UNAUTHORIZED) { toLogin(); return; }
      connected = false;
      stopHandsFree();
      refresh();
      if (closedByUs) return;
      if (convo.busy) convo.error("Connection lost while the agent was working.");
      setConn(root, "off", "disconnected");
      status("Disconnected.");
      if (!$("reconnect")) {
        root.querySelector("#conn").insertAdjacentHTML("afterend", '<button class="btn small" id="reconnect">Reconnect</button>');
        $("reconnect").onclick = (ev) => { ev.target.remove(); connect(); };
      }
    };
  }

  function play() {
    if (!audioChunks.length) { turnOver(); return; }
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = URL.createObjectURL(new Blob(audioChunks, { type: "audio/mpeg" }));
    player.src = objectUrl;
    stopBtn.disabled = false;
    if (hf) Object.assign(hf, { state: "speaking", voiced: 0 });
    status(hf ? "Speaking… talk to interrupt" : "Speaking…");
    player.play().catch(() => {
      status("Your browser blocked autoplay. Click anywhere, then press Stop audio to continue.");
    });
  }
  function stopPlayback() {
    player.pause();
    stopBtn.disabled = true;
  }
  player.onended = () => { stopBtn.disabled = true; turnOver(); };
  stopBtn.addEventListener("click", () => { stopPlayback(); turnOver(); });

  // ---------- input wiring ----------
  mic.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    if (mode === "hold") startHold();
    else if (hf) stopHandsFree();
    else startHandsFree();
  });
  ["pointerup", "pointerleave", "pointercancel"].forEach((ev) => mic.addEventListener(ev, () => { if (mode === "hold") endHold(); }));
  const typing = () => ["TEXTAREA", "INPUT", "SELECT"].includes(document.activeElement?.tagName);
  const onKeyDown = (e) => { if (mode === "hold" && e.code === "Space" && !e.repeat && !typing()) { e.preventDefault(); startHold(); } };
  const onKeyUp = (e) => { if (mode === "hold" && e.code === "Space" && !typing()) { e.preventDefault(); endHold(); } };
  window.addEventListener("keydown", onKeyDown);
  window.addEventListener("keyup", onKeyUp);

  function submitText() {
    const text = input.value.trim();
    if (!text || !connected || convo.busy) return;
    stopPlayback();
    if (hf) { hf.rec?.finish(false); hf.rec = null; hf.state = "waiting"; mic.classList.remove("recording"); }
    convo.user(text);
    convo.startPending();
    ws.send(JSON.stringify({ type: "text", text }));
    input.value = "";
    status("Thinking…");
    refresh();
  }
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submitText(); } });
  root.querySelector(".composer").addEventListener("submit", (e) => { e.preventDefault(); submitText(); });
  resetBtn.addEventListener("click", () => { stopPlayback(); ws?.send(JSON.stringify({ type: "reset" })); if (hf) listen(); });
  $("mode").addEventListener("change", (e) => {
    stopHandsFree();
    endHold();
    mode = e.target.value;
    save("voiceMode", mode);
    setMicLook();
    if (!convo.busy) status(readyText());
    refresh();
  });
  $("voice").addEventListener("change", (e) => {
    voice = e.target.value;
    save("voice", voice);
    stopHandsFree();
    const old = ws;
    connect();  // voice is a connection setting → reconnect; the old socket's handlers ignore it now
    old?.close();
  });

  if (!window.MediaRecorder || !navigator.mediaDevices) {
    toast("This browser can't record audio here. Use the text box instead (the replies are still spoken).", "error");
  }
  setMicLook();
  connect();
  refresh();

  return () => {
    closedByUs = true;
    stopHandsFree();
    endHold();
    player.pause();
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    window.removeEventListener("keydown", onKeyDown);
    window.removeEventListener("keyup", onKeyUp);
    ws?.close();
  };
}
