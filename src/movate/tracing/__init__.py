"""Tracing layer: pluggable Tracer interface, env-driven selection.

Two selectors, in precedence order:

1. ``MOVATE_TRACE_SINK`` (ADR 015) — the **deployment** sink selector. When
   set, it wins and the sink is treated as *explicitly requested* (a missing
   SDK is a hard, actionable error, not a silent fallback — the operator chose
   this sink, so a misconfig should be loud). Values:

   * ``none``     → :class:`SilentTracer` (off; trace data goes nowhere).
   * ``langfuse`` → :class:`LangfuseTracer` (rich LLM UI; self-hosted or Cloud).
   * ``otlp``     → :class:`OtelTracer` over a generic OTLP exporter — points
     at any OTLP backend: **Azure Monitor / Application Insights** (in-tenant,
     ADR 015 D3), Grafana Tempo, SigNoz, Honeycomb, etc. Configured by the
     standard OTel env vars (``OTEL_EXPORTER_OTLP_ENDPOINT`` /
     ``OTEL_EXPORTER_OTLP_HEADERS`` / ``OTEL_EXPORTER_OTLP_PROTOCOL``).
   * ``both``     → :class:`CompositeTracer` fanning out to Langfuse **and**
     OTLP (LLM UI + ops APM at once).

2. ``MOVATE_TRACER`` + auto-detect (legacy, **byte-for-byte unchanged**) —
   used only when ``MOVATE_TRACE_SINK`` is **unset**:

   * ``MOVATE_TRACER=stdout`` → :class:`StdoutTracer` (debug / CI override).
   * ``MOVATE_TRACER=langfuse`` → :class:`LangfuseTracer` (or silent if
     package/keys unusable).
   * ``MOVATE_TRACER=otel`` → :class:`OtelTracer` (or silent if
     package/endpoint unusable).
   * ``MOVATE_TRACER=composite`` → fan out to every configured backend; if
     none usable, silent.
   * Auto (``MOVATE_TRACER`` also unset):
     - both ``LANGFUSE_SECRET_KEY`` AND ``OTEL_EXPORTER_OTLP_ENDPOINT`` set →
       :class:`CompositeTracer` over both.
     - only ``LANGFUSE_SECRET_KEY`` set → :class:`LangfuseTracer`.
     - only ``OTEL_EXPORTER_OTLP_ENDPOINT`` set → :class:`OtelTracer`.
     - neither → :class:`SilentTracer` (no output; set ``MOVATE_TRACER=stdout``
       to see JSON spans on stderr).

Every backend fallback in the legacy path emits a single line on stderr
explaining why so a production misconfig is debuggable from the logs. In the
``MOVATE_TRACE_SINK`` path, a missing optional dependency raises
:class:`TraceSinkError` so the deployment fails fast with an install hint.
"""

from __future__ import annotations

import os
import sys

from movate.tracing.audit import record_audit_event
from movate.tracing.base import SpanCtx, Tracer
from movate.tracing.composite import CompositeTracer
from movate.tracing.log_correlation import (
    TraceContextFilter,
    TraceContextFormatter,
    install_log_correlation,
)
from movate.tracing.metrics import (
    METRIC_NAMES,
    dec_in_flight,
    dec_sse_connections,
    inc_in_flight,
    inc_sse_connections,
    init_metrics,
    record_job_completed,
    record_run_usage,
    record_voice_turn,
    record_workflow_completed,
    record_workflow_duration,
    register_pool_metrics,
)
from movate.tracing.null import SilentTracer
from movate.tracing.propagation import (
    attach_trace_context,
    continue_trace_context,
    detach_trace_context,
    inject_current_trace_context,
)
from movate.tracing.stdout import StdoutTracer

# Track which backend warning messages have already been emitted this
# process so multi-case eval runs don't repeat the same "Langfuse
# unavailable" line once per case.
_warned: set[str] = set()

__all__ = [
    "METRIC_NAMES",
    "CompositeTracer",
    "SilentTracer",
    "SpanCtx",
    "StdoutTracer",
    "TraceContextFilter",
    "TraceContextFormatter",
    "TraceSinkError",
    "Tracer",
    "attach_trace_context",
    "build_tracer",
    "continue_trace_context",
    "dec_in_flight",
    "dec_sse_connections",
    "detach_trace_context",
    "inc_in_flight",
    "inc_sse_connections",
    "init_metrics",
    "inject_current_trace_context",
    "install_log_correlation",
    "record_audit_event",
    "record_job_completed",
    "record_run_usage",
    "record_voice_turn",
    "record_workflow_completed",
    "record_workflow_duration",
    "register_pool_metrics",
]

# Valid values for the ADR-015 deployment sink selector.
_VALID_SINKS = ("none", "langfuse", "langsmith", "otlp", "both")


