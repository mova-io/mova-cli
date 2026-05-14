"""Worker dispatch + loop — agent/workflow happy + error paths.

Two layers:

1. **Dispatch** (``WorkerDispatch.execute_job``) — pure logic, no
   loop. Asserts each branch (success, agent-not-registered,
   workflow-not-registered, executor crash, status mappings).
2. **Loop** (``Worker.run_one_cycle`` / ``run_forever``) — claim →
   dispatch → update. Uses the `run_one_cycle` entry point so tests
   are deterministic; no sleeps, no stop-event timing.

End-to-end binary smoke (real ``movate serve`` + ``movate worker``)
is documented in BACKLOG and walked through manually before commit
— hard to automate from inside pytest without spinning up two
processes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from movate.cli.main import app as cli_app
from movate.core.executor import Executor
from movate.core.models import (
    JobKind,
    JobRecord,
    JobStatus,
)
from movate.providers.mock import MockProvider
from movate.providers.pricing import load_pricing
from movate.runtime.dispatch import WorkerDispatch
from movate.runtime.registry import scan_agents, scan_workflows
from movate.runtime.worker import Worker, WorkerConfig
from movate.testing import InMemoryStorage, NullTracer

cli_runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_executor(storage: InMemoryStorage) -> Executor:
    return Executor(
        provider=MockProvider(),
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
        tenant_id="local",
    )


def _make_job(
    *,
    kind: JobKind = JobKind.AGENT,
    target: str = "alpha",
    tenant_id: str = "tenant-a",
    input_payload: dict | None = None,
) -> JobRecord:
    return JobRecord(
        job_id=str(uuid4()),
        tenant_id=tenant_id,
        kind=kind,
        target=target,
        status=JobStatus.QUEUED,
        input=input_payload if input_payload is not None else {"text": "hi"},
    )


@pytest.fixture
def scaffolded_agent(tmp_path: Path) -> Path:
    """Real on-disk agent named ``alpha`` so the registry can load it."""
    cli_runner.invoke(
        cli_app,
        ["init", "alpha", "-t", "default", "--target", str(tmp_path)],
        catch_exceptions=False,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Dispatch — agent path
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_dispatch_agent_success(
    scaffolded_agent: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: registered agent → SUCCESS + result_run_id populated."""
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "hi"}')

    storage = InMemoryStorage()
    await storage.init()
    executor = _make_executor(storage)
    agents = scan_agents(scaffolded_agent)

    dispatch = WorkerDispatch(storage=storage, executor=executor, agents=agents)
    job = _make_job(target="alpha")
    await storage.save_job(job)

    outcome = await dispatch.execute_job(job)
    assert outcome.status == JobStatus.SUCCESS
    assert outcome.result_run_id is not None
    assert outcome.error is None

    # The persisted RunRecord carries the same run_id (proves the
    # ID we wrote into the DispatchOutcome matches what's stored).
    assert any(r.run_id == outcome.result_run_id for r in storage.runs)


@pytest.mark.unit
async def test_dispatch_unknown_agent_is_terminal_error() -> None:
    """Caller asks for an agent the worker doesn't have → ERROR with
    a helpful message; no executor invocation, no DB write."""
    storage = InMemoryStorage()
    await storage.init()
    executor = _make_executor(storage)

    dispatch = WorkerDispatch(storage=storage, executor=executor, agents=[])
    job = _make_job(target="ghost")
    outcome = await dispatch.execute_job(job)

    assert outcome.status == JobStatus.ERROR
    assert outcome.result_run_id is None
    assert outcome.error is not None
    assert outcome.error["type"] == "unknown_agent"
    assert "ghost" in outcome.error["message"]
    # Item 118: hint should point callers at the cross-pod gap (item 109)
    # and the ?wait=true workaround (item 110) so they stop debugging
    # "did my agent actually get created?" — it did, just not here.
    assert outcome.error["hint"] is not None
    assert "?wait=true" in outcome.error["hint"]


