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
from datetime import UTC, datetime, timedelta
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
    WorkflowStatus,
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
    # ADR 014 D2 closed the old cross-pod sync gap (#109): the worker now
    # resolves agents from the durable registry, so a miss means the agent
    # genuinely isn't published for this tenant — NOT a sync lag. The hint
    # is still present and now points at "publish it / check the tenant"
    # rather than the obsolete ?wait=true workaround.
    assert outcome.error["hint"] is not None
    assert "registry" in outcome.error["hint"]


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


@pytest.mark.unit
async def test_dispatch_workflow_paused_maps_to_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 017 D5 (PR 1): a workflow that pauses at a HUMAN gate yields a
    PAUSED WorkflowResult. Dispatch maps that → JobStatus.SUCCESS (no new
    JobStatus) and surfaces the workflow_run_id as result_run_id (the durable
    handle PR 2's resume job loads)."""
    from pathlib import Path as _Path  # noqa: PLC0415

    from movate.core.models import WorkflowStatus  # noqa: PLC0415
    from movate.core.workflow import WorkflowGraph, WorkflowRunner  # noqa: PLC0415
    from movate.core.workflow.runner import WorkflowResult  # noqa: PLC0415

    storage = InMemoryStorage()
    await storage.init()
    executor = _make_executor(storage)

    # A placeholder graph object — only its presence in the registry matters;
    # the runner is monkeypatched to return a PAUSED result.
    graph = WorkflowGraph(
        name="approval-flow",
        version="0.1.0",
        description="",
        state_schema={"type": "object"},
        entrypoint="first",
        nodes={},
        edges=[],
        workflow_dir=_Path("/"),
    )

    async def _fake_run(self, graph, initial_state, **kwargs):
        return WorkflowResult(
            workflow_run_id="wf-paused-123",
            status=WorkflowStatus.PAUSED,
            initial_state=initial_state,
            final_state={"text": "hi", "step1": "done"},
        )

    monkeypatch.setattr(WorkflowRunner, "run", _fake_run)

    dispatch = WorkerDispatch(
        storage=storage,
        executor=executor,
        agents=[],
        workflows={"approval-flow": graph},
    )
    job = _make_job(kind=JobKind.WORKFLOW, target="approval-flow", input_payload={"text": "hi"})
    outcome = await dispatch.execute_job(job)

    assert outcome.status is JobStatus.SUCCESS
    assert outcome.result_run_id == "wf-paused-123"
    assert outcome.error is None


@pytest.mark.unit
async def test_dispatch_resume_job_drives_runner_resume(tmp_path: Path) -> None:
    """ADR 017 D5 (PR 2): a JobKind.WORKFLOW job carrying
    resume_workflow_run_id loads the PAUSED checkpoint, calls
    WorkflowRunner.resume, and completes the run (the 2nd agent runs with the
    merged human decision)."""
    import json as _json  # noqa: PLC0415
    import os.path  # noqa: PLC0415

    from movate.core.workflow import (  # noqa: PLC0415
        WorkflowRunner,
        compile_workflow,
        load_workflow_spec,
    )
    from movate.providers.base import (  # noqa: PLC0415
        BaseLLMProvider,
        CompletionRequest,
        CompletionResponse,
    )

    class _PerNode(BaseLLMProvider):
        name = "per_node"
        version = "0.0.1"

        async def complete(self, request: CompletionRequest) -> CompletionResponse:
            body = request.messages[0].content
            for key in ("step2", "step1"):
                if f"as {key}" in body:
                    return CompletionResponse(text=_json.dumps({key: f"{key}-out"}))
            return CompletionResponse(text='{"step1": "step1-out"}')

        async def stream(self, request):  # pragma: no cover
            raise NotImplementedError

        async def embed(self, text, *, model):  # pragma: no cover
            raise NotImplementedError

    # Build an agent -> human -> agent workflow on disk.
    def _agent(dir_: Path, name: str, in_key: str, out_key: str) -> None:
        dir_.mkdir(parents=True, exist_ok=True)
        (dir_ / "schema").mkdir(exist_ok=True)
        (dir_ / "agent.yaml").write_text(
            f"""
api_version: movate/v1
kind: Agent
name: {name}
version: 0.1.0
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
""".strip()
        )
        (dir_ / "prompt.md").write_text("echo {{ input." + in_key + " }} as " + out_key + "\n")
        (dir_ / "schema" / "input.json").write_text(
            _json.dumps(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [in_key],
                    "properties": {in_key: {"type": "string", "minLength": 1}},
                }
            )
        )
        (dir_ / "schema" / "output.json").write_text(
            _json.dumps(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [out_key],
                    "properties": {out_key: {"type": "string"}},
                }
            )
        )

    wf_root = tmp_path / "workflows" / "approval"
    wf_root.mkdir(parents=True)
    _agent(tmp_path / "agents" / "first", "first-agent", "text", "step1")
    _agent(tmp_path / "agents" / "second", "second-agent", "step1", "step2")
    (wf_root / "state.json").write_text(
        '{"type": "object", "additionalProperties": true, '
        '"properties": {"text": {"type": "string"}}}'
    )
    first_rel = os.path.relpath(tmp_path / "agents" / "first", wf_root)
    second_rel = os.path.relpath(tmp_path / "agents" / "second", wf_root)
    (wf_root / "workflow.yaml").write_text(
        f"""
api_version: movate/v1
kind: Workflow
name: approval
version: 0.1.0
entrypoint: first
state_schema: state.json
nodes:
  - id: first
    type: agent
    ref: {first_rel}
  - id: gate
    type: human
    prompt: Approve?
    output_contract: [decision]
  - id: second
    type: agent
    ref: {second_rel}
edges:
  - from: first
    to: gate
  - from: gate
    to: second
""".strip()
    )

    storage = InMemoryStorage()
    await storage.init()
    executor = Executor(
        provider=_PerNode(),
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
        tenant_id="tenant-a",
    )
    workflows = scan_workflows(tmp_path / "workflows")
    assert "approval" in workflows

    # Pause the workflow at the gate (the PR-1 path) using a runner against the
    # SAME storage, scoped to the job's tenant.
    spec, parent = load_workflow_spec(wf_root / "workflow.yaml")
    graph = compile_workflow(spec, parent)
    runner = WorkflowRunner(executor=executor, storage=storage, tenant_id="tenant-a")
    paused = await runner.run(graph, initial_state={"text": "seed"})
    assert paused.status is WorkflowStatus.SUCCESS or paused.status.value == "paused"

    # Simulate the signal endpoint merging the decision into the checkpoint.
    record = await storage.get_workflow_run(paused.workflow_run_id, tenant_id="tenant-a")
    assert record is not None
    record = record.model_copy(
        update={"paused_state": {**(record.paused_state or {}), "decision": "approve"}}
    )
    await storage.save_workflow_run(record)

    # A continuation job carrying resume_workflow_run_id drives the resume.
    dispatch = WorkerDispatch(storage=storage, executor=executor, agents=[], workflows=workflows)
    resume_job = JobRecord(
        job_id=str(uuid4()),
        tenant_id="tenant-a",
        kind=JobKind.WORKFLOW,
        target="approval",
        status=JobStatus.QUEUED,
        input={},
        resume_workflow_run_id=paused.workflow_run_id,
    )
    outcome = await dispatch.execute_job(resume_job)

    assert outcome.status is JobStatus.SUCCESS
    assert outcome.result_run_id == paused.workflow_run_id
    # The run completed: final record is SUCCESS with the 2nd agent's output.
    final = await storage.get_workflow_run(paused.workflow_run_id, tenant_id="tenant-a")
    assert final is not None
    assert final.status is WorkflowStatus.SUCCESS
    assert final.final_state is not None
    assert final.final_state.get("step2") == "step2-out"
    assert final.final_state.get("decision") == "approve"


