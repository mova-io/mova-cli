"""Native Anthropic adapter — direct ``anthropic`` SDK calls.

This is the v0.6 alternative to :class:`LiteLLMProvider` for agents
that declare ``runtime: native_anthropic`` in ``agent.yaml``. The win:
features LiteLLM doesn't yet surface or surfaces lossily — prompt
caching headers, vision, thinking blocks, MCP-server tool ecosystem,
and the official streaming-event shape.

Scope (v0.6+)
-------------

* Text-in, text-out :meth:`complete` against ``messages.create``.
* Token-aware :meth:`stream` against ``messages.stream``.
* **Native tool-use loop** (PR 6, v0.6.1): :meth:`to_tool_spec` emits
  Anthropic's flat ``{name, description, input_schema}`` shape;
  :meth:`complete` accepts ``tools=`` in the request and surfaces
  ``tool_use`` content blocks as ``CompletionResponse(kind="tool_use", ...)``.
  The executor's tool-use loop is provider-agnostic — same
  :class:`CompletionResponse` shape from LiteLLM or native Anthropic.
* Exception translation matching :class:`LiteLLMProvider`'s taxonomy
  so the executor's retry + fallback policy treats native and
  LiteLLM failures identically.

This adapter is responsible for translating the executor's
OpenAI-style message history (``role="assistant"`` with ``tool_calls``
+ ``role="tool"`` results) into Anthropic's content-block format
(``tool_use`` content blocks on assistant messages, ``tool_result``
blocks on user messages). The executor never sees Anthropic's wire
format.

Vision + thinking blocks are still deferred.

Optional install:

    uv add 'movate-cli[anthropic]'

If the ``anthropic`` package isn't installed, constructing this class
raises ``ImportError`` with a pointer to the extra. Callers that
opportunistically register the adapter (see ``cli/_runtime.py``)
catch the ImportError silently — same try/except dance every
optional integration uses.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from movate.core.failures import (
    AuthError,
    ContentFilterError,
    ContextLengthError,
    ModelUnavailableError,
    MovateTimeoutError,
    RateLimitError,
    SchemaError,
)
from movate.core.models import TokenUsage
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
    Message,
    StreamChunk,
    ToolCallSpec,
)

if TYPE_CHECKING:
    import anthropic

    from movate.core.skill_loader import SkillBundle


class AnthropicProvider(BaseLLMProvider):
    """``BaseLLMProvider`` implementation that calls the official
    ``anthropic`` Python SDK directly.

    Provider strings on this runtime are bare Anthropic model ids —
    ``claude-sonnet-4-6``, ``claude-haiku-4-5-20251001`` — NOT the
    ``anthropic/...`` LiteLLM-style prefix. Set the ``ANTHROPIC_API_KEY``
    env var the way the official SDK expects."""

    name = "native_anthropic"
    version = "0.0.1"

    def __init__(self, *, client: anthropic.AsyncAnthropic | None = None) -> None:
        """``client`` exists for tests — pass a mock that exposes the
        ``messages.create`` / ``messages.stream`` shape. Production
        leaves it ``None`` and we construct from env vars."""
        if client is not None:
            self._client = client
        else:
            try:
                import anthropic as _anthropic  # noqa: PLC0415
            except ImportError as exc:
                raise ImportError(
                    "the 'anthropic' package is required for runtime: native_anthropic. "
                    "Install with: uv add 'movate-cli[anthropic]'"
                ) from exc
            self._client = _anthropic.AsyncAnthropic()

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        try:
            # Translate the executor's OpenAI-style history into the
            # shape the Anthropic SDK expects: system as a separate
            # kwarg, plus messages with content-block arrays for
            # ``tool_use`` / ``tool_result`` turns.
            system, user_messages = _translate_messages(request.messages)
            extra_kwargs: dict[str, Any] = {}
            if request.tools:
                # to_tool_spec already produced Anthropic-shaped specs
                # (flat ``{name, description, input_schema}``). Pass
                # through unchanged.
                extra_kwargs["tools"] = request.tools
            # mypy: the SDK declares ``messages`` as ``Iterable[MessageParam]``
            # (a TypedDict). Our user_messages are runtime-typed dicts with
            # the same keys — the SDK accepts them but the strict type
            # check rejects without an explicit cast.
            resp = await self._client.messages.create(
                model=request.provider,
                messages=user_messages,
                system=system,
                **extra_kwargs,
                **_translate_params(request.params),
            )
        except Exception as exc:
            _translate_exception(exc)
            raise  # _translate_exception always raises; satisfies mypy

        return _to_completion_response(resp)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        """Stream events from ``messages.stream``.

        The SDK yields a stream of event objects (text_delta,
        message_start, message_delta with usage, etc.). We project
        each event to a :class:`StreamChunk` with text deltas inline
        and the final usage on the closing chunk — same contract as
        :class:`LiteLLMProvider`."""
        try:
            system, user_messages = _translate_messages(request.messages)
            async with self._client.messages.stream(
                model=request.provider,
                messages=user_messages,
                system=system,
                **_translate_params(request.params),
            ) as event_stream:
                async for event in event_stream:
                    chunk = _stream_chunk_from_event(event)
                    if chunk is not None:
                        yield chunk
                # Final message carries the accumulated usage.
                final_message = await event_stream.get_final_message()
                yield StreamChunk(text="", tokens=_tokens_from_usage(final_message.usage))
        except Exception as exc:
            _translate_exception(exc)
            raise

    async def embed(self, text: str, *, model: str) -> list[float]:  # pragma: no cover
        raise NotImplementedError(
            "Anthropic doesn't currently offer first-party embeddings; "
            "use a separate embedding provider (Voyage AI is the recommended companion)."
        )

    def pricing_key(self, provider: str) -> str:
        """Native-Anthropic agents declare bare model ids (``claude-sonnet-4-6``)
        in ``agent.yaml: model.provider``, but ``pricing.yaml`` is keyed by
        LiteLLM-style strings (``anthropic/claude-sonnet-4-6``). Prepend
        the family prefix to bridge — same model id, two naming
        conventions."""
        if provider.startswith("anthropic/"):
            return provider
        return f"anthropic/{provider}"

    def to_tool_spec(self, skill: SkillBundle) -> dict[str, Any]:
        """Convert a skill to Anthropic's flat tool-spec shape.

        Anthropic's API takes ``[{name, description, input_schema}]``
        directly — NOT the nested ``{type: "function", function: {...}}``
        wrapper OpenAI uses. The base provider's default emits the
        OpenAI shape; we override here so the executor can pass the
        ``request.tools`` straight through to ``messages.create``.

        ADR 002 D1 — loop ownership lives in the executor. This method
        is a pure shape translation.
        """
        return {
            "name": skill.spec.name,
            "description": skill.spec.description or skill.spec.name,
            "input_schema": skill.input_schema,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _translate_messages(
    messages: list[Message],
) -> tuple[str, list[dict[str, Any]]]:
    """Translate the executor's OpenAI-style history to Anthropic's shape.

    The executor builds messages in OpenAI's wire format:

    * ``role="system"`` plain text
    * ``role="user"`` / ``role="assistant"`` plain text
    * ``role="assistant"`` with ``tool_calls=[{id, type:"function", function:{name, arguments}}]``
    * ``role="tool"`` with ``content=<tool result string>`` and
      ``tool_call_id=<id>``

    Anthropic expects:

    * ``system=`` separate kwarg
    * Messages with role only ``user`` or ``assistant``
    * Assistant tool calls as ``tool_use`` content blocks within the
      assistant message: ``[{type:"tool_use", id, name, input}]``
    * Tool results as ``tool_result`` content blocks within a
      ``user`` message: ``[{type:"tool_result", tool_use_id, content}]``
    * Consecutive tool results coalesce into a single user message
      with multiple ``tool_result`` blocks (Anthropic rejects
      back-to-back user messages).

    Returns ``(system_text, anthropic_messages)``.
    """
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []

    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
            continue

        if m.role == "tool":
            # Coalesce into the trailing user message if there is one,
            # else open a new one. The trailing message is a "user"
            # message we built earlier for this exact purpose.
            block = {
                "type": "tool_result",
                "tool_use_id": m.tool_call_id or "",
                "content": m.content,
            }
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue

        if m.role == "assistant" and m.tool_calls:
            blocks: list[dict[str, Any]] = []
            if m.content:
                # Some models emit a short text prelude before the
                # tool call ("Let me check..."). Preserve it as a
                # text block so the conversation reads correctly on
                # subsequent turns.
                blocks.append({"type": "text", "text": m.content})
            for call in m.tool_calls:
                fn = call.get("function", {}) or {}
                # ``arguments`` arrives as a JSON-encoded string in
                # the OpenAI shape. Anthropic wants the parsed dict
                # under ``input``. A malformed string would be a
                # bug in the upstream generator — pass through an
                # empty dict so the SDK validates and the model can
                # recover, rather than crashing here.
                try:
                    parsed_input = json.loads(fn.get("arguments", "") or "{}")
                except (TypeError, ValueError):
                    parsed_input = {}
                if not isinstance(parsed_input, dict):
                    parsed_input = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": parsed_input,
                    }
                )
            out.append({"role": "assistant", "content": blocks})
            continue

        # Plain user / assistant text — pass through as a single-block
        # string content. Anthropic accepts both string and list
        # content; we use string here to keep the wire payload
        # minimal for the common case.
        out.append({"role": m.role, "content": m.content})

    return ("\n\n".join(system_parts) if system_parts else ""), out


def _translate_params(params: dict[str, Any]) -> dict[str, Any]:
    """Anthropic requires ``max_tokens``. If the user didn't set it,
    pick a sane default — matches LiteLLM's default for Anthropic
    models. Everything else passes through unchanged."""
    out = dict(params)
    out.setdefault("max_tokens", 4096)
    return out


def _to_completion_response(resp: Any) -> CompletionResponse:
    """Convert ``messages.create`` response → :class:`CompletionResponse`.

    Walks the response's content blocks in order. The shape we surface
    depends on which blocks appear:

    * Any ``tool_use`` block → :class:`CompletionResponse` with
      ``kind="tool_use"`` carrying the first such block's name + id +
      input. Any text preceding the tool_use is preserved in ``.text``
      so the executor can log the model's reasoning. Multiple parallel
      tool_use blocks aren't supported (matches the LiteLLM PR 1
      decision); we take the first and ignore the rest. Follow-up to
      support parallel tool calls lands when the executor's tool-use
      loop gains a multi-dispatch path.
    * Otherwise → ``kind="final"`` with concatenated text from text
      blocks (covers the rare multi-block answer case cleanly).

    Thinking blocks are still deferred (separate API surface).
    """
    text_parts: list[str] = []
    tool_use_blocks: list[ToolCallSpec] = []

    for block in getattr(resp, "content", []) or []:
        block_type = getattr(block, "type", "")
        if block_type == "text":
            text_parts.append(getattr(block, "text", "") or "")
        elif block_type == "tool_use":
            # Collect ALL tool_use blocks — Anthropic can emit multiple
            # in one turn (parallel tool-use). The executor dispatches
            # them concurrently via asyncio.gather.
            name = getattr(block, "name", "") or ""
            bid = getattr(block, "id", "") or ""
            raw_input = getattr(block, "input", None)
            inp = raw_input if isinstance(raw_input, dict) else {}
            tool_use_blocks.append(ToolCallSpec(name=name, call_id=bid, input=inp))

    tokens = _tokens_from_usage(getattr(resp, "usage", None))
    raw = {
        "anthropic_model": getattr(resp, "model", ""),
        "stop_reason": getattr(resp, "stop_reason", ""),
    }
    text = "".join(text_parts)

    if tool_use_blocks:
        first = tool_use_blocks[0]
        return CompletionResponse(
            text=text,
            tokens=tokens,
            raw=raw,
            kind="tool_use",
            tool_name=first.name,
            tool_id=first.call_id,
            tool_input=first.input,
            parallel_tool_calls=tool_use_blocks,
        )

    return CompletionResponse(text=text, tokens=tokens, raw=raw)


def _stream_chunk_from_event(event: Any) -> StreamChunk | None:
    """Project one SDK event to a :class:`StreamChunk`.

    Returns ``None`` for events we don't surface (message_start,
    content_block_start without text, etc.) — the iterator filters
    these out rather than yielding empty chunks."""
    event_type = getattr(event, "type", "")
    if event_type == "content_block_delta":
        delta = getattr(event, "delta", None)
        if delta is not None and getattr(delta, "type", "") == "text_delta":
            return StreamChunk(text=getattr(delta, "text", "") or "")
    return None


def _tokens_from_usage(usage: Any) -> TokenUsage:
    """Convert SDK ``usage`` → :class:`TokenUsage`.

    Anthropic returns ``input_tokens`` + ``output_tokens`` (+ optional
    ``cache_creation_input_tokens`` / ``cache_read_input_tokens`` for
    prompt caching). We map cache reads to ``cached_input`` to match
    the OpenAI convention; cache writes don't have a counterpart yet
    in our :class:`TokenUsage` model — that's tracked but not surfaced."""
    if usage is None:
        return TokenUsage()
    return TokenUsage(
        input=int(getattr(usage, "input_tokens", 0) or 0),
        output=int(getattr(usage, "output_tokens", 0) or 0),
        cached_input=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
    )


