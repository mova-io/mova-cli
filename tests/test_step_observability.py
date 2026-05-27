"""ADR 024 PR 1 — per-step execution observability backbone.

Covers the PR1 slice of the ADR test matrix (D1 nested spans + D2 retention +
D5 cost-sum), hermetic: MockProvider + InMemoryStorage / SqliteProvider via the
parametrized ``storage`` fixture, no API keys, no real network.

1. single-turn no-skill run → exactly one TurnRecord, ``Metrics.cost_usd``
   equals the turn cost AND equals a pre-change baseline asserted explicitly
   (value-preserving regression guard for the dominant path).
2. multi-turn tool-using run → per-turn TurnRecords + per-SkillCallRecord
   ``cost_usd`` retained; ``Metrics.cost_usd`` == Σ(turns) + Σ(skills).
3. ADR 023 pre-retrieval step → a ``retrieval.*`` child span is emitted and its
   cost is accounted into the run total + retained as a turn-0 SkillCallRecord.
4. tracing OFF (SilentTracer) → NO spans emitted, but turns/skill_calls are
   still retained on the RunRecord (offline-first guard).
5. nested-span structure → ``agent.turn[*]`` are children of ``agent.execute``;
   ``skill.*`` / ``retrieval.*`` are children of their turn (parent_id wiring).
6. legacy RunRecord with no ``turns`` → loads + round-trips (no crash).
7. error mid-loop → the partial turns/skill_calls captured so far are persisted
   as an ERROR RunRecord; the real error is surfaced (not swallowed).

The skills used here are plain Python functions defined in this module so every
test is deterministic and offline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from movate.core.executor import Executor
from movate.core.failures import SchemaError
from movate.core.loader import load_agent
from movate.core.models import JobStatus, Metrics, RunRecord, RunRequest
from movate.providers.mock import MockProvider
from movate.providers.pricing import PricingTable, load_pricing
from movate.tracing.base import SpanCtx
from movate.tracing.null import SilentTracer

# ---------------------------------------------------------------------------
# Module-level skills (referenced by `entry` strings below)
# ---------------------------------------------------------------------------


async def _add_one_skill(inputs: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
    """A trivial deterministic skill: y = x + 1."""
    return {"y": int(inputs.get("x", 0)) + 1}


async def _exploding_skill(inputs: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
    """Raises so the retrieval / dispatch error paths are exercised."""
    raise RuntimeError("kaboom")


async def _fixed_chunks_skill(inputs: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
    """kb-vector-lookup-shaped retrieval output."""
    q = (inputs.get("question") or "").strip()
    return {
        "chunks": [
            {"text": f"CHUNK about {q}", "source": "/kb/a.md", "score": 0.9},
        ],
        "chunks_found": 1,
    }


# ---------------------------------------------------------------------------
# Capturing tracer — records the span tree (start/end) for assertions.
# ---------------------------------------------------------------------------


class _CapturingTracer:
    """Tracer double that retains every span it creates (in creation order)
    plus the events / attributes set on them, so tests can assert the
    ``agent.execute → agent.turn[i] → skill.*/retrieval.*`` parent wiring."""

    name = "capturing"

    def __init__(self) -> None:
        self.spans: list[SpanCtx] = []
        self.ended: list[tuple[str, str]] = []  # (span_id, status)
        self.events: list[dict[str, Any]] = []

    def start_span(
        self,
        name: str,
        attrs: dict[str, Any] | None = None,
        parent: SpanCtx | None = None,
    ) -> SpanCtx:
        span = SpanCtx(
            trace_id="trace-cap",
            name=name,
            attributes=dict(attrs or {}),
            parent_id=parent.span_id if parent else None,
        )
        self.spans.append(span)
        return span

    def end_span(self, span: SpanCtx, status: str = "ok") -> None:
        self.ended.append((span.span_id, status))

    def log_event(self, span: SpanCtx, event: dict[str, Any]) -> None:
        self.events.append(dict(event))

    def set_attribute(self, span: SpanCtx, key: str, value: Any) -> None:
        span.attributes[key] = value

    def log_generation(self, span: SpanCtx, **kwargs: Any) -> None:
        return None

    # --- assertion helpers ---
    def by_name(self, name: str) -> list[SpanCtx]:
        return [s for s in self.spans if s.name == name]

    def by_prefix(self, prefix: str) -> list[SpanCtx]:
        return [s for s in self.spans if s.name.startswith(prefix)]

    def root(self) -> SpanCtx:
        roots = self.by_name("agent.execute")
        assert roots, "expected an agent.execute root span"
        return roots[0]


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def pricing() -> PricingTable:
    return load_pricing()


# Calc skill: integer in → integer out. Used for the tool-use loop tests.
_CALC_SKILL_YAML = """\
api_version: movate/v1
kind: Skill
name: {name}
version: 0.1.0
description: test skill {name}
schema:
  input:
    x: integer
  output:
    y: integer
