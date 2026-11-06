from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from multiagent import (
    AgentConfig,
    AgentStore,
    AgentValidationError,
    LLMProvider,
    LLMResponse,
    Message,
    MultiAgentSystem,
    ProviderRegistry,
    ToolCall,
    ToolError,
    ToolRegistry,
    Usage,
)
from multiagent.api import create_app
from multiagent.auth import UserStore
from multiagent.llm.anthropic_provider import AnthropicProvider
from multiagent.llm.openai_provider import OpenAIProvider


class ScriptedProvider(LLMProvider):
    """Returns queued responses keyed by system prompt, records every call."""

    name = "mock"

    def __init__(self, scripts: dict[str, list[LLMResponse]]):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.calls: list[dict] = []

    async def complete(self, *, model, system, messages, tools, temperature=None, max_tokens=4096):
        self.calls.append({"system": system, "messages": list(messages), "tools": [t.name for t in tools]})
        return self.scripts[system.split("\n\n")[0]].pop(0)  # key on the agent's own prompt


def make_system(tmp_path, scripts):
    tools = ToolRegistry()

    @tools.tool
    def add(a: int, b: int) -> int:
        """Add two ints."""
        return a + b

    @tools.tool
    def boom() -> str:
        """Always fails."""
        raise ToolError("kaboom")

    provider = ScriptedProvider(scripts)
    providers = ProviderRegistry()
    providers.register("mock", lambda: provider)
    system = MultiAgentSystem(store=AgentStore(tmp_path / "agents.json"), tools=tools, providers=providers)
    return system, provider


def cfg(prompt, **kw):
    return AgentConfig(system_prompt=prompt, provider="mock", model="m", **kw)


def resp(text="", calls=()):
    return LLMResponse(text=text, tool_calls=list(calls), usage=Usage(input_tokens=1, output_tokens=1))


# ---------------------------------------------------------------- tools
def test_tool_schema_from_type_hints(tmp_path):
    system, _ = make_system(tmp_path, {})
    schema = system.tools.get("add").schema
    assert schema.description == "Add two ints."
    assert schema.parameters["required"] == ["a", "b"]
    assert schema.parameters["properties"]["a"]["type"] == "integer"
    assert schema.parameters["additionalProperties"] is False


# ------------------------------------------------------------- runtime
async def test_tool_loop_and_errors(tmp_path):
    system, provider = make_system(tmp_path, {"math": [
        resp(calls=[ToolCall(id="1", name="add", arguments={"a": 2, "b": 3}),
                    ToolCall(id="2", name="add", arguments={"a": "x"}),
                    ToolCall(id="3", name="boom")]),
        resp("done: 5"),
    ]})
    system.add_agent("math", cfg("math", tools=["add", "boom"]))
    r = await system.run("math", "2+3?")

    assert r.output == "done: 5" and r.stop_reason == "completed" and r.steps == 2
    tool_msgs = [m for m in r.messages if m.role == "tool"]
    assert [m.content for m in tool_msgs][0] == "5"
    assert tool_msgs[1].is_error and "Invalid arguments" in tool_msgs[1].content
    assert tool_msgs[2].is_error and tool_msgs[2].content == "kaboom"
    assert r.usage.input_tokens == 2


async def test_delegation_to_subagent(tmp_path):
    system, provider = make_system(tmp_path, {
        "boss": [resp(calls=[ToolCall(id="d1", name="ask_worker", arguments={"task": "add 1 and 1"})]),
                 resp("worker said 2")],
        "worker": [resp(calls=[ToolCall(id="w1", name="add", arguments={"a": 1, "b": 1})]), resp("2")],
    })
    system.add_agent("worker", cfg("worker", description="Does math", tools=["add"]))
    system.add_agent("boss", cfg("boss", sub_agents=["worker"]))

    r = await system.run("boss", "go")
    assert r.output == "worker said 2"
    assert provider.calls[0]["tools"] == ["ask_worker"]
    assert {e.agent for e in r.trace} == {"boss", "worker"}
    assert any(e.depth == 1 for e in r.trace)
    assert r.usage.input_tokens == 4  # parent + child usage aggregated


