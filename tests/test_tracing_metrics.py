"""OTel metrics instruments (R3 / item 33) — no-op safety + real-SDK datapoints.

Two layers, mirroring ``test_tracing_otel.py``:

1. **No-op safety** — the public record helpers never raise when metrics are
   uninitialized OR when the OTel extra is absent. These run in every env.
2. **Real-SDK datapoints** — gated behind ``_otel_installed()`` (like the span
   tests). With the SDK present, ``init_metrics(reader=InMemoryMetricReader())``
   builds a provider around an injected reader (the testing seam) so we assert on
   the exact datapoints + attributes each helper produces, with no network.

The module keeps process-global state (the global MeterProvider + the
instruments, held on ``metrics_mod._state``), so an autouse fixture snapshots and
restores it around every test — otherwise a successful ``init_metrics`` would
leak instruments into the next test.
"""

from __future__ import annotations

import pytest

import movate.tracing.metrics as metrics_mod
from movate.tracing import (
    dec_in_flight,
    inc_in_flight,
    init_metrics,
    record_job_completed,
    record_run_usage,
    record_voice_turn,
)
from movate.tracing.metrics import _State


def _otel_installed() -> bool:
    """Are the OTel SDK metrics packages importable? Gates the real-SDK tests
    below (dev / ``--all-extras`` builds have them; minimal CI doesn't)."""
    try:
        import opentelemetry.sdk.metrics  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False


@pytest.fixture(autouse=True)
def _reset_metrics_module() -> object:
    """Swap in a fresh ``_State`` around each test so a successful
    ``init_metrics`` doesn't leak instruments into the next test, then restore."""
    saved = metrics_mod._state
    metrics_mod._state = _State()
    yield
    metrics_mod._state = saved


def _clear_sink_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MOVATE_TRACE_SINK",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "OTEL_SERVICE_NAME",
        "MOVATE_ENV",
        "OTEL_DEPLOYMENT_ENVIRONMENT",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# No-op safety — helpers never raise (uninitialized OR OTel absent)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_helpers_are_noops_when_uninitialized() -> None:
    """Before init_metrics, every record helper is a silent no-op (no raise)."""
    # Instruments are None (the reset fixture guarantees it).
    record_job_completed(kind="agent", status="success", duration_ms=12, tenant_id="t1")
    record_run_usage(tenant_id="t1", tokens=100, cost_usd=0.01)
    record_voice_turn(tenant_id="t1", responded_ms=420.0, stt_final_ms=120.0)
    inc_in_flight(tenant_id="t1")
    dec_in_flight(tenant_id="t1")
    # Reached here without raising — the assertion is "no exception".


@pytest.mark.unit
def test_init_metrics_noop_when_sink_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """No OTLP sink + no endpoint → init_metrics leaves instruments None (no-op)."""
    _clear_sink_env(monkeypatch)
    init_metrics()
    assert metrics_mod._state.initialized is True
    assert metrics_mod._state.jobs_completed is None
    # And the helpers still no-op cleanly.
    record_job_completed(kind="agent", status="error", duration_ms=5, tenant_id="t1")


@pytest.mark.unit
def test_init_metrics_noop_when_sink_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """MOVATE_TRACE_SINK=none wins even with an endpoint set → metrics stay off."""
    _clear_sink_env(monkeypatch)
    monkeypatch.setenv("MOVATE_TRACE_SINK", "none")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    init_metrics()
    assert metrics_mod._state.jobs_completed is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "sink,endpoint,expected",
    [
        ("otlp", "", True),
        ("both", "", True),
        ("langfuse", "http://localhost:4318", False),
        ("none", "http://localhost:4318", False),
        ("", "http://localhost:4318", True),
        ("", "", False),
    ],
)
def test_otlp_metrics_enabled_condition(
    monkeypatch: pytest.MonkeyPatch, sink: str, endpoint: str, expected: bool
) -> None:
    """The enable condition mirrors the OtelTracer's: explicit OTLP sink, or
    (legacy) an endpoint when no sink turns it off."""
    _clear_sink_env(monkeypatch)
    if sink:
        monkeypatch.setenv("MOVATE_TRACE_SINK", sink)
    if endpoint:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    assert metrics_mod._otlp_metrics_enabled() is expected


