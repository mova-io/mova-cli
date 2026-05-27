"""ADR 023 — opt-in declarative pre-retrieval (auto-RAG) in the shared Executor.

Covers the full D-matrix (hermetic: MockProvider + InMemoryStorage /
SqliteProvider via the parametrized ``storage`` fixture, no API keys, no
real network):

1. No ``retrieval:`` block → execution byte-for-byte unchanged
   (non-RAG regression guard).
2. ``auto_into`` + empty/absent target field → retrieves, merges chunks,
   prompt is grounded.
3. ``auto_into`` + explicitly-passed target field + ``when: if_empty`` →
   retrieval SKIPPED, explicit value used (eval-determinism guard).
4. ``when: always`` → re-retrieves even when the field is non-empty.
5. No retriever / KB configured → no-op + notice, run SUCCEEDS.
6. Retrieval error + ``on_error: warn`` → ungrounded + notice (succeeds);
   ``on_error: fail`` → typed error.
7. Empty retrieval results → field set to ``[]``, run proceeds.
8. ``mdk validate`` rejects: unresolved ``retrieval.skill``; an
   ``auto_into`` field that can't hold the chunk shape; ambiguous
   ``query_from``.

The retrieval skills used here are plain Python functions defined in
this module (referenced by their ``entry`` strings) so every test is
deterministic and offline. Case 2 also exercises a skill that reads
``ctx.storage`` directly against a seeded KB to prove the phase grounds
against whatever ``StorageProvider`` is configured (ADR 023 D4 mock note).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.core.models import RunRequest
from movate.core.skill_backend import SkillExecutionContext
from movate.providers.mock import MockProvider
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import NullTracer

# ---------------------------------------------------------------------------
# Module-level retrieval skills (referenced by `entry` strings below)
# ---------------------------------------------------------------------------


async def _fixed_chunks_skill(
    inputs: dict[str, Any], ctx: SkillExecutionContext | None = None
) -> dict[str, Any]:
    """Returns two canned chunks shaped like kb-vector-lookup's output."""
    q = (inputs.get("question") or "").strip()
    return {
        "chunks": [
            {"text": f"CANNED-CHUNK-A about {q}", "source": "/kb/a.md", "score": 0.9},
            {"text": "CANNED-CHUNK-B refund window is 14 days", "source": "/kb/b.md", "score": 0.8},
        ],
        "chunks_found": 2,
    }


async def _empty_chunks_skill(
    inputs: dict[str, Any], ctx: SkillExecutionContext | None = None
) -> dict[str, Any]:
    """Returns zero chunks — the empty-KB / no-hit outcome (NOT an error)."""
    return {"chunks": [], "chunks_found": 0}


async def _exploding_skill(
    inputs: dict[str, Any], ctx: SkillExecutionContext | None = None
) -> dict[str, Any]:
    """Raises — exercises the SkillError → on_error path."""
    raise RuntimeError("embedding endpoint exploded")


async def _storage_backed_skill(
    inputs: dict[str, Any], ctx: SkillExecutionContext | None = None
) -> dict[str, Any]:
    """Reads the agent's KB chunks from ``ctx.storage`` and returns them.

    Proves the pre-retrieval phase runs against whatever
    ``StorageProvider`` the Executor was built with — a mock run against
    a seeded in-memory / sqlite KB is genuinely grounded (ADR 023 D4).
    Uses a fixed query embedding so no real embedder is needed.
    """
    assert ctx is not None
    storage = ctx.storage
    results = await storage.search_kb_chunks(
        agent=ctx.agent_name,
        tenant_id=ctx.tenant_id,
        query_embedding=[1.0, 0.0],
        limit=int(inputs.get("k") or 5),
    )
    return {
        "chunks": [
            {"text": r.chunk.text, "source": r.chunk.source, "score": round(r.score, 4)}
            for r in results
        ],
        "chunks_found": len(results),
    }


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def pricing() -> PricingTable:
    return load_pricing()


class _RecordingTracer(NullTracer):
    """NullTracer that records every log_event payload for assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict] = []

    def log_event(self, span, event) -> None:  # type: ignore[override]
        self.events.append(dict(event))


_RETRIEVAL_SKILL_YAML = """\
api_version: movate/v1
kind: Skill
name: {name}
version: 0.1.0
description: test retrieval skill {name}
schema:
  input:
    question: string
    k: integer?
  output:
    chunks:
      - text: string
        source: string?
        score: number?
    chunks_found: integer
