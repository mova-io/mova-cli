"""Layer-1 API hardening middlewares (ADR 033 — D2 + D6).

Three cross-cutting, **additive** concerns wrap the runtime app; this module
holds two of them (the third, D3 rate-limit response headers, already lives in
``runtime/errors.py`` + ``runtime/middleware.py`` and rides the existing
per-tenant limiter):

* **D2 — request correlation** (:class:`RequestIdMiddleware`): read/echo a
  stable ``X-Request-Id`` on every response and bind it to the request-id
  context (see :mod:`movate.runtime.request_context`) so logs and the error
  envelope's ``error.request_id`` all carry the SAME id.
* **D6 — payload size limit** (:class:`PayloadSizeLimitMiddleware`): reject an
  over-large request body with the standard ``413`` envelope, naming the
  configured limit, before a handler ever reads it.

Both are :class:`~starlette.middleware.base.BaseHTTPMiddleware` subclasses.
Neither changes any existing response body: D2 only adds a header (and
populates an already-additive envelope field), D6 only adds a NEW rejection
path. Registration order (request-id outermost) is enforced in
``runtime/app.build_app``.
"""

from __future__ import annotations

import logging
import os

from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from movate.runtime.errors import ErrorCode, http_error
from movate.runtime.request_context import (
    REQUEST_ID_HEADER,
    gen_request_id,
    reset_request_id,
    set_request_id,
)

logger = logging.getLogger(__name__)

# D6 — default max request body, in bytes. 25 MiB: comfortably above a normal
# JSON run/eval payload while still capping the big ones (bundle uploads, KB
# ingest) so a single client can't OOM a replica with an unbounded body.
# Operator-overridable via ``MDK_MAX_REQUEST_BYTES`` (an integer count of
# bytes); ``0`` or a non-positive / unparseable value disables the limit.
DEFAULT_MAX_REQUEST_BYTES = 25 * 1024 * 1024
MAX_REQUEST_BYTES_ENV = "MDK_MAX_REQUEST_BYTES"


def resolve_max_request_bytes(explicit: int | None = None) -> int:
    """Resolve the payload ceiling: explicit kwarg > env > default.

    Returns ``0`` to mean "no limit" (so the middleware short-circuits to a
    pure pass-through). A non-positive or unparseable ``MDK_MAX_REQUEST_BYTES``
    is treated as disabled rather than fatal — an operator typo shouldn't wedge
    the runtime; it just turns the guard off (logged once at build)."""
    if explicit is not None:
        return explicit if explicit > 0 else 0
    raw = os.environ.get(MAX_REQUEST_BYTES_ENV)
    if raw is None:
        return DEFAULT_MAX_REQUEST_BYTES
    try:
        parsed = int(raw)
    except ValueError:
        logger.warning("invalid %s=%r — disabling payload size limit", MAX_REQUEST_BYTES_ENV, raw)
        return 0
    return parsed if parsed > 0 else 0


class RequestIdMiddleware(BaseHTTPMiddleware):
    """D2 — bind + echo a per-request correlation id.

    On each request: take the inbound ``X-Request-Id`` (trimmed) if present
    and non-empty, else generate a UUID; bind it to the request-id context for
    the duration of the request (so logs and any error built via
    ``runtime/errors`` carry it); then set ``X-Request-Id`` on the response —
    success AND error alike. Because the context is bound *before* the rest of
    the stack runs, an error envelope's ``error.request_id`` (read from the
    same context in :func:`movate.runtime.errors.http_error`) equals this
    header for the same request.

    Mount this OUTERMOST so it wraps every other middleware (incl. the payload
    guard) — that way even a 413/429/500 carries the id.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        inbound = request.headers.get(REQUEST_ID_HEADER)
        request_id = inbound.strip() if inbound and inbound.strip() else gen_request_id()
        token = set_request_id(request_id)
        # Stash on request.state too so a handler that wants it explicitly
        # (rather than via the contextvar) has a non-magical accessor.
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        finally:
            reset_request_id(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


class PayloadSizeLimitMiddleware:
    """D6 — reject an over-large request body with a ``413`` envelope.

    A **pure-ASGI** middleware (deliberately NOT
    :class:`~starlette.middleware.base.BaseHTTPMiddleware`): it must wrap the
    ``receive`` channel to count bytes *as they stream*, and a
    ``BaseHTTPMiddleware`` that buffers/replays the body is incompatible with
    downstream streaming responses (the SSE run path) — it desyncs Starlette's
    cached-request receive protocol. The pure-ASGI form is stream-safe both
    ways: it never buffers the whole body and never re-injects messages.

    Two guards, cheapest first:

    1. **Declared size** — if ``Content-Length`` is present and exceeds the
       limit, reject immediately without reading a single byte (the common,
       honest-client case).
    2. **Streaming tally** — wrap ``receive`` so each inbound body chunk is
       added to a running total; the first chunk that pushes the total over
       the limit short-circuits with a ``413`` (catches a missing / lying
       ``Content-Length``, e.g. chunked transfer or a hostile client). Chunks
       are passed straight through untouched, so the read is non-destructive.

    The rejection uses the shared error envelope (``413`` / ``payload_too_large``)
    and states the limit in the message. Disabled (``max_bytes <= 0``) → pure
    pass-through, no receive wrapping.

    Only ``http`` scopes are inspected; ``websocket`` / ``lifespan`` pass
    through unchanged.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._max_bytes <= 0:
            await self._app(scope, receive, send)
            return

        # Guard 1: trust a declared Content-Length when present — reject before
        # reading anything. Malformed header → fall through to the tally guard.
        headers = Headers(scope=scope)
        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = -1
            if declared > self._max_bytes:
                await self._send_too_large(scope, receive, send)
                return

        # Guard 2: tally body bytes as they arrive. We buffer chunks while
        # counting, but bail the MOMENT the running total exceeds the cap — so
        # we never hold more than ~``max_bytes`` + one chunk in memory (the
        # whole point is to bound memory, not to permit an unbounded buffer).
        # A body that stays under the cap is forwarded chunk-for-chunk via a
        # replay ``receive`` so the read stays non-destructive.
        total = 0
        buffered: list[Message] = []
        more_body = True
        while more_body:
            message = await receive()
            buffered.append(message)
            if message["type"] != "http.request":
                # http.disconnect (or other) — no body left to count.
                break
            total += len(message.get("body", b""))
            if total > self._max_bytes:
                await self._send_too_large(scope, receive, send)
                return
            more_body = message.get("more_body", False)

        async def _replay_then_receive() -> Message:
            if buffered:
                return buffered.pop(0)
            return await receive()

        await self._app(scope, _replay_then_receive, send)

    async def _send_too_large(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Emit the ``413`` envelope directly over ASGI. Mirrors the FastAPI
        ``HTTPException`` path (``{"detail": {"error": {...}}}``) so the body
        shape is identical to every other error — the request-id middleware
        (outermost) adds ``X-Request-Id`` on the way out, and ``http_error``
        has already stamped the matching ``error.request_id`` from the active
        context."""
        limit_mb = self._max_bytes / (1024 * 1024)
        exc = http_error(
            ErrorCode.PAYLOAD_TOO_LARGE,
            status_code=413,
            message=(
                f"request body too large: limit is {self._max_bytes} bytes (~{limit_mb:.0f} MB)"
            ),
        )
        response = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        await response(scope, receive, send)


__all__ = [
    "DEFAULT_MAX_REQUEST_BYTES",
    "MAX_REQUEST_BYTES_ENV",
    "PayloadSizeLimitMiddleware",
    "RequestIdMiddleware",
    "resolve_max_request_bytes",
]
