"""ADR 039 Phase 2 — dual-export wiring tests.

The dual exporter is **opt-in** via env. Two layers of tests, mirroring how
``test_tracing_metrics.py`` is organized:

1. **Env-only / no-OTel-SDK paths** — predicate behavior, env-set vs unset,
   half-configured state, PII filter as a pure function. These run in every
   env, no OTel SDK required.
2. **Real-SDK wiring** — gated behind ``_otel_installed()`` like the existing
   tracing tests. Asserts that (a) when the env is set, a second
   span processor / metric reader is attached, (b) failure of the dual path
   doesn't break the primary, (c) the PII filter strips non-allow-listed
   attributes from the dual stream only.

Doctor-output tests live alongside as a third group.
"""

from __future__ import annotations

from typing import Any

import pytest

import movate.tracing.dual_export as dx
import movate.tracing.metrics as metrics_mod
from movate.tracing.dual_export import (
    ALLOWED_SPAN_ATTRIBUTES,
    ENV_TELEMETRY_CUSTOMER_ID,
    ENV_TELEMETRY_ENDPOINT,
    ENV_TELEMETRY_INSECURE,
    _filter_attrs,
    build_dual_metric_reader,
    build_dual_resource,
    build_dual_span_processor,
    dual_export_enabled,
    telemetry_customer_id,
    telemetry_endpoint,
    telemetry_insecure,
)


def _otel_installed() -> bool:
    """Are the OTel SDK packages importable?

    Gates the real-SDK tests; matches the gate in ``test_tracing_metrics.py``.
    """
    try:
        import opentelemetry.sdk.metrics  # noqa: PLC0415
        import opentelemetry.sdk.trace  # noqa: PLC0415

        _ = opentelemetry.sdk.metrics
        _ = opentelemetry.sdk.trace
        return True
    except ImportError:
        return False


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the per-process "warned-once" set so each test starts fresh.

    Without this, a test asserting "warning printed" runs second after one
    that already printed it and sees nothing.
    """
    dx._warned_keys.clear()
    # Also unset any inherited env so tests start from a clean Phase-2-off
    # baseline. Each test that needs Phase 2 on sets the envs explicitly.
    for var in (
        ENV_TELEMETRY_ENDPOINT,
        ENV_TELEMETRY_CUSTOMER_ID,
        ENV_TELEMETRY_INSECURE,
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "MOVATE_TRACE_SINK",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Env predicate — the gate on every Phase-2 builder
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dual_export_disabled_when_envs_unset() -> None:
    """Default state: both envs unset → dual export disabled (no warning)."""
    assert dual_export_enabled() is False
    assert telemetry_endpoint() == ""
    assert telemetry_customer_id() == ""
    assert telemetry_insecure() is False


@pytest.mark.unit
def test_dual_export_enabled_when_both_envs_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_TELEMETRY_ENDPOINT, "https://otel.movate.example:4318")
    monkeypatch.setenv(ENV_TELEMETRY_CUSTOMER_ID, "deadbeef0123456789")
    assert dual_export_enabled() is True
    assert telemetry_endpoint() == "https://otel.movate.example:4318"
    assert telemetry_customer_id() == "deadbeef0123456789"


@pytest.mark.unit
def test_dual_export_disabled_endpoint_only_logs_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Half-configured state: endpoint set but customer-id unset → disabled + one warning."""
    monkeypatch.setenv(ENV_TELEMETRY_ENDPOINT, "https://otel.movate.example:4318")
    # Customer-id intentionally unset.
    assert dual_export_enabled() is False
    err = capsys.readouterr().err
    assert "Phase 2 telemetry disabled" in err
    assert ENV_TELEMETRY_CUSTOMER_ID in err


@pytest.mark.unit
def test_half_configured_warning_emitted_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Multi-case eval loops MUST NOT flood stderr with the same warning."""
    monkeypatch.setenv(ENV_TELEMETRY_ENDPOINT, "https://otel.movate.example:4318")
    dual_export_enabled()
    dual_export_enabled()
    dual_export_enabled()
    err = capsys.readouterr().err
    assert err.count("Phase 2 telemetry disabled") == 1


@pytest.mark.unit
def test_telemetry_insecure_truthy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MDK_TELEMETRY_INSECURE`` is opt-in; only truthy values turn it on."""
    for truthy in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv(ENV_TELEMETRY_INSECURE, truthy)
        assert telemetry_insecure() is True, truthy
    for falsy in ("", "0", "false", "no", "off", "anything-else"):
        monkeypatch.setenv(ENV_TELEMETRY_INSECURE, falsy)
        assert telemetry_insecure() is False, falsy