@pytest.mark.unit
def test_init_metrics_noop_when_otel_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the otel extra being absent: even under an OTLP sink, init is a
    no-op (and helpers never raise)."""
    _clear_sink_env(monkeypatch)
    monkeypatch.setenv("MOVATE_TRACE_SINK", "otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setattr(metrics_mod, "_OTEL_METRICS_AVAILABLE", False)
    monkeypatch.setattr(metrics_mod, "_otel_metrics", None)
    init_metrics()
    assert metrics_mod._state.initialized is True
    assert metrics_mod._state.jobs_completed is None
    record_job_completed(kind="agent", status="success", duration_ms=1, tenant_id="t1")


# ---------------------------------------------------------------------------
# Real SDK — datapoints via an injected InMemoryMetricReader (the test seam)
# ---------------------------------------------------------------------------


def _collect(reader: object) -> dict[str, list]:
    """Flatten an InMemoryMetricReader's collected metrics into a name→datapoints
    map keyed by metric name. Always returns a dict (empty when nothing was
    recorded — ``get_metrics_data()`` returns ``None`` with no datapoints)."""
    data = reader.get_metrics_data()  # type: ignore[attr-defined]
    out: dict[str, list] = {}
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                out.setdefault(metric.name, []).extend(list(metric.data.data_points))
    return out


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_record_job_completed_emits_counter_and_histogram() -> None:
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: PLC0415

    reader = InMemoryMetricReader()
    init_metrics(reader=reader)
    assert metrics_mod._state.jobs_completed is not None  # provider built

    record_job_completed(kind="agent", status="success", duration_ms=42, tenant_id="tenant-a")
    record_job_completed(kind="eval", status="dead_letter", duration_ms=99, tenant_id="tenant-b")

    metrics = _collect(reader)
    assert "mdk.jobs.completed" in metrics
    assert "mdk.job.duration_ms" in metrics

    completed = {
        (dp.attributes["kind"], dp.attributes["status"], dp.attributes["tenant"]): dp.value
        for dp in metrics["mdk.jobs.completed"]
    }
    assert completed[("agent", "success", "tenant-a")] == 1
    assert completed[("eval", "dead_letter", "tenant-b")] == 1

    # Duration histogram carries kind+status (NOT tenant — low cardinality).
    dur_attrs = {
        (dp.attributes["kind"], dp.attributes["status"]) for dp in metrics["mdk.job.duration_ms"]
    }
    assert ("agent", "success") in dur_attrs
    assert ("eval", "dead_letter") in dur_attrs
    for dp in metrics["mdk.job.duration_ms"]:
        assert "tenant" not in dp.attributes


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_record_run_usage_emits_token_and_cost_counters() -> None:
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: PLC0415

    reader = InMemoryMetricReader()
    init_metrics(reader=reader)

    record_run_usage(tenant_id="tenant-a", tokens=150, cost_usd=0.0125)
    record_run_usage(tenant_id="tenant-a", tokens=50, cost_usd=0.005)

    metrics = _collect(reader)
    assert "mdk.run.tokens" in metrics
    assert "mdk.run.cost_usd" in metrics

    token_total = sum(dp.value for dp in metrics["mdk.run.tokens"])
    cost_total = sum(dp.value for dp in metrics["mdk.run.cost_usd"])
    assert token_total == 200
    assert cost_total == pytest.approx(0.0175)
    for dp in metrics["mdk.run.tokens"]:
        assert dp.attributes["tenant"] == "tenant-a"


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_record_run_usage_skips_none_values() -> None:
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: PLC0415

    reader = InMemoryMetricReader()
    init_metrics(reader=reader)
    # All-None → records nothing; no raise.
    record_run_usage(tenant_id="t1", tokens=None, cost_usd=None)
    metrics = _collect(reader)
    # Neither counter should have produced datapoints.
    assert not metrics.get("mdk.run.tokens")
    assert not metrics.get("mdk.run.cost_usd")


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_record_voice_turn_emits_latency_histograms_and_turn_counter() -> None:
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: PLC0415

    reader = InMemoryMetricReader()
    init_metrics(reader=reader)
    assert metrics_mod._state.voice_turns is not None  # instruments built

    record_voice_turn(
        tenant_id="tenant-a",
        responded_ms=850.0,
        stt_final_ms=300.0,
        tts_first_audio_ms=700.0,
        interrupted=True,
    )
    metrics = _collect(reader)
    assert "mdk.voice.responded_ms" in metrics
    assert "mdk.voice.stt_final_ms" in metrics
    assert "mdk.voice.tts_first_audio_ms" in metrics
    # Turn counter carries the barge-in flag + tenant.
    dp = metrics["mdk.voice.turns"][0]
    assert dp.attributes["interrupted"] == "true"
    assert dp.attributes["tenant"] == "tenant-a"


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_record_voice_turn_skips_none_milestones() -> None:
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: PLC0415

    reader = InMemoryMetricReader()
    init_metrics(reader=reader)
    # A turn that errored at STT: only the turn counter, no latency datapoints.
    record_voice_turn(tenant_id="t1")
    metrics = _collect(reader)
    assert metrics.get("mdk.voice.turns")  # counted
    assert not metrics.get("mdk.voice.responded_ms")
    assert not metrics.get("mdk.voice.stt_final_ms")


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_in_flight_up_down_counter() -> None:
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: PLC0415

    reader = InMemoryMetricReader()
    init_metrics(reader=reader)

    inc_in_flight(tenant_id="tenant-a")
    inc_in_flight(tenant_id="tenant-a")
    dec_in_flight(tenant_id="tenant-a")

    metrics = _collect(reader)
    assert "mdk.jobs.in_flight" in metrics
    total = sum(dp.value for dp in metrics["mdk.jobs.in_flight"])
    # +1 +1 -1 → net 1 in flight.
    assert total == 1


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_init_metrics_is_idempotent() -> None:
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: PLC0415

    reader = InMemoryMetricReader()
    init_metrics(reader=reader)
    first = metrics_mod._state.jobs_completed
    assert first is not None
    # Second call is a no-op: doesn't rebuild / re-register / raise.
    init_metrics(reader=InMemoryMetricReader())
    assert metrics_mod._state.jobs_completed is first


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_metric_exporter_class_selects_http_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP exporter by default; gRPC when OTEL_EXPORTER_OTLP_PROTOCOL=grpc —
    the metric mirror of the span exporter selection."""
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (  # noqa: PLC0415
        OTLPMetricExporter as Grpc,
    )
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (  # noqa: PLC0415
        OTLPMetricExporter as Http,
    )

    _clear_sink_env(monkeypatch)
    assert metrics_mod._otlp_metric_exporter_class() is Http
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    assert metrics_mod._otlp_metric_exporter_class() is Grpc


@pytest.mark.unit
def test_init_metrics_failsoft_on_build_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A provider-build failure emits one stderr line then degrades to no-op —
    never raises (mirrors the tracer's fail-soft logging)."""
    _clear_sink_env(monkeypatch)
    monkeypatch.setenv("MOVATE_TRACE_SINK", "otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    # Make the metrics available so we get past the early return, but force the
    # provider builder to blow up.
    # _otel_metrics just needs to be non-None to clear the early guard — the
    # builder is forced to raise before set_meter_provider is ever called.
    monkeypatch.setattr(metrics_mod, "_OTEL_METRICS_AVAILABLE", True)
    monkeypatch.setattr(metrics_mod, "_otel_metrics", pytest)

    def _boom(**kwargs: object) -> object:
        raise RuntimeError("synthetic build failure")

    monkeypatch.setattr(metrics_mod, "_build_meter_provider", _boom)
    init_metrics()  # must not raise
    assert metrics_mod._state.initialized is True
    assert metrics_mod._state.jobs_completed is None
    assert "OTel metrics unavailable" in capsys.readouterr().err