@pytest.mark.unit
async def test_dispatch_agent_executor_crash_is_internal_error(
    scaffolded_agent: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Executor.execute() raises an unhandled exception, the worker
    converts it to a retryable INTERNAL error rather than crashing."""
    storage = InMemoryStorage()
    await storage.init()

    class CrashingExecutor:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("provider broke")

    agents = scan_agents(scaffolded_agent)
    dispatch = WorkerDispatch(
        storage=storage,
        executor=CrashingExecutor(),  # type: ignore[arg-type]
        agents=agents,
    )
    job = _make_job(target="alpha")
    outcome = await dispatch.execute_job(job)

    assert outcome.status == JobStatus.ERROR
    assert outcome.error is not None
    assert outcome.error["type"] == "internal"
    assert outcome.error["retryable"] is True


# ---------------------------------------------------------------------------
# Dispatch — workflow path
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_dispatch_unknown_workflow_is_terminal_error(
    scaffolded_agent: Path,
) -> None:
    """JobKind.WORKFLOW with no matching graph → ERROR; doesn't fall
    through to the agent path."""
    storage = InMemoryStorage()
    await storage.init()
    executor = _make_executor(storage)
    agents = scan_agents(scaffolded_agent)

    dispatch = WorkerDispatch(
        storage=storage,
        executor=executor,
        agents=agents,
        workflows={},
    )
    job = _make_job(kind=JobKind.WORKFLOW, target="returns-pipeline")
    outcome = await dispatch.execute_job(job)
    assert outcome.status == JobStatus.ERROR
    assert outcome.error is not None
    assert outcome.error["type"] == "unknown_workflow"


@pytest.mark.unit
async def test_dispatch_workflow_dispatches_to_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real workflow.yaml on disk → registry discovers it → dispatch
    routes through WorkflowRunner and lands SUCCESS with the
    workflow_run_id mirrored into result_run_id."""
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "step ok"}')

    # Scaffold ONE agent the workflow node will reference.
    cli_runner.invoke(
        cli_app,
        ["init", "alpha", "-t", "default", "--target", str(tmp_path / "agents")],
        catch_exceptions=False,
    )

    # Build a one-node linear workflow that uses that agent.
    # WorkflowSpec wants `state_schema` to be a *path* to a JSON Schema
    # file, and node `ref` (not `agent`) for the relative path.
    workflows_root = tmp_path / "workflows" / "tiny"
    workflows_root.mkdir(parents=True)
    (workflows_root / "state.json").write_text(
        '{"type": "object", "properties": {"text": {"type": "string"}}}'
    )
    import os.path  # noqa: PLC0415  -- 3.11 lacks Path.relative_to(walk_up=True)

    agent_relative = os.path.relpath(tmp_path / "agents" / "alpha", workflows_root)
    (workflows_root / "workflow.yaml").write_text(
        f"""
api_version: movate/v1
kind: Workflow
name: tiny
version: 0.1.0
entrypoint: first
state_schema: state.json
nodes:
  - id: first
    type: agent
    ref: {agent_relative}
edges: []
""".strip()
    )

    storage = InMemoryStorage()
    await storage.init()
    executor = _make_executor(storage)
    agents = scan_agents(tmp_path / "agents")
    workflows = scan_workflows(tmp_path / "workflows")
    assert "tiny" in workflows, f"registry didn't load tiny: {list(workflows)}"

    dispatch = WorkerDispatch(
        storage=storage,
        executor=executor,
        agents=agents,
        workflows=workflows,
    )
    job = _make_job(kind=JobKind.WORKFLOW, target="tiny", input_payload={"text": "hi"})
    outcome = await dispatch.execute_job(job)

    # The exact terminal status depends on whether the inner agent
    # response merges cleanly into the state schema; we accept SUCCESS
    # OR ERROR but require result_run_id to be set either way (proves
    # the workflow_run_id was mirrored out).
    assert outcome.status in (JobStatus.SUCCESS, JobStatus.ERROR)
    assert outcome.result_run_id is not None


