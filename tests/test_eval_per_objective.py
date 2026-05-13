"""Tests for per-objective eval gating.

Adds three things on top of the existing eval engine:

1. ``EvalCase.objective`` — dataset rows can declare which objective
   they test via ``"objective": "<id>"``.
2. ``ObjectiveSummary`` — per-objective rollup in ``EvalSummary``,
   keyed by objective id, with the threshold from ``agent.yaml``.
3. ``--objective <id>`` CLI flag — runs only that objective's cases
   and gates on the objective's own threshold (not the eval-wide
   ``--gate``).

These tests assert each layer independently + the end-to-end CLI flow.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.eval import (
    EvalCase,
    EvalConfigError,
    EvalEngine,
    _build_objective_summaries,
    load_dataset,
)
from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.providers.mock import MockProvider
from movate.providers.pricing import load_pricing
from movate.providers.registry import ProviderRegistry
from movate.testing.doubles import InMemoryStorage, NullTracer

runner = CliRunner()


# ---------------------------------------------------------------------------
# Test fixture: an agent with declared objectives + tagged dataset
# ---------------------------------------------------------------------------


def _scaffold_objective_agent(tmp_path: Path) -> Path:
    """Build an agent with three objectives and a dataset whose cases
    are tagged with each objective's id. Returns the agent dir."""
    agent_dir = tmp_path / "obj-agent"
    agent_dir.mkdir()
    (agent_dir / "schema").mkdir()
    (agent_dir / "evals").mkdir()

    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": "obj-agent",
                "version": "0.1.0",
                "model": {"provider": "openai/gpt-4o-mini-2024-07-18"},
                "prompt": "./prompt.md",
                "schema": {
                    "input": "./schema/input.json",
                    "output": "./schema/output.json",
                },
                "evals": {"dataset": "./evals/dataset.jsonl"},
                "objectives": [
                    {"id": "routing-accuracy", "threshold": 0.9, "judge": "exact"},
                    {"id": "response-quality", "threshold": 0.7, "judge": "exact"},
                    {"id": "always-empty", "threshold": 0.5, "judge": "exact"},
                ],
            }
        )
    )
    (agent_dir / "prompt.md").write_text("Reply with: {{ input.q }}")
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["q"],
                "additionalProperties": False,
                "properties": {"q": {"type": "string"}},
            }
        )
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["a"],
                "additionalProperties": False,
                "properties": {"a": {"type": "string"}},
            }
        )
    )
    # Dataset: 3 routing cases (will pass), 2 quality cases (will pass),
    # 0 always-empty cases (no tagged rows — exercise the empty bucket).
    dataset_lines = [
        {"input": {"q": "a"}, "expected": {"a": "a"}, "objective": "routing-accuracy"},
        {"input": {"q": "b"}, "expected": {"a": "b"}, "objective": "routing-accuracy"},
        {"input": {"q": "c"}, "expected": {"a": "c"}, "objective": "routing-accuracy"},
        {"input": {"q": "d"}, "expected": {"a": "d"}, "objective": "response-quality"},
        {"input": {"q": "e"}, "expected": {"a": "e"}, "objective": "response-quality"},
    ]
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        "\n".join(json.dumps(line) for line in dataset_lines) + "\n"
    )
    (agent_dir / "evals" / "judge.yaml").write_text("method: exact\n")
    return agent_dir


def _make_executor(storage: InMemoryStorage) -> Executor:
    # MockProvider echoes the configured response. We don't care about
    # exact-match correctness in these tests (it's the per-objective
    # aggregation logic we're asserting on, not whether MockProvider
    # produces the right answer).
    provider = MockProvider(response='{"a": "echo"}')
    registry = ProviderRegistry(default_litellm=provider)
    return Executor(
        registry=registry,
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
        tenant_id="local",
    )


# ---------------------------------------------------------------------------
# 1. EvalCase.objective field — dataset loader
# ---------------------------------------------------------------------------


