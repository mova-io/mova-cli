"""WorkerDispatch eval-drift hook (ADR 016 D2).

When a *scheduled* eval (or one with a pinned baseline) completes, the
dispatch diffs the new EvalRecord against the prior eval and fires a drift
alert on regression via the injected NotificationDispatcher. Asserted with a
fake dispatcher + the deterministic JudgeStubProvider so no LLM is called.

Also asserts the no-regression-no-alert and ad-hoc-no-baseline paths so the
hook stays default-off for existing eval/job behaviour.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")

from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.core.models import (
    CanaryConfig,
    EvalRecord,
    JobKind,
    JobRecord,
    JobStatus,
    JudgeMethod,
)
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


# ---------------------------------------------------------------------------
# ADR 016 D5 — opt-in auto-rollback on drift. The scaffolded agent's eval
# records agent_version "0.1.0" (the template's version), so a canary whose
# challenger is "0.1.0" makes the scheduled eval a regression *on the
# challenger* — the auto-rollback trigger.
# ---------------------------------------------------------------------------

_CHALLENGER_VERSION = "0.1.0"  # the scaffolded agent's version → its eval slice


def _canary(
    *,
    agent: str = "demo",
    tenant_id: str = "test-tenant",
    auto_rollback: bool,
    weight: int = 25,
    challenger: str = _CHALLENGER_VERSION,
) -> CanaryConfig:
    return CanaryConfig(
        tenant_id=tenant_id,
        agent=agent,
        challenger_version=challenger,
        champion_version="champ-v0",
        weight=weight,
        auto_rollback=auto_rollback,
    )


@pytest.mark.unit
async def test_regression_on_challenger_with_auto_rollback_trips_kill_switch(
    tmp_path: Path, storage: InMemoryStorage
) -> None:
    """auto_rollback ON + regression on the challenger version → weight → 0."""
    agent_dir = scaffold_agent(tmp_path / "demo")
    bundle = load_agent(agent_dir)
    await storage.save_eval(_prior_eval(agent="demo", tenant_id="test-tenant", mean_score=1.0))
    await storage.save_canary_config(_canary(auto_rollback=True, weight=25))

    dispatcher = _FakeDispatcher()
    dispatch = _build_dispatch(storage, bundle, dispatcher)

    outcome = await dispatch.execute_job(_scheduled_eval_job(target="demo"))
    assert outcome.status == JobStatus.SUCCESS

    # The canary was auto-rolled back to the kill switch.
    rolled = await storage.get_canary_config("demo", tenant_id="test-tenant")
    assert rolled is not None
    assert rolled.weight == 0  # kill switch
    assert rolled.challenger_version == _CHALLENGER_VERSION  # pin preserved
    assert rolled.champion_version == "champ-v0"  # pin preserved

    # Operators got BOTH the drift alert and the auto-rollback alert.
    subjects = [a["subject"] for a in dispatcher.alerts]
    assert any("regressed" in (s or "") for s in subjects)
    assert any("auto-rollback" in (s or "") for s in subjects)


@pytest.mark.unit
async def test_regression_with_auto_rollback_off_only_alerts(
    tmp_path: Path, storage: InMemoryStorage
) -> None:
    """The regression guard: auto_rollback OFF → alert-only, canary UNCHANGED.

    Byte-for-byte today's behavior — the drift alert fires, but the canary
    weight is left exactly as set (ADR 016 D5 default = alert-only).
    """
    agent_dir = scaffold_agent(tmp_path / "demo")
    bundle = load_agent(agent_dir)
    await storage.save_eval(_prior_eval(agent="demo", tenant_id="test-tenant", mean_score=1.0))
    await storage.save_canary_config(_canary(auto_rollback=False, weight=25))

    dispatcher = _FakeDispatcher()
    dispatch = _build_dispatch(storage, bundle, dispatcher)

    outcome = await dispatch.execute_job(_scheduled_eval_job(target="demo"))
    assert outcome.status == JobStatus.SUCCESS

    # Canary is UNTOUCHED — no rollback.
    unchanged = await storage.get_canary_config("demo", tenant_id="test-tenant")
    assert unchanged is not None
    assert unchanged.weight == 25

    # Exactly the drift alert (no auto-rollback alert).
    assert len(dispatcher.alerts) == 1
    assert "regressed" in (dispatcher.alerts[0]["subject"] or "")
    assert all("auto-rollback" not in (a["subject"] or "") for a in dispatcher.alerts)


@pytest.mark.unit
async def test_regression_on_non_challenger_version_no_rollback(
    tmp_path: Path, storage: InMemoryStorage
) -> None:
    """A regression whose version isn't the challenger → no rollback."""
    agent_dir = scaffold_agent(tmp_path / "demo")
    bundle = load_agent(agent_dir)
    await storage.save_eval(_prior_eval(agent="demo", tenant_id="test-tenant", mean_score=1.0))
    # Challenger is a DIFFERENT version than the one the eval runs (0.1.0).
    await storage.save_canary_config(_canary(auto_rollback=True, weight=25, challenger="9.9.9"))

    dispatcher = _FakeDispatcher()
    dispatch = _build_dispatch(storage, bundle, dispatcher)

    outcome = await dispatch.execute_job(_scheduled_eval_job(target="demo"))
    assert outcome.status == JobStatus.SUCCESS

    unchanged = await storage.get_canary_config("demo", tenant_id="test-tenant")
    assert unchanged is not None
    assert unchanged.weight == 25  # untouched
    assert all("auto-rollback" not in (a["subject"] or "") for a in dispatcher.alerts)