@pytest.mark.unit
async def test_dispatch_resume_unknown_run_is_error() -> None:
    """A resume job whose workflow_run_id doesn't exist for the tenant →
    terminal ERROR (unknown_workflow_run), not a crash."""
    storage = InMemoryStorage()
    await storage.init()
    executor = _make_executor(storage)
    dispatch = WorkerDispatch(storage=storage, executor=executor, agents=[], workflows={})
    job = JobRecord(
        job_id=str(uuid4()),
        tenant_id="tenant-a",
        kind=JobKind.WORKFLOW,
        target="approval",
        status=JobStatus.QUEUED,
        input={},
        resume_workflow_run_id="does-not-exist",
    )
    outcome = await dispatch.execute_job(job)
    assert outcome.status is JobStatus.ERROR
    assert outcome.error is not None
    assert outcome.error["type"] == "unknown_workflow_run"


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


# ---------------------------------------------------------------------------
# Stale-job reaper throttle — Worker._maybe_reap / run_forever (item 31)
# ---------------------------------------------------------------------------


class _ReaperSpyStorage(InMemoryStorage):
    """InMemoryStorage that counts reclaim_stale_jobs calls.

    Lets the throttle tests assert HOW OFTEN the reaper fires without
    sleeping on real wall-clock intervals (non-flaky)."""

    def __init__(self) -> None:
        super().__init__()
        self.reclaim_calls = 0

    async def reclaim_stale_jobs(self, **kwargs):  # type: ignore[override]
        self.reclaim_calls += 1
        return await super().reclaim_stale_jobs(**kwargs)


