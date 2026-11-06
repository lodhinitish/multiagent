"""HTTP tools: tools created at runtime (e.g. from the web app) without writing Python.

An HTTP tool calls a URL with the arguments the model supplies:
  * `{param}` placeholders in the URL are filled from arguments (URL-encoded),
  * remaining arguments go in the query string for GET/DELETE, or the JSON body otherwise,
  * the model gets back "HTTP <status>\\n<body, truncated>".
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from .store import atomic_write_json
from .tools import Tool, ToolError

TOOL_NAME_PATTERN = r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$"
PARAM_NAME_PATTERN = r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$"
_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_PY_TYPES: dict[str, type] = {"string": str, "integer": int, "number": float, "boolean": bool}


class HttpToolParam(BaseModel):
    name: str = Field(pattern=PARAM_NAME_PATTERN)
    type: Literal["string", "integer", "number", "boolean"] = "string"
    description: str = ""
    required: bool = True


class HttpToolSpec(BaseModel):
    name: str = Field(pattern=TOOL_NAME_PATTERN)
    description: str = Field(min_length=1, description="Tells the model when and how to use the tool")
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url: str = Field(description="May contain {param} placeholders")
    headers: dict[str, str] = Field(default_factory=dict, description="Sent with every request, e.g. auth")
    parameters: list[HttpToolParam] = Field(default_factory=list)
    timeout: float = Field(20, gt=0, le=120)
    max_chars: int = Field(5000, ge=100, le=100_000, description="Response body is truncated to this")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _check(self) -> "HttpToolSpec":
        if not self.url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        if self.name.startswith("ask_"):
            raise ValueError("tool names starting with 'ask_' are reserved for sub-agents")
        names = [p.name for p in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("parameter names must be unique")
        by_name = {p.name: p for p in self.parameters}
        for ph in _PLACEHOLDER.findall(self.url):
            if ph not in by_name:
                raise ValueError(f"url placeholder {{{ph}}} has no matching parameter")
            if not by_name[ph].required:
                raise ValueError(f"parameter '{ph}' is used in the url so it must be required")
        return self

    def public(self) -> dict[str, Any]:
        """JSON for API responses, with header values masked (they often hold secrets)."""
        data = self.model_dump(mode="json")
        data["headers"] = {k: "••••" for k in self.headers}
        return data


def build_http_tool(spec: HttpToolSpec) -> Tool:
    fields: dict[str, Any] = {}
    for p in spec.parameters:
        t = _PY_TYPES[p.type]
        fields[p.name] = (t, Field(..., description=p.description or None)) if p.required \
            else (t | None, Field(None, description=p.description or None))
    model_name = "".join(part.capitalize() for part in re.split(r"[_-]", spec.name)) + "Args"
    args_model = create_model(model_name, __config__=ConfigDict(extra="forbid"), **fields)
    placeholders = set(_PLACEHOLDER.findall(spec.url))

    async def call(**kwargs: Any) -> str:
        args = {k: v for k, v in kwargs.items() if v is not None}
        url = spec.url
        for ph in placeholders:
            url = url.replace("{" + ph + "}", quote(str(args.pop(ph)), safe=""))
        request: dict[str, Any] = {"method": spec.method, "url": url, "headers": spec.headers}
        if args:
            request["params" if spec.method in ("GET", "DELETE") else "json"] = args
        try:
            async with httpx.AsyncClient(timeout=spec.timeout, follow_redirects=True) as client:
                r = await client.request(**request)
        except httpx.HTTPError as e:
            raise ToolError(f"Request failed: {type(e).__name__}: {e}") from e
        return f"HTTP {r.status_code}\n{r.text[:spec.max_chars]}"

    return Tool(name=spec.name, description=spec.description, func=call, args_model=args_model,
                timeout=spec.timeout + 5, kind="http")


class HttpToolStore:
    """HTTP tool specs mirrored to a JSON file (same atomic-write approach as AgentStore)."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._specs: dict[str, HttpToolSpec] = {}
        if self.path and self.path.exists():
            raw = json.loads(self.path.read_text() or "{}")
            self._specs = {n: HttpToolSpec.model_validate(d) for n, d in raw.items()}

    def _flush(self) -> None:
        if self.path:
            atomic_write_json(self.path, {n: s.model_dump(mode="json") for n, s in self._specs.items()})

    def get(self, name: str) -> HttpToolSpec:
        with self._lock:
            try:
                return self._specs[name]
            except KeyError:
                raise KeyError(f"Unknown HTTP tool '{name}'") from None

    def list(self) -> list[HttpToolSpec]:
        with self._lock:
            return list(self._specs.values())

    def __contains__(self, name: str) -> bool:
        return name in self._specs

    def put(self, spec: HttpToolSpec) -> HttpToolSpec:
        with self._lock:
            self._specs[spec.name] = spec
            self._flush()
            return spec

    def delete(self, name: str) -> None:
        with self._lock:
            self.get(name)
            del self._specs[name]
            self._flush()
