from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from multiagent import AgentStore, MultiAgentSystem, ProviderRegistry, ToolCall, ToolRegistry
from multiagent.api import create_app
from multiagent.http_tools import HttpToolStore

from test_system import ScriptedProvider, authed_client, resp

WEATHER = {
    "name": "weather", "description": "Weather for a city", "method": "GET",
    "url": "https://api.example.com/weather/{city}",
    "headers": {"Authorization": "Bearer secret"},
    "parameters": [{"name": "city", "type": "string", "required": True},
                   {"name": "days", "type": "integer", "required": False}],
}


def setup(tmp_path, scripts=None):
    provider = ScriptedProvider(scripts or {})
    providers = ProviderRegistry()
    providers.register("mock", lambda: provider)
    system = MultiAgentSystem(store=AgentStore(tmp_path / "agents.json"), tools=ToolRegistry(),
                              providers=providers, http_tools=HttpToolStore(tmp_path / "tools.json"))

    @system.tools.tool
    def add(a: int, b: int) -> int:
        """Add."""
        return a + b

    return authed_client(system), system


def agent(c, name, **kw):
    r = c.post("/agents", json={"name": name, "system_prompt": name, "provider": "mock", "model": "m", **kw})
    assert r.status_code == 201, r.text
    return r.json()


def test_http_tool_lifecycle_and_call(tmp_path, monkeypatch):
    c, system = setup(tmp_path, {"bot": [
        resp(calls=[ToolCall(id="1", name="weather", arguments={"city": "New Delhi", "days": 2})]), resp("sunny"),
    ]})
    r = c.post("/tools", json=WEATHER)
    assert r.status_code == 201, r.text
    info = r.json()
    assert info["kind"] == "http" and info["http"]["headers"] == {"Authorization": "••••"}
    assert info["parameters"]["required"] == ["city"]
    assert c.post("/tools", json=WEATHER).status_code == 409
    assert {t["name"]: t["kind"] for t in c.get("/tools").json()} == {"add": "python", "weather": "http"}

    seen = {}

    async def fake_request(self, method, url, **kw):
        seen.update(method=method, url=url, **kw)
        return httpx.Response(200, text="25C sunny", request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    agent(c, "bot", tools=["weather"])
    assert c.get("/tools/weather").json()["used_by"] == ["bot"]
    run = c.post("/agents/bot/run", json={"input": "weather?"}).json()
    assert run["output"] == "sunny"
    assert seen["url"] == "https://api.example.com/weather/New%20Delhi"
    assert seen["params"] == {"days": 2} and seen["headers"] == {"Authorization": "Bearer secret"}
    assert [m for m in run["messages"] if m["role"] == "tool"][0]["content"] == "HTTP 200\n25C sunny"

    # can't delete while attached; python tools can't be deleted at all
    assert c.delete("/tools/weather").status_code == 409
    assert c.delete("/tools/add").status_code == 409
    c.delete("/agents/bot/tools/weather")
    assert c.delete("/tools/weather").status_code == 204
    assert c.get("/tools/weather").status_code == 404
    assert HttpToolStore(tmp_path / "tools.json").list() == []


def test_http_tool_persists_and_validates(tmp_path):
    c, _ = setup(tmp_path)
    assert c.post("/tools", json={**WEATHER, "url": "ftp://x"}).status_code == 422
    assert c.post("/tools", json={**WEATHER, "url": "https://x/{nope}"}).status_code == 422
    assert c.post("/tools", json={**WEATHER, "name": "ask_x"}).status_code == 422
    assert c.post("/tools", json={**WEATHER, "name": "add"}).status_code == 409  # clashes with python tool
    assert c.post("/tools", json=WEATHER).status_code == 201

    c2, _ = setup(tmp_path)  # fresh system reading the same file
    assert c2.get("/tools/weather").json()["kind"] == "http"


def test_sub_agent_endpoints(tmp_path):
    c, _ = setup(tmp_path)
    agent(c, "lead")
    agent(c, "worker")
    assert c.post("/agents/lead/sub_agents/worker").json()["sub_agents"] == ["worker"]
    assert c.post("/agents/lead/sub_agents/worker").json()["sub_agents"] == ["worker"]  # idempotent
    assert c.post("/agents/worker/sub_agents/lead").status_code == 422  # cycle
    assert c.post("/agents/lead/sub_agents/ghost").status_code == 422
    assert c.post("/agents/ghost/sub_agents/worker").status_code == 404
    assert c.delete("/agents/lead/sub_agents/worker").json()["sub_agents"] == []


def test_web_app_is_served(tmp_path):
    c, _ = setup(tmp_path)
    assert c.get("/", follow_redirects=False).headers["location"] == "/app/"
