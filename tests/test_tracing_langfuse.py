"""LangfuseTracer + build_tracer dispatch.

Tests inject a fake Langfuse client so we don't need the real SDK installed.
The fake mirrors the v2 SDK surface we actually call.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from movate.tracing import SilentTracer, StdoutTracer, build_tracer
from movate.tracing.langfuse import (
    LangfuseTracer,
    LangfuseUnavailableError,
    _build_client_from_env,
)

# ---------------------------------------------------------------------------
# Fake Langfuse SDK
# ---------------------------------------------------------------------------


class _FakeHandle:
    """Stand-in for a Langfuse v2 trace ROOT (StatefulTraceClient).

    Deliberately has NO ``end()`` — matching the real v2 SDK, where the
    trace root is finalized via ``update()`` and flushed at shutdown.
    Only child spans (:class:`_FakeSpan`) expose ``end()``.
    """

    def __init__(self, kind: str, name: str, metadata: dict[str, Any]) -> None:
        self.kind = kind
        self.name = name
        self.id = f"{kind}-{name}-{id(self)}"
        self.metadata = dict(metadata)
        self.events: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.children: list[_FakeHandle] = []
        self.ended_with: dict[str, Any] | None = None

    def span(self, *, name: str, metadata: dict[str, Any]) -> _FakeSpan:
        child = _FakeSpan("span", name, metadata)
        self.children.append(child)
        return child

    def event(self, *, name: str, metadata: dict[str, Any]) -> None:
        self.events.append({"name": name, "metadata": dict(metadata)})

    def update(self, *, metadata: dict[str, Any]) -> None:
        self.updates.append(dict(metadata))


class _FakeSpan(_FakeHandle):
    """Child span (StatefulSpanClient) — adds ``end()`` like the real SDK."""

    def end(self, **kwargs: Any) -> None:
        self.ended_with = dict(kwargs)


class _FakeClient:
    def __init__(self) -> None:
        self.traces: list[_FakeHandle] = []
        self.flushed = 0

    def trace(self, *, name: str, metadata: dict[str, Any]) -> _FakeHandle:
        t = _FakeHandle("trace", name, metadata)
        self.traces.append(t)
        return t

    def flush(self) -> None:
        self.flushed += 1


# ---------------------------------------------------------------------------
# build_tracer dispatch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_tracer_default_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOVATE_TRACER", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    tracer = build_tracer()
    assert isinstance(tracer, SilentTracer)


@pytest.mark.unit
def test_build_tracer_explicit_stdout_overrides_langfuse_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOVATE_TRACER", "stdout")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-set")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-set")
    tracer = build_tracer()
    assert isinstance(tracer, StdoutTracer)


@pytest.mark.unit
def test_build_tracer_langfuse_falls_back_when_package_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """If MOVATE_TRACER=langfuse but the package isn't installed, fall back
    to SilentTracer with a one-time stderr warning. Never let tracing break
    a run, and never flood the terminal with JSON span lines."""
    import movate.tracing as _t
    _t._warned.discard("langfuse")  # reset per-process guard for test isolation
    monkeypatch.setenv("MOVATE_TRACER", "langfuse")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-set")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-set")
    # Force the import-time path to fail by hiding the langfuse module.
    monkeypatch.setitem(sys.modules, "langfuse", None)  # type: ignore[arg-type]

    tracer = build_tracer()
    assert isinstance(tracer, SilentTracer)
    assert "Langfuse unavailable" in capsys.readouterr().err


@pytest.mark.unit
def test_build_tracer_langfuse_falls_back_without_keys(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """MOVATE_TRACER=langfuse explicitly but keys missing → SilentTracer fallback."""
    import movate.tracing as _t
    _t._warned.discard("langfuse")  # reset per-process guard for test isolation
    monkeypatch.setenv("MOVATE_TRACER", "langfuse")
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    tracer = build_tracer()
    assert isinstance(tracer, SilentTracer)
    assert "must both be set" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# LangfuseTracer behaviour with a fake client
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_langfuse_tracer_creates_root_trace_for_top_level_span() -> None:
    fake = _FakeClient()
    t = LangfuseTracer(client=fake)

    ctx = t.start_span("agent.execute", attrs={"agent": "demo"})
    assert ctx.parent_id is None
    # Created exactly one trace.
    assert len(fake.traces) == 1
    assert fake.traces[0].name == "agent.execute"
    assert fake.traces[0].metadata == {"agent": "demo"}


@pytest.mark.unit
def test_langfuse_tracer_nests_child_spans_under_trace() -> None:
    fake = _FakeClient()
    t = LangfuseTracer(client=fake)

    parent = t.start_span("workflow.run", attrs={"workflow": "demo"})
    child = t.start_span("agent.execute", attrs={"node": "first"}, parent=parent)

    assert child.parent_id == parent.span_id
    assert child.trace_id == parent.trace_id
    # Trace got a child span attached.
    assert len(fake.traces) == 1
    assert len(fake.traces[0].children) == 1
    assert fake.traces[0].children[0].name == "agent.execute"


@pytest.mark.unit
def test_langfuse_tracer_log_event_and_set_attribute() -> None:
    fake = _FakeClient()
    t = LangfuseTracer(client=fake)
    ctx = t.start_span("agent.execute")
    t.log_event(ctx, {"prompt_hash": "abc123"})
    t.set_attribute(ctx, "model", "openai/gpt-4o-mini")

    handle = fake.traces[0]
    assert handle.events == [{"name": "event", "metadata": {"prompt_hash": "abc123"}}]
    assert handle.updates == [{"model": "openai/gpt-4o-mini"}]
    # set_attribute also mirrors into the local SpanCtx.
    assert ctx.attributes["model"] == "openai/gpt-4o-mini"


@pytest.mark.unit
def test_langfuse_tracer_end_span_closes_trace_root_via_update() -> None:
    """The trace ROOT has no ``end()`` in Langfuse v2; ``end_span`` must
    finalize it via ``update()`` (recording terminal status) rather than
    calling a non-existent ``end()`` — which previously raised
    ``AttributeError: 'StatefulTraceClient' object has no attribute 'end'``
    and aborted the run."""
    fake = _FakeClient()
    t = LangfuseTracer(client=fake)
    ctx = t.start_span("agent.execute")
    t.end_span(ctx, status="ok")

    root = fake.traces[0]
    assert root.ended_with is None  # never tried to .end() the root
    assert {"status": "ok"} in root.updates
    # Calls after end are no-ops (no AttributeError, no second event).
    t.log_event(ctx, {"too": "late"})
    assert len(root.events) == 0


@pytest.mark.unit
def test_langfuse_tracer_end_span_closes_child_via_end() -> None:
    """A child span (StatefulSpanClient) DOES have ``end()`` — use it."""
    fake = _FakeClient()
    t = LangfuseTracer(client=fake)
    parent = t.start_span("agent.execute")
    child = t.start_span("tool.call", parent=parent)
    t.end_span(child, status="ok")

    child_handle = fake.traces[0].children[0]
    assert child_handle.ended_with == {"metadata": {"status": "ok"}}


@pytest.mark.unit
def test_langfuse_tracer_end_span_never_raises_on_v2_trace_root() -> None:
    """Regression for the reported failure: ending a trace-root handle
    that lacks ``end()`` must not raise (tracing never breaks a run)."""
    fake = _FakeClient()
    t = LangfuseTracer(client=fake)
    ctx = t.start_span("eval.generate")
    # Would raise AttributeError before the capability-routing fix.
    t.end_span(ctx, status="ok")


@pytest.mark.unit
def test_langfuse_tracer_flush_calls_client() -> None:
    fake = _FakeClient()
    t = LangfuseTracer(client=fake)
    t.flush()
    t.flush()
    assert fake.flushed == 2


@pytest.mark.unit
def test_langfuse_tracer_orphaned_child_falls_through_to_root() -> None:
    """If a span's recorded parent has already ended, ``start_span(parent=...)``
    should still produce a usable handle (creates a new trace) rather than
    raising — tracing must never break a run."""
    fake = _FakeClient()
    t = LangfuseTracer(client=fake)
    parent = t.start_span("parent")
    t.end_span(parent)
    child = t.start_span("orphan", parent=parent)
    assert child.span_id  # handle was created
    # New trace was opened to host the orphan.
    assert len(fake.traces) == 2


# ---------------------------------------------------------------------------
# _build_client_from_env error paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_langfuse_unavailable_when_keys_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    with pytest.raises(LangfuseUnavailableError, match="must both be set"):
        _build_client_from_env()


@pytest.mark.unit
def test_langfuse_unavailable_when_package_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-set")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-set")
    monkeypatch.setitem(sys.modules, "langfuse", None)  # type: ignore[arg-type]
    with pytest.raises(LangfuseUnavailableError, match="not installed"):
        _build_client_from_env()