def _translate_exception(exc: Exception) -> None:
    """Map ``anthropic.*`` exceptions to ``MovateError`` subclasses so
    the executor's retry/fallback layer sees a single taxonomy.

    Always raises; never returns normally (mypy doesn't model this so
    callers also need ``raise`` after the call). Doesn't import
    ``anthropic`` at module scope so the rest of movate works when
    the extra isn't installed — we check the class name string
    instead."""
    cls = type(exc).__name__
    msg = str(exc)
    if cls in {"AuthenticationError", "PermissionDeniedError"}:
        raise AuthError(msg) from exc
    if cls == "RateLimitError":
        retry_after = _extract_retry_after(exc)
        raise RateLimitError(msg, retry_after=retry_after) from exc
    if cls == "APITimeoutError":
        raise MovateTimeoutError(msg) from exc
    if cls == "BadRequestError":
        low = msg.lower()
        if "context" in low and ("length" in low or "window" in low):
            raise ContextLengthError(msg) from exc
        if "content" in low and ("policy" in low or "filter" in low):
            raise ContentFilterError(msg) from exc
        raise SchemaError(msg) from exc
    if cls in {"APIConnectionError", "InternalServerError", "NotFoundError"}:
        raise ModelUnavailableError(msg) from exc
    # Unknown anthropic error — surface as ModelUnavailable so it's at
    # least retryable, but log the class name so operators can map it.
    raise ModelUnavailableError(f"unmapped anthropic.{cls}: {msg}") from exc


def _extract_retry_after(exc: Exception) -> float | None:
    """Anthropic surfaces retry-after in the ``response.headers``."""
    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None) or {}
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
