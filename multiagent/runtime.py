"""Agent execution loop and the MultiAgentSystem facade."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, ValidationError

from .llm import Message, ProviderRegistry, ToolCall, ToolSchema, Usage
from .http_tools import HttpToolSpec, HttpToolStore, build_http_tool
from .store import AgentConfig, AgentSpec, AgentStore
from .tools import ToolError, ToolRegistry, default_tools

log = logging.getLogger("multiagent")

SUBAGENT_PREFIX = "ask_"
MAX_DEPTH = 5


class TraceEvent(BaseModel):
    agent: str
    depth: int
    type: Literal["llm_response", "tool_call", "tool_result", "error"]
    data: dict[str, Any] = Field(default_factory=dict)
    ts: float = Field(default_factory=time.time)


class RunResult(BaseModel):
    agent: str
    output: str
    stop_reason: Literal["completed", "max_steps"]
    steps: int
    usage: Usage
    messages: list[Message]
    trace: list[TraceEvent]


class AgentValidationError(ValueError):
    pass


# Called synchronously for every trace event as it happens (used by the WebSocket API to stream progress).
EventCallback = Callable[[TraceEvent], None]


class MultiAgentSystem:
    def __init__(
        self,
        store: AgentStore | None = None,
        tools: ToolRegistry | None = None,
        providers: ProviderRegistry | None = None,
        http_tools: HttpToolStore | None = None,
    ) -> None:
        self.store = store or AgentStore()
        self.tools = tools or default_tools
        self.providers = providers or ProviderRegistry.default()
        self.http_tools = http_tools or HttpToolStore()
        for spec in self.http_tools.list():
            if spec.name in self.tools:
                log.warning("HTTP tool '%s' clashes with a Python tool; skipped", spec.name)
                continue
            self.tools.register(build_http_tool(spec))

    # ---------------------------------------------------------------- CRUD
    def add_agent(self, name: str, config: AgentConfig, *, replace: bool = False) -> AgentSpec:
        if name in self.store and not replace:
            raise AgentValidationError(f"Agent '{name}' already exists")
        spec = AgentSpec(name=name, **config.model_dump())
        self._validate(spec)
        return self.store.put(spec)

    def delete_agent(self, name: str) -> None:
        parents = [a.name for a in self.store.list() if name in a.sub_agents]
        if parents:
            raise AgentValidationError(f"Agent '{name}' is a sub-agent of {parents}; detach it first")
        self.store.delete(name)

    def _config_of(self, name: str) -> AgentConfig:
        spec = self.store.get(name)
        return AgentConfig(**spec.model_dump(include=set(AgentConfig.model_fields)))

    def set_sub_agent(self, name: str, sub: str, attached: bool) -> AgentSpec:
        """Attach or detach a sub-agent (validation, incl. cycles, runs as usual)."""
        cfg = self._config_of(name)
        if attached and sub not in cfg.sub_agents:
            cfg.sub_agents.append(sub)
        elif not attached:
            cfg.sub_agents = [s for s in cfg.sub_agents if s != sub]
        return self.add_agent(name, cfg, replace=True)

    # ---------------------------------------------------------- HTTP tools
    def add_http_tool(self, spec: HttpToolSpec) -> HttpToolSpec:
        if spec.name in self.tools:
            raise AgentValidationError(f"Tool '{spec.name}' already exists")
        tool = build_http_tool(spec)
        self.http_tools.put(spec)
        self.tools.register(tool)
        return spec

    def delete_tool(self, name: str) -> None:
        tool = self.tools.get(name)  # KeyError -> 404
        if tool.kind != "http":
            raise AgentValidationError(f"'{name}' is a Python tool defined in code; remove it there")
        users = [a.name for a in self.store.list() if name in a.tools]
        if users:
            raise AgentValidationError(f"Tool '{name}' is used by {users}; detach it first")
        self.http_tools.delete(name)
        self.tools.unregister(name)

    def _validate(self, spec: AgentSpec) -> None:
        if spec.provider not in self.providers.names():
            raise AgentValidationError(f"Unknown provider '{spec.provider}'. Available: {self.providers.names()}")
        missing = [t for t in spec.tools if t not in self.tools]
        if missing:
            raise AgentValidationError(f"Unknown tools: {missing}")
        unknown = [s for s in spec.sub_agents if s != spec.name and s not in self.store]
        if unknown:
            raise AgentValidationError(f"Unknown sub-agents: {unknown}")
        clash = [s for s in spec.sub_agents if SUBAGENT_PREFIX + s in spec.tools]
        if clash:
            raise AgentValidationError(f"Tool names clash with sub-agent tools: {[SUBAGENT_PREFIX + s for s in clash]}")
        self._check_cycles(spec)

    def _check_cycles(self, spec: AgentSpec) -> None:
        graph = {a.name: a.sub_agents for a in self.store.list()}
        graph[spec.name] = spec.sub_agents
        visiting: set[str] = set()
        done: set[str] = set()

        def dfs(node: str, path: list[str]) -> None:
            if node in visiting:
                raise AgentValidationError(f"Sub-agent cycle: {' -> '.join(path + [node])}")
            if node in done:
                return
            visiting.add(node)
            for child in graph.get(node, []):
                dfs(child, path + [node])
            visiting.discard(node)
            done.add(node)

        dfs(spec.name, [])

    # ----------------------------------------------------------------- run
    def tool_schemas_for(self, spec: AgentSpec) -> list[ToolSchema]:
        schemas = [self.tools.get(t).schema for t in spec.tools]
        for sub in spec.sub_agents:
            sub_spec = self.store.get(sub)
            schemas.append(
                ToolSchema(
                    name=SUBAGENT_PREFIX + sub,
                    description=f"Delegate a task to the '{sub}' agent. {sub_spec.description}".strip(),
                    parameters={
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": "Self-contained instructions; the sub-agent sees nothing else.",
                            }
                        },
                        "required": ["task"],
                        "additionalProperties": False,
                    },
                )
            )
        return schemas

    async def run(
        self,
        agent: str,
        input: str,
        *,
        history: list[Message] | None = None,
        on_event: EventCallback | None = None,
        system_suffix: str | None = None,
        _depth: int = 0,
        _trace: list[TraceEvent] | None = None,
    ) -> RunResult:
        if _depth > MAX_DEPTH:
            raise RuntimeError(f"Max delegation depth {MAX_DEPTH} exceeded")
        spec = self.store.get(agent)
        provider = self.providers.get(spec.provider)
        trace = _trace if _trace is not None else []
        schemas = self.tool_schemas_for(spec)
        messages = [*(history or []), Message(role="user", content=input)]
        usage = Usage()

        def emit(type_: str, **data: Any) -> None:
            event = TraceEvent(agent=agent, depth=_depth, type=type_, data=data)
            trace.append(event)
            if on_event is not None:
                try:
                    on_event(event)
                except Exception:  # a broken listener must not break the run
                    log.exception("on_event callback failed")

        for step in range(1, spec.max_steps + 1):
            resp = await provider.complete(
                model=spec.model,
                system=f"{spec.system_prompt}\n\n{system_suffix}" if system_suffix else spec.system_prompt,
                messages=messages,
                tools=schemas,
                temperature=spec.temperature,
                max_tokens=spec.max_tokens,
            )
            usage = usage + resp.usage
            emit("llm_response", step=step, text=resp.text, tool_calls=[tc.model_dump() for tc in resp.tool_calls],
                 stop_reason=resp.stop_reason)
            messages.append(Message(role="assistant", content=resp.text, tool_calls=resp.tool_calls))

            if not resp.tool_calls:
                return RunResult(agent=agent, output=resp.text, stop_reason="completed", steps=step,
                                 usage=usage, messages=messages, trace=trace)

            results = await asyncio.gather(
                *(self._execute(tc, spec, _depth, trace, emit, on_event) for tc in resp.tool_calls)
            )
            for tc, (content, is_error, sub_usage) in zip(resp.tool_calls, results, strict=True):
                usage = usage + sub_usage
                messages.append(Message(role="tool", tool_call_id=tc.id, content=content, is_error=is_error))

        last_text = next((m.content for m in reversed(messages) if m.role == "assistant" and m.content), "")
        return RunResult(agent=agent, output=last_text, stop_reason="max_steps", steps=spec.max_steps,
                         usage=usage, messages=messages, trace=trace)

    async def _execute(
        self, tc: ToolCall, spec: AgentSpec, depth: int,
        trace: list[TraceEvent], emit: Any, on_event: EventCallback | None = None,
    ) -> tuple[str, bool, Usage]:
        emit("tool_call", id=tc.id, name=tc.name, arguments=tc.arguments)
        sub_usage = Usage()
        try:
            if tc.name.startswith(SUBAGENT_PREFIX) and tc.name[len(SUBAGENT_PREFIX):] in spec.sub_agents:
                task = tc.arguments.get("task")
                if not isinstance(task, str) or not task.strip():
                    raise ToolError("'task' must be a non-empty string")
                sub = await self.run(tc.name[len(SUBAGENT_PREFIX):], task, on_event=on_event,
                                     _depth=depth + 1, _trace=trace)
                content, sub_usage = sub.output, sub.usage
            elif tc.name in spec.tools:
                content = await self.tools.get(tc.name)(tc.arguments)
            else:
                raise ToolError(f"Tool '{tc.name}' is not available to this agent")
            is_error = False
        except ValidationError as e:
            content, is_error = f"Invalid arguments: {e.errors(include_url=False)}", True
        except ToolError as e:
            content, is_error = str(e), True
        except asyncio.TimeoutError:
            content, is_error = f"Tool '{tc.name}' timed out", True
        except Exception as e:  # don't let one tool crash the whole run
            log.exception("Tool %s failed", tc.name)
            content, is_error = f"{type(e).__name__}: {e}", True
        emit("tool_result", id=tc.id, name=tc.name, content=content, is_error=is_error)
        return content, is_error, sub_usage
