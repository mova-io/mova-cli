"""Per-request correlation id (ADR 033 D2).

Every inbound HTTP request gets a stable **request id**: either the value
of an inbound ``X-Request-Id`` header (so a caller — or an upstream gateway
— can thread its own id straight through), or a freshly generated UUID when
the header is absent. That id is:

* bound to a :class:`~contextvars.ContextVar` for the duration of the request
  (so any code on the request's task can read it without plumbing it through
  call signatures),
* stamped onto every log record (via :class:`RequestIdFilter`, mirroring ADR
  024's :class:`~movate.tracing.log_correlation.TraceContextFilter`
  enrichment pattern), so log lines correlate to the request,
* echoed back on **every** response header ``X-Request-Id`` — success AND
  error — by :class:`~movate.runtime.hardening.RequestIdMiddleware`, and
* woven into the error envelope's ``error.request_id`` (see
  :mod:`movate.runtime.errors`) so a 4xx/5xx body carries the SAME id as the
  response header.

This module is deliberately dependency-free (stdlib only): the contextvar +
filter are usable from anywhere (execution plane, tracing edge) without
importing FastAPI. The middleware that *sets* the var lives in
``runtime/hardening.py`` next to the other Layer-1 middlewares.
"""

from __future__ import annotations

import contextlib
import logging
from contextvars import ContextVar, Token
from uuid import uuid4

# The canonical inbound/outbound header name. Lower-case lookups are handled
# by Starlette's case-insensitive ``Headers`` — this is the form we *emit*.
REQUEST_ID_HEADER = "X-Request-Id"

# Holds the active request's id for the lifetime of the request task. Default
# ``""`` (not ``None``) so a ``%(request_id)s`` log directive — or the error
# envelope read — never trips over ``None``; an empty value cleanly means
# "no request id in scope" (e.g. a log emitted at startup, outside any
# request, or a unit test exercising a helper directly).
_request_id_var: ContextVar[str] = ContextVar("movate_request_id", default="")


def gen_request_id() -> str:
    """Generate a fresh request id (UUID4 hex, 32 lower-hex chars).

    Hex (not the dashed form) keeps it compact and header-safe while still
    being globally unique; it lines up with the lower-hex style ADR 024 uses
    for trace/span ids."""
    return uuid4().hex


def set_request_id(request_id: str) -> Token[str]:
    """Bind ``request_id`` to the context for this request.

    Returns the :class:`~contextvars.Token` from :meth:`ContextVar.set` so the
    middleware can reset the var when the request finishes — important under a
    TestClient / threaded server where the same OS thread (and its contextvar
    snapshot) is reused across requests, so a stale id mustn't leak into the
    next one."""
    return _request_id_var.set(request_id)


def reset_request_id(token: Token[str]) -> None:
    """Restore the previous context value using the token from
    :func:`set_request_id`. Never raises — a reset failure (e.g. the token is
    from a different context after an unusual task hop) must not break the
    response path."""
    with contextlib.suppress(ValueError, LookupError):  # pragma: no cover - defensive
        _request_id_var.reset(token)


def get_request_id() -> str:
    """Return the active request id, or ``""`` when none is in scope.

    Read by :func:`movate.runtime.errors.http_error` so an error envelope's
    ``error.request_id`` matches the ``X-Request-Id`` response header without
    threading the id through every handler."""
    return _request_id_var.get()


class RequestIdFilter(logging.Filter):
    """Stamp the active request id onto every :class:`logging.LogRecord`.

    Enrichment-only (mirrors :class:`movate.tracing.log_correlation.\
TraceContextFilter`): always sets ``record.request_id`` (to ``""`` when no
    request is in scope, so a ``%(request_id)s`` format directive never raises
    on a missing attribute), NEVER drops a record (always returns ``True``),
    and NEVER raises. Attaching it lets a deployed runtime's logs be searched
    by the same id the client sees on the response, completing the
    client↔log↔trace correlation chain."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.request_id = _request_id_var.get()
        except Exception:  # pragma: no cover - enrichment must never break a log
            record.request_id = ""
        return True


def install_request_id_logging() -> None:
    """Idempotently attach :class:`RequestIdFilter` to the root logger and its
    handlers, so records created anywhere on the request task carry the id.

    Mirrors :func:`movate.tracing.log_correlation.install_log_correlation`:
    guarded by inspecting live filter state so repeated calls (CLI startup
    plus a defensive serve call) never double-attach. Safe to call
    unconditionally; a complete no-op beyond stamping the (possibly empty) id."""
    root = logging.getLogger()
    existing = next((f for f in root.filters if isinstance(f, RequestIdFilter)), None)
    request_filter = existing if existing is not None else RequestIdFilter()
    if existing is None:
        root.addFilter(request_filter)
    for handler in root.handlers:
        if not any(isinstance(f, RequestIdFilter) for f in handler.filters):
            handler.addFilter(request_filter)


__all__ = [
    "REQUEST_ID_HEADER",
    "RequestIdFilter",
    "gen_request_id",
    "get_request_id",
    "install_request_id_logging",
    "reset_request_id",
    "set_request_id",
]
