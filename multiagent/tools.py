"""Python-function tools.

    @tool
    def get_weather(city: str, unit: Literal["c", "f"] = "c") -> str:
        '''Current weather for a city.'''

The JSON schema is generated from type hints via pydantic, the description
from the docstring. Arguments are validated before the function is called.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from .llm.base import ToolSchema


class ToolError(Exception):
    """Raise inside a tool to return a clean error message to the model."""


@dataclass
class Tool:
    name: str
    description: str
    func: Callable[..., Any]
    args_model: type[BaseModel]
    timeout: float | None = 60.0
    kind: str = "python"  # "python" (defined in code) or "http" (created at runtime)

    @property
    def schema(self) -> ToolSchema:
        params = self.args_model.model_json_schema()
        params.pop("title", None)
        return ToolSchema(name=self.name, description=self.description, parameters=params)

    async def __call__(self, arguments: dict[str, Any]) -> str:
        args = self.args_model.model_validate(arguments)  # raises ValidationError
        kwargs = {k: getattr(args, k) for k in self.args_model.model_fields}
        if inspect.iscoroutinefunction(self.func):
            coro: Awaitable[Any] = self.func(**kwargs)
        else:
            coro = asyncio.to_thread(self.func, **kwargs)
        result = await asyncio.wait_for(coro, timeout=self.timeout)
        return _stringify(result)


def _stringify(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, BaseModel):
        return result.model_dump_json()
    try:
        return json.dumps(result, default=str)
    except TypeError:
        return str(result)


def _build_args_model(func: Callable[..., Any], name: str) -> type[BaseModel]:
    sig = inspect.signature(func)
    hints = inspect.get_annotations(func, eval_str=True)
    fields: dict[str, Any] = {}
    for pname, param in sig.parameters.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            raise TypeError(f"Tool '{name}': *args/**kwargs are not supported")
        ann = hints.get(pname, Any)
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[pname] = (ann, default)
    model_name = "".join(p.capitalize() for p in name.split("_")) + "Args"
    return create_model(model_name, __config__=ConfigDict(extra="forbid"), **fields)


def make_tool(
    func: Callable[..., Any],
    *,
    name: str | None = None,
    description: str | None = None,
    timeout: float | None = 60.0,
) -> Tool:
    tname = name or func.__name__
    desc = description or inspect.getdoc(func) or f"Call {tname}"
    return Tool(name=tname, description=desc, func=func, args_model=_build_args_model(func, tname), timeout=timeout)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, t: Tool, *, replace: bool = False) -> Tool:
        if t.name in self._tools and not replace:
            raise ValueError(f"Tool '{t.name}' already registered")
        self._tools[t.name] = t
        return t

    def tool(
        self,
        func: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        description: str | None = None,
        timeout: float | None = 60.0,
    ):
        """Decorator. Usable as `@registry.tool` or `@registry.tool(name=...)`."""

        def deco(f: Callable[..., Any]) -> Callable[..., Any]:
            self.register(make_tool(f, name=name, description=description, timeout=timeout))
            return f

        return deco(func) if func is not None else deco

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"Unknown tool '{name}'") from None

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def list(self) -> list[Tool]:
        return list(self._tools.values())


# Process-wide default registry + decorator shortcut
default_tools = ToolRegistry()
tool = default_tools.tool

__all__ = ["Tool", "ToolError", "ToolRegistry", "ValidationError", "default_tools", "make_tool", "tool"]
