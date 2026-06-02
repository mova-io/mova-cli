"""TraceContextFilter + install_log_correlation (item 38, log ⇄ trace correlation).

Behavior-first coverage of the standard OTel "logs ⇄ traces correlation" pattern:

* With an active recording span, a log record carries the matching 32-hex
  ``trace_id`` / 16-hex ``span_id`` and the formatter appends ``trace_id=<id>``.
* No active span (or OTel absent) → empty ids, record NOT dropped, formatter
  output byte-for-byte unchanged (no ``trace_id=`` suffix) so local CLI logs are
  unaffected.
* ``install_log_correlation()`` is idempotent.
* The filter never raises even if the OTel read throws.

The real-span test is gated on OTel availability exactly like
``tests/test_tracing_otel.py`` / ``tests/test_audit_telemetry.py``.
"""

from __future__ import annotations

import logging

import pytest

from movate.tracing import log_correlation
from movate.tracing.log_correlation import (
    TraceContextFilter,
    TraceContextFormatter,
    install_log_correlation,
)


def _otel_installed() -> bool:
    """Is the OTel trace API importable here? Mirrors test_audit_telemetry.py —
    in dev/CI (`uv sync --all-extras`) it's installed and we run the real
    active-span path; in a minimal build it isn't and we run the no-op path."""
    try:
        import opentelemetry.trace  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False


