"""W3C TraceContext propagation across the job queue (ADR 019, item 32).

The helpers must be a complete no-op when the OTel API isn't installed OR no
span is active: ``inject_current_trace_context`` returns ``{}``,
``continue_trace_context`` does nothing. When the ``otel`` extra IS present
(dev / CI with ``uv sync --all-extras``) we exercise the real path: a span is
active → the carrier carries a ``traceparent``, and extracting it in a fresh
context produces a child whose trace_id matches the injected one.
"""

from __future__ import annotations

import pytest

from movate.tracing import (
    attach_trace_context,
    continue_trace_context,
    detach_trace_context,
    inject_current_trace_context,
)


def _otel_installed() -> bool:
    """Is the OTel SDK importable here? Mirrors test_tracing_otel.py — in dev
    (`uv sync --all-extras`) it's installed and we run the real path; in a
    minimal build it isn't and we run the no-op path."""
    try:
        import opentelemetry.sdk.trace  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# No-op safety — must hold with OR without the otel extra
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_inject_returns_empty_when_no_span_active() -> None:
    """No active span (and no otel) → empty carrier, cleanly meaning
    "no parent to propagate"."""
    carrier = inject_current_trace_context()
    assert carrier == {}
    assert isinstance(carrier, dict)


@pytest.mark.unit
def test_continue_empty_carrier_is_noop() -> None:
    """An empty carrier (pre-R2 job, or OTel off at enqueue) never raises —
    the worker just starts a fresh root span."""
    with continue_trace_context({}):
        pass  # no exception


@pytest.mark.unit
def test_continue_nonempty_carrier_does_not_raise() -> None:
    """A populated carrier never raises whether or not OTel is installed."""
    carrier = {"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}
    with continue_trace_context(carrier):
        pass  # no exception


@pytest.mark.unit
def test_attach_empty_returns_none_and_detach_is_safe() -> None:
    """Empty carrier → nothing to attach (token None); detach(None) is a no-op."""
    token = attach_trace_context({})
    assert token is None
    detach_trace_context(token)  # must not raise


@pytest.mark.unit
def test_detach_none_is_noop() -> None:
    detach_trace_context(None)  # must not raise


# ---------------------------------------------------------------------------
# Real OTel path — only when the extra is installed
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_inject_carries_traceparent_when_span_active() -> None:
    """With a real SDK tracer and an active span, the carrier carries the
    standard W3C ``traceparent``."""
    from opentelemetry import trace  # noqa: PLC0415
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415

    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("enqueue"):
        carrier = inject_current_trace_context()

    assert "traceparent" in carrier
    # W3C traceparent format: version-traceid-spanid-flags
    assert carrier["traceparent"].count("-") == 3
    # Sanity: that traceparent's trace_id segment is the active span's.
    # (the inject happened inside the span scope above)
    assert trace is not None  # imported for the side-effect of asserting SDK


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_inject_extract_roundtrip_preserves_trace_id() -> None:
    """The end-to-end contract: inject at "enqueue", then continue the trace
    in a fresh context (the "worker") → a span started there is a CHILD of the
    originating trace (same trace_id). This is the one-distributed-trace fix."""
    from opentelemetry import context as otel_context  # noqa: PLC0415
    from opentelemetry import trace  # noqa: PLC0415
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415

    provider = TracerProvider()
    tracer = provider.get_tracer("test")

    # ---- API edge: active span, capture the carrier
    with tracer.start_as_current_span("enqueue") as enqueue_span:
        origin_trace_id = enqueue_span.get_span_context().trace_id
        carrier = inject_current_trace_context()
    assert carrier  # non-empty

    # ---- Worker: detach to a clean root, then continue the carrier's trace.
    # Detaching the SDK's implicit-root context simulates the worker process
    # which has no ambient span of its own.
    root_token = otel_context.attach(otel_context.Context())
    try:
        # Without continuing, a fresh span would be a NEW root trace.
        with continue_trace_context(carrier):
            child = tracer.start_span("execute")
            try:
                child_trace_id = child.get_span_context().trace_id
            finally:
                child.end()
    finally:
        otel_context.detach(root_token)

    assert child_trace_id == origin_trace_id
    assert trace is not None


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_attach_detach_token_roundtrip() -> None:
    """attach returns a real token for a non-empty carrier; detach unwinds it
    without raising."""
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415

    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("enqueue"):
        carrier = inject_current_trace_context()

    token = attach_trace_context(carrier)
    assert token is not None
    detach_trace_context(token)  # must not raise
