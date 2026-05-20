"""Tests for ``movate.kb.trace.emit_to_tracer`` (PR-V).

Operators inspecting an agent run in Langfuse / OTel see retrieval
stages as nested child spans under the agent's run span, instead
of needing ``--trace`` at the CLI to see them.

Coverage:
* Root span + one child per stage emitted in order
* Child span attributes carry duration_ms, counts, details,
  chunk_count, chunk_ids_preview
* parent_span parameter wires the root under a caller-supplied parent
* Empty trace → only root span (no children)
* Tracer exception swallowed — never blocks retrieval
* Skill template integration: when ctx.tracer is set, the
  kb-vector-lookup skill creates a SearchTrace + emits it
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from movate.core.models import KbChunk
from movate.kb.trace import SearchTrace, emit_to_tracer
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Fake tracer for inspection
# ---------------------------------------------------------------------------


class _FakeSpanCtx:
    """Stand-in for SpanCtx — just an id + name + attrs we record."""

    def __init__(self, name: str, attrs: dict[str, Any], parent_id: str | None) -> None:
        import uuid  # noqa: PLC0415

        self.span_id = uuid.uuid4().hex
        self.name = name
        self.attributes = attrs
        self.parent_id = parent_id


class _FakeTracer:
    """Records start_span / end_span calls in order for inspection."""

    name = "fake"

    def __init__(self) -> None:
        self.starts: list[_FakeSpanCtx] = []
        self.ends: list[tuple[str, str]] = []  # (span_id, status)

    def start_span(
        self,
        name: str,
        attrs: dict[str, Any] | None = None,
        parent: _FakeSpanCtx | None = None,
    ) -> _FakeSpanCtx:
        ctx = _FakeSpanCtx(name, dict(attrs or {}), parent.span_id if parent else None)
        self.starts.append(ctx)
        return ctx

    def end_span(self, span: _FakeSpanCtx, status: str = "ok") -> None:
        self.ends.append((span.span_id, status))

    def log_event(self, span: _FakeSpanCtx, event: dict[str, Any]) -> None:
        pass

    def set_attribute(self, span: _FakeSpanCtx, key: str, value: Any) -> None:
        span.attributes[key] = value


# ---------------------------------------------------------------------------
# emit_to_tracer — happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_emit_to_tracer_creates_root_and_one_child_per_stage() -> None:
    trace = SearchTrace()
    trace.record("retrieve[0]", 45.0, output_count=20, chunk_ids=["c1", "c2"])
    trace.record("rerank", 200.0, input_count=20, output_count=5, chunk_ids=["c1"])

    tracer = _FakeTracer()
    root = emit_to_tracer(trace, tracer)

    # 1 root + 2 stage children = 3 spans started.
    assert len(tracer.starts) == 3
    assert tracer.starts[0].name == "kb_search"
    assert tracer.starts[1].name == "retrieve[0]"
    assert tracer.starts[2].name == "rerank"
    # Children parented to the root.
    assert tracer.starts[1].parent_id == tracer.starts[0].span_id
    assert tracer.starts[2].parent_id == tracer.starts[0].span_id
    # All spans ended in reverse order (innermost first).
    assert len(tracer.ends) == 3
    # Returns the root.
    assert root is tracer.starts[0]


@pytest.mark.unit
def test_emit_to_tracer_stamps_duration_and_counts_on_children() -> None:
    trace = SearchTrace()
    trace.record(
        "retrieve[0]",
        45.5,
        input_count=0,
        output_count=20,
        details={"mode": "hybrid", "variant": "refunds?"},
    )
    tracer = _FakeTracer()
    emit_to_tracer(trace, tracer)
    child = tracer.starts[1]
    assert child.attributes["duration_ms"] == 45.5
    assert child.attributes["input_count"] == 0
    assert child.attributes["output_count"] == 20
    assert child.attributes["mode"] == "hybrid"
    assert child.attributes["variant"] == "refunds?"


@pytest.mark.unit
def test_emit_to_tracer_includes_chunk_count_and_preview() -> None:
    """When chunk_ids is non-None, the child span carries chunk_count
    + chunk_ids_preview (capped to 10)."""
    trace = SearchTrace()
    big_chunks = [f"c{i}" for i in range(20)]
    trace.record("retrieve[0]", 10.0, output_count=20, chunk_ids=big_chunks)
    tracer = _FakeTracer()
    emit_to_tracer(trace, tracer)
    child = tracer.starts[1]
    assert child.attributes["chunk_count"] == 20
    # Preview capped at 10.
    assert len(child.attributes["chunk_ids_preview"]) == 10
    assert child.attributes["chunk_ids_preview"][0] == "c0"


@pytest.mark.unit
def test_emit_to_tracer_omits_chunk_attrs_when_chunk_ids_is_none() -> None:
    """The rewriter stage (chunk_ids=None) → no chunk_count /
    chunk_ids_preview keys on the span."""
    trace = SearchTrace()
    trace.record("rewrite", 180.0, output_count=3, chunk_ids=None)
    tracer = _FakeTracer()
    emit_to_tracer(trace, tracer)
    child = tracer.starts[1]
    assert "chunk_count" not in child.attributes
    assert "chunk_ids_preview" not in child.attributes


@pytest.mark.unit
def test_emit_to_tracer_under_parent_span() -> None:
    """When parent_span is supplied, the root span parents under it
    (not as a new top-level trace)."""
    tracer = _FakeTracer()
    fake_parent = _FakeSpanCtx("run", {}, None)
    trace = SearchTrace()
    trace.record("retrieve[0]", 1.0, output_count=1)
    emit_to_tracer(trace, tracer, parent_span=fake_parent)
    # Root's parent_id matches the supplied span.
    assert tracer.starts[0].parent_id == fake_parent.span_id


@pytest.mark.unit
def test_emit_to_tracer_empty_trace_emits_root_only() -> None:
    """A SearchTrace with no stages → root span only (no children)."""
    tracer = _FakeTracer()
    emit_to_tracer(SearchTrace(), tracer)
    assert len(tracer.starts) == 1
    assert tracer.starts[0].name == "kb_search"
    # Stage count = 0 on the root.
    assert tracer.starts[0].attributes["stage_count"] == 0


@pytest.mark.unit
def test_emit_to_tracer_custom_root_name() -> None:
    tracer = _FakeTracer()
    emit_to_tracer(SearchTrace(), tracer, root_name="my_pipeline")
    assert tracer.starts[0].name == "my_pipeline"


# ---------------------------------------------------------------------------
# Defensive — tracer failure swallowed
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_emit_to_tracer_swallows_tracer_exception() -> None:
    """An exploding tracer never blocks retrieval — observability is
    a sink, not a critical path."""
    broken = MagicMock()
    broken.start_span.side_effect = RuntimeError("Langfuse 5xx")
    # Should NOT raise.
    result = emit_to_tracer(SearchTrace(), broken)
    assert result is None


@pytest.mark.unit
def test_emit_to_tracer_swallows_end_span_exception() -> None:
    """Even if start succeeds but end_span fails mid-loop, the call
    returns cleanly + the failure doesn't bubble up to the agent."""
    tracer = MagicMock()
    tracer.start_span.return_value = _FakeSpanCtx("root", {}, None)
    tracer.end_span.side_effect = RuntimeError("oops")
    trace = SearchTrace()
    trace.record("retrieve[0]", 1.0)
    # Should NOT raise.
    emit_to_tracer(trace, tracer)