def _make_record(msg: str = "hello") -> logging.LogRecord:
    return logging.LogRecord(
        name="movate.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


# ---------------------------------------------------------------------------
# Filter: no active span / OTel absent → empty ids, record never dropped
# ---------------------------------------------------------------------------


def test_filter_no_active_span_sets_empty_ids_and_keeps_record() -> None:
    """No active span → both ids empty, and the record is NOT dropped."""
    record = _make_record()
    result = TraceContextFilter().filter(record)
    assert result is True  # never drops a log record
    assert record.trace_id == ""
    assert record.span_id == ""


def test_filter_always_sets_both_attributes() -> None:
    """The attributes are ALWAYS set (so a %(trace_id)s directive never raises)."""
    record = _make_record()
    TraceContextFilter().filter(record)
    assert hasattr(record, "trace_id")
    assert hasattr(record, "span_id")


def test_filter_never_raises_when_otel_read_throws(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the OTel read raises, the filter swallows it → empty ids, returns True."""

    class _Boom:
        def get_current_span(self) -> object:
            raise RuntimeError("otel exploded")

    monkeypatch.setattr(log_correlation, "_OTEL_AVAILABLE", True)
    monkeypatch.setattr(log_correlation, "_otel_trace", _Boom())

    record = _make_record()
    result = TraceContextFilter().filter(record)
    assert result is True
    assert record.trace_id == ""
    assert record.span_id == ""


def test_filter_empty_when_span_context_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    """An invalid (all-zero, non-recording) span context → empty ids, not 0-padding."""

    class _InvalidCtx:
        is_valid = False
        trace_id = 0
        span_id = 0

    class _Span:
        def get_span_context(self) -> object:
            return _InvalidCtx()

    class _FakeTrace:
        def get_current_span(self) -> object:
            return _Span()

    monkeypatch.setattr(log_correlation, "_OTEL_AVAILABLE", True)
    monkeypatch.setattr(log_correlation, "_otel_trace", _FakeTrace())

    record = _make_record()
    TraceContextFilter().filter(record)
    assert record.trace_id == ""
    assert record.span_id == ""


def test_filter_stamps_ids_from_valid_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid span context → 32-hex trace_id + 16-hex span_id, zero-padded."""

    class _Ctx:
        is_valid = True
        trace_id = 0xABCDEF0123456789ABCDEF0123456789
        span_id = 0x00000000DEADBEEF

    class _Span:
        def get_span_context(self) -> object:
            return _Ctx()

    class _FakeTrace:
        def get_current_span(self) -> object:
            return _Span()

    monkeypatch.setattr(log_correlation, "_OTEL_AVAILABLE", True)
    monkeypatch.setattr(log_correlation, "_otel_trace", _FakeTrace())

    record = _make_record()
    TraceContextFilter().filter(record)
    assert record.trace_id == "abcdef0123456789abcdef0123456789"
    assert len(record.trace_id) == 32
    assert record.span_id == "00000000deadbeef"
    assert len(record.span_id) == 16


# ---------------------------------------------------------------------------
# Formatter: append trace_id only when non-empty (local CLI logs unchanged)
# ---------------------------------------------------------------------------


def test_formatter_unchanged_when_no_trace_id() -> None:
    """No trace_id → formatter output is byte-for-byte the delegate's output."""
    delegate = logging.Formatter("%(name)s %(levelname)s %(message)s")
    fmt = TraceContextFormatter(delegate)
    record = _make_record("hi there")
    record.trace_id = ""

    expected = delegate.format(record)
    got = fmt.format(record)
    assert got == expected
    assert "trace_id=" not in got


def test_formatter_appends_trace_id_when_present() -> None:
    """Non-empty trace_id → a single ` trace_id=<id>` suffix is appended."""
    delegate = logging.Formatter("%(name)s %(levelname)s %(message)s")
    fmt = TraceContextFormatter(delegate)
    record = _make_record("hi there")
    record.trace_id = "abcdef0123456789abcdef0123456789"

    base = delegate.format(record)
    got = fmt.format(record)
    assert got == f"{base} trace_id=abcdef0123456789abcdef0123456789"


# ---------------------------------------------------------------------------
# install_log_correlation: idempotent wiring onto the root logger + handlers
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_root_logger() -> logging.Logger:
    """A pristine root logger with one stream handler, restored after the test.

    install_log_correlation() mutates the *root* logger's filters/handlers, so
    we snapshot and restore them to keep other tests' logging untouched.
    """
    root = logging.getLogger()
    saved_filters = list(root.filters)
    saved_handlers = list(root.handlers)
    saved_formatters = [(h, h.formatter) for h in root.handlers]

    # Start clean: drop existing handlers/filters, add a single stream handler
    # with a known formatter so assertions are deterministic.
    for f in saved_filters:
        root.removeFilter(f)
    for h in saved_handlers:
        root.removeHandler(h)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    try:
        yield root
    finally:
        for f in list(root.filters):
            root.removeFilter(f)
        for h in list(root.handlers):
            root.removeHandler(h)
        for f in saved_filters:
            root.addFilter(f)
        for h in saved_handlers:
            root.addHandler(h)
        for h, ffmt in saved_formatters:
            h.setFormatter(ffmt)


def test_install_attaches_filter_and_formatter(isolated_root_logger: logging.Logger) -> None:
    """A single install attaches the filter (root + handler) and wraps the formatter."""
    install_log_correlation()
    root = isolated_root_logger

    assert sum(isinstance(f, TraceContextFilter) for f in root.filters) == 1
    handler = root.handlers[0]
    assert sum(isinstance(f, TraceContextFilter) for f in handler.filters) == 1
    assert isinstance(handler.formatter, TraceContextFormatter)


def test_install_is_idempotent(isolated_root_logger: logging.Logger) -> None:
    """Calling install twice doesn't double-attach the filter or double-wrap."""
    install_log_correlation()
    install_log_correlation()
    root = isolated_root_logger

    assert sum(isinstance(f, TraceContextFilter) for f in root.filters) == 1
    handler = root.handlers[0]
    assert sum(isinstance(f, TraceContextFilter) for f in handler.filters) == 1
    assert isinstance(handler.formatter, TraceContextFormatter)
    # The wrapped formatter's delegate must NOT itself be a TraceContextFormatter
    # (i.e. we didn't wrap our wrapper).
    assert not isinstance(handler.formatter._delegate, TraceContextFormatter)


def test_install_idempotent_no_double_suffix(
    isolated_root_logger: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a double install, a stamped record gets exactly ONE trace_id= suffix."""

    class _Ctx:
        is_valid = True
        trace_id = 0x11111111111111111111111111111111
        span_id = 0x2222222222222222

    class _Span:
        def get_span_context(self) -> object:
            return _Ctx()

    class _FakeTrace:
        def get_current_span(self) -> object:
            return _Span()

    monkeypatch.setattr(log_correlation, "_OTEL_AVAILABLE", True)
    monkeypatch.setattr(log_correlation, "_otel_trace", _FakeTrace())

    install_log_correlation()
    install_log_correlation()

    root = isolated_root_logger
    handler = root.handlers[0]
    record = _make_record("payload")
    # Mimic the handler pipeline: filters enrich, then the formatter renders.
    for f in handler.filters:
        f.filter(record)
    out = handler.formatter.format(record)  # type: ignore[union-attr]
    assert out.count("trace_id=") == 1
    assert out == "payload trace_id=11111111111111111111111111111111"


# ---------------------------------------------------------------------------
# Wiring: build_app() installs the filter at the runtime edge (item 38) so
# correlation is active whenever the runtime is built — direct ASGI/uvicorn
# factory or embedded — not only under the CLI top-level callback.
# ---------------------------------------------------------------------------


def test_build_app_installs_log_correlation(isolated_root_logger: logging.Logger) -> None:
    """Building the runtime app attaches a TraceContextFilter to the root logger.

    Mirrors how ``build_app`` already wires ``install_request_id_logging``: the
    log↔trace filter must be live wherever the runtime executes, regardless of
    whether the process came up through the CLI callback. Needs the optional
    ``runtime`` extra (fastapi); skipped in a minimal build.
    """
    pytest.importorskip("fastapi")
    from movate.runtime import build_app  # noqa: PLC0415
    from movate.testing import InMemoryStorage  # noqa: PLC0415

    build_app(InMemoryStorage())

    root = isolated_root_logger
    assert any(isinstance(f, TraceContextFilter) for f in root.filters)


# ---------------------------------------------------------------------------
# Real OTel span end-to-end (gated on the SDK being installed)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_real_active_span_stamps_and_formats() -> None:
    """With a real recording span, the record carries the matching ids and the
    formatter appends the matching trace_id.

    Uses a *local* TracerProvider via ``start_as_current_span`` (sets the span on
    the current OTel context that ``get_current_span()`` reads) rather than the
    global ``set_tracer_provider`` — no process-wide state is mutated. Mirrors
    the convention in test_audit_telemetry.py / test_tracing_otel.py.
    """
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415

    provider = TracerProvider()
    tracer = provider.get_tracer("test")

    trace_filter = TraceContextFilter()
    delegate = logging.Formatter("%(message)s")
    formatter = TraceContextFormatter(delegate)

    with tracer.start_as_current_span("unit") as span:
        ctx = span.get_span_context()
        expected_trace = format(ctx.trace_id, "032x")
        expected_span = format(ctx.span_id, "016x")

        record = _make_record("inside span")
        assert trace_filter.filter(record) is True
        assert record.trace_id == expected_trace
        assert len(record.trace_id) == 32
        assert record.span_id == expected_span
        assert len(record.span_id) == 16

        out = formatter.format(record)
        assert out == f"inside span trace_id={expected_trace}"

    # Outside the span, the no-op path applies: empty id, no suffix.
    after = _make_record("after span")
    assert trace_filter.filter(after) is True
    assert after.trace_id == ""
    assert formatter.format(after) == "after span"
