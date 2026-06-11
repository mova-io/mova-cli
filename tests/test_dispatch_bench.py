"""Tests for WorkerDispatch._execute_bench (JobKind.BENCH async path).

BACKLOG #64. Mirrors ``tests/test_dispatch_eval.py``: a mock bench job
runs through the dispatch, persists a ``BenchRecord``, and returns
SUCCESS with the produced ``bench_id`` in ``result_run_id``.

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
        kind=JobKind.BENCH,
        target=target,
        input={
            "models": ["openai/gpt-4o-mini-2024-07-18"],
            "input": {"text": "hi"},
            "mock": True,
            "runs": 1,
            "gate_mode": "mean",
            **cfg,
        },
    )


def _dispatch(storage: InMemoryStorage, pricing, *, agents) -> WorkerDispatch:
    provider = JudgeStubProvider(agent_response='{"message": "Hello!"}', judge_score=0.9)
    executor = Executor(
        provider=provider,
        pricing=pricing,
        storage=storage,
        tracer=NullTracer(),
    )
    return WorkerDispatch(
        storage=storage,
        executor=executor,
        agents=agents,
        use_mock_for_eval=True,  # forces mock for eval + bench jobs
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_execute_bench_job_persists_bench_record(
    tmp_path: Path, storage: InMemoryStorage, pricing
) -> None:
    """bench job runs a mock bench, saves a BenchRecord, returns SUCCESS."""
    agent_dir = scaffold_agent(tmp_path / "demo")
    bundle = load_agent(agent_dir)
    dispatch = _dispatch(storage, pricing, agents=[bundle])

    job = _make_job(target="demo")
    outcome = await dispatch.execute_job(job)

    assert outcome.status == JobStatus.SUCCESS
    assert outcome.result_run_id is not None  # bench_id stored here
    assert outcome.error is None

    assert len(storage.bench) == 1
    record = storage.bench[0]
    assert record.bench_id == outcome.result_run_id
    assert record.agent == "demo"
    assert record.tenant_id == "test-tenant"
    assert [m.provider for m in record.models] == ["openai/gpt-4o-mini-2024-07-18"]


@pytest.mark.unit
async def test_execute_bench_honors_pregenerated_bench_id(
    tmp_path: Path, storage: InMemoryStorage, pricing
) -> None:
    """The API pre-generates bench_id and passes it through job.input;
    the persisted record + result_run_id must use that exact id."""
    agent_dir = scaffold_agent(tmp_path / "demo")
    bundle = load_agent(agent_dir)
    dispatch = _dispatch(storage, pricing, agents=[bundle])

    pregenerated = uuid4().hex
    job = _make_job(target="demo", bench_id=pregenerated)
    outcome = await dispatch.execute_job(job)

    assert outcome.result_run_id == pregenerated
    assert storage.bench[0].bench_id == pregenerated


@pytest.mark.unit
async def test_execute_bench_job_unknown_agent(
    tmp_path: Path, storage: InMemoryStorage, pricing
) -> None:
    """bench job for an unregistered agent returns ERROR + persists nothing."""
    dispatch = _dispatch(storage, pricing, agents=[])

    job = _make_job(target="missing-agent")
    outcome = await dispatch.execute_job(job)

    assert outcome.status == JobStatus.ERROR
    assert outcome.error is not None
    assert outcome.error["type"] == "unknown_agent"
    assert len(storage.bench) == 0


@pytest.mark.unit
async def test_execute_bench_job_no_models(
    tmp_path: Path, storage: InMemoryStorage, pricing
) -> None:
    """An empty models list is a config error — no record persisted."""
    agent_dir = scaffold_agent(tmp_path / "demo")
    bundle = load_agent(agent_dir)
    dispatch = _dispatch(storage, pricing, agents=[bundle])

    job = _make_job(target="demo", models=[])
    outcome = await dispatch.execute_job(job)

    assert outcome.status == JobStatus.ERROR
    assert outcome.error is not None
    assert outcome.error["type"] == "bench_config"
    assert len(storage.bench) == 0


# ---------------------------------------------------------------------------
# Governance threading (task #45): the bench sub-executor inherits the worker
# executor's policies — same regression coverage as test_dispatch_eval.py.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_bench_job_threads_policy_so_gates_evaluate(
    tmp_path: Path, storage: InMemoryStorage, pricing
) -> None:
    """``_execute_bench`` previously built its sub-executor without the main
    path's policies (the task #45 gap) — bench runs were ungoverned. With a
    non-permissive ModelPolicy on the worker executor, the benched run must
    record a governance effect into the ambient scope."""
    from movate.core.config import ModelPolicy  # noqa: PLC0415
    from movate.governance.effects import governance_effect_scope  # noqa: PLC0415

    agent_dir = scaffold_agent(tmp_path / "demo")
    bundle = load_agent(agent_dir)
    provider = JudgeStubProvider(agent_response='{"message": "Hello!"}', judge_score=0.9)
    executor = Executor(
        provider=provider,
        pricing=pricing,
        storage=storage,
        tracer=NullTracer(),
        policy=ModelPolicy(allowed_providers=["openai"]),
    )
    dispatch = WorkerDispatch(
        storage=storage,
        executor=executor,
        agents=[bundle],
        use_mock_for_eval=True,
    )

    with governance_effect_scope() as scope:
        outcome = await dispatch.execute_job(_make_job(target="demo"))

    assert outcome.status == JobStatus.SUCCESS
    assert scope.effect == "allow"  # gates evaluated on the bench sub-executor
