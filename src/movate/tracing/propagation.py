"""W3C TraceContext propagation across the async job queue (ADR 019, item 32).

A job's lifecycle spans two processes: the API pod enqueues a
:class:`~movate.core.models.JobRecord`, and a worker later claims and executes
it. Without propagation those are *two disconnected traces* — you can't see
``submit → queue-wait → claim → execute → result`` as ONE distributed trace.

This module is the **only** place opentelemetry context/propagation is touched.
The carrier (a plain ``dict[str, str]`` of ``traceparent`` / ``tracestate``) is
captured at the enqueue edge, persisted on the job record by the storage layer
(which never imports OTel — it just stores a dict), and re-attached in the
worker so the job's root span nests under the originating trace.

Vendor-neutral by design (ADR 001): this is the **standard W3C TraceContext**
via :func:`opentelemetry.propagate.inject` / ``extract`` — no Azure-specific
code. Every function here is a complete no-op when the OTel API isn't installed
(the ``otel`` extra is off) or no span is active: ``inject`` returns ``{}``,
``attach`` returns ``None``, and the ``continue_trace_context`` contextmanager
does nothing. Propagation must never break enqueue or execution.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

# Import the OTel propagation/context API lazily so this module loads even when
# the optional ``otel`` extra isn't installed (mirrors ``tracing/otel.py``).
# When absent, every helper degrades to a no-op rather than raising.
_otel_propagate: Any = None
_otel_context: Any = None
_OTEL_PROPAGATION_AVAILABLE = False
try:
    import opentelemetry.context as _otel_context_module
    import opentelemetry.propagate as _otel_propagate_module

    _otel_propagate = _otel_propagate_module
    _otel_context = _otel_context_module
    _OTEL_PROPAGATION_AVAILABLE = True
except ImportError:  # pragma: no cover - covered by the no-otel no-op tests
    pass


def inject_current_trace_context() -> dict[str, str]:
    """Capture the active span's W3C trace context as a carrier dict.

    Called at the enqueue edge (the API handler) and stamped onto
    :attr:`JobRecord.trace_context`. Returns the W3C carrier
    (``traceparent`` + optional ``tracestate``) for the currently-active
    span via :func:`opentelemetry.propagate.inject`.

    Returns ``{}`` when the OTel API isn't importable OR no span is active
    (``inject`` writes nothing into the carrier in that case) — so an empty
    dict cleanly means "no parent to propagate". Never raises.
    """
    if not _OTEL_PROPAGATION_AVAILABLE or _otel_propagate is None:
        return {}
    carrier: dict[str, str] = {}
    try:
        _otel_propagate.inject(carrier)
    except Exception:  # pragma: no cover - propagation must never break enqueue
        return {}
    return carrier


def attach_trace_context(carrier: dict[str, str]) -> object | None:
    """Extract ``carrier`` and attach it as the current OTel context.

    Returns an opaque token to pass to :func:`detach_trace_context` (so the
    attach can be unwound), or ``None`` when the carrier is empty / the OTel
    API is absent / attach failed. Prefer the :func:`continue_trace_context`
    contextmanager over calling this directly — it guarantees the token is
    detached even on error, so callers can't leak it. Never raises.
    """
    if (
        not carrier
        or not _OTEL_PROPAGATION_AVAILABLE
        or _otel_propagate is None
        or _otel_context is None
    ):
        return None
    try:
        ctx = _otel_propagate.extract(carrier)
        token: object = _otel_context.attach(ctx)
        return token
    except Exception:  # pragma: no cover - propagation must never break execution
        return None


def detach_trace_context(token: object | None) -> None:
    """Detach a context token returned by :func:`attach_trace_context`.

    No-op on ``None`` (nothing was attached) or when OTel is absent. Never
    raises — a failed detach must not break the worker.
    """
    if token is None or not _OTEL_PROPAGATION_AVAILABLE or _otel_context is None:
        return
    with contextlib.suppress(Exception):  # pragma: no cover - never break execution
        _otel_context.detach(token)


@contextlib.contextmanager
def continue_trace_context(carrier: dict[str, str]) -> Iterator[None]:
    """Continue the originating distributed trace for the duration of the block.

    Attaches the W3C ``carrier`` extracted from a :class:`JobRecord` as the
    current OTel context so spans started inside the ``with`` block (notably
    the executor's top-level ``agent.execute`` / workflow root span, which use
    the ambient context as their implicit parent) nest under the originating
    trace. Detaches on exit — even on exception — so the attach token never
    leaks.

    A complete no-op when ``carrier`` is empty (pre-R2 job, or OTel not active
    at enqueue → the worker starts a fresh root span, today's behavior) or the
    OTel API isn't installed. Safe to call unconditionally.
    """
    token = attach_trace_context(carrier)
    try:
        yield
    finally:
        detach_trace_context(token)
