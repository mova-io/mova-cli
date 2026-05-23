"""LangfuseTracer + build_tracer dispatch.

Tests inject a fake Langfuse client so we don't need the real SDK installed.
The fake mirrors the **v3 SDK** surface we actually call: the client exposes
``start_span(...)`` (returns a span object that anchors the trace) plus
``create_score(...)`` / ``flush()`` / ``shutdown()``; span objects expose
``start_span(...)`` (child), ``start_observation(as_type="generation", ...)``,
``create_event(...)``, ``update(...)``, ``update_trace(...)``, ``end()``, and a
``trace_id`` attribute. This replaces the v2 ``client.trace(...)`` /
``trace.span(...)`` / ``span.generation(...)`` model.
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
# Fake Langfuse v3 SDK
# ---------------------------------------------------------------------------


class _FakeObservation:
    """Stand-in for a Langfuse v3 observation (LangfuseSpan / generation).

    In v3 every observation — root span included — exposes ``end()`` (the v2
    quirk where the trace ROOT lacked ``end()`` is gone) and carries a
    ``trace_id`` set at construction. Child spans/generations are created off
    the parent via ``start_span`` / ``start_observation``.
    """

    _counter = 0

    def __init__(self, kind: str, name: str, metadata: dict[str, Any] | None = None) -> None:
        type(self)._counter += 1
        self.kind = kind
        self.name = name
        # v3 trace ids are per-trace; a root span mints a new one, children
        # inherit the parent's (set by _FakeObservation.start_span below).
        self.trace_id = f"trace-{type(self)._counter}"
        self.metadata = dict(metadata or {})
        self.events: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.trace_updates: list[dict[str, Any]] = []
        self.children: list[_FakeObservation] = []
        self.generations: list[dict[str, Any]] = []
        self.ended = False

    def start_span(self, *, name: str, metadata: dict[str, Any]) -> _FakeObservation:
        child = _FakeObservation("span", name, metadata)
        child.trace_id = self.trace_id  # children share the trace
        self.children.append(child)
        return child

    def start_observation(self, *, as_type: str, name: str, **kwargs: Any) -> _FakeObservation:
        assert as_type == "generation"
        gen = _FakeObservation("generation", name)
        gen.trace_id = self.trace_id
        record = {"name": name, **kwargs}
        self.generations.append(record)
        self.children.append(gen)
        return gen

    def create_event(self, *, name: str, metadata: dict[str, Any]) -> None:
        self.events.append({"name": name, "metadata": dict(metadata)})

    def update(self, *, metadata: dict[str, Any] | None = None, **kwargs: Any) -> None:
        if metadata is not None:
            self.updates.append(dict(metadata))

    def update_trace(self, **kwargs: Any) -> None:
        self.trace_updates.append(dict(kwargs))

    def end(self, **kwargs: Any) -> None:
        self.ended = True


class _FakeClient:
    def __init__(self) -> None:
        self.roots: list[_FakeObservation] = []
        self.flushed = 0
        self.shutdowns = 0
        self.scores: list[dict[str, Any]] = []

    def start_span(self, *, name: str, metadata: dict[str, Any]) -> _FakeObservation:
        root = _FakeObservation("span", name, metadata)
        self.roots.append(root)
        return root

    def create_score(
        self,
        *,
        trace_id: str,
        name: str,
        value: float,
        data_type: str | None = None,
        comment: str | None = None,
    ) -> None:
        # v3 create_score returns None (no Score object).
        self.scores.append(
            {
                "trace_id": trace_id,
                "name": name,
                "value": value,
                "data_type": data_type,
                "comment": comment,
            }
        )

    def flush(self) -> None:
        self.flushed += 1

    def shutdown(self) -> None:
        self.shutdowns += 1


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
# LangfuseTracer behaviour with a fake v3 client
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_langfuse_tracer_creates_root_span_for_top_level_span() -> None:
    fake = _FakeClient()
    t = LangfuseTracer(client=fake)

    ctx = t.start_span("agent.execute", attrs={"agent": "demo"})
    assert ctx.parent_id is None
    # Created exactly one root span (the v3 trace anchor).
    assert len(fake.roots) == 1
    assert fake.roots[0].name == "agent.execute"
    assert fake.roots[0].metadata == {"agent": "demo"}
    # trace_id flows from the span object's v3 trace_id attribute.
    assert ctx.trace_id == fake.roots[0].trace_id


@pytest.mark.unit
def test_langfuse_tracer_maps_native_fields_to_update_trace() -> None:
    """``_session_id`` / ``_user_id`` / ``_tags`` are popped out of metadata
    and pushed onto the trace via v3 ``update_trace`` (v2 passed them as
    ``client.trace(...)`` kwargs)."""
    fake = _FakeClient()
    t = LangfuseTracer(client=fake)
    ctx = t.start_span(
        "agent.execute",
        attrs={
            "agent": "demo",
            "_session_id": "sess-1",
            "_user_id": "user-1",
            "_tags": ["a", "b"],
        },
    )
    root = fake.roots[0]
    # Native fields stripped from metadata, forwarded via update_trace.
    assert root.metadata == {"agent": "demo"}
    assert root.trace_updates == [{"session_id": "sess-1", "user_id": "user-1", "tags": ["a", "b"]}]
    assert ctx.attributes == {"agent": "demo"}


@pytest.mark.unit
def test_langfuse_tracer_no_update_trace_when_no_native_fields() -> None:
    """When no ``_``-prefixed native fields are present, ``update_trace`` is
    not called at all (don't push empty trace updates)."""
    fake = _FakeClient()
    t = LangfuseTracer(client=fake)
    t.start_span("agent.execute", attrs={"agent": "demo"})
    assert fake.roots[0].trace_updates == []


@pytest.mark.unit
def test_langfuse_tracer_nests_child_spans_under_root() -> None:
    fake = _FakeClient()
    t = LangfuseTracer(client=fake)

    parent = t.start_span("workflow.run", attrs={"workflow": "demo"})
    child = t.start_span("agent.execute", attrs={"node": "first"}, parent=parent)

    assert child.parent_id == parent.span_id
    assert child.trace_id == parent.trace_id
    # Root got a child span attached.
    assert len(fake.roots) == 1
    assert len(fake.roots[0].children) == 1
    assert fake.roots[0].children[0].name == "agent.execute"


@pytest.mark.unit
def test_langfuse_tracer_log_event_and_set_attribute() -> None:
    fake = _FakeClient()
    t = LangfuseTracer(client=fake)
    ctx = t.start_span("agent.execute")
    t.log_event(ctx, {"prompt_hash": "abc123"})
    t.set_attribute(ctx, "model", "openai/gpt-4o-mini")

    handle = fake.roots[0]
    assert handle.events == [{"name": "event", "metadata": {"prompt_hash": "abc123"}}]
    assert handle.updates == [{"model": "openai/gpt-4o-mini"}]
    # set_attribute also mirrors into the local SpanCtx.
    assert ctx.attributes["model"] == "openai/gpt-4o-mini"


@pytest.mark.unit
def test_langfuse_tracer_end_span_closes_root_via_end() -> None:
    """In v3 the root span DOES have ``end()`` (unlike v2, where the trace
    root was finalized via ``update()``). ``end_span`` records terminal
    status via ``update`` then calls ``end()``."""
    fake = _FakeClient()
    t = LangfuseTracer(client=fake)
    ctx = t.start_span("agent.execute")
    t.end_span(ctx, status="ok")

    root = fake.roots[0]
    assert root.ended is True
    assert {"status": "ok"} in root.updates
    # Calls after end are no-ops (handle popped; no second event).
    t.log_event(ctx, {"too": "late"})
    assert len(root.events) == 0


@pytest.mark.unit
def test_langfuse_tracer_end_span_closes_child_via_end() -> None:
    """A child span is closed with ``end()`` and gets terminal status."""
    fake = _FakeClient()
    t = LangfuseTracer(client=fake)
    parent = t.start_span("agent.execute")
    child = t.start_span("tool.call", parent=parent)
    t.end_span(child, status="ok")

    child_handle = fake.roots[0].children[0]
    assert child_handle.ended is True
    assert {"status": "ok"} in child_handle.updates


@pytest.mark.unit
def test_langfuse_tracer_log_generation_emits_generation_observation() -> None:
    """log_generation creates a child generation observation with v3
    ``usage_details`` + ``cost_details`` and ends it immediately."""
    fake = _FakeClient()
    t = LangfuseTracer(client=fake)
    ctx = t.start_span("agent.execute")
    t.log_generation(
        ctx,
        model="openai/gpt-4o-mini",
        input_messages=[{"role": "user", "content": "hi"}],
        output_text="hello",
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.002,
    )

    root = fake.roots[0]
    assert len(root.generations) == 1
    gen = root.generations[0]
    assert gen["name"] == "llm-completion"
    assert gen["model"] == "openai/gpt-4o-mini"
    assert gen["input"] == [{"role": "user", "content": "hi"}]
    assert gen["output"] == "hello"
    assert gen["usage_details"] == {"input": 10, "output": 5, "total": 15}
    assert gen["cost_details"] == {"total": 0.002}
    # The generation observation was ended immediately.
    assert root.children[-1].kind == "generation"
    assert root.children[-1].ended is True


@pytest.mark.unit
def test_langfuse_tracer_log_generation_omits_cost_when_zero() -> None:
    fake = _FakeClient()
    t = LangfuseTracer(client=fake)
    ctx = t.start_span("agent.execute")
    t.log_generation(
        ctx,
        model="m",
        input_messages=[],
        output_text="x",
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.0,
    )
    assert fake.roots[0].generations[0]["cost_details"] is None


@pytest.mark.unit
def test_langfuse_tracer_end_span_never_raises_on_root() -> None:
    """Ending a root span must not raise (tracing never breaks a run)."""
    fake = _FakeClient()
    t = LangfuseTracer(client=fake)
    ctx = t.start_span("eval.generate")
    t.end_span(ctx, status="ok")  # no exception


@pytest.mark.unit
def test_langfuse_tracer_flush_calls_client() -> None:
    fake = _FakeClient()
    t = LangfuseTracer(client=fake)
    t.flush()
    t.flush()
    assert fake.flushed == 2


@pytest.mark.unit
def test_langfuse_tracer_shutdown_calls_client_shutdown() -> None:
    """v3 is OTel-based; ``shutdown`` flushes and tears down the exporter."""
    fake = _FakeClient()
    t = LangfuseTracer(client=fake)
    t.shutdown()
    assert fake.shutdowns == 1


@pytest.mark.unit
def test_langfuse_tracer_shutdown_falls_back_to_flush() -> None:
    """A client exposing only ``flush()`` (e.g. an older stub) still works:
    ``shutdown`` falls back to ``flush``."""

    class _FlushOnly:
        def __init__(self) -> None:
            self.flushed = 0

        def flush(self) -> None:
            self.flushed += 1

    fake = _FlushOnly()
    t = LangfuseTracer(client=fake)
    t.shutdown()
    assert fake.flushed == 1


@pytest.mark.unit
def test_langfuse_tracer_orphaned_child_falls_through_to_root() -> None:
    """If a span's recorded parent has already ended, ``start_span(parent=...)``
    should still produce a usable handle (creates a new root span) rather than
    raising — tracing must never break a run."""
    fake = _FakeClient()
    t = LangfuseTracer(client=fake)
    parent = t.start_span("parent")
    t.end_span(parent)
    child = t.start_span("orphan", parent=parent)
    assert child.span_id  # handle was created
    # New root span was opened to host the orphan.
    assert len(fake.roots) == 2


# ---------------------------------------------------------------------------
# score_trace / push_run_feedback_score (Langfuse-specific extensions)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_langfuse_tracer_score_trace_pushes_create_score() -> None:
    fake = _FakeClient()
    t = LangfuseTracer(client=fake)
    # v3 create_score returns None (no Score object) → score_trace returns None.
    result = await t.score_trace(
        trace_id="trace-xyz",
        name="eval_accuracy",
        value=0.9,
        comment="good",
    )
    assert result is None
    assert fake.scores == [
        {
            "trace_id": "trace-xyz",
            "name": "eval_accuracy",
            "value": 0.9,
            "data_type": "NUMERIC",
            "comment": "good",
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_langfuse_tracer_score_trace_noop_without_trace_id() -> None:
    fake = _FakeClient()
    t = LangfuseTracer(client=fake)
    assert await t.score_trace(trace_id="", name="x", value=1.0) is None
    assert fake.scores == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_langfuse_tracer_push_run_feedback_score() -> None:
    class _Metrics:
        langfuse_trace_id = "trace-fb"

    class _Run:
        metrics = _Metrics()

    class _Feedback:
        score = 1
        comment = "nice"

    fake = _FakeClient()
    t = LangfuseTracer(client=fake)
    # v3 create_score returns None, so the cross-link id is no longer available.
    result = await t.push_run_feedback_score(_Run(), _Feedback())
    assert result is None
    assert fake.scores == [
        {
            "trace_id": "trace-fb",
            "name": "user_feedback",
            "value": 1.0,
            "data_type": "NUMERIC",
            "comment": "nice",
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_langfuse_tracer_push_run_feedback_score_skips_without_trace() -> None:
    class _Metrics:
        langfuse_trace_id = None
        trace_id = None

    class _Run:
        metrics = _Metrics()

    class _Feedback:
        score = 1
        comment = None

    fake = _FakeClient()
    t = LangfuseTracer(client=fake)
    assert await t.push_run_feedback_score(_Run(), _Feedback()) is None
    assert fake.scores == []


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
