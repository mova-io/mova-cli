"""Tests for WorkerDispatch._execute_eval (JobKind.EVAL async path).

Requires the runtime extras (fastapi) — skipped automatically in
environments where only the core package is installed.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")  # skip whole module if runtime extras not installed

from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.core.models import JobKind, JobRecord, JobStatus
from movate.providers.pricing import load_pricing
from movate.runtime.dispatch import WorkerDispatch
from movate.testing import (
    InMemoryStorage,
    JudgeStubProvider,
    NullTracer,
    scaffold_agent,
)


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def pricing():
    return load_pricing()


def _make_job(*, target: str, **cfg) -> JobRecord:
    return JobRecord(
        job_id=str(uuid4()),
        tenant_id="test-tenant",
        kind=JobKind.EVAL,
        target=target,
        input={"mock": True, "runs": 1, "gate_mode": "mean", "gate": 0.7, **cfg},
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_execute_eval_job_persists_eval_record(
    tmp_path: Path, storage: InMemoryStorage, pricing
) -> None:
    """eval job runs mock eval, saves EvalRecord, returns SUCCESS with eval_id."""
    agent_dir = scaffold_agent(tmp_path / "demo")
    bundle = load_agent(agent_dir)

    provider = JudgeStubProvider(agent_response='{"message": "Hello!"}', judge_score=0.9)
    executor = Executor(
        provider=provider,
        pricing=pricing,
        storage=storage,
        tracer=NullTracer(),
    )
    dispatch = WorkerDispatch(
        storage=storage,
        executor=executor,
        agents=[bundle],
        use_mock_for_eval=True,  # mock provider skips LiteLLM
    )

    job = _make_job(target="demo")
    outcome = await dispatch.execute_job(job)

    assert outcome.status == JobStatus.SUCCESS
    assert outcome.result_run_id is not None  # eval_id stored here
    assert outcome.error is None

    # EvalRecord was persisted
    assert len(storage.evals) == 1
    record = storage.evals[0]
    assert record.eval_id == outcome.result_run_id
    assert record.agent == "demo"
    assert record.tenant_id == "test-tenant"


@pytest.mark.unit
async def test_execute_eval_job_unknown_agent(
    tmp_path: Path, storage: InMemoryStorage, pricing
) -> None:
    """eval job for an unregistered agent returns ERROR with unknown_agent type."""
    provider = JudgeStubProvider(agent_response='{"message": "x"}', judge_score=0.9)
    executor = Executor(
        provider=provider,
        pricing=pricing,
        storage=storage,
        tracer=NullTracer(),
    )
    dispatch = WorkerDispatch(
        storage=storage,
        executor=executor,
        agents=[],
        use_mock_for_eval=True,
    )

    job = _make_job(target="missing-agent")
    outcome = await dispatch.execute_job(job)

    assert outcome.status == JobStatus.ERROR
    assert outcome.error is not None
    assert outcome.error["type"] == "unknown_agent"
    # No eval records persisted
    assert len(storage.evals) == 0


@pytest.mark.unit
async def test_agent_jobs_still_route_with_eval_dispatch(
    tmp_path: Path, storage: InMemoryStorage, pricing
) -> None:
    """AGENT jobs still route correctly after EVAL kind was added to dispatch."""
    agent_dir = scaffold_agent(tmp_path / "demo")
    bundle = load_agent(agent_dir)
    provider = JudgeStubProvider(agent_response='{"message": "Hello!"}', judge_score=0.9)
    executor = Executor(
        provider=provider,
        pricing=pricing,
        storage=storage,
        tracer=NullTracer(),
    )
    dispatch = WorkerDispatch(
        storage=storage,
        executor=executor,
        agents=[bundle],
        use_mock_for_eval=True,
    )

    agent_job = JobRecord(
        job_id=str(uuid4()),
        tenant_id="t",
        kind=JobKind.AGENT,
        target="demo",
        input={"text": "hi"},
    )
    outcome = await dispatch.execute_job(agent_job)
    assert outcome.status == JobStatus.SUCCESS


# ---------------------------------------------------------------------------
# ADR 019 (item 32) — trace continuation in the worker. execute_job wraps the
# whole execution in continue_trace_context(job.trace_context); it must be a
# complete no-op for the empty (back-compat / pre-R2) carrier and must not
# crash for a populated carrier, with or without the OTel extra installed.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "trace_context",
    [
        {},  # default — pre-R2 row / OTel off at enqueue → fresh root
        {"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"},
    ],
)
async def test_execute_job_continues_trace_context_without_crash(
    tmp_path: Path, storage: InMemoryStorage, pricing, trace_context
) -> None:
    """The worker re-attaches the job's trace_context for execution. Both an
    empty carrier (back-compat) and a populated one execute successfully."""
    agent_dir = scaffold_agent(tmp_path / "demo")
    bundle = load_agent(agent_dir)
    provider = JudgeStubProvider(agent_response='{"message": "Hello!"}', judge_score=0.9)
    executor = Executor(
        provider=provider,
        pricing=pricing,
        storage=storage,
        tracer=NullTracer(),
    )
    dispatch = WorkerDispatch(
        storage=storage,
        executor=executor,
        agents=[bundle],
        use_mock_for_eval=True,
    )

    agent_job = JobRecord(
        job_id=str(uuid4()),
        tenant_id="t",
        kind=JobKind.AGENT,
        target="demo",
        input={"text": "hi"},
        trace_context=trace_context,
    )
    outcome = await dispatch.execute_job(agent_job)
    assert outcome.status == JobStatus.SUCCESS