@pytest.mark.unit
async def test_regression_no_canary_config_no_rollback(
    tmp_path: Path, storage: InMemoryStorage
) -> None:
    """A regression with no canary at all → alert only, nothing to roll back."""
    agent_dir = scaffold_agent(tmp_path / "demo")
    bundle = load_agent(agent_dir)
    await storage.save_eval(_prior_eval(agent="demo", tenant_id="test-tenant", mean_score=1.0))

    dispatcher = _FakeDispatcher()
    dispatch = _build_dispatch(storage, bundle, dispatcher)

    outcome = await dispatch.execute_job(_scheduled_eval_job(target="demo"))
    assert outcome.status == JobStatus.SUCCESS

    assert await storage.get_canary_config("demo", tenant_id="test-tenant") is None
    assert all("auto-rollback" not in (a["subject"] or "") for a in dispatcher.alerts)


# ---------------------------------------------------------------------------
# Item 40 — control-plane audit telemetry for the drift auto-rollback path.
# An auto-rollback is a server-initiated canary traffic change; it joins the
# same "who did what" trail as the human-driven canary.promote/rollback
# endpoints (item 35), with a synthetic ``system:drift`` actor. We assert via
# caplog on the ``movate.audit`` logger (the repo's caplog-not-capsys
# convention — movate emits through stdlib logging; see tests/conftest.py).
# ---------------------------------------------------------------------------

_AUDIT_LOGGER = "movate.audit"


def _audit_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _AUDIT_LOGGER]


@pytest.mark.unit
async def test_auto_rollback_emits_audit_event(
    tmp_path: Path, storage: InMemoryStorage, caplog: pytest.LogCaptureFixture
) -> None:
    """When an auto-rollback actually fires, a ``canary.auto_rollback`` audit
    event lands on the ``movate.audit`` trail with the system actor + tenant."""
    agent_dir = scaffold_agent(tmp_path / "demo")
    bundle = load_agent(agent_dir)
    await storage.save_eval(_prior_eval(agent="demo", tenant_id="test-tenant", mean_score=1.0))
    await storage.save_canary_config(_canary(auto_rollback=True, weight=25))

    dispatcher = _FakeDispatcher()
    dispatch = _build_dispatch(storage, bundle, dispatcher)

    with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER):
        outcome = await dispatch.execute_job(_scheduled_eval_job(target="demo"))
    assert outcome.status == JobStatus.SUCCESS

    recs = _audit_records(caplog)
    assert len(recs) == 1
    audit = recs[0].audit  # type: ignore[attr-defined]
    assert audit["action"] == "canary.auto_rollback"
    assert audit["actor"] == "system:drift"  # server-initiated, no human actor
    assert audit["tenant_id"] == "test-tenant"
    # target mirrors the canary.promote/rollback ``agent@version`` convention.
    assert audit["target"] == "demo@champ-v0"
    assert audit["challenger_version"] == _CHALLENGER_VERSION
    assert audit["champion_version"] == "champ-v0"
    assert audit["reason"] == "drift_regression"


@pytest.mark.unit
async def test_no_auto_rollback_audit_event_when_disabled(
    tmp_path: Path, storage: InMemoryStorage, caplog: pytest.LogCaptureFixture
) -> None:
    """auto_rollback OFF → alert-only, so NO ``canary.auto_rollback`` audit
    event. The guard must fire the audit trail only on a real rollback."""
    agent_dir = scaffold_agent(tmp_path / "demo")
    bundle = load_agent(agent_dir)
    await storage.save_eval(_prior_eval(agent="demo", tenant_id="test-tenant", mean_score=1.0))
    await storage.save_canary_config(_canary(auto_rollback=False, weight=25))

    dispatcher = _FakeDispatcher()
    dispatch = _build_dispatch(storage, bundle, dispatcher)

    with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER):
        outcome = await dispatch.execute_job(_scheduled_eval_job(target="demo"))
    assert outcome.status == JobStatus.SUCCESS

    # Regression detected (alert fired) but no rollback → no audit event.
    assert _audit_records(caplog) == []