# ---------------------------------------------------------------------------
# Skill template integration
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_skill_emits_search_trace_when_tracer_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kb-vector-lookup skill template — when ctx.tracer is set —
    creates a SearchTrace + emits it after the search. End-to-end:
    we can pull spans off the fake tracer."""
    from movate.core.skill_backend.base import SkillExecutionContext  # noqa: PLC0415
    from movate.templates.skill_kb_vector_lookup import impl  # noqa: PLC0415

    storage = InMemoryStorage()
    await storage.init()
    # Seed one chunk so the search has something to return.
    await storage.save_kb_chunk(
        KbChunk(
            tenant_id="t1",
            agent="rag-qa",
            source="/tmp/x.md",
            text="hello",
            embedding=[1.0, 0.0],
            embedding_model="openai/text-embedding-3-small",
            content_hash="h",
        )
    )
    # Stub the embedding call so the skill doesn't need an OpenAI key.
    from movate.kb import search as search_mod  # noqa: PLC0415

    async def fake_embed(texts, *, model="", api_key=None, timeout_s=60.0):
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(search_mod, "embed_texts", fake_embed)

    tracer = _FakeTracer()
    parent = _FakeSpanCtx("agent_run", {}, None)
    ctx = SkillExecutionContext(
        tenant_id="t1",
        agent_name="rag-qa",
        storage=storage,
        tracer=tracer,
        parent_span=parent,
    )
    await impl.run({"question": "hello"}, ctx=ctx)

    # Tracer should have a root + one retrieve[0] child.
    assert any(s.name == "kb_search" for s in tracer.starts)
    assert any(s.name == "retrieve[0]" for s in tracer.starts)
    # Retrieve child parented under the kb_search root.
    root = next(s for s in tracer.starts if s.name == "kb_search")
    retrieve = next(s for s in tracer.starts if s.name == "retrieve[0]")
    assert retrieve.parent_id == root.span_id
    # And the kb_search root is parented under the agent_run span.
    assert root.parent_id == parent.span_id


@pytest.mark.unit
async def test_skill_skips_trace_export_when_no_tracer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ctx has no tracer (CLI path / ctx=None), no SearchTrace
    is created — zero overhead for the default code path."""
    from movate.core.skill_backend.base import SkillExecutionContext  # noqa: PLC0415
    from movate.templates.skill_kb_vector_lookup import impl  # noqa: PLC0415

    storage = InMemoryStorage()
    await storage.init()
    await storage.save_kb_chunk(
        KbChunk(
            tenant_id="t1",
            agent="rag-qa",
            source="/tmp/x.md",
            text="hello",
            embedding=[1.0, 0.0],
            embedding_model="openai/text-embedding-3-small",
            content_hash="h",
        )
    )
    from movate.kb import search as search_mod  # noqa: PLC0415

    async def fake_embed(texts, *, model="", api_key=None, timeout_s=60.0):
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(search_mod, "embed_texts", fake_embed)

    ctx = SkillExecutionContext(
        tenant_id="t1",
        agent_name="rag-qa",
        storage=storage,
        # No tracer set.
    )
    result = await impl.run({"question": "hello"}, ctx=ctx)
    # Skill still returned chunks normally — trace export skip
    # didn't break the retrieval path.
    assert result["chunks_found"] == 1


# Silence unused-import warning when asyncio is imported but unused.
_ = asyncio
