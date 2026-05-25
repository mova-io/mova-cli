"""Log ⇄ trace correlation: stamp the active OTel trace context onto logs (item 38).

The runtime emits two telemetry streams that, on a deployed runtime, land in two
different Azure services: OTel **spans** flow to Application Insights (via the
OTel Collector, items 32/33/41), while structured **logs** flow to Log Analytics
over stdout. Without a shared key you can't pivot from a trace in App Insights to
its correlated log lines in Log Analytics (or back). This module closes that gap
with the standard OTel "logs ⇄ traces correlation" pattern: a logging
:class:`~logging.Filter` that reads the *active* span context and stamps its
``trace_id`` / ``span_id`` onto every :class:`~logging.LogRecord`, plus a
formatter that surfaces the ``trace_id`` in the deployed log line so an operator
can search Log Analytics by it.

This is the tracing layer — the observability *edge*. The OTel trace API is
imported lazily (mirroring :mod:`movate.tracing.propagation` /
:mod:`movate.tracing.audit`) so the module loads even when the optional ``otel``
extra isn't installed. The whole thing is a complete **no-op** when OTel is
absent or no span is active: the filter sets BOTH ids to ``""`` (so a
``%(trace_id)s`` format directive never raises on a missing attribute), NEVER
drops a record (always returns ``True``), and NEVER raises (the OTel read is
wrapped in try/except → empty ids). The formatter leaves a log line byte-for-byte
unchanged when there's no active span, so local interactive CLI logs are
unaffected.
"""

from __future__ import annotations

import logging
from typing import Any

# Import the OTel trace API lazily so this module loads even when the optional
# ``otel`` extra isn't installed (mirrors ``tracing/propagation.py`` /
# ``tracing/audit.py``). When absent, the filter stamps empty ids — a complete
# no-op — rather than raising.
_otel_trace: Any = None
_OTEL_AVAILABLE = False
try:
    import opentelemetry.trace as _otel_trace_module

    _otel_trace = _otel_trace_module
    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - covered by the no-otel no-op tests
    pass


class TraceContextFilter(logging.Filter):
    """Stamp the active OTel trace context onto every :class:`logging.LogRecord`.

    On each record, reads the current span context via
    ``opentelemetry.trace.get_current_span().get_span_context()`` (guarded lazy
    import). When OTel is present AND the context is valid (non-zero trace id),
    sets ``record.trace_id`` to the 32-hex trace id and ``record.span_id`` to the
    16-hex span id — the standard OTel/W3C lower-hex zero-padded forms, which is
    exactly how the ids appear in App Insights so a Log Analytics search by
    ``trace_id`` joins straight back to the trace.

    Otherwise (OTel extra absent, no active/valid span, or the OTel read raised)
    sets BOTH ids to the empty string ``""``. Setting them unconditionally means
    a ``%(trace_id)s`` format directive can never raise on a missing attribute,
    and an empty value cleanly means "no trace to correlate".

    A :class:`logging.Filter` attached to a handler/logger conventionally returns
    a bool to *gate* a record; this filter is used purely to *enrich* records, so
    it ALWAYS returns ``True`` — it must never drop a log line — and never raises.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        trace_id = ""
        span_id = ""
        if _OTEL_AVAILABLE and _otel_trace is not None:
            try:
                ctx = _otel_trace.get_current_span().get_span_context()
                # ``is_valid`` is False for the no-op/invalid span (all-zero ids)
                # returned when nothing is recording, so we don't stamp 32 zeros.
                if ctx is not None and getattr(ctx, "is_valid", False):
                    trace_id = format(ctx.trace_id, "032x")
                    span_id = format(ctx.span_id, "016x")
            except Exception:  # pragma: no cover - enrichment must never break a log
                trace_id = ""
                span_id = ""
        record.trace_id = trace_id
        record.span_id = span_id
        # Enrichment-only filter: never drop a record.
        return True


class TraceContextFormatter(logging.Formatter):
    """A :class:`logging.Formatter` that appends ``trace_id=<id>`` when present.

    Wraps a *delegate* formatter (whatever the handler already had) so the base
    log line is produced exactly as before, then appends a single
    ``" trace_id=<id>"`` suffix ONLY when ``record.trace_id`` is non-empty. With
    no active span the suffix is omitted, so a local interactive CLI log line is
    byte-for-byte identical to the un-wrapped formatter's output — full
    backward compatibility, no change to existing log parsing.

    Surfacing the id in the formatted line (rather than only on the record) is
    what makes it queryable in Log Analytics, where the deployed runtime's
    plain-text stdout lands.
    """

    def __init__(self, delegate: logging.Formatter) -> None:
        # Don't call super().__init__ with a format — we delegate formatting
        # entirely so we inherit the handler's existing format string/style.
        super().__init__()
        self._delegate = delegate

    def format(self, record: logging.LogRecord) -> str:
        base = self._delegate.format(record)
        trace_id = getattr(record, "trace_id", "")
        if trace_id:
            return f"{base} trace_id={trace_id}"
        return base


def install_log_correlation() -> None:
    """Idempotently wire trace-context correlation into the root logger.

    Attaches a single :class:`TraceContextFilter` to the root logger itself (so
    records created before any handler is attached still get stamped) and to each
    of the root logger's handlers (so the ids are present by the time the handler
    filters/formats). Wraps each handler's formatter in a
    :class:`TraceContextFormatter` so a non-empty ``trace_id`` is appended to the
    deployed log line, while local CLI logs (no active span) stay unchanged.

    Idempotent: guarded by inspecting the live handler/logger state, so calling
    it more than once (CLI startup plus a defensive serve/worker call) never
    double-attaches the filter or double-wraps a formatter. Safe to call
    unconditionally — a complete no-op when OTel is absent (the filter just
    stamps empty ids and the formatter appends nothing).
    """
    root = logging.getLogger()

    # Reuse a single filter instance across the root logger and its handlers.
    existing = next((f for f in root.filters if isinstance(f, TraceContextFilter)), None)
    trace_filter = existing if existing is not None else TraceContextFilter()
    if existing is None:
        root.addFilter(trace_filter)

    for handler in root.handlers:
        if not any(isinstance(f, TraceContextFilter) for f in handler.filters):
            handler.addFilter(trace_filter)
        current = handler.formatter
        if not isinstance(current, TraceContextFormatter):
            # ``logging.Formatter()`` with no args mimics the handler's implicit
            # default ("%(message)s") when no formatter was set, matching stdlib.
            delegate = current if current is not None else logging.Formatter()
            handler.setFormatter(TraceContextFormatter(delegate))