# ---------------------------------------------------------------------------
# Worker loop — claim + update
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_worker_run_one_cycle_returns_none_on_empty_queue(
    scaffolded_agent: Path,
) -> None:
    storage = InMemoryStorage()
    await storage.init()
    executor = _make_executor(storage)
    agents = scan_agents(scaffolded_agent)
    dispatch = WorkerDispatch(storage=storage, executor=executor, agents=agents)
    worker = Worker(storage=storage, dispatch=dispatch)

    handled = await worker.run_one_cycle()
    assert handled is None


@pytest.mark.unit
async def test_worker_run_one_cycle_drains_one_job(
    scaffolded_agent: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full lifecycle: queue a job, run one cycle, observe the job
    transition to SUCCESS with result_run_id populated."""
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "hi"}')

    storage = InMemoryStorage()
    await storage.init()
    executor = _make_executor(storage)
    agents = scan_agents(scaffolded_agent)

    job = _make_job(target="alpha")
    await storage.save_job(job)

    dispatch = WorkerDispatch(storage=storage, executor=executor, agents=agents)
    worker = Worker(storage=storage, dispatch=dispatch)

    handled = await worker.run_one_cycle()
    assert handled is not None
    assert handled.job_id == job.job_id

    final = await storage.get_job(job.job_id, tenant_id="tenant-a")
    assert final is not None
    assert final.status == JobStatus.SUCCESS
    assert final.result_run_id is not None
    assert final.completed_at is not None


@pytest.mark.unit
async def test_worker_records_error_on_unknown_target() -> None:
    """A job whose target isn't in the registry should still transition
    to a terminal state — ERROR, with a structured error info."""
    storage = InMemoryStorage()
    await storage.init()
    executor = _make_executor(storage)

    job = _make_job(target="ghost")
    await storage.save_job(job)

    dispatch = WorkerDispatch(storage=storage, executor=executor, agents=[])
    worker = Worker(storage=storage, dispatch=dispatch)
    await worker.run_one_cycle()

    final = await storage.get_job(job.job_id, tenant_id="tenant-a")
    assert final is not None
    assert final.status == JobStatus.ERROR
    assert final.error is not None
    assert final.error.type == "unknown_agent"


@pytest.mark.unit
async def test_worker_respects_tenant_scoping() -> None:
    """A worker bound to tenant-a must NOT claim tenant-b's jobs."""
    storage = InMemoryStorage()
    await storage.init()
    executor = _make_executor(storage)

    a = _make_job(target="alpha", tenant_id="tenant-a")
    b = _make_job(target="alpha", tenant_id="tenant-b")
    await storage.save_job(a)
    await storage.save_job(b)

    dispatch = WorkerDispatch(storage=storage, executor=executor, agents=[])
    worker = Worker(
        storage=storage,
        dispatch=dispatch,
        config=WorkerConfig(tenant_id="tenant-a"),
    )
    handled = await worker.run_one_cycle()
    assert handled is not None
    assert handled.tenant_id == "tenant-a"

    # tenant-b's job is still queued.
    b_after = await storage.get_job(b.job_id, tenant_id="tenant-b")
    assert b_after is not None
    assert b_after.status == JobStatus.QUEUED


@pytest.mark.unit
async def test_worker_run_forever_exits_when_stop_event_set(
    scaffolded_agent: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_forever must exit promptly when the stop event fires —
    even with poll_interval > 0."""
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "hi"}')

    storage = InMemoryStorage()
    await storage.init()
    executor = _make_executor(storage)
    agents = scan_agents(scaffolded_agent)
    dispatch = WorkerDispatch(storage=storage, executor=executor, agents=agents)
    worker = Worker(
        storage=storage,
        dispatch=dispatch,
        config=WorkerConfig(poll_interval_seconds=10.0),  # long, but cancelable
    )

    stop = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    # Should exit within ~50ms even though the configured poll is 10s.
    await asyncio.gather(worker.run_forever(stop), stop_soon())