implementation:
  kind: python
  entry: {entry}
cost:
  per_call_usd: {cost}
"""

# Retrieval skill: question in → chunks out. Used for the pre-retrieval test.
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
  per_call_usd: {cost}
"""


def _write_skill(
    parent: Path, name: str, *, entry: str, cost: float = 0.0, kind: str = "calc"
) -> Path:
    """Write a python-kind skill dir. ``kind`` selects the schema shape:
    ``calc`` (x→y) or ``retrieval`` (question→chunks)."""
    skill_dir = parent / name
    skill_dir.mkdir(parents=True)
    template = _CALC_SKILL_YAML if kind == "calc" else _RETRIEVAL_SKILL_YAML
    (skill_dir / "skill.yaml").write_text(template.format(name=name, entry=entry, cost=cost))
    module, _, func = entry.partition(":")
    (skill_dir / "impl.py").write_text(f"from {module} import {func} as run\n")
    return skill_dir


# Canonical-format input schema (path form) so ``context`` can be a genuinely
# OPTIONAL ``list[string]`` — inline shorthand can't mark an array optional
# (the ``?`` suffix is scalar-only), exactly like the ADR 023 RAG tests.
_RAG_INPUT_SCHEMA_YAML = """\
version: 1
type: object
fields:
  question:
    type: string
  context:
    type: list[string]
required:
  - question
"""

_RAG_OUTPUT_SCHEMA_YAML = """\
version: 1
type: object
fields:
  answer:
    type: string
required:
  - answer
"""


def _write_agent(
    project_root: Path,
    *,
    name: str = "agent",
    skills: list[str] | None = None,
    retrieval_block: str | None = None,
    context_field: bool = False,
) -> Path:
    """Write a flat agent dir (sibling of ``skills/``)."""
    agent_dir = project_root / name
    agent_dir.mkdir(parents=True)
    skills_block = ""
    if skills:
        skills_block = "skills:\n" + "".join(f"  - {s}\n" for s in skills)
    block = f"{retrieval_block}\n" if retrieval_block else ""

    if context_field:
        # Path-form canonical schema (an optional array field can't be inline).
        schema_dir = agent_dir / "schema"
        schema_dir.mkdir()
        (schema_dir / "input.yaml").write_text(_RAG_INPUT_SCHEMA_YAML)
        (schema_dir / "output.yaml").write_text(_RAG_OUTPUT_SCHEMA_YAML)
        schema_block = "schema:\n  input: ./schema/input.yaml\n  output: ./schema/output.yaml\n"
        prompt = (
            "Q: {{ input.question }}\n"
            "{% if input.context is defined and input.context %}CONTEXT:\n"
            "{% for c in input.context %}- {{ c }}\n{% endfor %}"
            "{% else %}(no context){% endif %}"
        )
    else:
        schema_block = "schema:\n  input:\n    question: string\n  output:\n    answer: string\n"
        prompt = "Q: {{ input.question }}"

    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        f"name: {name}\n"
        "version: 0.1.0\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        f"{schema_block}"
        f"{skills_block}"
        f"{block}"
    )
    (agent_dir / "prompt.md").write_text(prompt)
    return agent_dir


def _executor(storage: Any, pricing: PricingTable, tracer: Any, **kw: Any) -> Executor:
    return Executor(
        provider=kw.pop("provider", MockProvider(response='{"answer": "ok"}')),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )


# ---------------------------------------------------------------------------
# Case 1 — single-turn no-skill run: one TurnRecord, cost == baseline
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_single_turn_no_skill_one_turn_record_and_cost_unchanged(
    tmp_path: Path, storage: Any, pricing: PricingTable
) -> None:
    """A vanilla single-shot agent produces exactly ONE TurnRecord and the
    run-level ``cost_usd`` equals that turn's cost — which equals the legacy
    single-completion pricing figure (computed independently here as the
    explicit baseline). Value-preserving regression guard for the dominant,
    non-tool path (ADR 024 D5)."""
    tracer = _CapturingTracer()
    agent_dir = _write_agent(tmp_path, name="vanilla")
    bundle = load_agent(agent_dir)
    ex = _executor(storage, pricing, tracer)
    resp = await ex.execute(bundle, RunRequest(agent="vanilla", input={"question": "hi"}))

    assert resp.status == "success"
    run = await storage.get_run(resp.run_id, tenant_id="local")
    assert run is not None
    # Exactly one turn (the single final completion), no skills.
    assert len(run.turns) == 1
    assert run.skill_calls == []
    turn = run.turns[0]
    assert turn.index == 1
    assert turn.model == "openai/gpt-4o-mini-2024-07-18"
    assert turn.finish_reason == "final"

    # Regression baseline: re-price the run's accumulated tokens the way the
    # pre-change executor did (a single pricing-table lookup over the final
    # completion). The new per-turn sum must equal it exactly.
    baseline = pricing.cost_for(provider="openai/gpt-4o-mini-2024-07-18", tokens=run.metrics.tokens)
    assert baseline > 0  # mock reports tokens; price > 0
    assert run.metrics.cost_usd == pytest.approx(baseline)
    assert turn.cost_usd == pytest.approx(baseline)
    assert run.metrics.cost_usd == pytest.approx(turn.cost_usd)


# ---------------------------------------------------------------------------
# Case 2 — multi-turn tool run: per-turn + per-skill cost retained + summed
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_multi_turn_tool_run_costs_retained_and_summed(
    tmp_path: Path, storage: Any, pricing: PricingTable
) -> None:
    """A tool-using run records one TurnRecord per LLM round-trip and a
    SkillCallRecord (with its ``cost_usd`` + ``turn`` linkage) per dispatch.
    ``Metrics.cost_usd`` == Σ(turn costs) + Σ(skill costs)."""
    _write_skill(tmp_path / "skills", "add-one", entry=f"{__name__}:_add_one_skill", cost=0.002)
    agent_dir = _write_agent(tmp_path, name="calc", skills=["add-one"])
    bundle = load_agent(agent_dir)
    assert len(bundle.skills) == 1

    tracer = _CapturingTracer()
    # Two tool calls across two tool-use turns, then a final answer (turn 3).
    provider = MockProvider(
        response='{"answer": "done"}',
        tool_script=[("add-one", {"x": 1}), ("add-one", {"x": 2})],
    )
    ex = _executor(storage, pricing, tracer, provider=provider)
    resp = await ex.execute(bundle, RunRequest(agent="calc", input={"question": "add"}))

    assert resp.status == "success"
    run = await storage.get_run(resp.run_id, tenant_id="local")
    assert run is not None
    # Three completions → three TurnRecords (two tool_use turns + one final).
    assert [t.index for t in run.turns] == [1, 2, 3]
    assert [t.finish_reason for t in run.turns] == ["tool_use", "tool_use", "final"]
    # Two skill calls, each $0.002, each linked to the turn that requested it.
    assert len(run.skill_calls) == 2
    assert all(sc.cost_usd == pytest.approx(0.002) for sc in run.skill_calls)
    assert [sc.turn for sc in run.skill_calls] == [1, 2]
    # Each skill's turn matches a real TurnRecord index.
    turn_indexes = {t.index for t in run.turns}
    assert all(sc.turn in turn_indexes for sc in run.skill_calls)

    sum_turns = sum(t.cost_usd for t in run.turns)
    sum_skills = sum(sc.cost_usd for sc in run.skill_calls)
    assert sum_skills == pytest.approx(0.004)
    assert run.metrics.cost_usd == pytest.approx(sum_turns + sum_skills)


