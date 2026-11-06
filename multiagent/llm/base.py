"""Provider-agnostic message types and the LLMProvider interface.

Agents speak only these canonical types; each provider adapter translates
to/from its vendor wire format.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    role: Literal["user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)  # assistant only
    tool_call_id: str | None = None  # tool only
    is_error: bool = False  # tool only


class ToolSchema(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema (object)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class LLMResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: str | None = None
    usage: Usage = Field(default_factory=Usage)


class LLMProvider(ABC):
    """Implement `complete` to add a new provider."""

    name: str

    @abstractmethod
    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSchema],
        temperature: float | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse: ...