async def test_max_steps(tmp_path):
    loop = [resp("thinking", [ToolCall(id=str(i), name="add", arguments={"a": 1, "b": 1})]) for i in range(3)]
    system, _ = make_system(tmp_path, {"loop": loop})
    system.add_agent("loop", cfg("loop", tools=["add"], max_steps=3))
    r = await system.run("loop", "x")
    assert r.stop_reason == "max_steps" and r.output == "thinking"


def test_validation(tmp_path):
    system, _ = make_system(tmp_path, {})
    with pytest.raises(AgentValidationError, match="Unknown tools"):
        system.add_agent("a", cfg("a", tools=["nope"]))
    with pytest.raises(AgentValidationError, match="Unknown sub-agents"):
        system.add_agent("a", cfg("a", sub_agents=["ghost"]))
    with pytest.raises(AgentValidationError, match="Unknown provider"):
        system.add_agent("a", AgentConfig(system_prompt="x", provider="nope"))
    system.add_agent("a", cfg("a"))
    system.add_agent("b", cfg("b", sub_agents=["a"]))
    with pytest.raises(AgentValidationError, match="cycle"):
        system.add_agent("a", cfg("a", sub_agents=["b"]), replace=True)
    with pytest.raises(AgentValidationError, match="sub-agent of"):
        system.delete_agent("a")


def test_persistence(tmp_path):
    system, _ = make_system(tmp_path, {})
    system.add_agent("p", cfg("persist me", tools=["add"]))
    reloaded = AgentStore(tmp_path / "agents.json")
    assert reloaded.get("p").system_prompt == "persist me"
    assert json.loads((tmp_path / "agents.json").read_text())["p"]["tools"] == ["add"]


# ---------------------------------------------------------------- API
def test_api_crud_attach_and_run(tmp_path):
    system, _ = make_system(tmp_path, {"api": [resp(calls=[ToolCall(id="1", name="add", arguments={"a": 4, "b": 5})]),
                                               resp("9")]})
    c = authed_client(system)

    assert {t["name"] for t in c.get("/tools").json()} == {"add", "boom"}
    r = c.post("/agents", json={"name": "api", "system_prompt": "api", "provider": "mock", "model": "m"})
    assert r.status_code == 201
    assert c.post("/agents", json={"name": "api", "system_prompt": "x", "provider": "mock"}).status_code == 409
    assert c.post("/agents/api/tools/add").json()["tools"] == ["add"]
    assert c.post("/agents/api/tools/nope").status_code == 422

    run = c.post("/agents/api/run", json={"input": "4+5", "include_trace": False}).json()
    assert run["output"] == "9" and run["trace"] == []

    assert c.delete("/agents/api/tools/add").json()["tools"] == []
    assert c.delete("/agents/api").status_code == 204
    assert c.get("/agents/api").status_code == 404


# ------------------------------------------------------- wire formats
def _history():
    return [
        Message(role="user", content="hi"),
        Message(role="assistant", content="", tool_calls=[ToolCall(id="a", name="t", arguments={"x": 1}),
                                                          ToolCall(id="b", name="t", arguments={})]),
        Message(role="tool", tool_call_id="a", content="r1"),
        Message(role="tool", tool_call_id="b", content="r2", is_error=True),
    ]


def test_anthropic_wire_merges_tool_results():
    wire = AnthropicProvider._to_wire(_history())
    assert [m["role"] for m in wire] == ["user", "assistant", "user"]
    assert [b["tool_use_id"] for b in wire[2]["content"]] == ["a", "b"]
    assert wire[2]["content"][1]["is_error"] is True


def test_openai_wire():
    wire = OpenAIProvider._to_wire("sys", _history())
    assert [m["role"] for m in wire] == ["system", "user", "assistant", "tool", "tool"]
    assert json.loads(wire[2]["tool_calls"][0]["function"]["arguments"]) == {"x": 1}


def authed_client(system, **kw) -> TestClient:
    """TestClient for create_app(system) with an in-memory user store, logged in as the admin."""
    app = create_app(system, users=UserStore(), **kw)
    users = app.state.users
    token, _ = users.issue_token(users.list()[0].username)
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})
