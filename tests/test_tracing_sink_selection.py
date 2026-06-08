"""``build_tracer`` honors the canonical ``MDK_TRACE_SINK`` (MDK rename, #757).

The deployment sets ``MDK_TRACE_SINK`` (canonical prefix), but ``build_tracer``
historically read only the legacy ``MOVATE_TRACE_SINK`` — so a deployed
``MDK_TRACE_SINK`` silently fell through to the legacy ``MOVATE_TRACER``
auto-detect (traces went to stdout, never Langfuse; observed 2026-06-08). These
pin: ``MDK_TRACE_SINK`` is read, wins over the legacy alias, and the
unknown-value error names the canonical var.
"""

from __future__ import annotations

import pytest

from movate.tracing import SilentTracer, TraceSinkError, build_tracer

# Env that could otherwise steer the tracer; cleared per-test for determinism.
_TRACE_ENV = (
    "MDK_TRACE_SINK",
    "MOVATE_TRACE_SINK",
    "MDK_TRACER",
    "MOVATE_TRACER",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
)


@pytest.fixture(autouse=True)
def _clean_trace_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in _TRACE_ENV:
        monkeypatch.delenv(k, raising=False)


@pytest.mark.unit
def test_build_tracer_honors_mdk_trace_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MDK_TRACE_SINK`` (canonical) is read directly by build_tracer."""
    monkeypatch.setenv("MDK_TRACE_SINK", "none")
    assert isinstance(build_tracer(), SilentTracer)


@pytest.mark.unit
def test_mdk_trace_sink_wins_over_legacy_movate(monkeypatch: pytest.MonkeyPatch) -> None:
    """When both are set, the canonical ``MDK_TRACE_SINK`` wins."""
    monkeypatch.setenv("MDK_TRACE_SINK", "none")
    monkeypatch.setenv("MOVATE_TRACE_SINK", "otlp")  # would need an endpoint; must be ignored
    assert isinstance(build_tracer(), SilentTracer)


@pytest.mark.unit
def test_legacy_movate_trace_sink_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """The legacy ``MOVATE_TRACE_SINK`` alias still selects the sink."""
    monkeypatch.setenv("MOVATE_TRACE_SINK", "none")
    assert isinstance(build_tracer(), SilentTracer)


@pytest.mark.unit
def test_unknown_sink_error_names_mdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognized value fails loud, naming the canonical var."""
    monkeypatch.setenv("MDK_TRACE_SINK", "bogus-sink")
    with pytest.raises(TraceSinkError, match="MDK_TRACE_SINK"):
        build_tracer()