class TraceSinkError(Exception):
    """An explicitly-requested ``MOVATE_TRACE_SINK`` could not be built.

    Raised (not swallowed) when the operator selects a sink via
    ``MOVATE_TRACE_SINK`` but its optional dependency is missing or its
    config is unusable. Unlike the legacy ``MOVATE_TRACER`` auto-detect path
    (which fails soft to silent/stdout so tracing never breaks a run), an
    *explicit deployment choice* should fail loudly with an actionable hint.
    """


def build_tracer() -> Tracer:
    """Select a Tracer from the environment.

    The deployment sink selector (ADR 015) wins when set: ``MDK_TRACE_SINK``
    is canonical, ``MOVATE_TRACE_SINK`` the deprecated alias (MDK rename). We
    read ``MDK_`` first and fall back to ``MOVATE_`` directly here — NOT relying
    on the ``sync_env_aliases`` bridge — because the tracer can be built before
    that shim runs on some entry paths, which silently dropped a deployed
    ``MDK_TRACE_SINK`` to the legacy ``MOVATE_TRACER`` auto-detect (it sent traces
    to stdout instead of Langfuse — observed 2026-06-08, #757). When neither is
    set, the legacy ``MOVATE_TRACER`` auto-detect path runs, byte-for-byte
    unchanged.
    """
    sink = (
        (os.environ.get("MDK_TRACE_SINK") or os.environ.get("MOVATE_TRACE_SINK") or "")
        .strip()
        .lower()
    )
    if sink:
        return _build_from_sink(sink)

    return _build_legacy()


# ---------------------------------------------------------------------------
# MOVATE_TRACE_SINK — deployment sink selector (ADR 015). Explicit choice →
# fail loud on a missing dep so a misconfigured deploy is obvious.
# ---------------------------------------------------------------------------


def _build_from_sink(sink: str) -> Tracer:
    if sink not in _VALID_SINKS:
        raise TraceSinkError(
            f"MDK_TRACE_SINK={sink!r} is not recognized; valid values: {', '.join(_VALID_SINKS)}"
        )

    if sink == "none":
        return SilentTracer()

    if sink == "langfuse":
        return _require_langfuse()

    if sink == "langsmith":
        return _require_langsmith()

    if sink == "otlp":
        return _require_otel()

    # sink == "both": fan out to Langfuse + OTLP. Both are required — a
    # half-configured dual sink should fail loud rather than silently drop one.
    return CompositeTracer([_require_langfuse(), _require_otel()])


def _require_langfuse() -> Tracer:
    """Build the Langfuse tracer or raise an actionable :class:`TraceSinkError`."""
    try:
        from movate.tracing.langfuse import (  # noqa: PLC0415 - lazy by design
            LangfuseTracer,
            LangfuseUnavailableError,
        )
    except ImportError as exc:  # pragma: no cover - tracer module has no deps
        raise TraceSinkError(f"langfuse tracer module failed to import: {exc}") from exc
    try:
        return LangfuseTracer()
    except LangfuseUnavailableError as exc:
        raise TraceSinkError(
            "MOVATE_TRACE_SINK=langfuse but Langfuse is unavailable: "
            f"{exc}. Install with `uv tool install --reinstall movate-cli "
            "--extra langfuse` and set LANGFUSE_SECRET_KEY / LANGFUSE_PUBLIC_KEY."
        ) from exc


def _require_langsmith() -> Tracer:
    """Build the LangSmith tracer or raise an actionable :class:`TraceSinkError`."""
    try:
        from movate.integrations.langsmith_tracer import (  # noqa: PLC0415 - lazy by design
            LangSmithTracer,
            LangSmithUnavailableError,
        )
    except ImportError as exc:  # pragma: no cover - tracer module has no deps
        raise TraceSinkError(f"langsmith tracer module failed to import: {exc}") from exc
    try:
        return LangSmithTracer()
    except LangSmithUnavailableError as exc:
        raise TraceSinkError(
            "MOVATE_TRACE_SINK=langsmith but LangSmith is unavailable: "
            f"{exc}. Install with `uv sync --extra langchain` "
            "and set LANGSMITH_API_KEY."
        ) from exc


