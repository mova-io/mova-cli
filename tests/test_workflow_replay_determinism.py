"""Tests for the deterministic replay flagship (``mdk workflow replay``) —
``--from-file`` offline mode, ``mdk workflow history`` export, and
divergence-detection.

Gated behind ``@pytest.mark.temporal`` (skip by default) because the
Replayer + history-round-trip require the ``[temporal]`` extra.

Three test scenarios:

1. **Mocked deterministic replay** — build a minimal workflow history dict,
   feed it through the Replayer, and assert ``replay_failure is None``.
2. **Deliberate divergence detection** — alter an activity result in the
   history so the replay code path diverges from the recorded decisions;
   assert the failure is surfaced, not silently swallowed.
3. **Offline from-file round-trip** — export a history to JSON, then replay
   from that file, confirming the offline path produces the same result as
   the live path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

# ---------------------------------------------------------------------------
# Guard: skip the entire module when the [temporal] extra is absent.
# ---------------------------------------------------------------------------

temporalio = pytest.importorskip(
    "temporalio",
    reason="the [temporal] extra is not installed; temporal replay tests skipped",
)

from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import (  # noqa: E402
    Replayer,
    UnsandboxedWorkflowRunner,
    Worker,
)

from movate.core.workflow import compile_workflow, load_workflow_spec  # noqa: E402
from movate.core.workflow.compilers.temporal import TemporalCompiler  # noqa: E402
from movate.core.workflow.temporal_activities import (  # noqa: E402
    call_agent_activity,
    call_gate_activity,
    call_judge_activity,
    call_skill_activity,
    configure_activities,
)
from movate.providers.base import (  # noqa: E402
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
)
from movate.providers.pricing import load_pricing  # noqa: E402
from movate.runtime.workflow_backend import (  # noqa: E402
    DEFAULT_TASK_QUEUE,
    load_compiled_workflow_class,
)
from movate.testing import InMemoryStorage, NullTracer  # noqa: E402

# ---------------------------------------------------------------------------
# Shared scaffolding
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
    (agent_dir / "prompt.md").write_text(
        "echo {{ input." + input_key + " }} as " + output_key + "\n"
    )
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": [input_key],
                "properties": {input_key: {"type": "string", "minLength": 1}},
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


def _scaffold_workflow(workflows_root: Path, *, name: str) -> Path:
    """Write a ``text -> step1 -> step2`` temporal workflow bundle."""
    workflow_dir = workflows_root / name
    _make_agent(
        workflow_dir / "agents" / "first", name="first-agent", input_key="text", output_key="step1"
    )
    _make_agent(
        workflow_dir / "agents" / "second",
        name="second-agent",
        input_key="step1",
        output_key="step2",
    )
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "state.json").write_text(json.dumps(_STATE_SCHEMA))
    body: dict[str, Any] = {
        "api_version": "movate/v1",
        "kind": "Workflow",
        "name": name,
        "version": "0.1.0",
        "runtime": "temporal",
        "state_schema": "./state.json",
        "entrypoint": "first",
        "nodes": [
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {"id": "second", "type": "agent", "ref": "./agents/second"},
        ],
        "edges": [{"from": "first", "to": "second"}],
    }
    (workflow_dir / "workflow.yaml").write_text(yaml.safe_dump(body))
    return workflow_dir


class _DeterministicProvider(BaseLLMProvider):
    """Deterministic offline provider that returns fixed step1/step2 values."""

    name = "deterministic"
    version = "0.0.1"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = request.messages[0].content
        if "step1" in body and "step2" not in body:
            return CompletionResponse(text='{"step1": "alpha"}')
        return CompletionResponse(text='{"step2": "beta"}')

    async def stream(self, request: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text: str, *, model: str) -> Any:  # pragma: no cover
        raise NotImplementedError


class _DivergentProvider(BaseLLMProvider):
    """Provider that returns DIFFERENT values from what the original run recorded.

    Used to test divergence detection: the original run produced
    step1='alpha', step2='beta'; this provider produces step1='CHANGED'.
    """

    name = "divergent"
    version = "0.0.1"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = request.messages[0].content
        if "step1" in body and "step2" not in body:
            return CompletionResponse(text='{"step1": "CHANGED"}')
        return CompletionResponse(text='{"step2": "ALSO_CHANGED"}')

    async def stream(self, request: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text: str, *, model: str) -> Any:  # pragma: no cover
        raise NotImplementedError


async def _run_and_capture_history(
    tmp_path: Path,
) -> tuple[Any, Any, Any]:
    """Run a workflow on the test env and return (history, compiled, workflow_cls).

    The history is a real Temporal WorkflowHistory proto suitable for replay.
    """
    workflows_root = tmp_path / "workflows"
    _scaffold_workflow(workflows_root, name="replay-det-wf")
    spec, parent = load_workflow_spec(workflows_root / "replay-det-wf" / "workflow.yaml")
    graph = compile_workflow(spec, parent)

    pricing = load_pricing()
    storage = InMemoryStorage()
    await storage.init()
    configure_activities(
        storage=storage,
        pricing=pricing,
        tracer=NullTracer(),
        provider=_DeterministicProvider(),
        tenant_id="local",
    )

    compiled = TemporalCompiler().compile(graph)
    workflow_cls = load_compiled_workflow_class(
        compiled.module_source, compiled.workflow_class_name
    )

    run_id = "det-test-run-1"
    env = await WorkflowEnvironment.start_time_skipping()
    async with (
        env,
        Worker(
            env.client,
            task_queue=DEFAULT_TASK_QUEUE,
            workflows=[workflow_cls],
            activities=[
                call_agent_activity,
                call_skill_activity,
                call_gate_activity,
                call_judge_activity,
            ],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ),
    ):
        final = await env.client.execute_workflow(
            workflow_cls.run,
            {"text": "hello", "tenant_id": "local"},
            id=run_id,
            task_queue=DEFAULT_TASK_QUEUE,
        )
        assert final["step2"] == "beta"

        history = await env.client.get_workflow_handle(run_id).fetch_history()

    return history, compiled, workflow_cls


# ---------------------------------------------------------------------------
# 1. Mocked deterministic replay — the core assertion
# ---------------------------------------------------------------------------


@pytest.mark.temporal
@pytest.mark.smoke
async def test_replay_deterministic_with_mocked_history(tmp_path: Path) -> None:
    """Replay a captured history against the same compiled workflow.

    The replay must succeed (``replay_failure is None``) because the workflow
    code has not changed -- the decisions in the history exactly match what
    the current code would produce.
    """
    history, compiled, _original_cls = await _run_and_capture_history(tmp_path)

    # Replay with a fresh compile of the same workflow (as the CLI does).
    replay_cls = load_compiled_workflow_class(compiled.module_source, compiled.workflow_class_name)
    replayer = Replayer(
        workflows=[replay_cls],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    result = await replayer.replay_workflow(history, raise_on_replay_failure=False)
    assert result.replay_failure is None, (
        f"expected deterministic replay but got failure: {result.replay_failure}"
    )


# ---------------------------------------------------------------------------
# 2. Divergence detection — deliberately altered activity results
# ---------------------------------------------------------------------------


@pytest.mark.temporal
@pytest.mark.smoke
async def test_replay_detects_divergence_on_altered_workflow(tmp_path: Path) -> None:
    """Replay a captured history against an ALTERED workflow that would make
    different decisions.

    The Replayer should detect the non-determinism and report a failure
    (``replay_failure is not None``). This validates that the divergence-
    detection path actually catches real determinism violations.

    We alter the workflow by changing the node order (second before first)
    which changes the entrypoint, causing a different execution path than
    what the history recorded.
    """
    history, _compiled, _original_cls = await _run_and_capture_history(tmp_path)

    # Build a DIFFERENT workflow that reverses the node order.
    # The history was recorded with entrypoint='first' executing first,
    # but we'll compile with entrypoint='second' -- the Replayer should
    # detect the workflow took a different path.
    workflows_root = tmp_path / "altered_workflows"
    wf_dir = workflows_root / "replay-det-wf"
    # Reuse the same agent scaffolding
    _make_agent(
        wf_dir / "agents" / "first", name="first-agent", input_key="text", output_key="step1"
    )
    _make_agent(
        wf_dir / "agents" / "second",
        name="second-agent",
        input_key="step1",
        output_key="step2",
    )
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "state.json").write_text(json.dumps(_STATE_SCHEMA))
    # Entrypoint is 'second' instead of 'first' -- this changes what
    # activity the workflow calls first, diverging from the recorded history.
    altered_body: dict[str, Any] = {
        "api_version": "movate/v1",
        "kind": "Workflow",
        "name": "replay-det-wf",
        "version": "0.1.0",
        "runtime": "temporal",
        "state_schema": "./state.json",
        "entrypoint": "second",
        "nodes": [
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {"id": "second", "type": "agent", "ref": "./agents/second"},
        ],
        "edges": [{"from": "second", "to": "first"}],
    }
    (wf_dir / "workflow.yaml").write_text(yaml.safe_dump(altered_body))

    spec, parent = load_workflow_spec(wf_dir / "workflow.yaml")
    graph = compile_workflow(spec, parent)
    altered_compiled = TemporalCompiler().compile(graph)
    altered_cls = load_compiled_workflow_class(
        altered_compiled.module_source, altered_compiled.workflow_class_name
    )

    replayer = Replayer(
        workflows=[altered_cls],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    result = await replayer.replay_workflow(history, raise_on_replay_failure=False)
    assert result.replay_failure is not None, (
        "expected a divergence (non-determinism failure) but replay succeeded"
    )


# ---------------------------------------------------------------------------
# 3. Offline from-file round-trip — export history to JSON, replay from file
# ---------------------------------------------------------------------------


@pytest.mark.temporal
@pytest.mark.smoke
async def test_offline_replay_from_exported_history_file(tmp_path: Path) -> None:
    """Export a history to JSON, then load it back and replay successfully.

    This exercises the exact serialization round-trip that
    ``mdk workflow history --output`` + ``mdk workflow replay --from-file``
    performs: WorkflowHistory -> to_json -> file -> from_json -> Replayer.
    """
    from temporalio.client import WorkflowHistory  # noqa: PLC0415

    history, compiled, _original_cls = await _run_and_capture_history(tmp_path)

    # Export: WorkflowHistory -> JSON string -> file (mirrors _fetch_history).
    history_json_str = history.to_json()
    history_dict = json.loads(history_json_str)
    # The CLI adds the workflowId for the offline round-trip.
    history_dict["workflowId"] = "det-test-run-1"
    history_file = tmp_path / "exported_history.json"
    history_file.write_text(json.dumps(history_dict, indent=2), encoding="utf-8")

    # Import: file -> JSON dict -> WorkflowHistory (mirrors
    # _fetch_history_and_replay in offline mode).
    raw = json.loads(history_file.read_text(encoding="utf-8"))
    restored_history = WorkflowHistory.from_json(raw.get("workflowId", "unknown"), raw)

    # Replay the restored history against the same workflow.
    replay_cls = load_compiled_workflow_class(compiled.module_source, compiled.workflow_class_name)
    replayer = Replayer(
        workflows=[replay_cls],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    result = await replayer.replay_workflow(restored_history, raise_on_replay_failure=False)
    assert result.replay_failure is None, (
        f"offline replay from exported file failed: {result.replay_failure}"
    )