# ---------------------------------------------------------------------------
# PII filter — pure function, no SDK needed
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_filter_attrs_keeps_only_allow_listed() -> None:
    """The allow-list is keep-only — unknown attributes are dropped."""
    attrs = {
        "agent": "demo",
        "provider": "openai",
        "status": "ok",
        # NOT in allow-list — must be dropped.
        "tenant_id": "acme-finance",
        "job_id": "j-12345",
        "input_text": "secret prompt content the user typed",
        "description": "free-form blob that could embed PII",
        "chunk_ids_preview": ["doc-1", "doc-2"],
    }
    filtered = _filter_attrs(attrs)
    assert filtered == {"agent": "demo", "provider": "openai", "status": "ok"}


@pytest.mark.unit
def test_filter_attrs_drops_unknown_keys_by_default() -> None:
    """Future attribute added in src/movate/core/ defaults to dropped (not leaked).

    The whole point of an allow-list, asserted explicitly.
    """
    attrs = {"some_future_attribute": "anything"}
    assert _filter_attrs(attrs) == {}


@pytest.mark.unit
def test_allowed_span_attributes_excludes_pii_vector_keys() -> None:
    """Explicit denial — keys ADR 039 D3 calls out as redacted/dropped MUST NOT
    appear in the allow-list."""
    forbidden = {
        "tenant_id",
        "job_id",
        "run_id",
        "workflow_run_id",
        "input_text",
        "description",
        "chunk_ids_preview",
        "exception.message",
        # The full exception message can embed values (whereas
        # `exception.type` is the class name only) — the allow-list takes
        # the type, not the message.
    }
    leaked = forbidden & ALLOWED_SPAN_ATTRIBUTES
    assert not leaked, f"PII-vector keys leaked into allow-list: {leaked}"


# ---------------------------------------------------------------------------
# Builders return None when Phase 2 is off — primary stream untouched
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_builders_return_none_when_disabled() -> None:
    """Both builders are None-when-disabled so callers can do `if x is not None: add()`.

    This is the contract the otel.py / metrics.py wiring depends on.
    """
    assert build_dual_span_processor() is None
    assert build_dual_metric_reader() is None


