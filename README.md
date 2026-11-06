# multiagent

A small multi-agent framework you can run as a library or a REST service. Each agent has its own system prompt, its own provider and model (Anthropic or OpenAI), a set of Python tools, and a list of sub-agents it is allowed to hand work to.

```
multiagent/
  llm/base.py               canonical Message / ToolCall / LLMProvider interface
  llm/anthropic_provider.py Claude adapter (merges tool_result blocks into the user turn)
  llm/openai_provider.py    OpenAI Chat Completions adapter (works with any compatible base_url)
  tools.py                  @tool decorator: JSON schema from type hints, pydantic validation, timeouts
  store.py                  AgentSpec + thread-safe JSON store with atomic writes
  runtime.py                agent loop, parallel tool calls, sub-agent delegation, trace, usage
  api.py                    FastAPI app
  builtin_tools.py          calculator, current_time, http_get (examples)
  loan_tools.py             get_loans_by_phone, get_emi_details, create_loan (JSON-file store, LOANS_FILE)
  http_tools.py             HTTP tools: tools created at runtime (from the web app) that call a URL
  ws.py                     WebSocket endpoints: text chat and voice chat
  speech.py                 speech-to-text / text-to-speech (OpenAI by default, pluggable)
  static/app/               Agent Studio web app (plain JS, no build step), served at /
```

## Run

```bash
pip install -e ".[all,dev]"
cp .env.example .env   # then put ANTHROPIC_API_KEY / OPENAI_API_KEY and ADMIN_PASSWORD in .env (loaded automatically)
TOOL_MODULES=multiagent.builtin_tools,myapp.tools uvicorn multiagent.api:app --reload
pytest
```

## Web app (Agent Studio)

Start the server and open **http://localhost:8000/**.

| Screen | What you can do |
|---|---|
| **Agents** | See every agent with its model, tools, sub-agents and the agents that use it. From each card you can open Chat, Voice, Edit or + Sub-agent, or delete it |
| **New / Edit agent** | Set the name, description, system prompt, provider and model, steps, temperature and tokens; tick tools and sub-agents. "+ Create new sub-agent" creates an agent and attaches it in one go |
| **Tools** | See every tool with its parameters, its kind (Python or HTTP) and which agents use it. Search, filter, and delete HTTP tools |
| **New HTTP tool** | Define a tool without writing code: method, URL with `{param}` placeholders, typed parameters, and secret headers (masked in the UI and API) |
| **Chat** | Text chat over the WebSocket. Shows tool calls and delegations live and keeps the conversation until "New conversation" |
| **Voice** | Hold the button (or the Space bar) to talk; the reply is shown and spoken. You can pick a voice, or type and still hear the reply |

## Authentication

Every API route and WebSocket requires a logged-in user. Only `POST /auth/login`, the web app's files (so the login page can load) and `/docs` are public. In `/docs`, click **Authorize** to try the API.

**First admin.** When `users.json` (`USERS_FILE`) has no users, the server creates an admin on startup from `ADMIN_USERNAME` (default `admin`) and `ADMIN_PASSWORD` in `.env`. If `ADMIN_PASSWORD` is empty, it generates a random password and prints it in the server log once. These settings are only read while `users.json` has no users. To start over, stop the server, delete `users.json`, and start again.

**Logging in.** Use any one of these:

| How | Use it for |
|---|---|
| Web app login page | The browser. It sets an HttpOnly `session` cookie, which also covers the chat and voice WebSockets |
| `POST /auth/login` `{"username", "password"}` → `{"token", "expires_at", "user"}`, then `Authorization: Bearer <token>` | Scripts and other apps |
| `Authorization: Basic` (`curl -u user:password`) | Quick calls without a login step |
| `?token=<token>` | WebSocket clients that can't set headers |

```bash
TOKEN=$(curl -s localhost:8000/auth/login -H 'content-type: application/json' \
  -d "{\"username\": \"admin\", \"password\": \"$ADMIN_PASSWORD\"}" | python3 -c 'import sys, json; print(json.load(sys.stdin)["token"])')
curl -H "Authorization: Bearer $TOKEN" localhost:8000/agents
```

