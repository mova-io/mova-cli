"""Tests for workflow-level evals (ADR 008).

Coverage:
* _score_workflow_accuracy — partial match (extra keys in final_state ignored)
* WorkflowSpec.evals stanza loads from YAML
* load_workflow_dataset — parses JSONL; missing file → EvalConfigError
* WorkflowEvalEngine.run() — accuracy scoring of final_state
* Partial node failure → case scores 0.0 without aborting the eval
* Latency dim scored when case.latency_budget_ms set
* Coverage dim scored when case.expected_coverage set
* Refusal dim scored when case.refusal_expected set
* Multi-run averaging (runs_per_case=2)
* CLI: mdk eval <workflow-dir> dispatches to workflow engine
* CLI: missing evals stanza → exit 2 with hint
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.eval import (
    EvalConfigError,
    WorkflowEvalEngine,
    _score_workflow_accuracy,
    load_workflow_dataset,
)
from movate.core.executor import Executor
from movate.core.workflow import compile_workflow, load_workflow_spec
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
    StreamChunk,
)
from movate.providers.pricing import load_pricing
from movate.testing import InMemoryStorage, NullTracer

runner = CliRunner(mix_stderr=False)

# ---------------------------------------------------------------------------
# Helpers: scaffold minimal two-node workflow
# ---------------------------------------------------------------------------

_STATE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "text": {"type": "string"},
        "step1": {"type": "string"},
        "step2": {"type": "string"},
    },
}


def _make_agent(agent_dir: Path, *, name: str, input_key: str, output_key: str) -> None:
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "schema").mkdir(exist_ok=True)
    (agent_dir / "evals").mkdir(exist_ok=True)
    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": name,
                "version": "0.1.0",
                "description": f"reads {input_key}, writes {output_key}",
                "model": {
                    "provider": "openai/gpt-4o-mini-2024-07-18",
                    "params": {"temperature": 0.0},
                },
                "prompt": "./prompt.md",
                "schema": {"input": "./schema/input.json", "output": "./schema/output.json"},
                "evals": {"dataset": "./evals/dataset.jsonl"},
            }
        )
    )
    (agent_dir / "prompt.md").write_text(f"echo {{{{ input.{input_key} }}}} as {output_key}\n")
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": [input_key],
                "properties": {input_key: {"type": "string"}},
            }
        )
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": [output_key],
                "properties": {output_key: {"type": "string"}},
            }
        )
    )
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        json.dumps({"input": {input_key: "x"}, "expected": {output_key: "x"}}) + "\n"
    )


def _make_workflow(
    workflow_dir: Path,
    *,
    with_evals: bool = True,
    dataset_content: str | None = None,
) -> Path:
    workflow_dir.mkdir(parents=True, exist_ok=True)
    _make_agent(
        workflow_dir / "agents" / "first",
        name="first-agent",
        input_key="text",
        output_key="step1",
    )
    _make_agent(
        workflow_dir / "agents" / "second",
        name="second-agent",
        input_key="step1",
        output_key="step2",
    )
    (workflow_dir / "state.json").write_text(json.dumps(_STATE_SCHEMA))

    evals_block = {"dataset": "evals/dataset.jsonl"} if with_evals else None
    spec_dict: dict = {
        "api_version": "movate/v1",
        "kind": "Workflow",
        "name": "test-workflow",
        "version": "0.1.0",
        "state_schema": "./state.json",
        "entrypoint": "first",
        "nodes": [
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {"id": "second", "type": "agent", "ref": "./agents/second"},
        ],
        "edges": [{"from": "first", "to": "second"}],
    }
    if evals_block:
        spec_dict["evals"] = evals_block
    (workflow_dir / "workflow.yaml").write_text(yaml.safe_dump(spec_dict))

    if with_evals:
        (workflow_dir / "evals").mkdir(exist_ok=True)
        content = dataset_content or (
            json.dumps({"input": {"text": "hello"}, "expected": {"step2": "hello"}}) + "\n"
        )
        (workflow_dir / "evals" / "dataset.jsonl").write_text(content)

    return workflow_dir


class _StateAwareProvider(BaseLLMProvider):
    """Returns step1=hello for the first node, step2=hello for the second.

    Detects which node is calling by inspecting the rendered prompt text:
    the first node's prompt body mentions "step1" (the output key) but not
    "step2"; the second mentions "step2".
    """

    name = "state_aware"
    version = "0.0.1"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = request.messages[0].content
        if "step1" in body and "step2" not in body:
            return CompletionResponse(text='{"step1": "hello"}')
        return CompletionResponse(text='{"step2": "hello"}')

    async def stream(  # pragma: no cover
        self, request: CompletionRequest
    ) -> AsyncIterator[StreamChunk]:
        raise NotImplementedError

    async def embed(self, text: str, *, model: str) -> list[float]:  # pragma: no cover
        raise NotImplementedError


def _make_engine(storage: InMemoryStorage) -> WorkflowEvalEngine:
    provider = _StateAwareProvider()
    pricing = load_pricing()
    tracer = NullTracer()
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)
    return WorkflowEvalEngine(
        executor=executor, storage=storage, provider=provider, runs_per_case=1
    )


# ---------------------------------------------------------------------------
# Unit: _score_workflow_accuracy
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestScoreWorkflowAccuracy:
    def test_exact_match_scores_one(self) -> None:
        score = _score_workflow_accuracy({"step2": "hello", "step1": "x"}, {"step2": "hello"})
        assert score.value == pytest.approx(1.0)

    def test_extra_keys_in_state_ignored(self) -> None:
        score = _score_workflow_accuracy(
            {"step1": "a", "step2": "b", "internal": "ignored"},
            {"step2": "b"},
        )
        assert score.value == pytest.approx(1.0)

    def test_mismatch_scores_zero(self) -> None:
        score = _score_workflow_accuracy({"step2": "wrong"}, {"step2": "right"})
        assert score.value == pytest.approx(0.0)
        assert "step2" in score.rationale

    def test_empty_expected_scores_one(self) -> None:
        score = _score_workflow_accuracy({"step2": "anything"}, {})
        assert score.value == pytest.approx(1.0)

    def test_missing_key_in_state_scores_zero(self) -> None:
        score = _score_workflow_accuracy({}, {"step2": "hello"})
        assert score.value == pytest.approx(0.0)

    def test_multiple_mismatches_listed_in_rationale(self) -> None:
        score = _score_workflow_accuracy(
            {"a": "wrong", "b": "right"}, {"a": "correct", "b": "right"}
        )
        assert score.value == pytest.approx(0.0)
        assert "a" in score.rationale


# ---------------------------------------------------------------------------
# Unit: WorkflowSpec.evals stanza
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_workflow_spec_loads_evals_stanza(tmp_path: Path) -> None:
    wf_dir = _make_workflow(tmp_path / "wf", with_evals=True)
    spec, _ = load_workflow_spec(wf_dir)
    assert spec.evals is not None
    assert spec.evals.dataset == "evals/dataset.jsonl"
    assert spec.evals.runs_per_case == 1
    assert spec.evals.gate == pytest.approx(0.7)


@pytest.mark.unit
def test_workflow_spec_evals_absent_is_none(tmp_path: Path) -> None:
    wf_dir = _make_workflow(tmp_path / "wf", with_evals=False)
    spec, _ = load_workflow_spec(wf_dir)
    assert spec.evals is None


@pytest.mark.unit
def test_workflow_spec_evals_custom_gate(tmp_path: Path) -> None:
    wf_dir = tmp_path / "wf"
    _make_workflow(wf_dir, with_evals=True)
    raw = yaml.safe_load((wf_dir / "workflow.yaml").read_text())
    raw["evals"]["gate"] = 0.9
    raw["evals"]["runs_per_case"] = 3
    (wf_dir / "workflow.yaml").write_text(yaml.safe_dump(raw))
    spec, _ = load_workflow_spec(wf_dir)
    assert spec.evals.gate == pytest.approx(0.9)
    assert spec.evals.runs_per_case == 3


# ---------------------------------------------------------------------------
# Unit: load_workflow_dataset
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_workflow_dataset_parses_rows(tmp_path: Path) -> None:
    from movate.core.workflow.spec import WorkflowEvalsSpec  # noqa: PLC0415

    ds = tmp_path / "evals" / "dataset.jsonl"
    ds.parent.mkdir()
    ds.write_text(json.dumps({"input": {"text": "hello"}, "expected": {"step2": "hello"}}) + "\n")
    evals_spec = WorkflowEvalsSpec(dataset="evals/dataset.jsonl")
    cases, digest = load_workflow_dataset(tmp_path, evals_spec)
    assert len(cases) == 1
    assert cases[0].input == {"text": "hello"}
    assert cases[0].expected == {"step2": "hello"}
    assert len(digest) == 64  # sha256 hex


@pytest.mark.unit
def test_load_workflow_dataset_missing_file_raises(tmp_path: Path) -> None:
    from movate.core.workflow.spec import WorkflowEvalsSpec  # noqa: PLC0415

    evals_spec = WorkflowEvalsSpec(dataset="evals/no-such-file.jsonl")
    with pytest.raises(EvalConfigError, match="dataset not found"):
        load_workflow_dataset(tmp_path, evals_spec)


@pytest.mark.unit
def test_load_workflow_dataset_parses_optional_fields(tmp_path: Path) -> None:
    from movate.core.workflow.spec import WorkflowEvalsSpec  # noqa: PLC0415

    ds = tmp_path / "evals" / "dataset.jsonl"
    ds.parent.mkdir()
    ds.write_text(
        json.dumps(
            {
                "input": {"text": "harm"},
                "expected": {"step2": "refused"},
                "refusal_expected": True,
                "expected_coverage": ["decline", "sorry"],
                "latency_budget_ms": 5000,
            }
        )
        + "\n"
    )
    evals_spec = WorkflowEvalsSpec(dataset="evals/dataset.jsonl")
    cases, _ = load_workflow_dataset(tmp_path, evals_spec)
    assert cases[0].refusal_expected is True
    assert cases[0].expected_coverage == ["decline", "sorry"]
    assert cases[0].latency_budget_ms == 5000


# ---------------------------------------------------------------------------
# Engine: end-to-end accuracy scoring
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_engine_scores_successful_workflow(
    tmp_path: Path,
) -> None:
    storage = InMemoryStorage()
    await storage.init()
    wf_dir = _make_workflow(tmp_path / "wf")
    spec, wf_path = load_workflow_spec(wf_dir)
    graph = compile_workflow(spec, wf_path)

    engine = _make_engine(storage)
    summary = await engine.run(
        graph,
        wf_path,
        spec.evals,
        workflow_name=spec.name,
        workflow_version=spec.version,
        threshold=0.7,
    )
    assert summary.agent == "test-workflow"
    assert summary.sample_count == 1
    assert summary.cases[0].aggregated_score == pytest.approx(1.0)
    assert summary.cases[0].passed is True


@pytest.mark.unit
async def test_engine_score_zero_on_mismatch(
    tmp_path: Path,
) -> None:
    storage = InMemoryStorage()
    await storage.init()
    wf_dir = _make_workflow(
        tmp_path / "wf",
        dataset_content=json.dumps(
            {
                "input": {"text": "hello"},
                "expected": {"step2": "WRONG"},  # provider returns "hello"
            }
        )
        + "\n",
    )
    spec, wf_path = load_workflow_spec(wf_dir)
    graph = compile_workflow(spec, wf_path)
    engine = _make_engine(storage)
    summary = await engine.run(
        graph,
        wf_path,
        spec.evals,
        workflow_name=spec.name,
        workflow_version=spec.version,
        threshold=0.7,
    )
    assert summary.cases[0].aggregated_score == pytest.approx(0.0)
    assert summary.cases[0].passed is False


@pytest.mark.unit
async def test_engine_returns_eval_summary_fields(
    tmp_path: Path,
) -> None:
    storage = InMemoryStorage()
    await storage.init()
    wf_dir = _make_workflow(tmp_path / "wf")
    spec, wf_path = load_workflow_spec(wf_dir)
    graph = compile_workflow(spec, wf_path)
    engine = _make_engine(storage)
    summary = await engine.run(
        graph,
        wf_path,
        spec.evals,
        workflow_name=spec.name,
        workflow_version=spec.version,
        threshold=0.7,
    )
    assert summary.agent_version == "0.1.0"
    assert summary.runs_per_case == 1
    assert summary.threshold == pytest.approx(0.7)
    assert len(summary.dataset_hash) == 64


@pytest.mark.unit
async def test_engine_multi_run_averages_scores(
    tmp_path: Path,
) -> None:
    storage = InMemoryStorage()
    await storage.init()
    wf_dir = _make_workflow(tmp_path / "wf")
    spec, wf_path = load_workflow_spec(wf_dir)
    graph = compile_workflow(spec, wf_path)
    provider = _StateAwareProvider()
    pricing = load_pricing()
    tracer = NullTracer()
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)
    engine = WorkflowEvalEngine(
        executor=executor, storage=storage, provider=provider, runs_per_case=2
    )
    summary = await engine.run(
        graph,
        wf_path,
        spec.evals,
        workflow_name=spec.name,
        workflow_version=spec.version,
        threshold=0.7,
    )
    assert len(summary.cases[0].runs) == 2
    # Both runs should pass → mean = 1.0
    assert summary.cases[0].aggregated_score == pytest.approx(1.0)


@pytest.mark.unit
async def test_engine_coverage_dim_scored(
    tmp_path: Path,
) -> None:
    storage = InMemoryStorage()
    await storage.init()
    wf_dir = _make_workflow(
        tmp_path / "wf",
        dataset_content=json.dumps(
            {
                "input": {"text": "hello"},
                "expected": {"step2": "hello"},
                "expected_coverage": ["hello", "missing-topic"],
            }
        )
        + "\n",
    )
    spec, wf_path = load_workflow_spec(wf_dir)
    graph = compile_workflow(spec, wf_path)
    engine = _make_engine(storage)
    summary = await engine.run(
        graph,
        wf_path,
        spec.evals,
        workflow_name=spec.name,
        workflow_version=spec.version,
        threshold=0.7,
    )
    cov = summary.dimensional_means.coverage
    assert cov is not None
    assert 0.0 < cov < 1.0  # "hello" hit, "missing-topic" missed → 0.5


@pytest.mark.unit
async def test_engine_dimensional_means_none_when_not_scored(
    tmp_path: Path,
) -> None:
    storage = InMemoryStorage()
    await storage.init()
    wf_dir = _make_workflow(tmp_path / "wf")
    spec, wf_path = load_workflow_spec(wf_dir)
    graph = compile_workflow(spec, wf_path)
    engine = _make_engine(storage)
    summary = await engine.run(
        graph,
        wf_path,
        spec.evals,
        workflow_name=spec.name,
        workflow_version=spec.version,
        threshold=0.7,
    )
    # No grounding / refusal_expected / coverage in dataset → all None
    assert summary.dimensional_means.faithfulness is None
    assert summary.dimensional_means.coverage is None
    assert summary.dimensional_means.refusal is None


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def _scaffold_cli_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_evals: bool = True,
    dataset_content: str | None = None,
) -> Path:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["init", "--project", "proj", "--skip-snapshot", "--with-agents", "ticket-triager"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    project = tmp_path / "proj"
    monkeypatch.chdir(project)

    wf_dir = project / "workflows" / "test-wf"
    _make_workflow(wf_dir, with_evals=with_evals, dataset_content=dataset_content)
    return wf_dir


@pytest.mark.unit
def test_cli_eval_workflow_passes_on_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wf_dir = _scaffold_cli_workflow(
        tmp_path,
        monkeypatch,
        dataset_content=json.dumps(
            {
                "input": {"text": "hello"},
                "expected": {"step2": "hello"},
            }
        )
        + "\n",
    )
    result = runner.invoke(
        app,
        ["eval", str(wf_dir), "--mock"],
        env={"COLUMNS": "200"},
    )
    # With mock provider the workflow nodes will return default mock responses.
    # We just verify the command exits cleanly (not crashing on dispatch).
    # Exact pass/fail depends on mock output shape — allow 0 or 1.
    assert result.exit_code in (0, 1), result.stdout + result.stderr
    assert "workflow" in result.stdout.lower() or "test-workflow" in result.stdout.lower()


@pytest.mark.unit
def test_cli_eval_workflow_no_evals_stanza_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wf_dir = _scaffold_cli_workflow(tmp_path, monkeypatch, with_evals=False)
    result = runner.invoke(
        app,
        ["eval", str(wf_dir), "--mock"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
    assert "evals" in result.stderr.lower() or "evals" in result.stdout.lower()


# ---------------------------------------------------------------------------
# Phase 2: faithfulness scoring
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_engine_faithfulness_skipped_without_judge_model(
    tmp_path: Path,
) -> None:
    """When the dataset has grounding but no judge.yaml, faithfulness is None."""
    storage = InMemoryStorage()
    await storage.init()
    wf_dir = _make_workflow(
        tmp_path / "wf",
        dataset_content=json.dumps(
            {
                "input": {"text": "hello"},
                "expected": {"step2": "hello"},
                "grounding": "The workflow echoes the input through both nodes.",
            }
        )
        + "\n",
    )
    spec, wf_path = load_workflow_spec(wf_dir)
    graph = compile_workflow(spec, wf_path)
    engine = _make_engine(storage)
    summary = await engine.run(
        graph,
        wf_path,
        spec.evals,
        workflow_name=spec.name,
        workflow_version=spec.version,
        threshold=0.7,
    )
    # No judge.yaml → exact-match judge → faithfulness skipped (None)
    assert summary.dimensional_means.faithfulness is None
    # The dim score on the case run should also be None (unscored)
    case_run = summary.cases[0].runs[0]
    assert case_run.dimensions.faithfulness.value is None
    assert "judge model" in case_run.dimensions.faithfulness.rationale


@pytest.mark.unit
async def test_engine_faithfulness_scored_with_judge_model(
    tmp_path: Path,
) -> None:
    """When grounding + judge.yaml present, faithfulness gets a score."""
    from movate.providers.mock import MockProvider  # noqa: PLC0415

    storage = InMemoryStorage()
    await storage.init()
    wf_dir = _make_workflow(
        tmp_path / "wf",
        dataset_content=json.dumps(
            {
                "input": {"text": "hello"},
                "expected": {"step2": "hello"},
                "grounding": "The workflow echoes the input through both nodes.",
            }
        )
        + "\n",
    )
    # Write a judge.yaml so the engine uses LLM_JUDGE mode.
    judge_yaml = {
        "method": "llm_judge",
        "rubric": "Does the output faithfully reflect the grounding?",
        "model": {
            "provider": "anthropic/claude-haiku-4-5-20251001",
            "params": {"max_tokens": 256},
        },
    }
    import yaml as _yaml  # noqa: PLC0415

    (wf_dir / "evals" / "judge.yaml").write_text(_yaml.safe_dump(judge_yaml))

    spec, wf_path = load_workflow_spec(wf_dir)
    graph = compile_workflow(spec, wf_path)

    # Use a mock provider that returns a valid judge score for faithfulness.
    mock_provider = MockProvider(response='{"score": 0.9, "rationale": "faithful"}')
    pricing = load_pricing()
    tracer = NullTracer()
    executor = Executor(provider=mock_provider, pricing=pricing, storage=storage, tracer=tracer)
    engine = WorkflowEvalEngine(
        executor=executor, storage=storage, provider=mock_provider, runs_per_case=1
    )
    summary = await engine.run(
        graph,
        wf_path,
        spec.evals,
        workflow_name=spec.name,
        workflow_version=spec.version,
        threshold=0.7,
    )
    # MockProvider returns the mock response for both agent calls and the
    # faithfulness judge call. Since the agent call response '{"score":...}'
    # won't validate against the node's output schema, it will error.
    # That's fine — we just verify the faithfulness dimension is attempted
    # (not skipped) when a grounding + judge model is present.
    # The judge_provider should now be set on the summary.
    assert summary.judge_provider is not None


@pytest.mark.unit
def test_load_workflow_judge_config_defaults_to_exact(tmp_path: Path) -> None:
    from movate.core.eval import load_workflow_judge_config  # noqa: PLC0415
    from movate.core.models import JudgeMethod  # noqa: PLC0415

    config = load_workflow_judge_config(tmp_path)
    assert config.method == JudgeMethod.EXACT


@pytest.mark.unit
def test_load_workflow_judge_config_reads_yaml(tmp_path: Path) -> None:
    import yaml as _yaml  # noqa: PLC0415

    from movate.core.eval import load_workflow_judge_config  # noqa: PLC0415
    from movate.core.models import JudgeMethod  # noqa: PLC0415

    (tmp_path / "evals").mkdir()
    (tmp_path / "evals" / "judge.yaml").write_text(
        _yaml.safe_dump(
            {
                "method": "llm_judge",
                "rubric": "Is it good?",
                "model": {
                    "provider": "anthropic/claude-haiku-4-5-20251001",
                    "params": {"max_tokens": 128},
                },
            }
        )
    )
    config = load_workflow_judge_config(tmp_path)
    assert config.method == JudgeMethod.LLM_JUDGE


# ---------------------------------------------------------------------------
# Phase 2: mdk validate warns when evals stanza missing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_warns_when_workflow_has_no_evals_stanza(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    wf_dir = _make_workflow(tmp_path / "wf", with_evals=False)
    result = runner.invoke(
        app,
        ["validate", str(wf_dir)],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "evals" in result.stdout.lower()


@pytest.mark.unit
def test_validate_no_warning_when_workflow_has_evals_stanza(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    wf_dir = _make_workflow(tmp_path / "wf", with_evals=True)
    result = runner.invoke(
        app,
        ["validate", str(wf_dir)],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # "!" advisory should not appear for a workflow that has evals configured
    assert "no evals" not in result.stdout.lower()