implementation:
  kind: python
  entry: {entry}
cost:
  per_call_usd: 0.0001
"""


def _write_retrieval_skill(
    parent: Path, name: str, *, entry: str = f"{__name__}:_fixed_chunks_skill"
) -> Path:
    """Write a python-kind retrieval skill dir.

    ``entry`` is a ``module:func`` string. A real ``impl.py`` is written
    alongside ``skill.yaml`` (re-exporting the named function as ``run``)
    so the skill passes ``mdk validate``'s impl.py-reachability check —
    matching the real-world skill layout (``<name>.impl:run``). Runtime
    dispatch still resolves the ``entry`` exactly as given, so tests that
    point ``entry`` at a specific module-level skill function stay
    deterministic and offline.
    """
    skill_dir = parent / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(_RETRIEVAL_SKILL_YAML.format(name=name, entry=entry))
    # A real impl.py so `mdk validate`'s impl.py-reachability check passes.
    # It re-exports the configured entry function as `run` (importlib in the
    # python backend resolves the hyphenated dir as a namespace package).
    module, _, func = entry.partition(":")
    (skill_dir / "impl.py").write_text(f"from {module} import {func} as run\n")
    return skill_dir


# Canonical-format schema files (the business-readable DSL) so `context`
# can be a genuinely OPTIONAL `list[string]` — inline shorthand can't
# mark an array field optional (the `?` suffix is scalar-only), which is
# exactly the shape the if_empty / always cases need.
_INPUT_SCHEMA_YAML = """\
version: 1
type: object
fields:
  question:
    type: string
  context:
    type: list[string]
{extra_fields}required:
{required}
"""

_OUTPUT_SCHEMA_YAML = """\
version: 1
type: object
fields:
  answer:
    type: string
required:
  - answer