Tokens last `AUTH_TOKEN_HOURS` (default 12). Passwords are stored as salted PBKDF2-SHA256 hashes. After 5 failed logins for the same username from the same address within 5 minutes, further attempts get `429`. A WebSocket without valid credentials receives an error and is closed with code `4401`.

**Users.** Any logged-in user can use agents and tools. Only admins can manage users, either on the web app's **Account** page (click your name in the sidebar) or through the API:

| Method | Path | Who | What |
|---|---|---|---|
| POST | `/auth/login`, `/auth/logout` | anyone | Log in (returns a token and sets the cookie) / clear the cookie |
| GET | `/auth/me` | user | Who you are |
| POST | `/auth/password` | user | `{"current_password", "new_password"}`. Logs out your other sessions |
| GET / POST | `/auth/users` | admin | List users / create one: `{"username", "password", "role": "user" or "admin"}` |
| PUT | `/auth/users/{username}/password` | admin | `{"password"}` |
| DELETE | `/auth/users/{username}` | admin | Delete a user (not yourself, not the last admin) |

## Tools (Python functions)

```python
# myapp/tools.py
from typing import Literal
from multiagent import tool, ToolError

@tool
def lookup_ticket(ticket_id: str, fields: list[str] | None = None) -> dict:
    """Fetch a support ticket by ID."""
    ...

@tool(name="send_sms", timeout=10)
async def sms(to: str, body: str, priority: Literal["low", "high"] = "low") -> str:
    """Send an SMS."""
    if not to.startswith("+"):
        raise ToolError("Phone number must be in E.164 format")  # the model sees this message
    ...
```

Tools are loaded at startup through the `TOOL_MODULES` environment variable. New Python code can't be added at runtime, but you can create **HTTP tools** at runtime (in the web app, or with `POST /tools`). They're stored in `http_tools.json` (`TOOLS_FILE`):

```bash
curl -u admin:$ADMIN_PASSWORD -X POST localhost:8000/tools -H 'content-type: application/json' -d '{
  "name": "get_weather", "description": "Current weather for a city",
  "method": "GET", "url": "https://wttr.in/{city}?format=3",
  "parameters": [{"name": "city", "type": "string", "required": true}]}'
```

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/tools`, `/tools/{name}`, `/providers` | List what you can attach (tools include `kind` and `used_by`) |
| POST / DELETE | `/tools`, `/tools/{name}` | Create or delete an HTTP tool. Python tools can't be deleted |
| POST | `/agents` | Create an agent |
| GET/PUT/DELETE | `/agents/{name}` | Read, replace or delete an agent |
| POST/DELETE | `/agents/{name}/tools/{tool}` | Attach or detach a tool |
| POST/DELETE | `/agents/{name}/sub_agents/{sub}` | Attach or detach a sub-agent (cycles are rejected) |
| POST | `/agents/{name}/run` | `{"input": "...", "history": [], "include_trace": true}` |

```bash
curl -u admin:$ADMIN_PASSWORD -X POST localhost:8000/agents -H 'content-type: application/json' -d '{
  "name": "researcher", "description": "Fetches and summarises web pages",
  "system_prompt": "You research topics using http_get. Cite URLs.",
  "provider": "anthropic", "model": "claude-sonnet-4-5", "tools": ["http_get"]}'

curl -u admin:$ADMIN_PASSWORD -X POST localhost:8000/agents -H 'content-type: application/json' -d '{
  "name": "lead", "system_prompt": "Break the task down and delegate.",
  "provider": "openai", "model": "gpt-4.1",
  "tools": ["calculator"], "sub_agents": ["researcher"]}'

curl -u admin:$ADMIN_PASSWORD -X POST localhost:8000/agents/lead/run -d '{"input": "..."}' -H 'content-type: application/json'
```

## WebSockets

Both sockets keep the conversation for as long as the connection is open, so each message follows on from the last. Send `{"type": "reset"}` to start over. Errors such as a provider failure or a bad message arrive as `{"type": "error", "message": ...}`, and the socket stays open.

### Text to text: `ws://localhost:8000/ws/agents/{name}/chat?trace=true`

