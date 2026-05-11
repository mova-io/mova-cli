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


class LiteLLMProvider(BaseLLMProvider):
    name = "litellm"
    version = "0.0.1"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        try:
            resp = await litellm.acompletion(
                model=request.provider,
                messages=[m.model_dump() for m in request.messages],
                num_retries=0,  # movate owns retries
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
                messages=[m.model_dump() for m in request.messages],
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
    """
    choices = getattr(resp, "choices", None) or []
    text = ""
    if choices:
        msg = getattr(choices[0], "message", None)
        if msg is not None:
            text = getattr(msg, "content", "") or ""

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


def _cached_input_tokens(usage: Any) -> int:
    if usage is None:
        return 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None:
        return 0
    return int(getattr(details, "cached_tokens", 0) or 0)
