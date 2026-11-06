from __future__ import annotations

from typing import Any

from .base import LLMProvider, LLMResponse, Message, ToolCall, ToolSchema, Usage


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str | None = None, client: Any | None = None):
        if client is None:
            from anthropic import AsyncAnthropic  # lazy: optional dependency

            client = AsyncAnthropic(api_key=api_key)
        self._client = client

    @staticmethod
    def _to_wire(messages: list[Message]) -> list[dict[str, Any]]:
        """Anthropic wants tool results as `tool_result` blocks inside a *user*
        turn, and consecutive same-role turns merged."""
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id,
                    "content": m.content,
                    "is_error": m.is_error,
                }
                role, blocks = "user", [block]
            elif m.role == "assistant":
                blocks = [{"type": "text", "text": m.content}] if m.content else []
                blocks += [
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
                    for tc in m.tool_calls
                ]
                role = "assistant"
            else:
                role, blocks = "user", [{"type": "text", "text": m.content}]

            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": blocks})
        return out

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSchema],
        temperature: float | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            "system": system,
            "messages": self._to_wire(messages),
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
        if temperature is not None:
            kwargs["temperature"] = temperature

        resp = await self._client.messages.create(**kwargs)

        text_parts, calls = [], []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {})))
        return LLMResponse(
            text="".join(text_parts),
            tool_calls=calls,
            stop_reason=resp.stop_reason,
            usage=Usage(
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
            ),
        )
