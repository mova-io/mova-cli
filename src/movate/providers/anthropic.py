"""Native Anthropic adapter — direct ``anthropic`` SDK calls.

This is the v0.6 alternative to :class:`LiteLLMProvider` for agents
that declare ``runtime: native_anthropic`` in ``agent.yaml``. The win:
features LiteLLM doesn't yet surface or surfaces lossily — prompt
caching headers, vision, thinking blocks, MCP-server tool ecosystem,
and the official streaming-event shape.

Scope of this first cut (no tool-use yet)
-----------------------------------------

Tool-use, vision, and thinking blocks are intentionally deferred to
follow-up commits because they each require schema / Message-type
changes that compound the diff. This first cut ships:

* Text-in, text-out :meth:`complete` against ``messages.create``.
* Token-aware :meth:`stream` against ``messages.stream``.
* Exception translation matching :class:`LiteLLMProvider`'s taxonomy
  so the executor's retry + fallback policy treats native and
  LiteLLM failures identically.

Optional install:

    uv add 'movate-cli[anthropic]'

If the ``anthropic`` package isn't installed, constructing this class
raises ``ImportError`` with a pointer to the extra. Callers that
opportunistically register the adapter (see ``cli/_runtime.py``)
catch the ImportError silently — same try/except dance every
optional integration uses.
"""

from __future__ import annotations

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
    StreamChunk,
)

if TYPE_CHECKING:
    import anthropic


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
            # Anthropic separates the system prompt from messages —
            # extract any role=system content from the request and
            # pass it via the ``system=`` kwarg.
            system, user_messages = _split_system(request.messages)
            # mypy: the SDK declares ``messages`` as ``Iterable[MessageParam]``
            # (a TypedDict). Our user_messages are runtime-typed dicts with
            # the same keys — the SDK accepts them but the strict type
            # check rejects without an explicit cast.
            resp = await self._client.messages.create(
                model=request.provider,
                messages=user_messages,  # type: ignore[arg-type]
                system=system,
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
            system, user_messages = _split_system(request.messages)
            async with self._client.messages.stream(
                model=request.provider,
                messages=user_messages,  # type: ignore[arg-type]
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_system(messages: list[Any]) -> tuple[str, list[dict[str, Any]]]:
    """Split off the system message — Anthropic's API takes it as a
    separate kwarg, not as a message with role='system'."""
    system_parts: list[str] = []
    user_messages: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
        else:
            # Anthropic only accepts roles 'user' and 'assistant' in
            # the messages array. Pass through tool roles unchanged so
            # a future tool-use commit can use them; the SDK validates.
            user_messages.append({"role": m.role, "content": m.content})
    return ("\n\n".join(system_parts) if system_parts else ""), user_messages


def _translate_params(params: dict[str, Any]) -> dict[str, Any]:
    """Anthropic requires ``max_tokens``. If the user didn't set it,
    pick a sane default — matches LiteLLM's default for Anthropic
    models. Everything else passes through unchanged."""
    out = dict(params)
    out.setdefault("max_tokens", 4096)
    return out


def _to_completion_response(resp: Any) -> CompletionResponse:
    """Convert ``messages.create`` response → :class:`CompletionResponse`.

    Text comes from the first text content block; tool-use / thinking
    blocks are intentionally ignored in this first cut (they need
    schema changes — follow-ups)."""
    text = ""
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", "") == "text":
            text = getattr(block, "text", "") or ""
            break

    return CompletionResponse(
        text=text,
        tokens=_tokens_from_usage(getattr(resp, "usage", None)),
        raw={
            "anthropic_model": getattr(resp, "model", ""),
            "stop_reason": getattr(resp, "stop_reason", ""),
        },
    )


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
