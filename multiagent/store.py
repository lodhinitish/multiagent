"""Agent definitions + JSON-file persistence."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

NAME_PATTERN = r"^[a-zA-Z][a-zA-Z0-9_-]{0,47}$"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def atomic_write_json(path: Path, data: object) -> None:
    """Write JSON via a temp file + rename so readers never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class AgentConfig(BaseModel):
    """User-editable fields (what the API accepts)."""

    description: str = Field("", description="Shown to parent agents when this agent is used as a sub-agent")
    system_prompt: str
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-5"
    tools: list[str] = Field(default_factory=list, description="Names of registered Python tools")
    sub_agents: list[str] = Field(default_factory=list, description="Agents this one may delegate to")
    max_steps: int = Field(10, ge=1, le=100)
    temperature: float | None = Field(None, ge=0, le=2)
    max_tokens: int = Field(4096, ge=1)


class AgentSpec(AgentConfig):
    name: str = Field(pattern=NAME_PATTERN)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class AgentStore:
    """Thread-safe in-memory dict mirrored to a JSON file (atomic writes)."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._agents: dict[str, AgentSpec] = {}
        if self.path and self.path.exists():
            raw = json.loads(self.path.read_text() or "{}")
            self._agents = {n: AgentSpec.model_validate(d) for n, d in raw.items()}

    def _flush(self) -> None:
        if self.path:
            atomic_write_json(self.path, {n: s.model_dump(mode="json") for n, s in self._agents.items()})

    def get(self, name: str) -> AgentSpec:
        with self._lock:
            try:
                return self._agents[name]
            except KeyError:
                raise KeyError(f"Unknown agent '{name}'") from None

    def list(self) -> list[AgentSpec]:
        with self._lock:
            return list(self._agents.values())

    def __contains__(self, name: str) -> bool:
        return name in self._agents

    def put(self, spec: AgentSpec) -> AgentSpec:
        with self._lock:
            if spec.name in self._agents:
                spec = spec.model_copy(update={"created_at": self._agents[spec.name].created_at, "updated_at": _now()})
            self._agents[spec.name] = spec
            self._flush()
            return spec

    def delete(self, name: str) -> None:
        with self._lock:
            self.get(name)
            del self._agents[name]
            self._flush()
