"""Tracing layer: pluggable Tracer interface, env-driven selection.

Selection precedence (lazy — optional deps only import when actually
needed; tracing must never break a run):

* ``MOVATE_TRACER=stdout`` → :class:`StdoutTracer` (debug / CI override).
* ``MOVATE_TRACER=langfuse`` → :class:`LangfuseTracer` (or silent if
  package/keys unusable).
* ``MOVATE_TRACER=otel`` → :class:`OtelTracer` (or silent if
  package/endpoint unusable).
* ``MOVATE_TRACER=composite`` → fan out to every configured backend; if
  none usable, silent.
* Auto (env unset):
  - both ``LANGFUSE_SECRET_KEY`` AND ``OTEL_EXPORTER_OTLP_ENDPOINT`` set →
    :class:`CompositeTracer` over both.
  - only ``LANGFUSE_SECRET_KEY`` set → :class:`LangfuseTracer`.
  - only ``OTEL_EXPORTER_OTLP_ENDPOINT`` set → :class:`OtelTracer`.
  - neither → :class:`SilentTracer` (no output; set ``MOVATE_TRACER=stdout``
    to see JSON spans on stderr).

Every backend fallback emits a single line on stderr explaining why so a
production misconfig is debuggable from the logs.
"""

from __future__ import annotations

import os
import sys

from movate.tracing.base import SpanCtx, Tracer
from movate.tracing.composite import CompositeTracer
from movate.tracing.null import SilentTracer
from movate.tracing.stdout import StdoutTracer

__all__ = [
    "CompositeTracer",
    "SilentTracer",
    "SpanCtx",
    "StdoutTracer",
    "Tracer",
    "build_tracer",
]


def build_tracer() -> Tracer:
    """Auto-select a Tracer based on env vars."""
    explicit = os.environ.get("MOVATE_TRACER", "").strip().lower()

    if explicit == "stdout":
        return StdoutTracer(stream=sys.stderr)

    if explicit == "composite":
        return _build_composite_or_fallback(explicit_request=True)

    if explicit == "langfuse":
        return _build_langfuse_or_fallback()

    if explicit == "otel":
        return _build_otel_or_fallback()

    # Auto-detect: both / one / neither configured.
    has_lf = bool(os.environ.get("LANGFUSE_SECRET_KEY", "").strip())
    has_otel = bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip())
    if has_lf and has_otel:
        return _build_composite_or_fallback(explicit_request=False)
    if has_lf:
        return _build_langfuse_or_fallback()
    if has_otel:
        return _build_otel_or_fallback()

    return SilentTracer()


# ---------------------------------------------------------------------------
# Per-backend builders — all fail-soft, all log a single stderr line on miss
# ---------------------------------------------------------------------------


def _build_langfuse_or_fallback() -> Tracer:
    tracer = _try_build_langfuse()
    return tracer if tracer is not None else StdoutTracer(stream=sys.stderr)


def _build_otel_or_fallback() -> Tracer:
    tracer = _try_build_otel()
    return tracer if tracer is not None else StdoutTracer(stream=sys.stderr)


def _build_composite_or_fallback(*, explicit_request: bool) -> Tracer:
    """Build a composite over whatever backends are usable.

    If only one backend works, return it directly (no need to wrap a
    single tracer). If none work, fall back to silent.
    """
    delegates: list[Tracer] = []
    lf = _try_build_langfuse()
    if lf is not None:
        delegates.append(lf)
    ot = _try_build_otel()
    if ot is not None:
        delegates.append(ot)

    if not delegates:
        if explicit_request:
            sys.stderr.write(
                "[movate] composite tracer: no usable backends, falling back to stdout\n"
            )
        return StdoutTracer(stream=sys.stderr)
    if len(delegates) == 1:
        return delegates[0]
    return CompositeTracer(delegates)


def _try_build_langfuse() -> Tracer | None:
    try:
        from movate.tracing.langfuse import (  # noqa: PLC0415 - lazy by design
            LangfuseTracer,
            LangfuseUnavailableError,
        )

        try:
            return LangfuseTracer()
        except LangfuseUnavailableError as exc:
            sys.stderr.write(f"[movate] Langfuse unavailable, skipping: {exc}\n")
            return None
    except ImportError as exc:  # pragma: no cover - tracer module has no deps
        sys.stderr.write(f"[movate] Langfuse tracer module failed to import: {exc}\n")
        return None


def _try_build_otel() -> Tracer | None:
    try:
        from movate.tracing.otel import (  # noqa: PLC0415 - lazy by design
            OtelTracer,
            OtelUnavailableError,
        )

        try:
            return OtelTracer()
        except OtelUnavailableError as exc:
            sys.stderr.write(f"[movate] OTel unavailable, skipping: {exc}\n")
            return None
    except ImportError as exc:  # pragma: no cover - tracer module has no deps
        sys.stderr.write(f"[movate] OTel tracer module failed to import: {exc}\n")
        return None