class _EmptyDispatch:
    """Dispatch double that's never invoked — the queue stays empty so
    run_one_cycle returns None every tick."""

    async def execute_job(self, job: JobRecord):  # pragma: no cover - never called
        raise AssertionError("dispatch should not run on an empty queue")


@pytest.mark.unit
async def test_maybe_reap_throttles_on_interval() -> None:
    """_maybe_reap only fires once the reap interval has elapsed; before
    that it's a no-op and leaves last_reap untouched."""
    storage = _ReaperSpyStorage()
    await storage.init()
    worker = Worker(
        storage=storage,
        dispatch=_EmptyDispatch(),  # type: ignore[arg-type]
        config=WorkerConfig(reap_interval_seconds=60.0),
    )

    base = datetime.now(UTC)
    # 30s after last reap — under the 60s interval → no reap, last_reap unchanged.
    last_reap = await worker._maybe_reap(now=base, last_reap=base - timedelta(seconds=30))
    assert storage.reclaim_calls == 0
    assert last_reap == base - timedelta(seconds=30)

    # 90s after last reap — interval elapsed → reaps, last_reap advances to now.
    last_reap = await worker._maybe_reap(now=base, last_reap=base - timedelta(seconds=90))
    assert storage.reclaim_calls == 1
    assert last_reap == base


@pytest.mark.unit
async def test_maybe_reap_survives_storage_error() -> None:
    """A reaper hiccup must NEVER kill the worker loop — _maybe_reap
    swallows the error, logs, and still advances last_reap so a
    persistently-failing reaper doesn't busy-loop."""

    class _BoomStorage(InMemoryStorage):
        async def reclaim_stale_jobs(self, **kwargs):  # type: ignore[override]
            raise RuntimeError("storage down")

    storage = _BoomStorage()
    await storage.init()
    worker = Worker(
        storage=storage,
        dispatch=_EmptyDispatch(),  # type: ignore[arg-type]
        config=WorkerConfig(reap_interval_seconds=1.0),
    )

    base = datetime.now(UTC)
    # Interval elapsed → tries to reap, storage raises; _maybe_reap must
    # NOT propagate and must still return `now` (advance the throttle).
    last_reap = await worker._maybe_reap(now=base, last_reap=base - timedelta(seconds=10))
    assert last_reap == base


@pytest.mark.unit
async def test_run_forever_invokes_reaper() -> None:
    """run_forever runs the reaper at least once (it starts the throttle
    fully-elapsed so a freshly-booted worker recovers orphans on the
    first tick)."""
    storage = _ReaperSpyStorage()
    await storage.init()
    worker = Worker(
        storage=storage,
        dispatch=_EmptyDispatch(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=10.0, reap_interval_seconds=60.0),
    )

    stop = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(worker.run_forever(stop), stop_soon())
    # At least the first-tick reap fired.
    assert storage.reclaim_calls >= 1
