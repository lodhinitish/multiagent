from __future__ import annotations

import os
from typing import Callable

from .base import LLMProvider, LLMResponse, Message, ToolCall, ToolSchema, Usage

__all__ = [
    "LLMProvider", "LLMResponse", "Message", "ToolCall", "ToolSchema", "Usage",
    "ProviderRegistry",
]


class ProviderRegistry:
    """Lazily builds one provider instance per name. Register custom providers
    (Gemini, Ollama, a mock for tests...) with `register`."""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], LLMProvider]] = {}
        self._instances: dict[str, LLMProvider] = {}

    def register(self, name: str, factory: Callable[[], LLMProvider]) -> None:
        self._factories[name] = factory
        self._instances.pop(name, None)

    def get(self, name: str) -> LLMProvider:
        if name not in self._instances:
            if name not in self._factories:
                raise KeyError(f"Unknown provider '{name}'. Available: {sorted(self._factories)}")
            self._instances[name] = self._factories[name]()
        return self._instances[name]

    def names(self) -> list[str]:
        return sorted(self._factories)

    @classmethod
    def default(cls) -> "ProviderRegistry":
        reg = cls()

        def _key(var: str) -> str:
            # Checked when the provider is first used (not cached on failure), so a clear message
            # reaches the caller instead of the SDK's generic auth error.
            key = (os.getenv(var) or "").strip()
            if not key:
                raise RuntimeError(f"{var} is not set. Add it to the .env file and restart the server.")
            return key

        def _anthropic() -> LLMProvider:
            from .anthropic_provider import AnthropicProvider
            return AnthropicProvider(api_key=_key("ANTHROPIC_API_KEY"))

        def _openai() -> LLMProvider:
            from .openai_provider import OpenAIProvider
            base_url = os.getenv("OPENAI_BASE_URL")
            # Local OpenAI-compatible servers (Ollama, vLLM) often need no key.
            key = (os.getenv("OPENAI_API_KEY") or "").strip() or (None if base_url else _key("OPENAI_API_KEY"))
            return OpenAIProvider(api_key=key or "not-needed", base_url=base_url)

        reg.register("anthropic", _anthropic)
        reg.register("openai", _openai)
        return reg