def _require_otel() -> Tracer:
    """Build the OTLP tracer or raise an actionable :class:`TraceSinkError`.

    The generic OTLP exporter is configured via the standard OTel env vars
    (``OTEL_EXPORTER_OTLP_ENDPOINT`` / ``OTEL_EXPORTER_OTLP_HEADERS`` /
    ``OTEL_EXPORTER_OTLP_PROTOCOL``). For Azure Monitor / Application Insights,
    point ``OTEL_EXPORTER_OTLP_ENDPOINT`` at the App Insights OTLP ingestion
    endpoint and supply the auth header via ``OTEL_EXPORTER_OTLP_HEADERS`` —
    no Azure-specific SDK required (ADR 001 portability).
    """
    try:
        from movate.tracing.otel import (  # noqa: PLC0415 - lazy by design
            OtelTracer,
            OtelUnavailableError,
        )
    except ImportError as exc:  # pragma: no cover - tracer module has no deps
        raise TraceSinkError(f"otel tracer module failed to import: {exc}") from exc
    try:
        return OtelTracer()
    except OtelUnavailableError as exc:
        raise TraceSinkError(
            "MOVATE_TRACE_SINK=otlp but the OTLP exporter is unavailable: "
            f"{exc}. Install the OTel extra (`uv sync --extra otel` / "
            "`pip install 'mdk[otel]'`) and set OTEL_EXPORTER_OTLP_ENDPOINT "
            "(e.g. your Azure Monitor / App Insights OTLP ingestion endpoint)."
        ) from exc


# ---------------------------------------------------------------------------
# Legacy MOVATE_TRACER + auto-detect path — preserved byte-for-byte for
# backward compatibility (unchanged when MOVATE_TRACE_SINK is unset).
# ---------------------------------------------------------------------------


def _build_legacy() -> Tracer:
    """Auto-select a Tracer based on env vars (legacy ``MOVATE_TRACER`` path)."""
    explicit = os.environ.get("MOVATE_TRACER", "").strip().lower()

    if explicit == "stdout":
        return StdoutTracer(stream=sys.stderr)

    if explicit == "composite":
        return _build_composite_or_fallback(explicit_request=True)

    if explicit == "langfuse":
        return _build_langfuse_or_fallback()

    if explicit == "langsmith":
        return _build_langsmith_or_fallback()

    if explicit == "otel":
        return _build_otel_or_fallback()

    # Auto-detect: both / one / neither configured.
    has_lf = bool(os.environ.get("LANGFUSE_SECRET_KEY", "").strip())
    has_otel = bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip())
    has_ls = bool(os.environ.get("LANGSMITH_API_KEY", "").strip())
    if has_lf and has_otel:
        return _build_composite_or_fallback(explicit_request=False)
    if has_lf:
        return _build_langfuse_or_fallback()
    if has_otel:
        return _build_otel_or_fallback()
    if has_ls:
        return _build_langsmith_or_fallback()

    return SilentTracer()


# ---------------------------------------------------------------------------
# Per-backend builders — all fail-soft, all log a single stderr line on miss
# ---------------------------------------------------------------------------


def _build_langfuse_or_fallback() -> Tracer:
    tracer = _try_build_langfuse()
    if tracer is not None:
        return tracer
    # Langfuse was configured (keys present) but unavailable — the one-time
    # warning from _try_build_langfuse already told the operator what to fix.
    # Fall back to SilentTracer, NOT StdoutTracer: the operator asked for
    # Langfuse, not a flood of JSON spans interleaved with progress bars.
    # Use MOVATE_TRACER=stdout explicitly if you want span output.
    return SilentTracer()


def _build_langsmith_or_fallback() -> Tracer:
    tracer = _try_build_langsmith()
    if tracer is not None:
        return tracer
    # Same rationale as Langfuse fallback above.
    return SilentTracer()


def _build_otel_or_fallback() -> Tracer:
    tracer = _try_build_otel()
    if tracer is not None:
        return tracer
    # Same rationale as Langfuse fallback above.
    return SilentTracer()


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
            _warn_once("langfuse", f"[movate] Langfuse unavailable, skipping: {exc}")
            return None
    except ImportError as exc:  # pragma: no cover - tracer module has no deps
        _warn_once("langfuse-import", f"[movate] Langfuse tracer module failed to import: {exc}")
        return None


def _try_build_langsmith() -> Tracer | None:
    try:
        from movate.integrations.langsmith_tracer import (  # noqa: PLC0415 - lazy by design
            LangSmithTracer,
            LangSmithUnavailableError,
        )

        try:
            return LangSmithTracer()
        except LangSmithUnavailableError as exc:
            _warn_once("langsmith", f"[movate] LangSmith unavailable, skipping: {exc}")
            return None
    except ImportError as exc:  # pragma: no cover - tracer module has no deps
        _warn_once("langsmith-import", f"[movate] LangSmith tracer module failed to import: {exc}")
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
            _warn_once("otel", f"[movate] OTel unavailable, skipping: {exc}")
            return None
    except ImportError as exc:  # pragma: no cover - tracer module has no deps
        _warn_once("otel-import", f"[movate] OTel tracer module failed to import: {exc}")
        return None


def _warn_once(key: str, message: str) -> None:
    """Emit ``message`` to stderr at most once per process for ``key``.

    Prevents repeated identical warnings when ``build_tracer()`` is called
    for every agent execution in a multi-case eval run. The first call for
    each ``key`` writes the message; subsequent calls are no-ops.
    """
    if key not in _warned:
        _warned.add(key)
        sys.stderr.write(message + "\n")
