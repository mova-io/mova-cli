"""Lyzr Studio HTTP adapter — read-only invocation of Lyzr-hosted agents.

The v0.7 bridge for customers whose existing agents live on Lyzr. MDK
agents that declare ``runtime: lyzr`` in ``agent.yaml`` are invoked via
Lyzr's documented public inference endpoint
(``POST /v3/inference/chat/``). The Lyzr agent ID lives in the
``model.provider`` field as ``lyzr/<agent_id>``.

Why no Lyzr SDK dependency
--------------------------

This adapter speaks Lyzr's HTTP API directly with ``httpx`` (already
a movate dep). That keeps license posture clean: we don't embed any of
Lyzr's source code, just call their documented public endpoint. The
``pyproject.toml`` doesn't grow; the SDK version-pinning headache stays
out; the adapter survives Lyzr SDK refactors.

Scope of this first cut
-----------------------

* Text-in, text-out :meth:`complete` against ``/v3/inference/chat/``.
* Session ID auto-generated per call when not passed via
  ``request.params``; multi-turn callers can pass a stable
  ``session_id`` to preserve Lyzr-side conversation history.
* No streaming (Lyzr's public chat endpoint is request/response).
* Token counts unknown — Lyzr's response doesn't include them.
  Cost tracking via the agent's declared ``model.provider`` field is
  the operator's responsibility for v0.7; we'll surface "Lyzr-billed,
  not movate-billed" in the metrics in a follow-up.

Strategic posture
-----------------

This adapter is a **migration bridge**, not a long-term capability.
Pair with ``mdk import lyzr <agent.json>`` to clone a Lyzr agent into
MDK as a native ``runtime: litellm`` agent. Re-running the eval suite
on both lets the customer prove parity before flipping the runtime.

Required env: ``LYZR_API_KEY`` (per-agent API key from Lyzr Studio).
Optional env: ``LYZR_API_BASE`` (default ``https://agent-prod.studio.lyzr.ai``).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from movate.core.failures import (
    AuthError,
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

_DEFAULT_BASE = "https://agent-prod.studio.lyzr.ai"
_INFERENCE_PATH = "/v3/inference/chat/"

# HTTP status code constants — named so the status-handling branch
# reads as intent ("if response was unauthenticated") rather than as
# magic numbers. Lint rule PLR2004 flags raw HTTP ints; this is the
# canonical fix.
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_BAD_REQUEST = 400
_HTTP_INTERNAL_SERVER_ERROR = 500


class LyzrProvider(BaseLLMProvider):
    """``BaseLLMProvider`` implementation that calls Lyzr Studio's
    inference endpoint via HTTP.

    Provider strings on this runtime are ``lyzr/<agent_id>`` where
    ``<agent_id>`` is the 24-hex Lyzr Studio agent identifier (visible
    in the Lyzr UI and in the agent's exported JSON as ``_id``).
    """

    name = "lyzr"
    version = "0.0.1"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        """``api_key`` defaults to ``$LYZR_API_KEY``; ``base_url`` to
        ``$LYZR_API_BASE`` or the production Lyzr endpoint.

        ``timeout`` covers the full HTTP round-trip including Lyzr's
        internal manager-plus-managed-agents orchestration, which can
        be slow for fan-out — 60s is generous and matches the
        anthropic/openai adapters."""
        self._api_key = api_key or os.environ.get("LYZR_API_KEY")
        self._base_url = (
            base_url
            or os.environ.get("LYZR_API_BASE")
            or _DEFAULT_BASE
        ).rstrip("/")
        self._timeout = timeout
        # We don't fail in __init__ if api_key is missing — the
        # registry constructs adapters opportunistically and a missing
        # LYZR_API_KEY is a normal state when no Lyzr agents are
        # being run. The error surfaces on first .complete() call
        # with a clean AuthError.

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if not self._api_key:
            raise AuthError(
                "LYZR_API_KEY env var is not set. Lyzr-hosted agents "
                "(runtime: lyzr) require this key — copy it from Lyzr "
                "Studio → Agent detail → API Key."
            )

        agent_id = self._parse_agent_id(request.provider)
        message = self._extract_user_message(request)

        # Session ID: callers can pass a stable one for multi-turn;
        # otherwise we generate a fresh one per call so each eval case
        # is independent (no conversation bleeding across runs).
        session_id = request.params.get(
            "session_id"
        ) or f"{agent_id}-{uuid.uuid4().hex[:11]}"
        user_id = request.params.get("user_id") or "mdk-runtime"

        body = {
            "user_id": user_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "message": message,
        }
        headers = {
            "x-api-key": self._api_key,
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}{_INFERENCE_PATH}",
                    headers=headers,
                    json=body,
                )
        except httpx.TimeoutException as exc:
            raise MovateTimeoutError(
                f"Lyzr inference timed out after {self._timeout}s "
                f"(agent_id={agent_id})"
            ) from exc
        except httpx.RequestError as exc:
            raise ModelUnavailableError(
                f"Lyzr API request failed: {exc}"
            ) from exc

        if resp.status_code in {_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN}:
            raise AuthError(
                f"Lyzr returned {resp.status_code} — check LYZR_API_KEY "
                f"is correct and authorized for agent {agent_id!r}"
            )
        if resp.status_code == _HTTP_TOO_MANY_REQUESTS:
            raise RateLimitError(
                f"Lyzr rate-limited ({resp.status_code}); inspect "
                f"Lyzr Studio for the account's request quota",
                retry_after=_parse_retry_after(resp.headers),
            )
        if resp.status_code >= _HTTP_INTERNAL_SERVER_ERROR:
            raise ModelUnavailableError(
                f"Lyzr returned {resp.status_code}: {resp.text[:200]}"
            )
        if resp.status_code >= _HTTP_BAD_REQUEST:
            raise SchemaError(
                f"Lyzr returned {resp.status_code}: {resp.text[:200]}"
            )

        try:
            data: dict[str, Any] = resp.json()
        except ValueError as exc:  # pragma: no cover — defensive
            raise SchemaError(
                f"Lyzr response was not valid JSON: {resp.text[:200]}"
            ) from exc

        # Lyzr's chat endpoint returns text in a "response" key based
        # on observed behavior. We fall back to common alternatives so
        # we're robust to minor API shape changes. If a future Lyzr
        # release adds token counts we'll lift them into TokenUsage.
        text = (
            data.get("response")
            or data.get("message")
            or data.get("output")
            or ""
        )

        return CompletionResponse(
            text=text,
            tokens=TokenUsage(input=0, output=0),
            raw={
                "lyzr_agent_id": agent_id,
                "lyzr_session_id": session_id,
                "lyzr_raw_response": data,
            },
        )

    async def stream(
        self, request: CompletionRequest
    ) -> AsyncIterator[StreamChunk]:  # pragma: no cover — not supported
        raise NotImplementedError(
            "Streaming is not supported on runtime: lyzr — Lyzr's "
            "public inference endpoint is request/response only. "
            "Run with --no-stream or switch the agent to runtime: "
            "litellm (or another native runtime) for streaming."
        )
        # Unreachable yield — makes this an async generator so the
        # method signature matches the BaseLLMProvider Protocol
        # contract (AsyncIterator[StreamChunk], not a coroutine).
        yield

    async def embed(
        self, text: str, *, model: str
    ) -> list[float]:  # pragma: no cover — not supported
        raise NotImplementedError(
            "Embedding is not supported on runtime: lyzr"
        )

    # ----- helpers --------------------------------------------------------

    @staticmethod
    def _parse_agent_id(provider: str) -> str:
        """Extract ``<agent_id>`` from ``provider="lyzr/<agent_id>"``.

        Model validation upstream guarantees the prefix exists, but
        we re-check here so the adapter is robust if called directly
        (e.g. in tests)."""
        if not provider.startswith("lyzr/"):
            raise SchemaError(
                f"Lyzr provider string must be 'lyzr/<agent_id>', got {provider!r}"
            )
        agent_id = provider[len("lyzr/") :]
        if not agent_id:
            raise SchemaError(
                "Lyzr provider string is missing the agent id after 'lyzr/'"
            )
        return agent_id

    @staticmethod
    def _extract_user_message(request: CompletionRequest) -> str:
        """Lyzr takes a single ``message`` string. We use the *last*
        user-role message; system messages are dropped (Lyzr agents
        encode their system prompt internally via ``agent_instructions``
        on the Lyzr side). Multi-turn conversation history isn't
        forwarded — that's Lyzr-side state keyed by ``session_id``."""
        user_messages = [m for m in request.messages if m.role == "user"]
        if not user_messages:
            raise SchemaError(
                "Lyzr requires at least one user-role message; "
                "got messages: "
                f"{[m.role for m in request.messages]}"
            )
        return user_messages[-1].content


def _parse_retry_after(headers: Any) -> float | None:
    """Best-effort parse of a ``Retry-After`` HTTP header value into
    seconds. Returns ``None`` if absent or unparseable so the
    movate retry policy falls back to its exponential default."""
    raw = headers.get("retry-after") if hasattr(headers, "get") else None
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
