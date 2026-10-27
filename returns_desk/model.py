"""A thin wrapper around the Anthropic SDK.

The agent loop only calls `Model.create`, so swapping the model (or putting a
scripted fake in its place for tests) doesn't touch the loop.
"""

from typing import Any, Protocol

import anthropic

from returns_desk import config

# Models that accept server-side refusal fallbacks (`fallbacks: "default"`).
# If a safety classifier declines a request, the API reruns it on a model
# Anthropic picks for that refusal category instead of returning the refusal.
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}


class Model(Protocol):
    name: str

    def create(self, *, system: str, messages: list, tools: list[dict]) -> Any: ...


class AnthropicModel:
    def __init__(self, name: str = config.AGENT_MODEL, effort: str = config.AGENT_EFFORT,
                 client: anthropic.Anthropic | None = None):
        self.name = name
        self.effort = effort
        self.client = client or anthropic.Anthropic()

    def create(self, *, system: str, messages: list, tools: list[dict]) -> Any:
        extra: dict[str, Any] = {}
        if self.name in _FALLBACK_MODELS:
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        return self.client.beta.messages.create(
            model=self.name,
            max_tokens=config.MAX_TOKENS,
            system=system,
            messages=messages,
            tools=tools,
            output_config={"effort": self.effort},
            # Caches the system prompt, tools and history so each step only pays for new tokens.
            cache_control={"type": "ephemeral"},
            **extra,
        )