def test_dataset_loader_populates_case_objective(tmp_path: Path) -> None:
    agent_dir = _scaffold_objective_agent(tmp_path)
    bundle = load_agent(agent_dir)
    cases, _digest = load_dataset(bundle)
    assert len(cases) == 5
    assert cases[0].objective == "routing-accuracy"
    assert cases[3].objective == "response-quality"


def test_dataset_loader_legacy_no_objective_field(tmp_path: Path) -> None:
    """Existing datasets without an ``objective`` field should still load.
    Cases without the field have ``objective is None`` (default bucket)."""
    agent_dir = tmp_path / "legacy-agent"
    agent_dir.mkdir()
    (agent_dir / "schema").mkdir()
    (agent_dir / "evals").mkdir()
    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": "legacy-agent",
                "version": "0.1.0",
                "model": {"provider": "openai/gpt-4o-mini-2024-07-18"},
                "prompt": "./prompt.md",
                "schema": {
                    "input": "./schema/input.json",
                    "output": "./schema/output.json",
                },
                "evals": {"dataset": "./evals/dataset.jsonl"},
            }
        )
    )
    (agent_dir / "prompt.md").write_text("x: {{ input.x }}")
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["x"],
                "additionalProperties": False,
                "properties": {"x": {"type": "string"}},
            }
        )
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["y"],
                "additionalProperties": False,
                "properties": {"y": {"type": "string"}},
            }
        )
    )
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        json.dumps({"input": {"x": "a"}, "expected": {"y": "a"}}) + "\n"
    )

    bundle = load_agent(agent_dir)
    cases, _ = load_dataset(bundle)
    assert len(cases) == 1
    assert cases[0].objective is None


# ---------------------------------------------------------------------------
# 2. _build_objective_summaries — per-objective rollup
# ---------------------------------------------------------------------------


def test_build_objective_summaries_groups_by_objective(tmp_path: Path) -> None:
    agent_dir = _scaffold_objective_agent(tmp_path)
    bundle = load_agent(agent_dir)

    # Hand-build case summaries (faster than running the engine).
    from movate.core.eval import CaseRun, CaseSummary, load_judge_config  # noqa: PLC0415
    from movate.core.models import (  # noqa: PLC0415
        Metrics,
        RunResponse,
    )

    def _case(score: float, objective: str) -> CaseSummary:
        case = EvalCase(input={"q": "x"}, expected={"a": "x"}, objective=objective)
        resp = RunResponse(status="success", data={"a": "x"}, metrics=Metrics())
        return CaseSummary(
            case=case,
            runs=[CaseRun(response=resp, score=score, rationale="ok")],
            aggregated_score=score,
            passed=score >= 0.5,
        )

    summaries = _build_objective_summaries(
        bundle,
        [
            _case(1.0, "routing-accuracy"),
            _case(1.0, "routing-accuracy"),
            _case(0.8, "routing-accuracy"),
            _case(0.6, "response-quality"),
        ],
        load_judge_config(bundle),
    )
    by_id = {s.objective_id: s for s in summaries}

    # routing-accuracy: 3 cases, mean 0.933, threshold 0.9 → pass
    assert by_id["routing-accuracy"].sample_count == 3
    assert by_id["routing-accuracy"].mean_score == pytest.approx(0.9333, rel=1e-3)
    assert by_id["routing-accuracy"].passed is True

    # response-quality: 1 case at 0.6, threshold 0.7 → FAIL
    assert by_id["response-quality"].sample_count == 1
    assert by_id["response-quality"].mean_score == pytest.approx(0.6)
    assert by_id["response-quality"].passed is False

    # always-empty: 0 cases. Not passing (sample_count check).
    assert by_id["always-empty"].sample_count == 0
    assert by_id["always-empty"].passed is False


