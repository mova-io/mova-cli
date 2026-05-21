"""``BaseLLMProvider`` Protocol — the only LLM seam in movate.

Adapters return raw text + token usage. They MUST NOT compute cost or call
external pricing APIs — pricing is derived in the executor from a versioned
local table (see :mod:`movate.providers.pricing`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from movate.core.models import TokenUsage

if TYPE_CHECKING:
    from movate.core.skill_loader import SkillBundle


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str

    # Tool-use fields. Only populated during a tool-use loop:
    #
    # * On an ``assistant`` turn that emits a tool call,
    #   ``tool_calls`` carries the call list in OpenAI's wire format.
    # * On a ``tool`` turn carrying the tool's result, ``tool_call_id``
    #   echoes the assistant turn's call id so the model can correlate
    #   results to calls.
    #
    # Both default to ``None`` so existing single-shot Messages serialize
    # identically. The LiteLLM provider dumps messages with
    # ``exclude_none=True`` so ``null`` fields never leave the wire.
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    """LiteLLM-style model string, e.g. 'openai/gpt-4o-mini-2024-07-18'."""

    messages: list[Message]
    params: dict[str, Any] = Field(default_factory=dict)
    tools: list[dict[str, Any]] | None = None
    """Tool specs the model may invoke during this completion. Each
    entry is in the OpenAI-style function-call format
    (``{"type": "function", "function": {"name": ..., "parameters": ...}}``).
    ``None`` (the default) keeps the request in single-shot mode — no
    tool-use loop. Built by :meth:`BaseLLMProvider.to_tool_spec` from
    the agent's resolved :class:`SkillBundle` list."""


class ToolCallSpec(BaseModel):
    """One tool call in a model turn — name, correlation id, and arguments.

    Used in :attr:`CompletionResponse.parallel_tool_calls` to carry all
    tool calls a model emits in a single turn. A turn with a single tool
    call still populates the list with one entry so the executor can use
    a unified dispatch path regardless of call count.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    """Tool (skill) name the model wants invoked."""
    call_id: str
    """Provider-assigned correlation id, echoed back in the tool_result."""
    input: dict[str, Any] = Field(default_factory=dict)
    """Parsed arguments the model wants the tool called with."""


class CompletionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    raw: dict[str, Any] = Field(default_factory=dict)

    # Tool-use fields. Populated when the model emits a tool_use turn
    # instead of a final response. Default values keep existing
    # single-shot callers untouched — ``kind="final"`` is the v0.5
    # behavior. ADR 002 D1 (loop ownership lives in Executor).
    kind: Literal["final", "tool_use"] = "final"
    """``final`` = model gave its final answer; ``tool_use`` = model
    wants the executor to invoke a tool and feed the result back."""
    tool_name: str = ""
    """Name of the skill the model wants invoked (matches
    ``SkillSpec.name``). Empty unless ``kind == "tool_use"``.
    Always mirrors ``parallel_tool_calls[0].name`` for backward compat."""
    tool_id: str = ""
    """Provider-assigned identifier for this tool call. The executor
    must echo it back in the matching ``tool_result`` so the model can
    correlate. Empty unless ``kind == "tool_use"``.
    Always mirrors ``parallel_tool_calls[0].call_id`` for backward compat."""
    tool_input: dict[str, Any] = Field(default_factory=dict)
    """The arguments the model wants the tool called with. Validated
    by :func:`dispatch_skill` against the skill's input schema before
    the backend is invoked. Empty unless ``kind == "tool_use"``.
    Always mirrors ``parallel_tool_calls[0].input`` for backward compat."""

    parallel_tool_calls: list[ToolCallSpec] = Field(default_factory=list)
    """All tool calls emitted in this turn.
    Always has one entry when ``kind == "tool_use"`` (matching the
    singular ``tool_name / tool_id / tool_input`` fields); has two or
    more when the model issued parallel calls in a single turn.
    Empty for ``kind == "final"`` responses."""


class StreamChunk(BaseModel):
    """One slice of a streaming response.

    Most chunks carry a small ``text`` delta (a token or two) and an
    empty ``tokens``. The FINAL chunk in a stream carries the
    accumulated usage stats — token totals aren't knowable until the
    provider closes the stream.

    ``raw`` is the provider's native chunk payload for adapters that
    want to forward extra signal (e.g. Anthropic's content-block
    types). Generic streaming code ignores it; advanced adapters
    can peek."""

    model_config = ConfigDict(extra="forbid")

    text: str = ""
    tokens: TokenUsage | None = None
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

    def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        """Stream a completion as :class:`StreamChunk` slices.

        Implementations MUST:

        * Yield at least one chunk (an empty-text chunk is fine if the
          provider returns nothing).
        * Yield the final usage stats on the LAST chunk's ``tokens``
          field. Callers rely on this for cost accounting.
        * Translate provider exceptions to ``MovateError`` subclasses
          the same way :meth:`complete` does — the executor's retry +
          fallback layer treats stream failures identically to one-shot
          failures."""
        ...

    async def embed(self, text: str, *, model: str) -> list[float]:
        """Embed is reserved for v0.5+ (retrieval); raise NotImplementedError until then."""
        ...

    def pricing_key(self, provider: str) -> str | None:
        """Map the agent's ``model.provider`` string to a key in the
        :mod:`movate.providers.pricing` table.

        Different runtimes use different naming for the same model:

        * LiteLLM agents use the prefixed form (``anthropic/claude-haiku-4-5``)
          which IS the pricing-table key — default impl returns it unchanged.
        * Native Anthropic / OpenAI agents use bare model ids
          (``claude-haiku-4-5``) — their adapters override this to prepend
          the family prefix so cost lookups succeed.
        * LangChain agents wrap an opaque Runnable — the underlying model
          isn't visible to movate, so the adapter returns ``None`` and the
          executor records ``cost_usd=0`` with a note.

        Default impl is the LiteLLM-style pass-through — adapters that need
        a translation override.
        """
        return provider

    def to_tool_spec(self, skill: SkillBundle) -> dict[str, Any]:
        """Convert a :class:`SkillBundle` into the provider's native tool format.

        Default implementation produces the OpenAI-style function-call
        schema (``{"type": "function", "function": {...}}``), which
        LiteLLM passes through to most upstream providers unchanged.
        Native-SDK adapters (Anthropic native, etc.) override this in
        PR 6 to emit their own shape.

        ADR 002 D1 — loop ownership is in :class:`Executor`, so this
        method is a pure conversion. The skill's input schema becomes
        the tool's ``parameters`` field; the skill's name + description
        flow through verbatim.
        """
        return {
            "type": "function",
            "function": {
                "name": skill.spec.name,
                "description": skill.spec.description or skill.spec.name,
                "parameters": skill.input_schema,
            },
        }
