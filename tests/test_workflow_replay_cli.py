"""Tests for ``mdk workflow replay <run_id>`` — deterministic time-travel
replay against a Temporal run's history (ADR 054 D6 / Phase 3).

Three behaviours, mirroring the build's test plan:

1. **Hermetic run→replay** (needs ``[temporal]``) — compile a small workflow,
   RUN it on ``WorkflowEnvironment.start_time_skipping()`` to produce a real
   event history, fetch that history, then feed it back through
   ``temporalio.worker.Replayer`` and assert it reproduces every decision
   (``replay_failure is None``). This is the load-bearing assertion that the
   replay path actually verifies determinism.
2. **Native-run rejection** (no extra) — a stored run whose on-disk workflow is
   ``runtime: native`` exits cleanly with the "replay requires a Temporal-backed
   run" message, NOT a traceback.
3. **Missing-extra path** (no extra, mocked) — a temporal run whose
   ``require_backend_available`` raises ``WorkflowBackendError`` exits cleanly
   with the actionable install/connection hint.

The hermetic case skips when the ``[temporal]`` extra is absent; the rejection
+ missing-extra cases run everywhere (they never import ``temporalio``).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import typer
import yaml

from movate.cli import workflow_cmd
from movate.core.models import WorkflowRunRecord, WorkflowStatus
from movate.runtime import workflow_backend
from movate.storage import build_storage

# ---------------------------------------------------------------------------
# Shared scaffolding — a minimal linear two-node workflow on disk, mirroring
# tests/test_temporal_execution.py so the graph compiles on both backends.
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


def _scaffold_workflow(workflows_root: Path, *, name: str, runtime: str | None) -> Path:
    """Write a ``text → step1 → step2`` workflow bundle under ``workflows_root/<name>``.

    Returns the workflow directory. ``runtime`` (when set) writes a ``runtime:``
    key so the same scaffolding produces native and temporal variants.
    """
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
        "state_schema": "./state.json",
        "entrypoint": "first",
        "nodes": [
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {"id": "second", "type": "agent", "ref": "./agents/second"},
        ],
        "edges": [{"from": "first", "to": "second"}],
    }
    if runtime is not None:
        body["runtime"] = runtime
    (workflow_dir / "workflow.yaml").write_text(yaml.safe_dump(body))
    return workflow_dir


async def _store_run(*, run_id: str, workflow: str, tenant_id: str = "local") -> None:
    """Persist a SUCCESS WorkflowRunRecord into the env-configured storage."""
    storage = build_storage()
    await storage.init()
    try:
        await storage.save_workflow_run(
            WorkflowRunRecord(
                workflow_run_id=run_id,
                tenant_id=tenant_id,
                workflow=workflow,
                workflow_version="0.1.0",
                status=WorkflowStatus.SUCCESS,
                initial_state={"text": "hello"},
                final_state={"text": "hello", "step1": "alpha", "step2": "beta"},
            )
        )
    finally:
        await storage.close()


@pytest.fixture
def _local_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``build_storage`` at a throwaway sqlite file (hermetic)."""
    db = tmp_path / "replay.db"
    monkeypatch.setenv("MOVATE_DB", str(db))
    # Ensure no Postgres URL hijacks selection.
    monkeypatch.delenv("MOVATE_DB_URL", raising=False)
    monkeypatch.delenv("MOVATE_PG_URL", raising=False)
    return db