| Direction | Message |
|---|---|
| client → server | plain text, or `{"type": "message", "text": "..."}`, `{"type": "reset"}`, `{"type": "ping"}` |
| server → client | `ready`, then for each message: `event` × N (only with `trace=true`; `event` is `llm_response`, `tool_call` or `tool_result`, with `agent` and `depth`), then `{"type": "response", "text", "stop_reason", "steps", "usage"}` |

```bash
python examples/ws_chat_client.py lead --trace   # logs in with ADMIN_USERNAME/ADMIN_PASSWORD from .env, or --user/--password
```
```bash
websocat -H "Authorization: Bearer $TOKEN" 'ws://localhost:8000/ws/agents/lead/chat'   # then type: What is 17% of 2340?
```

### Voice: `ws://localhost:8000/ws/agents/{name}/voice`

Query parameters: `input_format` (`webm` by default; also `wav`, `mp3`, `m4a`, `mp4`, `ogg` or `flac`), `output_format` (`mp3` by default; also `opus`, `aac`, `flac`, `wav` or `pcm`), `voice` (`alloy`, `nova`, ...), `language` (an ISO code, optional) and `trace`.

| Direction | Message |
|---|---|
| client → server | **binary frames** with the audio of one utterance, in chunks of any size; then `{"type": "end"}`. Also `{"type": "text", "text"}` (typed input, spoken reply), `{"type": "cancel"}` (drop the buffered audio) and `{"type": "reset"}` |
| server → client | `{"type": "transcript", "text"}` → `event`s → `{"type": "response", "text", ...}` → `{"type": "audio_start", "format"}` → **binary audio chunks**, streamed as they are synthesised → `{"type": "audio_end", "bytes"}` |

Flow: audio → speech-to-text (`STT_MODEL`, default `gpt-4o-mini-transcribe`) → agent run (any provider, with tools and sub-agents) → text-to-speech (`TTS_MODEL`, default `gpt-4o-mini-tts`). Speech always uses `OPENAI_API_KEY`, even when the agent runs on Claude. To use another vendor, subclass `SpeechToText` / `TextToSpeech` in `speech.py` and pass them in with `create_app(stt=..., tts=...)`.

```bash
python examples/ws_voice_client.py lead question.wav -o reply.mp3 --voice nova
```
In voice mode the agent is told to answer in short spoken sentences: amounts and dates in words, IDs spelled out, no markdown. Any markdown left in the reply is removed before speech. The transcript still shows the reply as written. Set `TTS_INSTRUCTIONS` to change how the voice sounds (supported by `gpt-4o-mini-tts` and newer models).

To talk from a browser, open the web app and go to **Agents → Voice** on any agent. There are two modes:

- **Hands-free** (default): tap the microphone once and talk normally. It sends what you said when you pause, speaks the reply, then listens again. Talk over the reply to interrupt it. Tap again to end the conversation.
- **Hold to talk**: hold the button (or the Space bar) while you speak, then let go.

## How delegation works

Each sub-agent appears to its parent as a tool named `ask_<name>` that takes a single `task` string. The parent sees the sub-agent's `description` as the tool description. The sub-agent runs in a fresh context, sees only `task`, and its final text comes back as the tool result. Providers can be mixed: a GPT lead can delegate to a Claude worker. Token usage is summed across every agent in the run, and `trace` records each LLM response, tool call and tool result together with the agent name and delegation depth.

## Safety rails

- Unknown tools, sub-agents and providers are rejected when you create an agent.
- Sub-agent cycles are rejected, and delegation is capped at a depth of 5.
- You can't delete an agent while another agent still uses it as a sub-agent.
- Tool errors, validation failures and timeouts go back to the model as `is_error` results, so they don't crash the run.
- `max_steps` caps each agent loop. When the cap is hit, the run returns `stop_reason: "max_steps"`.

## Extending

- **New provider:** subclass `LLMProvider`, then call `system.providers.register("gemini", lambda: GeminiProvider())`.
- **Library use:** `MultiAgentSystem(store=AgentStore("agents.json")).add_agent(...)` and `await system.run(...)`.
- **Ideas for later:** token-by-token streaming, auth on the API, and a SQL-backed store that implements the same `get/list/put/delete` methods.