"""


def _write_rag_agent(
    project_root: Path,
    *,
    name: str = "rag",
    retrieval_block: str | None = None,
    skill_name: str = "kb-lookup",
    context_required: bool = False,
    extra_input: str = "",
) -> Path:
    """Write a flat agent dir (sibling of ``skills/``) with a question +
    context input. ``retrieval_block`` is spliced verbatim into agent.yaml.

    ``context`` is optional by default so an absent field triggers
    if_empty retrieval; pass ``context_required=True`` to make it
    required. The input schema is written as a canonical-format
    ``schema/input.yaml`` (path form) because inline shorthand cannot
    express an optional array field.
    """
    agent_dir = project_root / name
    agent_dir.mkdir(parents=True)
    schema_dir = agent_dir / "schema"
    schema_dir.mkdir()
    required = "  - question\n" + ("  - context\n" if context_required else "")
    (schema_dir / "input.yaml").write_text(
        _INPUT_SCHEMA_YAML.format(extra_fields=extra_input, required=required.rstrip("\n"))
    )
    (schema_dir / "output.yaml").write_text(_OUTPUT_SCHEMA_YAML)
    block = f"\n{retrieval_block}\n" if retrieval_block else "\n"
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        f"name: {name}\n"
        "version: 0.1.0\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        "schema:\n"
        "  input: ./schema/input.yaml\n"
        "  output: ./schema/output.yaml\n"
        "skills:\n"
        f"  - {skill_name}\n"
        f"{block}"
    )
    # Prompt renders the context list so grounding shows up in the
    # rendered_prompt trace event.
    # `is defined` guard is required: the loader renders with
    # StrictUndefined, so a bare `{% if input.context %}` raises when the
    # optional field is absent (the non-RAG / no-op paths).
    (agent_dir / "prompt.md").write_text(
        "Q: {{ input.question }}\n"
        "{% if input.context is defined and input.context %}CONTEXT:\n"
        "{% for c in input.context %}- {{ c }}\n{% endfor %}"
        "{% else %}(no context){% endif %}"
    )
    return agent_dir


def _make_executor(storage: Any, pricing: PricingTable, tracer: Any) -> Executor:
    return Executor(
        provider=MockProvider(response='{"answer": "ok"}'),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )


def _rendered_prompt(tracer: _RecordingTracer) -> str:
    evts = [e for e in tracer.events if "rendered_prompt" in e]
    assert evts, "expected a rendered_prompt trace event"
    return evts[0]["rendered_prompt"]


# ---------------------------------------------------------------------------
# Case 1 — no retrieval block → byte-for-byte unchanged
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_no_retrieval_block_is_unchanged(
    tmp_path: Path, storage, pricing: PricingTable
) -> None:
    """Non-RAG regression guard: with NO ``retrieval.auto_into``, the
    Executor never enters the pre-retrieval phase — no pre_retrieval
    event, no skill invocation, no field mutation."""
    _write_retrieval_skill(tmp_path / "skills", "kb-lookup")
    agent_dir = _write_rag_agent(tmp_path)  # no retrieval block
    bundle = load_agent(agent_dir)
    assert not bundle.spec.retrieval.auto_retrieval_enabled

    rec = _RecordingTracer()
    ex = _make_executor(storage, pricing, rec)
    req = RunRequest(agent="rag", input={"question": "what is the refund policy?"})
    resp = await ex.execute(bundle, req)

    assert resp.status == "success"
    # No pre-retrieval events at all.
    assert not any("pre_retrieval" in k for e in rec.events for k in e)
    # The input field was untouched (still absent).
    assert "context" not in req.input
    # Prompt rendered the empty-context branch.
    assert "(no context)" in _rendered_prompt(rec)


# ---------------------------------------------------------------------------
# Case 2 — auto_into + empty field → retrieves, merges, grounded
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_auto_into_empty_field_grounds(
    tmp_path: Path, storage, pricing: PricingTable
) -> None:
    """``auto_into`` + absent field → the phase retrieves, merges chunk
    TEXTS as list[string] into the field, and the prompt is grounded."""
    _write_retrieval_skill(tmp_path / "skills", "kb-lookup")
    agent_dir = _write_rag_agent(
        tmp_path,
        retrieval_block="retrieval:\n  auto_into: context\n  skill: kb-lookup\n  top_k: 4\n",
    )
    bundle = load_agent(agent_dir)
    assert bundle.spec.retrieval.auto_retrieval_enabled

    rec = _RecordingTracer()
    ex = _make_executor(storage, pricing, rec)
    req = RunRequest(agent="rag", input={"question": "refund policy?"})
    resp = await ex.execute(bundle, req)

    assert resp.status == "success"
    # Field populated with list[string] chunk texts.
    assert req.input["context"] == [
        "CANNED-CHUNK-A about refund policy?",
        "CANNED-CHUNK-B refund window is 14 days",
    ]
    merged = [e for e in rec.events if e.get("pre_retrieval") == "merged"]
    assert merged and merged[0]["chunks_merged"] == 2
    # Prompt is grounded — the chunk text shows up in the rendered prompt.
    assert "CANNED-CHUNK-A about refund policy?" in _rendered_prompt(rec)


@pytest.mark.unit
async def test_auto_into_grounds_against_seeded_kb(
    tmp_path: Path, storage, pricing: PricingTable
) -> None:
    """Mock run against a seeded in-memory / sqlite KB is genuinely
    grounded — the skill reads ``ctx.storage`` (the Executor's
    configured StorageProvider), proving grounding is real (D4)."""
    from movate.core.models import KbChunk  # noqa: PLC0415

    await storage.save_kb_chunk(
        KbChunk(
            tenant_id="local",
            agent="rag",
            source="/kb/policy.md",
            text="Annual subscriptions are refundable within 14 days.",
            embedding=[1.0, 0.0],
            embedding_model="test/dim2",
            content_hash="h1",
        )
    )
    _write_retrieval_skill(
        tmp_path / "skills", "kb-lookup", entry=f"{__name__}:_storage_backed_skill"
    )
    agent_dir = _write_rag_agent(
        tmp_path,
        retrieval_block="retrieval:\n  auto_into: context\n  skill: kb-lookup\n",
    )
    bundle = load_agent(agent_dir)

    rec = _RecordingTracer()
    ex = _make_executor(storage, pricing, rec)
    req = RunRequest(agent="rag", input={"question": "refund?"})
    resp = await ex.execute(bundle, req)

    assert resp.status == "success"
    assert req.input["context"] == ["Annual subscriptions are refundable within 14 days."]
    assert "refundable within 14 days" in _rendered_prompt(rec)


# ---------------------------------------------------------------------------
# Case 3 — explicit field + if_empty → retrieval SKIPPED
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_explicit_field_if_empty_skips_retrieval(
    tmp_path: Path, storage, pricing: PricingTable
) -> None:
    """Eval-determinism guard: ``when: if_empty`` (default) respects an
    explicitly-passed value — retrieval is skipped, explicit value used."""
    _write_retrieval_skill(tmp_path / "skills", "kb-lookup")
    agent_dir = _write_rag_agent(
        tmp_path,
        retrieval_block="retrieval:\n  auto_into: context\n  skill: kb-lookup\n",
    )
    bundle = load_agent(agent_dir)

    rec = _RecordingTracer()
    ex = _make_executor(storage, pricing, rec)
    explicit = ["EXPLICIT caller-supplied grounding"]
    req = RunRequest(agent="rag", input={"question": "q", "context": list(explicit)})
    resp = await ex.execute(bundle, req)

    assert resp.status == "success"
    # Explicit value preserved; no canned chunks merged.
    assert req.input["context"] == explicit
    skipped = [e for e in rec.events if e.get("pre_retrieval_skipped") == "field_non_empty"]
    assert skipped
    assert not any(e.get("pre_retrieval") == "merged" for e in rec.events)
    assert "EXPLICIT caller-supplied grounding" in _rendered_prompt(rec)


# ---------------------------------------------------------------------------
# Case 4 — when: always → re-retrieves even when non-empty
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_when_always_reretrieves(tmp_path: Path, storage, pricing: PricingTable) -> None:
    """``when: always`` re-retrieves and OVERWRITES even a non-empty
    field with fresh chunks."""
    _write_retrieval_skill(tmp_path / "skills", "kb-lookup")
    agent_dir = _write_rag_agent(
        tmp_path,
        retrieval_block="retrieval:\n  auto_into: context\n  skill: kb-lookup\n  when: always\n",
    )
    bundle = load_agent(agent_dir)

    rec = _RecordingTracer()
    ex = _make_executor(storage, pricing, rec)
    req = RunRequest(agent="rag", input={"question": "q", "context": ["STALE value"]})
    resp = await ex.execute(bundle, req)

    assert resp.status == "success"
    # The stale value was replaced by retrieved chunks.
    assert "STALE value" not in req.input["context"]
    assert req.input["context"][0].startswith("CANNED-CHUNK-A")
    assert any(e.get("pre_retrieval") == "merged" for e in rec.events)


# ---------------------------------------------------------------------------
# Case 5 — no retriever / skill not wired → no-op + notice, succeeds
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_missing_skill_is_noop_notice(
    tmp_path: Path, storage, pricing: PricingTable, caplog: pytest.LogCaptureFixture
) -> None:
    """A run-time-missing retrieval skill (e.g. a bundle that skipped
    validate) is a NO-OP with a notice — the run SUCCEEDS, field set to
    [] (D4). Validate would normally catch this; runtime must not
    hard-fail the dominant path."""
    # Wire a real skill so the agent LOADS, then point retrieval at a
    # DIFFERENT (unwired) name to simulate the run-time-missing case.
    _write_retrieval_skill(tmp_path / "skills", "kb-lookup")
    agent_dir = _write_rag_agent(
        tmp_path,
        retrieval_block="retrieval:\n  auto_into: context\n  skill: not-wired\n",
    )
    bundle = load_agent(agent_dir)

    rec = _RecordingTracer()
    ex = _make_executor(storage, pricing, rec)
    req = RunRequest(agent="rag", input={"question": "q"})
    with caplog.at_level(logging.WARNING):
        resp = await ex.execute(bundle, req)

    assert resp.status == "success"
    assert req.input["context"] == []
    assert "not-wired" in caplog.text
    assert "pre-retrieval" in caplog.text


# ---------------------------------------------------------------------------
# Case 6 — retrieval error: warn proceeds, fail aborts
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_retrieval_error_on_error_warn_proceeds(
    tmp_path: Path, storage, pricing: PricingTable, caplog: pytest.LogCaptureFixture
) -> None:
    """``on_error: warn`` (default) → proceed ungrounded + notice; the
    run SUCCEEDS, field set to []."""
    _write_retrieval_skill(tmp_path / "skills", "kb-lookup", entry=f"{__name__}:_exploding_skill")
    agent_dir = _write_rag_agent(
        tmp_path,
        retrieval_block="retrieval:\n  auto_into: context\n  skill: kb-lookup\n  on_error: warn\n",
    )
    bundle = load_agent(agent_dir)

    rec = _RecordingTracer()
    ex = _make_executor(storage, pricing, rec)
    req = RunRequest(agent="rag", input={"question": "q"})
    with caplog.at_level(logging.WARNING):
        resp = await ex.execute(bundle, req)

    assert resp.status == "success"
    assert req.input["context"] == []
    assert "exploded" in caplog.text
    # A typed SkillError type was surfaced on the notice event.
    assert any("pre_retrieval_error" in e for e in rec.events)


@pytest.mark.unit
async def test_retrieval_error_on_error_fail_aborts(
    tmp_path: Path, storage, pricing: PricingTable
) -> None:
    """``on_error: fail`` → the run aborts with a typed MovateError."""
    _write_retrieval_skill(tmp_path / "skills", "kb-lookup", entry=f"{__name__}:_exploding_skill")
    agent_dir = _write_rag_agent(
        tmp_path,
        retrieval_block="retrieval:\n  auto_into: context\n  skill: kb-lookup\n  on_error: fail\n",
    )
    bundle = load_agent(agent_dir)

    rec = _RecordingTracer()
    ex = _make_executor(storage, pricing, rec)
    req = RunRequest(agent="rag", input={"question": "q"})
    resp = await ex.execute(bundle, req)

    # The executor maps the raised SkillError to a failed run rather than
    # crashing the process — but it must be a non-success outcome, and
    # the field is NOT populated.
    assert resp.status != "success"
    assert "context" not in req.input


@pytest.mark.unit
async def test_retrieval_error_fail_raises_typed_error_directly(
    tmp_path: Path, storage, pricing: PricingTable
) -> None:
    """White-box: the pre-retrieval helper raises a typed
    :class:`ToolError` (the MovateError taxonomy) when ``on_error:
    fail``, carrying the underlying SkillError type/message. The
    executor's outer ``except MovateError`` handler maps it to a failed
    RunResponse."""
    from movate.core.failures import ToolError  # noqa: PLC0415

    _write_retrieval_skill(tmp_path / "skills", "kb-lookup", entry=f"{__name__}:_exploding_skill")
    agent_dir = _write_rag_agent(
        tmp_path,
        retrieval_block="retrieval:\n  auto_into: context\n  skill: kb-lookup\n  on_error: fail\n",
    )
    bundle = load_agent(agent_dir)
    rec = _RecordingTracer()
    ex = _make_executor(storage, pricing, rec)
    req = RunRequest(agent="rag", input={"question": "q"})
    with pytest.raises(ToolError) as excinfo:
        await ex._maybe_pre_retrieve(
            bundle=bundle,
            request=req,
            span=rec.start_span("t", {}),
            run_id="r1",
            tenant_id="local",
        )
    assert excinfo.value.retryable is False
    assert "kb-lookup" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Case 7 — empty results → field set to [], run proceeds
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_empty_results_sets_empty_list(
    tmp_path: Path, storage, pricing: PricingTable, caplog: pytest.LogCaptureFixture
) -> None:
    """Empty retrieval results → ``auto_into`` set to [] and run proceeds
    (deterministic; the prompt's no-context branch fires)."""
    _write_retrieval_skill(
        tmp_path / "skills", "kb-lookup", entry=f"{__name__}:_empty_chunks_skill"
    )
    agent_dir = _write_rag_agent(
        tmp_path,
        retrieval_block="retrieval:\n  auto_into: context\n  skill: kb-lookup\n",
    )
    bundle = load_agent(agent_dir)

    rec = _RecordingTracer()
    ex = _make_executor(storage, pricing, rec)
    req = RunRequest(agent="rag", input={"question": "q"})
    with caplog.at_level(logging.WARNING):
        resp = await ex.execute(bundle, req)

    assert resp.status == "success"
    assert req.input["context"] == []
    # A merge event fired with 0 chunks, and a "0 chunks" notice logged.
    merged = [e for e in rec.events if e.get("pre_retrieval") == "merged"]
    assert merged and merged[0]["chunks_merged"] == 0
    assert "0 chunks" in caplog.text
    assert "(no context)" in _rendered_prompt(rec)


# ---------------------------------------------------------------------------
# Case 8 — mdk validate rejects misconfigured retrieval blocks
# ---------------------------------------------------------------------------


def _run_validate(agent_dir: Path):
    import typer  # noqa: PLC0415

    from movate.cli.validate import _validate_agent  # noqa: PLC0415

    try:
        _validate_agent(agent_dir, strict=False, run_linter=False)
    except typer.Exit as exc:
        return exc.exit_code
    return 0


@pytest.mark.unit
def test_validate_rejects_unresolved_retrieval_skill(tmp_path: Path) -> None:
    _write_retrieval_skill(tmp_path / "skills", "kb-lookup")
    # retrieval.skill points at a name NOT in the agent's skills list.
    agent_dir = _write_rag_agent(
        tmp_path,
        retrieval_block="retrieval:\n  auto_into: context\n  skill: ghost-skill\n",
    )
    assert _run_validate(agent_dir) == 2


@pytest.mark.unit
def test_validate_rejects_auto_into_wrong_shape(tmp_path: Path) -> None:
    """``auto_into`` pointing at a string (not list[string]) field is
    rejected — the field can't hold the chunk shape."""
    _write_retrieval_skill(tmp_path / "skills", "kb-lookup")
    # auto_into: question — but question is a `string`, not list[string].
    agent_dir = _write_rag_agent(
        tmp_path,
        retrieval_block="retrieval:\n  auto_into: question\n  skill: kb-lookup\n",
    )
    assert _run_validate(agent_dir) == 2


@pytest.mark.unit
def test_validate_rejects_missing_auto_into_field(tmp_path: Path) -> None:
    """``auto_into`` naming a non-existent field is rejected."""
    _write_retrieval_skill(tmp_path / "skills", "kb-lookup")
    agent_dir = _write_rag_agent(
        tmp_path,
        retrieval_block="retrieval:\n  auto_into: nonexistent\n  skill: kb-lookup\n",
    )
    assert _run_validate(agent_dir) == 2


@pytest.mark.unit
def test_validate_rejects_ambiguous_query_from(tmp_path: Path) -> None:
    """``query_from`` unset AND the primary text field is ambiguous (two
    non-canonical string fields) → rejected."""
    _write_retrieval_skill(tmp_path / "skills", "kb-lookup")
    agent_dir = tmp_path / "ambig"
    agent_dir.mkdir()
    schema_dir = agent_dir / "schema"
    schema_dir.mkdir()
    # Two string fields, neither canonical (no query/question/text/message),
    # so the default query_from resolution is ambiguous.
    (schema_dir / "input.yaml").write_text(
        "version: 1\n"
        "type: object\n"
        "fields:\n"
        "  topic:\n"
        "    type: string\n"
        "  subject:\n"
        "    type: string\n"
        "  context:\n"
        "    type: list[string]\n"
        "required:\n"
        "  - topic\n"
        "  - subject\n"
    )
    (schema_dir / "output.yaml").write_text(_OUTPUT_SCHEMA_YAML)
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: ambig\n"
        "version: 0.1.0\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        "schema:\n"
        "  input: ./schema/input.yaml\n"
        "  output: ./schema/output.yaml\n"
        "skills:\n"
        "  - kb-lookup\n"
        "retrieval:\n"
        "  auto_into: context\n"
        "  skill: kb-lookup\n"
    )
    (agent_dir / "prompt.md").write_text("{{ input.topic }}")
    assert _run_validate(agent_dir) == 2


@pytest.mark.unit
def test_validate_accepts_well_formed_retrieval_block(tmp_path: Path) -> None:
    """A well-formed opt-in block passes validation (exit 0)."""
    _write_retrieval_skill(tmp_path / "skills", "kb-lookup")
    agent_dir = _write_rag_agent(
        tmp_path,
        retrieval_block=(
            "retrieval:\n  auto_into: context\n  skill: kb-lookup\n  query_from: question\n"
        ),
    )
    assert _run_validate(agent_dir) == 0


@pytest.mark.unit
def test_validate_ignores_pipeline_only_retrieval_block(tmp_path: Path) -> None:
    """A plain ``retrieval:`` pipeline block (hybrid/rewrite, no
    auto_into) does NOT trigger the ADR 023 checks — back-compat."""
    _write_retrieval_skill(tmp_path / "skills", "kb-lookup")
    agent_dir = _write_rag_agent(
        tmp_path,
        retrieval_block="retrieval:\n  hybrid: true\n  rerank: true\n",
    )
    assert _run_validate(agent_dir) == 0