def test_build_objective_summaries_empty_for_agents_without_objectives(
    tmp_path: Path,
) -> None:
    """Agents without ``objectives:`` get an empty list. Legacy path."""
    # Use the legacy agent (no objectives in agent.yaml).
    agent_dir = tmp_path / "legacy"
    agent_dir.mkdir()
    (agent_dir / "schema").mkdir()
    (agent_dir / "evals").mkdir()
    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": "legacy",
                "version": "0.1.0",
                "model": {"provider": "openai/gpt-4o-mini-2024-07-18"},
                "prompt": "./prompt.md",
                "schema": {
                    "input": "./schema/input.json",
                    "output": "./schema/output.json",
                },
            }
        )
    )
    (agent_dir / "prompt.md").write_text("hi")
    (agent_dir / "schema" / "input.json").write_text('{"type": "object"}')
    (agent_dir / "schema" / "output.json").write_text('{"type": "object"}')

    bundle = load_agent(agent_dir)
    from movate.core.eval import load_judge_config  # noqa: PLC0415

    summaries = _build_objective_summaries(bundle, [], load_judge_config(bundle))
    assert summaries == []


# ---------------------------------------------------------------------------
# 3. EvalEngine — objective_filter + unknown-id validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_rejects_unknown_objective_in_dataset(tmp_path: Path) -> None:
    """A dataset case declaring an objective that doesn't exist on the
    agent must fail loudly at engine entry — not run the eval and produce
    confusing "no objective summaries" output."""
    agent_dir = _scaffold_objective_agent(tmp_path)
    # Append a bad case to the dataset.
    bad_line = json.dumps(
        {"input": {"q": "x"}, "expected": {"a": "x"}, "objective": "does-not-exist"}
    )
    dataset = agent_dir / "evals" / "dataset.jsonl"
    dataset.write_text(dataset.read_text() + bad_line + "\n")

    bundle = load_agent(agent_dir)
    storage = InMemoryStorage()
    await storage.init()
    engine = EvalEngine(executor=_make_executor(storage), provider=MockProvider())

    with pytest.raises(EvalConfigError, match="does-not-exist"):
        await engine.run(bundle)


@pytest.mark.asyncio
async def test_engine_rejects_unknown_objective_filter(tmp_path: Path) -> None:
    agent_dir = _scaffold_objective_agent(tmp_path)
    bundle = load_agent(agent_dir)
    storage = InMemoryStorage()
    await storage.init()
    engine = EvalEngine(
        executor=_make_executor(storage),
        provider=MockProvider(),
        objective_filter="ghost-objective",
    )
    with pytest.raises(EvalConfigError, match="ghost-objective"):
        await engine.run(bundle)


@pytest.mark.asyncio
async def test_engine_objective_filter_with_zero_matching_cases(tmp_path: Path) -> None:
    """If --objective points at a valid id but the dataset has zero rows
    tagged with it, that's a clear actionable error — not a silent
    empty-success."""
    agent_dir = _scaffold_objective_agent(tmp_path)
    bundle = load_agent(agent_dir)
    storage = InMemoryStorage()
    await storage.init()
    engine = EvalEngine(
        executor=_make_executor(storage),
        provider=MockProvider(),
        objective_filter="always-empty",
    )
    with pytest.raises(EvalConfigError, match="matched zero cases"):
        await engine.run(bundle)


# ---------------------------------------------------------------------------
# 4. CLI integration — --objective flag end-to-end
# ---------------------------------------------------------------------------


def test_cli_eval_without_objective_flag_still_works(tmp_path: Path) -> None:
    """Backwards compat: no --objective → existing behavior."""
    agent_dir = _scaffold_objective_agent(tmp_path)
    result = runner.invoke(app, ["eval", str(agent_dir), "--mock", "--gate", "0.5"])
    # Mock provider returns '{"a": "echo"}' for all cases; exact-match
    # against 'a', 'b', 'c'... all fail → overall_pass False → exit 1.
    # We just assert the run COMPLETED (no exception) and the cases
    # block rendered.
    assert "obj-agent" in result.stdout or "obj-agent" in (result.stderr or "")


def test_cli_eval_with_unknown_objective_flag_exits_2(tmp_path: Path) -> None:
    agent_dir = _scaffold_objective_agent(tmp_path)
    # mix_stderr=False so we can read result.stderr independently.
    cli_runner = CliRunner(mix_stderr=False)
    result = cli_runner.invoke(
        app,
        ["eval", str(agent_dir), "--mock", "--objective", "not-real"],
    )
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "not-real" in combined
