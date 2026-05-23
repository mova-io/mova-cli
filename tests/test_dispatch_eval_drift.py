"""WorkerDispatch eval-drift hook (ADR 016 D2).

When a *scheduled* eval (or one with a pinned baseline) completes, the
dispatch diffs the new EvalRecord against the prior eval and fires a drift
alert on regression via the injected NotificationDispatcher. Asserted with a
fake dispatcher + the deterministic JudgeStubProvider so no LLM is called.

Also asserts the no-regression-no-alert and ad-hoc-no-baseline paths so the
hook stays default-off for existing eval/job behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")

from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.core.models import EvalRecord, JobKind, JobRecord, JobStatus, JudgeMethod
from movate.providers.pricing import load_pricing
from movate.runtime.dispatch import WorkerDispatch
from movate.testing import (
    InMemoryStorage,
    JudgeStubProvider,
    NullTracer,
    scaffold_agent,
)


class _FakeDispatcher:
    name = "fake"

    def __init__(self) -> None:
        self.alerts: list[dict[str, str | None]] = []

    async def notify_terminal(self, job) -> None:  # type: ignore[no-untyped-def]
        pass

    async def notify_alert(self, *, subject: str, body: str, email: str | None) -> None:
        self.alerts.append({"subject": subject, "body": body, "email": email})


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


def _prior_eval(
    *, agent: str, tenant_id: str, mean_score: float, pass_rate: float = 1.0
) -> EvalRecord:
    return EvalRecord(
        eval_id="prior-baseline",
        tenant_id=tenant_id,
        agent=agent,
        agent_version="0.1.0",
        dataset_hash="h",
        judge_method=JudgeMethod.EXACT,
        judge_provider=None,
        runs_per_case=1,
        gate_mode="mean",
        threshold=0.7,
        mean_score=mean_score,
        pass_rate=pass_rate,
        sample_count=1,
        total_cost_usd=0.0,
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )


def _scheduled_eval_job(*, target: str, tenant_id: str = "test-tenant", **cfg) -> JobRecord:
    return JobRecord(
        job_id=str(uuid4()),
        tenant_id=tenant_id,
        kind=JobKind.EVAL,
        target=target,
        input={
            "mock": True,
            "runs": 1,
            "gate_mode": "mean",
            "gate": 0.7,
            "scheduled": True,
            "regression_tolerance": 0.05,
            "notify_email": "ops@example.com",
            **cfg,
        },
        notify_email="ops@example.com",
    )


def _build_dispatch(storage, agent_bundle, dispatcher) -> WorkerDispatch:
    executor = Executor(
        provider=JudgeStubProvider(agent_response='{"message": "Hello!"}', judge_score=0.9),
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
    )
    return WorkerDispatch(
        storage=storage,
        executor=executor,
        agents=[agent_bundle],
        use_mock_for_eval=True,
        notifier=dispatcher,
    )


@pytest.mark.unit
async def test_scheduled_eval_regression_fires_alert(
    tmp_path: Path, storage: InMemoryStorage
) -> None:
    """A scheduled eval that drops below a high baseline fires a drift alert."""
    agent_dir = scaffold_agent(tmp_path / "demo")
    bundle = load_agent(agent_dir)
    # Seed a high baseline so the mock eval (typically a low exact-match
    # score) regresses past tolerance.
    await storage.save_eval(_prior_eval(agent="demo", tenant_id="test-tenant", mean_score=1.0))

    dispatcher = _FakeDispatcher()
    dispatch = _build_dispatch(storage, bundle, dispatcher)

    job = _scheduled_eval_job(target="demo")
    outcome = await dispatch.execute_job(job)

    assert outcome.status == JobStatus.SUCCESS
    # New EvalRecord persisted alongside the seeded baseline.
    assert len(storage.evals) == 2
    # Regression vs the 1.0 baseline → exactly one alert, to the schedule's email.
    assert len(dispatcher.alerts) == 1
    assert dispatcher.alerts[0]["email"] == "ops@example.com"
    assert "demo" in dispatcher.alerts[0]["subject"]


@pytest.mark.unit
async def test_scheduled_eval_no_baseline_no_alert(
    tmp_path: Path, storage: InMemoryStorage
) -> None:
    """First-ever scheduled eval (no prior) records but doesn't alert."""
    agent_dir = scaffold_agent(tmp_path / "demo")
    bundle = load_agent(agent_dir)

    dispatcher = _FakeDispatcher()
    dispatch = _build_dispatch(storage, bundle, dispatcher)

    outcome = await dispatch.execute_job(_scheduled_eval_job(target="demo"))
    assert outcome.status == JobStatus.SUCCESS
    assert len(storage.evals) == 1
    assert dispatcher.alerts == []


@pytest.mark.unit
async def test_scheduled_eval_within_tolerance_no_alert(
    tmp_path: Path, storage: InMemoryStorage
) -> None:
    """A baseline at the same low scores as the run → no regression, no alert."""
    agent_dir = scaffold_agent(tmp_path / "demo")
    bundle = load_agent(agent_dir)
    # Baseline at 0.0 on both metrics — the mock exact-match run produces the
    # same floor, so neither mean_score nor pass_rate regresses past tolerance.
    await storage.save_eval(
        _prior_eval(agent="demo", tenant_id="test-tenant", mean_score=0.0, pass_rate=0.0)
    )

    dispatcher = _FakeDispatcher()
    dispatch = _build_dispatch(storage, bundle, dispatcher)

    outcome = await dispatch.execute_job(_scheduled_eval_job(target="demo"))
    assert outcome.status == JobStatus.SUCCESS
    assert dispatcher.alerts == []


@pytest.mark.unit
async def test_adhoc_eval_no_baseline_intent_skips_drift(
    tmp_path: Path, storage: InMemoryStorage
) -> None:
    """A plain (non-scheduled, no baseline_id) eval never runs the drift check.

    Even with a high prior eval present, an ad-hoc job carries no baseline
    intent → byte-for-byte the old behaviour, no alert.
    """
    agent_dir = scaffold_agent(tmp_path / "demo")
    bundle = load_agent(agent_dir)
    await storage.save_eval(_prior_eval(agent="demo", tenant_id="test-tenant", mean_score=1.0))

    dispatcher = _FakeDispatcher()
    dispatch = _build_dispatch(storage, bundle, dispatcher)

    # No "scheduled" marker, no baseline_id.
    job = JobRecord(
        job_id=str(uuid4()),
        tenant_id="test-tenant",
        kind=JobKind.EVAL,
        target="demo",
        input={"mock": True, "runs": 1, "gate_mode": "mean", "gate": 0.7},
    )
    outcome = await dispatch.execute_job(job)
    assert outcome.status == JobStatus.SUCCESS
    assert dispatcher.alerts == []