# ---------------------------------------------------------------------------
# 2. Native-run rejection — clean exit, not a crash (no extra needed).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_replay_rejects_native_run(
    tmp_path: Path, _local_db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A run whose on-disk workflow is ``runtime: native`` is rejected cleanly."""
    workflows_root = tmp_path / "workflows"
    _scaffold_workflow(workflows_root, name="native-wf", runtime=None)  # default native
    asyncio.run(_store_run(run_id="run-native-1", workflow="native-wf"))

    with pytest.raises(typer.Exit) as ei:
        workflow_cmd.replay(
            run_id="run-native-1",
            workflows_path=workflows_root,
            tenant_id="local",
            as_json=False,
        )
    assert ei.value.exit_code == 2
    # Rich soft-wraps stderr; normalize whitespace before matching.
    err = " ".join(capsys.readouterr().err.split())
    assert "replay requires a Temporal-backed run" in err
    assert "this run used native" in err


@pytest.mark.unit
def test_replay_unknown_run_exits_cleanly(
    tmp_path: Path, _local_db: Path, capsys: pytest.CaptureFixture
) -> None:
    """A run id with no stored record exits 1 with a browse hint, not a crash."""
    with pytest.raises(typer.Exit) as ei:
        workflow_cmd.replay(
            run_id="does-not-exist",
            workflows_path=tmp_path / "workflows",
            tenant_id="local",
            as_json=False,
        )
    assert ei.value.exit_code == 1
    assert "not found" in capsys.readouterr().err


@pytest.mark.unit
def test_replay_missing_definition_exits_cleanly(
    tmp_path: Path, _local_db: Path, capsys: pytest.CaptureFixture
) -> None:
    """A stored run whose workflow.yaml is gone from disk exits 2, not a crash."""
    asyncio.run(_store_run(run_id="run-orphan-1", workflow="ghost-wf"))
    with pytest.raises(typer.Exit) as ei:
        workflow_cmd.replay(
            run_id="run-orphan-1",
            workflows_path=tmp_path / "workflows",  # empty — no ghost-wf bundle
            tenant_id="local",
            as_json=False,
        )
    assert ei.value.exit_code == 2
    assert "not found under" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 3. Missing-extra path — a temporal run whose backend isn't available exits
#    cleanly with the actionable hint (mocked; no extra needed).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_replay_temporal_missing_extra_exits_cleanly(
    tmp_path: Path, _local_db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A ``runtime: temporal`` run where the [temporal] extra/connection is
    missing exits 2 with the install/connection hint — never a traceback."""
    workflows_root = tmp_path / "workflows"
    _scaffold_workflow(workflows_root, name="temporal-wf", runtime="temporal")
    asyncio.run(_store_run(run_id="run-temporal-1", workflow="temporal-wf"))

    def _raise(_runtime: str) -> None:
        raise workflow_backend.WorkflowBackendError(
            "The [temporal] extra is not installed. Install with: ..."
        )

    monkeypatch.setattr(workflow_backend, "require_backend_available", _raise)

    with pytest.raises(typer.Exit) as ei:
        workflow_cmd.replay(
            run_id="run-temporal-1",
            workflows_path=workflows_root,
            tenant_id="local",
            as_json=False,
        )
    assert ei.value.exit_code == 2
    # The bracketed "[temporal]" is consumed by Rich markup (the codebase-wide
    # error() convention), but the actionable hint survives — the operator still
    # sees an install instruction rather than a traceback.
    err = " ".join(capsys.readouterr().err.split())
    assert "extra is not installed" in err
    assert "Install with" in err


# ---------------------------------------------------------------------------
# 1. Hermetic run→replay — the core determinism-verification assertion.
#    Skips cleanly when the [temporal] extra is absent.
# ---------------------------------------------------------------------------

temporalio = pytest.importorskip(
    "temporalio",
    reason="the [temporal] extra is not installed; hermetic replay test skipped",
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
    call_human_activity,
    call_judge_activity,
    call_skill_activity,
    configure_activities,
    persist_workflow_result_activity,
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


class _StateAwareProvider(BaseLLMProvider):
    """Deterministic offline provider (mirrors test_temporal_execution.py)."""

    name = "state_aware"
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


@pytest.mark.smoke
async def test_run_then_replay_is_deterministic(tmp_path: Path) -> None:
    """RUN a compiled workflow on the test env → capture its history → REPLAY it.

    The replay reproduces every recorded decision, so ``replay_failure`` is
    ``None``. This exercises the exact ``Replayer(...).replay_workflow(history,
    raise_on_replay_failure=False)`` path the CLI command uses — same compile
    (``load_compiled_workflow_class``), same unsandboxed runner — minus the
    live Temporal connection (the test env stands in for the server).
    """
    workflows_root = tmp_path / "workflows"
    _scaffold_workflow(workflows_root, name="temporal-wf", runtime="temporal")
    spec, parent = load_workflow_spec(workflows_root / "temporal-wf" / "workflow.yaml")
    graph = compile_workflow(spec, parent)
    assert graph.runtime == "temporal"

    pricing = load_pricing()
    storage = InMemoryStorage()
    await storage.init()
    configure_activities(
        storage=storage,
        pricing=pricing,
        tracer=NullTracer(),
        provider=_StateAwareProvider(),
        tenant_id="local",
    )

    compiled = TemporalCompiler().compile(graph)
    workflow_cls = load_compiled_workflow_class(
        compiled.module_source, compiled.workflow_class_name
    )

    run_id = "replay-hermetic-1"
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
                call_human_activity,
                persist_workflow_result_activity,
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

        # Fetch the durable history by id (== run_id, ADR 054 D6) — the same
        # call the CLI makes: client.get_workflow_handle(run_id).fetch_history().
        history = await env.client.get_workflow_handle(run_id).fetch_history()

    # Replay the history against a FRESH compile of the same workflow class —
    # this is the determinism check the command performs.
    replay_cls = load_compiled_workflow_class(compiled.module_source, compiled.workflow_class_name)
    replayer = Replayer(
        workflows=[replay_cls],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    result = await replayer.replay_workflow(history, raise_on_replay_failure=False)
    assert result.replay_failure is None  # ✓ replayed deterministically
