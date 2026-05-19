"""LiteLLM-backed implementation of :class:`BaseLLMProvider`.

This is the only place in movate that imports LiteLLM. Two important choices:

1. ``num_retries=0`` — movate's :func:`movate.core.retry.run_with_retries`
   owns the retry policy. Letting LiteLLM also retry would compound delays
   and obscure the typed failure taxonomy.

2. Exceptions are translated to :class:`movate.core.failures.MovateError`
   subclasses so the executor can act on a single taxonomy. LiteLLM's
   ``OPENAI_PROXY_*`` style errors are translated by string-sniffing where
   the structured exception class doesn't disambiguate.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, cast

import litellm
from litellm import exceptions as lle

# Re-export so existing imports + tests continue to work. The actual
# filter install happens at ``movate/__init__.py`` import time so the
# filter is on the ``LiteLLM`` logger BEFORE any code path could
# trigger ``import litellm`` (this module is one such path, but not
# the only one).
from movate import _LiteLLMBotocoreNoiseFilter  # noqa: F401
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

log = logging.getLogger(__name__)

# LiteLLM emits a noisy startup log line by default; quiet it.
litellm.suppress_debug_info = True


def reset_logging_worker_for_new_event_loop() -> None:
    """Reset LiteLLM's module-level ``GLOBAL_LOGGING_WORKER`` so the
    next ``asyncio.run`` initializes its queue against the new loop.

    LiteLLM lazily creates an ``asyncio.Queue`` inside
    ``LoggingWorker._ensure_queue`` on the FIRST ``acompletion`` call.
    The queue binds to whichever event loop touched it. When the
    current ``asyncio.run`` closes the loop, the queue is stranded;
    the next ``asyncio.run`` (e.g. wizard preview-gen → scorecard
    scoring) creates a NEW loop and the stranded queue explodes::

        RuntimeError: <Queue at 0x...> is bound to a different event loop

    Historical fix (PR #197 era) consolidated preflight + sweep into
    one ``asyncio.run`` to avoid the bug. But PR #212's wizard
    preview-gen added a SECOND ``asyncio.run`` upstream of the
    scorecard's run — re-introducing the original problem on the
    single-agent generate-then-score path.

    This helper is the smaller, lower-blast-radius alternative to
    rearchitecting the whole wizard into one event loop: call it
    before ``asyncio.run`` to ensure ``GLOBAL_LOGGING_WORKER`` will
    re-initialize its queue against the FRESH loop on first
    ``acompletion``. Idempotent — safe to call when the worker
    hasn't been touched yet (no-op).

    Best-effort: silently catches ImportError + AttributeError if
    LiteLLM's internals change, so a future LiteLLM rev that
    renames the global doesn't break movate.
    """
    try:
        from litellm.litellm_core_utils.logging_worker import (  # noqa: PLC0415
            GLOBAL_LOGGING_WORKER,
        )
    except ImportError:
        return  # LiteLLM unavailable or restructured — no-op.
    try:
        # The worker task is tied to the dead loop too — cancel ref
        # so the next start() rebuilds it. We don't need to await
        # the cancel; the loop is already closed.
        GLOBAL_LOGGING_WORKER._worker_task = None
        GLOBAL_LOGGING_WORKER._queue = None
    except AttributeError:
        # LiteLLM renamed the internals — fail open rather than crash.
        return


class LiteLLMProvider(BaseLLMProvider):
    name = "litellm"
    version = "0.0.1"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        # Build the kwargs first; only include ``tools`` when the agent
        # has skills wired. LiteLLM accepts ``tools=None`` cleanly, but
        # being explicit keeps the request payload minimal in the
        # single-shot case and avoids upstream providers that mishandle
        # an empty tools array.
        extra_kwargs: dict[str, Any] = {}
        if request.tools:
            extra_kwargs["tools"] = request.tools

        try:
            resp = await litellm.acompletion(
                model=request.provider,
                messages=[m.model_dump(exclude_none=True) for m in request.messages],
                num_retries=0,  # movate owns retries
                **extra_kwargs,
                **request.params,
            )
        except lle.AuthenticationError as exc:
            raise AuthError(str(exc)) from exc
        except lle.RateLimitError as exc:
            retry_after = _extract_retry_after(exc)
            raise RateLimitError(str(exc), retry_after=retry_after) from exc
        except lle.Timeout as exc:
            raise MovateTimeoutError(str(exc)) from exc
        except lle.ContextWindowExceededError as exc:
            raise ContextLengthError(str(exc)) from exc
        except lle.ContentPolicyViolationError as exc:
            raise ContentFilterError(str(exc)) from exc
        except lle.BadRequestError as exc:
            msg = str(exc).lower()
            if "context" in msg and "length" in msg:
                raise ContextLengthError(str(exc)) from exc
            if "content" in msg and ("policy" in msg or "filter" in msg):
                raise ContentFilterError(str(exc)) from exc
            raise SchemaError(str(exc)) from exc
        except lle.APIConnectionError as exc:
            raise ModelUnavailableError(str(exc)) from exc
        except lle.ServiceUnavailableError as exc:
            raise ModelUnavailableError(str(exc)) from exc
        except lle.InternalServerError as exc:
            raise ModelUnavailableError(str(exc)) from exc

        return _to_completion_response(resp)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        """Stream a completion via LiteLLM's ``stream=True``.

        We force ``stream_options={"include_usage": True}`` so the
        final chunk carries token totals — without it, cost accounting
        downstream would have to guess. Exception translation matches
        :meth:`complete`: the executor's retry / fallback layer can
        treat one-shot and streaming failures interchangeably."""
        # Merge user params with the streaming-specific options. User
        # params win on conflict, but ``stream`` and ``stream_options``
        # are forced so cost accounting always works.
        params = dict(request.params)
        existing_opts = params.pop("stream_options", None) or {}
        params["stream_options"] = {**existing_opts, "include_usage": True}

        try:
            resp = await litellm.acompletion(
                model=request.provider,
                messages=[m.model_dump(exclude_none=True) for m in request.messages],
                stream=True,
                num_retries=0,  # movate owns retries
                **params,
            )
        except lle.AuthenticationError as exc:
            raise AuthError(str(exc)) from exc
        except lle.RateLimitError as exc:
            retry_after = _extract_retry_after(exc)
            raise RateLimitError(str(exc), retry_after=retry_after) from exc
        except lle.Timeout as exc:
            raise MovateTimeoutError(str(exc)) from exc
        except lle.ContextWindowExceededError as exc:
            raise ContextLengthError(str(exc)) from exc
        except lle.ContentPolicyViolationError as exc:
            raise ContentFilterError(str(exc)) from exc
        except lle.BadRequestError as exc:
            msg = str(exc).lower()
            if "context" in msg and "length" in msg:
                raise ContextLengthError(str(exc)) from exc
            if "content" in msg and ("policy" in msg or "filter" in msg):
                raise ContentFilterError(str(exc)) from exc
            raise SchemaError(str(exc)) from exc
        except lle.APIConnectionError as exc:
            raise ModelUnavailableError(str(exc)) from exc
        except lle.ServiceUnavailableError as exc:
            raise ModelUnavailableError(str(exc)) from exc
        except lle.InternalServerError as exc:
            raise ModelUnavailableError(str(exc)) from exc

        async for chunk in resp:
            yield _stream_chunk_from_litellm(chunk)

    async def embed(self, text: str, *, model: str) -> list[float]:  # pragma: no cover - v0.5
        raise NotImplementedError("embed lands in v0.5 with retrieval")


def _extract_retry_after(exc: Exception) -> float | None:
    """LiteLLM stores retry-after on different attrs across versions."""
    for attr in ("retry_after", "_retry_after"):
        v = getattr(exc, attr, None)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def _to_completion_response(resp: Any) -> CompletionResponse:
    """Convert a LiteLLM ModelResponse to our CompletionResponse.

    Token usage is pulled from ``resp.usage``; LiteLLM's reported cost is
    placed in ``raw['litellm_cost_usd']`` for drift checks against the
    canonical pricing table — never used by the executor for billing.

    Tool-use: if the model emitted a tool call instead of a final
    answer, ``choices[0].message.tool_calls`` is non-empty. We return
    a ``CompletionResponse(kind="tool_use", ...)`` carrying the first
    tool call's name + id + parsed JSON arguments. Multiple parallel
    tool calls in a single turn aren't supported in PR 1 — we take
    the first and ignore the rest (rare in practice; deferred to PR 6).
    """
    choices = getattr(resp, "choices", None) or []
    text = ""
    tool_calls: list[Any] = []
    if choices:
        msg = getattr(choices[0], "message", None)
        if msg is not None:
            text = getattr(msg, "content", "") or ""
            tool_calls = list(getattr(msg, "tool_calls", None) or [])

    usage = getattr(resp, "usage", None)
    tokens = TokenUsage(
        input=int(getattr(usage, "prompt_tokens", 0) or 0),
        output=int(getattr(usage, "completion_tokens", 0) or 0),
        cached_input=int(_cached_input_tokens(usage)),
    )

    raw: dict[str, Any] = {
        "litellm_model": getattr(resp, "model", ""),
    }
    hidden = cast(dict[str, Any] | None, getattr(resp, "_hidden_params", None))
    if hidden:
        cost = hidden.get("response_cost")
        if cost is not None:
            raw["litellm_cost_usd"] = float(cost)

    if tool_calls:
        first = tool_calls[0]
        # LiteLLM normalizes upstream shapes to the OpenAI structure:
        # ``{id, type: "function", function: {name, arguments: str}}``.
        # In practice we see both attribute-access and dict-key access
        # depending on LiteLLM version — handle both. ``arguments`` is
        # a JSON-encoded string; parse it. A malformed JSON argument is
        # the provider's bug — we surface it as an empty dict so the
        # executor's input-schema validator catches the issue with a
        # readable error rather than crashing here.
        function = getattr(first, "function", None)
        if function is None and isinstance(first, dict):
            function = first.get("function") or {}
        if function is None:
            function = {}
        tool_name = _func_field(function, "name")
        args_raw = _func_field(function, "arguments")
        tool_id = getattr(first, "id", "") or (
            first.get("id", "") if isinstance(first, dict) else ""
        )
        try:
            import json  # noqa: PLC0415

            tool_input = json.loads(args_raw) if args_raw else {}
        except (TypeError, ValueError):
            tool_input = {}
        if not isinstance(tool_input, dict):
            tool_input = {}
        return CompletionResponse(
            text=text,
            tokens=tokens,
            raw=raw,
            kind="tool_use",
            tool_name=tool_name,
            tool_id=tool_id,
            tool_input=tool_input,
        )

    return CompletionResponse(text=text, tokens=tokens, raw=raw)


def _stream_chunk_from_litellm(chunk: Any) -> StreamChunk:
    """Convert one LiteLLM stream slice to our :class:`StreamChunk`.

    Two shapes to handle:

    * Mid-stream content delta: ``chunk.choices[0].delta.content`` has
      the new token(s); ``chunk.usage`` is ``None``.
    * Final chunk with usage stats (because we passed
      ``stream_options={"include_usage": True}``): the ``choices``
      may be empty and ``chunk.usage`` carries totals.

    LiteLLM normalises across providers, so we don't peek at the raw
    provider format here."""
    text = ""
    choices = getattr(chunk, "choices", None) or []
    if choices:
        delta = getattr(choices[0], "delta", None)
        if delta is not None:
            text = getattr(delta, "content", "") or ""

    tokens: TokenUsage | None = None
    usage = getattr(chunk, "usage", None)
    if usage is not None:
        tokens = TokenUsage(
            input=int(getattr(usage, "prompt_tokens", 0) or 0),
            output=int(getattr(usage, "completion_tokens", 0) or 0),
            cached_input=int(_cached_input_tokens(usage)),
        )

    return StreamChunk(text=text, tokens=tokens)


def _func_field(function: Any, name: str) -> str:
    """Pull a tool-call function field whether ``function`` is an
    attribute-style object or a plain dict.

    LiteLLM versions differ on this; both shapes appear in upstream
    test fixtures. Returning ``""`` on missing is fine — downstream
    code treats an empty name as "no tool call we can dispatch."
    """
    if isinstance(function, dict):
        v = function.get(name, "")
        return str(v) if v is not None else ""
    v = getattr(function, name, "")
    return str(v) if v is not None else ""


def _cached_input_tokens(usage: Any) -> int:
    if usage is None:
        return 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None:
        return 0
    return int(getattr(details, "cached_tokens", 0) or 0)