# ---------------------------------------------------------------------------
# Case 3 — ADR 023 pre-retrieval step → retrieval.* span + cost accounted
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_pre_retrieval_emits_retrieval_span_and_accounts_cost(
    tmp_path: Path, storage: Any, pricing: PricingTable
) -> None:
    """The ADR 023 auto-RAG phase emits a ``retrieval.<skill>`` child span,
    accounts the retrieval skill's per-call cost into the run total, and
    retains it as a turn-0 SkillCallRecord."""
    _write_skill(
        tmp_path / "skills",
        "kb-lookup",
        entry=f"{__name__}:_fixed_chunks_skill",
        cost=0.003,
        kind="retrieval",
    )
    agent_dir = _write_agent(
        tmp_path,
        name="rag",
        skills=["kb-lookup"],
        retrieval_block=(
            "retrieval:\n  auto_into: context\n  skill: kb-lookup\n  query_from: question\n"
        ),
        context_field=True,
    )
    bundle = load_agent(agent_dir)
    assert bundle.spec.retrieval.auto_retrieval_enabled

    tracer = _CapturingTracer()
    ex = _executor(storage, pricing, tracer)
    resp = await ex.execute(bundle, RunRequest(agent="rag", input={"question": "refunds?"}))

    assert resp.status == "success"
    # A retrieval.* span was emitted, child of the run root.
    retrieval_spans = tracer.by_prefix("retrieval.")
    assert len(retrieval_spans) == 1
    assert retrieval_spans[0].name == "retrieval.kb-lookup"
    assert retrieval_spans[0].parent_id == tracer.root().span_id
    assert retrieval_spans[0].attributes.get("cost_usd") == pytest.approx(0.003)

    run = await storage.get_run(resp.run_id, tenant_id="local")
    assert run is not None
    # Turn-0 retrieval retained as a SkillCallRecord (offline-first).
    retrieval_calls = [sc for sc in run.skill_calls if sc.skill == "kb-lookup" and sc.turn == 0]
    assert len(retrieval_calls) == 1
    assert retrieval_calls[0].cost_usd == pytest.approx(0.003)
    # Cost accounted: run total includes the retrieval cost.
    assert run.metrics.cost_usd >= 0.003


# ---------------------------------------------------------------------------
# Case 4 — tracing OFF (SilentTracer): no spans, but records still retained
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_tracing_off_still_retains_turns_and_skill_calls(
    tmp_path: Path, storage: Any, pricing: PricingTable
) -> None:
    """With the production no-op SilentTracer, no observable spans are
    produced, yet the RunRecord still carries the per-turn + per-skill trail
    (offline-first guard — ``mdk explain`` works without a backend)."""
    _write_skill(tmp_path / "skills", "add-one", entry=f"{__name__}:_add_one_skill", cost=0.001)
    agent_dir = _write_agent(tmp_path, name="calc", skills=["add-one"])
    bundle = load_agent(agent_dir)

    provider = MockProvider(
        response='{"answer": "done"}',
        tool_script=[("add-one", {"x": 7})],
    )
    ex = _executor(storage, pricing, SilentTracer(), provider=provider)
    resp = await ex.execute(bundle, RunRequest(agent="calc", input={"question": "add"}))

    assert resp.status == "success"
    run = await storage.get_run(resp.run_id, tenant_id="local")
    assert run is not None
    # Retained even though tracing is off.
    assert len(run.turns) == 2  # one tool_use turn + one final
    assert len(run.skill_calls) == 1
    assert run.skill_calls[0].cost_usd == pytest.approx(0.001)
    assert run.skill_calls[0].turn == 1


# ---------------------------------------------------------------------------
# Case 5 — nested span structure: parent_id wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_nested_span_structure_parent_wiring(
    tmp_path: Path, storage: Any, pricing: PricingTable
) -> None:
    """``agent.turn[i]`` spans are children of the ``agent.execute`` root;
    ``skill.*`` spans are children of the turn that dispatched them."""
    _write_skill(tmp_path / "skills", "add-one", entry=f"{__name__}:_add_one_skill", cost=0.0)
    agent_dir = _write_agent(tmp_path, name="calc", skills=["add-one"])
    bundle = load_agent(agent_dir)

    tracer = _CapturingTracer()
    provider = MockProvider(
        response='{"answer": "done"}',
        tool_script=[("add-one", {"x": 1})],
    )
    ex = _executor(storage, pricing, tracer, provider=provider)
    resp = await ex.execute(bundle, RunRequest(agent="calc", input={"question": "add"}))
    assert resp.status == "success"

    root = tracer.root()
    turn_spans = tracer.by_prefix("agent.turn[")
    assert len(turn_spans) == 2  # tool_use turn + final turn
    # Every turn span is a direct child of the run root.
    assert all(ts.parent_id == root.span_id for ts in turn_spans)

    skill_spans = tracer.by_prefix("skill.")
    assert len(skill_spans) == 1
    # The skill span's parent is a turn span (NOT the root directly).
    turn_span_ids = {ts.span_id for ts in turn_spans}
    assert skill_spans[0].parent_id in turn_span_ids
    assert skill_spans[0].parent_id != root.span_id
    # Every span we opened was also closed.
    ended_ids = {sid for sid, _ in tracer.ended}
    for s in [root, *turn_spans, *skill_spans]:
        assert s.span_id in ended_ids


