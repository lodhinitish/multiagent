from __future__ import annotations

import json
from typing import Any

from .base import LLMProvider, LLMResponse, Message, ToolCall, ToolSchema, Usage


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key: str | None = None, base_url: str | None = None, client: Any | None = None):
        if client is None:
            from openai import AsyncOpenAI  # lazy: optional dependency

            client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._client = client

    @staticmethod
    def _to_wire(system: str, messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
        for m in messages:
            if m.role == "tool":
                out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
            elif m.role == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.content or None}
                if m.tool_calls:
                    msg["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                        }
                        for tc in m.tool_calls
                    ]
                out.append(msg)
            else:
                out.append({"role": "user", "content": m.content})
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
            "messages": self._to_wire(system, messages),
            "max_completion_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in tools
            ]
            # GPT-5-family reasoning models reject function tools on /v1/chat/completions
            # unless reasoning is explicitly disabled.
            if model.startswith("gpt-5"):
                kwargs["reasoning_effort"] = "none"
        if temperature is not None:
            kwargs["temperature"] = temperature

        resp = await self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        calls = []
        for tc in choice.message.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                # Surface the raw string so the tool layer reports a validation error
                args = {"__raw__": tc.function.arguments}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))

        usage = resp.usage
        return LLMResponse(
            text=choice.message.content or "",
            tool_calls=calls,
            stop_reason=choice.finish_reason,
            usage=Usage(
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            ),
        )
