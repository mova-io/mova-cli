"""``BaseLLMProvider`` Protocol — the only LLM seam in movate.

Adapters return raw text + token usage. They MUST NOT compute cost or call
external pricing APIs — pricing is derived in the executor from a versioned
local table (see :mod:`movate.providers.pricing`).
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from movate.core.models import TokenUsage


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    """LiteLLM-style model string, e.g. 'openai/gpt-4o-mini-2024-07-18'."""

    messages: list[Message]
    params: dict[str, Any] = Field(default_factory=dict)


class CompletionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    raw: dict[str, Any] = Field(default_factory=dict)


class BaseLLMProvider(Protocol):
    """The only contract movate code uses to talk to a model.

    Implementations must map provider-specific exceptions to
    :class:`movate.core.failures.MovateError` subclasses so the retry
    layer can act on a single taxonomy.
    """

    name: str
    version: str

    async def complete(self, request: CompletionRequest) -> CompletionResponse: ...

    async def stream(self, request: CompletionRequest) -> Any:
        """Stream is reserved for v0.2+; raise NotImplementedError until then."""
        ...

    async def embed(self, text: str, *, model: str) -> list[float]:
        """Embed is reserved for v0.5+ (retrieval); raise NotImplementedError until then."""
        ...