# ---------------------------------------------------------------------------
# Case 6 — legacy RunRecord with no `turns` loads + round-trips
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_legacy_run_record_without_turns_round_trips(
    storage: Any, pricing: PricingTable
) -> None:
    """A RunRecord persisted WITHOUT ``turns`` (the legacy shape) loads back
    cleanly — ``turns`` defaults to an empty list, no crash. Guards the
    additive-field backward-compat contract for the DB providers."""
    legacy = RunRecord(
        run_id="legacy-1",
        job_id="job-1",
        tenant_id="local",
        agent="old-agent",
        agent_version="0.1.0",
        prompt_hash="deadbeef",
        provider="openai/gpt-4o-mini-2024-07-18",
        provider_version="0.0.1",
        pricing_version=pricing.version,
        status=JobStatus.SUCCESS,
        input={"question": "x"},
        output={"answer": "y"},
        metrics=Metrics(cost_usd=0.01),
        # No turns / skill_calls passed — they default to [].
    )
    assert legacy.turns == []
    await storage.save_run(legacy)

    loaded = await storage.get_run("legacy-1", tenant_id="local")
    assert loaded is not None
    assert loaded.turns == []
    assert loaded.skill_calls == []
    assert loaded.metrics.cost_usd == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# Case 6b — round-trip WITH turns + per-skill cost through the DB providers
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_turns_and_skill_costs_round_trip(
    tmp_path: Path, storage: Any, pricing: PricingTable
) -> None:
    """End-to-end: a tool run's turns + per-skill cost survive save_run →
    get_run on whatever backend the parametrized fixture selects (memory /
    sqlite / postgres) — i.e. the new ``turns`` column actually round-trips."""
    _write_skill(tmp_path / "skills", "add-one", entry=f"{__name__}:_add_one_skill", cost=0.005)
    agent_dir = _write_agent(tmp_path, name="calc", skills=["add-one"])
    bundle = load_agent(agent_dir)
    provider = MockProvider(
        response='{"answer": "done"}',
        tool_script=[("add-one", {"x": 1})],
    )
    ex = _executor(storage, pricing, SilentTracer(), provider=provider)
    resp = await ex.execute(bundle, RunRequest(agent="calc", input={"question": "add"}))
    assert resp.status == "success"

    loaded = await storage.get_run(resp.run_id, tenant_id="local")
    assert loaded is not None
    assert len(loaded.turns) == 2
    assert loaded.turns[0].finish_reason == "tool_use"
    assert loaded.turns[-1].finish_reason == "final"
    assert len(loaded.skill_calls) == 1
    assert loaded.skill_calls[0].cost_usd == pytest.approx(0.005)
    assert loaded.skill_calls[0].turn == 1


# ---------------------------------------------------------------------------
# Case 7 — error mid-loop → partial turns/skills persisted, real error raised
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_error_mid_loop_persists_partial_trail(
    tmp_path: Path, storage: Any, pricing: PricingTable
) -> None:
    """A run that fails AFTER dispatching a skill (here: the model's final
    answer fails output-schema validation) still persists the partial trail
    (the captured turns + skill calls) as an ERROR RunRecord, and surfaces the
    real error — it is not swallowed."""
    _write_skill(tmp_path / "skills", "add-one", entry=f"{__name__}:_add_one_skill", cost=0.002)
    agent_dir = _write_agent(tmp_path, name="calc", skills=["add-one"])
    bundle = load_agent(agent_dir)

    # First turn dispatches a skill; the final completion returns NON-conforming
    # output (missing the required `answer` key) → SchemaError after the loop.
    provider = MockProvider(
        response='{"not_answer": "oops"}',
        tool_script=[("add-one", {"x": 3})],
    )
    ex = _executor(storage, pricing, SilentTracer(), provider=provider)
    resp = await ex.execute(bundle, RunRequest(agent="calc", input={"question": "add"}))

    # The real error is surfaced (not swallowed).
    assert resp.status == "error"
    assert resp.error is not None
    assert resp.error.type == SchemaError("x").failure_type.value

    # A partial ERROR RunRecord was persisted with the trail captured so far.
    run = await storage.get_run(resp.run_id, tenant_id="local")
    assert run is not None
    assert run.status == JobStatus.ERROR
    assert len(run.skill_calls) == 1
    assert run.skill_calls[0].skill == "add-one"
    assert run.skill_calls[0].turn == 1
    # Both completions captured before the schema failure (tool_use + final).
    assert len(run.turns) == 2