# ---------------------------------------------------------------------------
# Real-SDK tests — assertion that the SECOND processor / reader is attached
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_meter_provider_has_one_reader_when_phase2_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With Phase 2 off, ``_build_meter_provider`` builds the primary reader only."""
    monkeypatch.setenv("MOVATE_TRACE_SINK", "otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    # Phase 2 envs intentionally unset.
    provider = metrics_mod._build_meter_provider(reader=None)
    try:
        # The MeterProvider's metric_readers list is private SDK state; we
        # observe via the protected attr (matches the rest of this test file's
        # introspection style).
        readers = getattr(provider, "_sdk_config", None)
        # Cross-version safe: try a couple of attribute paths the SDK has used.
        if readers is None:
            readers = getattr(provider, "_metric_readers", None)
        if readers is not None and hasattr(readers, "metric_readers"):
            readers = readers.metric_readers
        assert readers is not None, "could not introspect MeterProvider readers"
        assert len(list(readers)) == 1
    finally:
        provider.shutdown()


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_meter_provider_has_two_readers_when_phase2_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With both envs set, the MeterProvider gets the primary + the dual reader."""
    monkeypatch.setenv("MOVATE_TRACE_SINK", "otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv(ENV_TELEMETRY_ENDPOINT, "http://movate-collector:4318")
    monkeypatch.setenv(ENV_TELEMETRY_CUSTOMER_ID, "deadbeef0123456789")
    monkeypatch.setenv(ENV_TELEMETRY_INSECURE, "1")  # http endpoint for the test

    provider = metrics_mod._build_meter_provider(reader=None)
    try:
        readers = getattr(provider, "_sdk_config", None)
        if readers is None:
            readers = getattr(provider, "_metric_readers", None)
        if readers is not None and hasattr(readers, "metric_readers"):
            readers = readers.metric_readers
        assert readers is not None
        assert len(list(readers)) == 2
    finally:
        # Shut down the provider so its readers' background threads stop —
        # otherwise the unreachable Movate endpoint floods stderr with
        # NameResolutionError retries during the rest of the suite.
        provider.shutdown()


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_tracer_provider_has_extra_span_processor_when_phase2_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With both envs set, _build_provider_from_env attaches a second BatchSpanProcessor."""
    from movate.tracing.otel import _build_provider_from_env  # noqa: PLC0415

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv(ENV_TELEMETRY_ENDPOINT, "http://movate-collector:4318")
    monkeypatch.setenv(ENV_TELEMETRY_CUSTOMER_ID, "deadbeef0123456789")
    monkeypatch.setenv(ENV_TELEMETRY_INSECURE, "1")

    provider = _build_provider_from_env()
    try:
        # TracerProvider exposes its multi-span-processor via ._active_span_processor
        # which wraps a list. The cross-version-safe probe is to count attached
        # processors via the public API the SDK uses — every processor has its
        # own ``on_end``; just confirm the wrapper indicates >1.
        wrapper = getattr(provider, "_active_span_processor", None)
        assert wrapper is not None
        span_processors = getattr(wrapper, "_span_processors", None)
        assert span_processors is not None, "couldn't introspect span processors"
        assert len(list(span_processors)) >= 2
    finally:
        provider.shutdown()


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_tracer_provider_has_one_span_processor_when_phase2_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (Phase 2 off): only the primary BatchSpanProcessor is attached."""
    from movate.tracing.otel import _build_provider_from_env  # noqa: PLC0415

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    # Phase 2 envs unset.
    provider = _build_provider_from_env()
    try:
        wrapper = getattr(provider, "_active_span_processor", None)
        assert wrapper is not None
        span_processors = getattr(wrapper, "_span_processors", None)
        assert span_processors is not None
        assert len(list(span_processors)) == 1
    finally:
        provider.shutdown()


# ---------------------------------------------------------------------------
# Failure isolation — dual stream failure MUST NOT break primary
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_dual_exporter_init_failure_does_not_break_primary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Force the dual span exporter constructor to raise; the TracerProvider must
    still build with the primary processor attached + a single warning logged."""
    from movate.tracing.otel import _build_provider_from_env  # noqa: PLC0415

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv(ENV_TELEMETRY_ENDPOINT, "http://movate-collector:4318")
    monkeypatch.setenv(ENV_TELEMETRY_CUSTOMER_ID, "deadbeef0123456789")
    monkeypatch.setenv(ENV_TELEMETRY_INSECURE, "1")

    # Patch the dual span exporter class to a constructor that always raises.
    class _Boom:
        def __init__(self, *_: Any, **__: Any) -> None:
            raise RuntimeError("synthetic exporter failure")

    monkeypatch.setattr(dx, "_otlp_span_exporter_class", lambda: _Boom)

    provider = _build_provider_from_env()
    try:
        # Provider was built; primary processor present, dual one suppressed.
        wrapper = getattr(provider, "_active_span_processor", None)
        assert wrapper is not None
        span_processors = list(wrapper._span_processors)
        assert len(span_processors) == 1
        err = capsys.readouterr().err
        assert "Phase-2 telemetry endpoint unreachable" in err
    finally:
        provider.shutdown()


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_dual_metric_reader_failure_does_not_break_primary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Same isolation contract on the metrics side."""
    monkeypatch.setenv("MOVATE_TRACE_SINK", "otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv(ENV_TELEMETRY_ENDPOINT, "http://movate-collector:4318")
    monkeypatch.setenv(ENV_TELEMETRY_CUSTOMER_ID, "deadbeef0123456789")
    monkeypatch.setenv(ENV_TELEMETRY_INSECURE, "1")

    class _Boom:
        def __init__(self, *_: Any, **__: Any) -> None:
            raise RuntimeError("synthetic metric exporter failure")

    monkeypatch.setattr(dx, "_otlp_metric_exporter_class", lambda: _Boom)

    provider = metrics_mod._build_meter_provider(reader=None)
    try:
        readers = getattr(provider, "_sdk_config", None)
        if readers is None:
            readers = getattr(provider, "_metric_readers", None)
        if readers is not None and hasattr(readers, "metric_readers"):
            readers = readers.metric_readers
        assert readers is not None
        assert len(list(readers)) == 1  # only the primary; dual suppressed
        err = capsys.readouterr().err
        assert "Phase-2 telemetry endpoint unreachable" in err
    finally:
        provider.shutdown()


# ---------------------------------------------------------------------------
# PII-filtering SpanProcessor — strips disallowed attrs from dual stream only
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_pii_filtering_processor_drops_disallowed_attrs() -> None:
    """The filter-wrapped processor hands a redacted ``ReadableSpan`` to its parent
    ``BatchSpanProcessor.on_end`` — we assert by intercepting the super call,
    rather than driving the BSP's async queue + a sampling decision (which the
    real SDK gates on a SampledTraceFlags-set SpanContext).

    The contract under test is: PiiFilteringSpanProcessor.on_end is called with
    a span carrying disallowed attributes, and the inner BSP receives a wrapper
    span whose ``.attributes`` are filtered. The primary processor in
    otel.py (which is an unwrapped BatchSpanProcessor) never sees this code
    path — that's the "primary unaffected" half of the invariant.
    """
    processor_cls = dx._build_pii_filtering_processor_class()
    assert processor_cls is not None

    # Capture what the SUPER (BatchSpanProcessor.on_end) is handed.
    captured: list[Any] = []

    # Replace the base class's on_end with a sink we control. We do this via
    # the processor instance's MRO — the simplest way to intercept the
    # super().on_end(wrapped) call without driving the SDK's worker thread.
    from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

    original_on_end = BatchSpanProcessor.on_end

    def _capture(self: Any, span: Any) -> None:
        captured.append(span)

    try:
        BatchSpanProcessor.on_end = _capture  # type: ignore[method-assign]

        # Need an exporter for BSP construction even though we don't use it.
        class _NullExporter:
            def export(self, spans: Any) -> Any:
                from opentelemetry.sdk.trace.export import (  # noqa: PLC0415
                    SpanExportResult,
                )

                return SpanExportResult.SUCCESS

            def shutdown(self) -> None:
                pass

            def force_flush(self, timeout_millis: int = 30_000) -> bool:
                return True

        processor = processor_cls(_NullExporter())

        class _FakeSpan:
            def __init__(self, attrs: dict[str, Any]) -> None:
                self.attributes = attrs
                self.name = "agent.execute"

        processor.on_end(
            _FakeSpan(
                {
                    "agent": "demo",
                    "provider": "openai",
                    "tenant_id": "acme-finance",  # disallowed
                    "input_text": "leak vector",  # disallowed
                }
            )
        )

        assert len(captured) == 1
        wrapper = captured[0]
        attrs = dict(wrapper.attributes)
        assert attrs == {"agent": "demo", "provider": "openai"}
        assert "tenant_id" not in attrs
        assert "input_text" not in attrs
        # The wrapper proxies non-attribute fields back to the original.
        assert wrapper.name == "agent.execute"
    finally:
        BatchSpanProcessor.on_end = original_on_end  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Dual Resource — customer attribute + scrubbed deployment.environment
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_build_dual_resource_stamps_customer_and_drops_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dual Resource carries `customer=<hash>` and drops `deployment.environment`."""
    monkeypatch.setenv(ENV_TELEMETRY_CUSTOMER_ID, "deadbeef0123456789")
    base = {
        "service.name": "movate-runtime",
        "service.version": "2026.5.27.1",
        "deployment.environment": "prod-acme",  # could leak a name
    }
    resource = build_dual_resource(base_attrs=base)
    assert resource is not None
    rattrs = dict(resource.attributes)
    assert rattrs.get("customer") == "deadbeef0123456789"
    assert "deployment.environment" not in rattrs
    # service.name + version still carry over — those are safe.
    assert rattrs.get("service.name") == "movate-runtime"


# ---------------------------------------------------------------------------
# Doctor — Phase 2 telemetry row output
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_doctor_phase2_section_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from movate.cli.doctor import _render_phase2_telemetry_section  # noqa: PLC0415

    rows: list[tuple[str, str]] = []

    def _add(check: str, result: str, *extra: str) -> None:
        rows.append((check, result))

    _render_phase2_telemetry_section(_add)
    # Exactly one row, marked "off".
    assert len(rows) == 1
    check, result = rows[0]
    assert check == "phase 2 telemetry"
    assert "off" in result
    assert ENV_TELEMETRY_ENDPOINT in result


@pytest.mark.unit
def test_doctor_phase2_section_set_shows_endpoint_and_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from movate.cli.doctor import _render_phase2_telemetry_section  # noqa: PLC0415

    monkeypatch.setenv(ENV_TELEMETRY_ENDPOINT, "https://otel.movate.example:4318")
    monkeypatch.setenv(ENV_TELEMETRY_CUSTOMER_ID, "deadbeef0123456789")

    rows: list[tuple[str, str]] = []

    def _add(check: str, result: str, *extra: str) -> None:
        rows.append((check, result))

    _render_phase2_telemetry_section(_add)
    # One section row + one informational follow-up.
    assert len(rows) == 2
    main = rows[0][1]
    assert "https://otel.movate.example:4318" in main
    # First 8 chars of the hash, never the full ID.
    assert "deadbeef" in main
    assert "0123456789" not in main


@pytest.mark.unit
def test_doctor_phase2_section_half_configured_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from movate.cli.doctor import _render_phase2_telemetry_section  # noqa: PLC0415

    monkeypatch.setenv(ENV_TELEMETRY_ENDPOINT, "https://otel.movate.example:4318")
    # Customer-id intentionally unset.

    rows: list[tuple[str, str]] = []

    def _add(check: str, result: str, *extra: str) -> None:
        rows.append((check, result))

    _render_phase2_telemetry_section(_add)
    assert len(rows) == 1
    _check, result = rows[0]
    assert "missing" in result.lower()
    assert ENV_TELEMETRY_CUSTOMER_ID in result
