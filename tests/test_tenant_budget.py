"""Per-tenant monthly cost ceiling — storage + executor + CLI.

Three layers, each tested in isolation:

1. **Storage round-trip** — ``get_tenant_budget`` /
   ``upsert_tenant_budget`` / ``list_tenant_budgets`` /
   ``sum_tenant_cost_current_month``. Parametrized over memory +
   sqlite (+ postgres when configured).
2. **Executor enforcement** — ``_check_tenant_budget`` blocks at
   execute() entry when current-month spend ≥ budget; no provider
   call fires; failure persisted with ``tenant_budget_exceeded``
   type. Unlimited / no-row case lets the run proceed.
3. **CLI** — ``movate tenants set-budget | clear-budget | show |
   list`` against tmp sqlite storage.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from movate.cli.main import app as cli_app
from movate.core.executor import Executor
from movate.core.failures import FailureType
from movate.core.loader import load_agent
from movate.core.models import (
    JobStatus,
    Metrics,
    RunRecord,
    RunRequest,
    TenantBudget,
    TokenUsage,
)
from movate.providers.mock import MockProvider
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import InMemoryStorage, NullTracer, scaffold_agent

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _run_for_tenant(*, tenant_id: str, cost: float, created_at: datetime) -> RunRecord:
    """Build a RunRecord with the exact tenant + cost + timestamp we
    want for the sum-of-cost math tests. Skips the executor — these
    are pure storage tests."""
    return RunRecord(
        run_id=uuid4().hex,
        job_id=uuid4().hex,
        tenant_id=tenant_id,
        agent="x-agent",
        agent_version="0.1.0",
        prompt_hash="a" * 64,
        provider="mock/p",
        provider_version="0.0.1",
        pricing_version="2025-01",
        status=JobStatus.SUCCESS,
        input={"text": "hi"},
        metrics=Metrics(latency_ms=1, cost_usd=cost, tokens=TokenUsage()),
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# 1. Storage round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_get_tenant_budget_returns_none_when_unset(storage) -> None:
    """A tenant with no budget row → unlimited by default. None signals
    'no policy configured' (the v0.x-compatible state)."""
    assert await storage.get_tenant_budget("never-set") is None


@pytest.mark.unit
async def test_upsert_then_get_round_trip(storage) -> None:
    budget = TenantBudget(tenant_id="t1", monthly_usd_limit=500.0)
    await storage.upsert_tenant_budget(budget)
    loaded = await storage.get_tenant_budget("t1")
    assert loaded is not None
    assert loaded.tenant_id == "t1"
    assert loaded.monthly_usd_limit == 500.0


@pytest.mark.unit
async def test_upsert_preserves_created_at_updates_updated_at(storage) -> None:
    """Second upsert keeps the original created_at + bumps updated_at —
    operators see 'first set' and 'last touched' separately."""
    original = TenantBudget(
        tenant_id="t1",
        monthly_usd_limit=100.0,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    await storage.upsert_tenant_budget(original)

    # Bump the limit; storage should preserve created_at but refresh updated_at.
    bumped = TenantBudget(
        tenant_id="t1",
        monthly_usd_limit=1000.0,
        created_at=datetime(2030, 6, 6, tzinfo=UTC),  # storage ignores this on update
        updated_at=datetime(2030, 6, 6, tzinfo=UTC),
    )
    await storage.upsert_tenant_budget(bumped)
    loaded = await storage.get_tenant_budget("t1")
    assert loaded is not None
    assert loaded.monthly_usd_limit == 1000.0
    # The fact that storage refreshes updated_at server-side means the
    # value we see is a recent NOW(), not the 2030 value we passed.
    assert loaded.updated_at > loaded.created_at


@pytest.mark.unit
async def test_clear_budget_via_upsert_with_none_limit(storage) -> None:
    """Setting monthly_usd_limit=None acts as 'clear' — row stays for
    audit history; cap becomes effectively unlimited."""
    await storage.upsert_tenant_budget(TenantBudget(tenant_id="t1", monthly_usd_limit=200.0))
    await storage.upsert_tenant_budget(TenantBudget(tenant_id="t1", monthly_usd_limit=None))
    loaded = await storage.get_tenant_budget("t1")
    assert loaded is not None
    assert loaded.monthly_usd_limit is None


@pytest.mark.unit
async def test_list_tenant_budgets_returns_oldest_first(storage) -> None:
    """``list_tenant_budgets`` orders by created_at ASC so the operator
    sees the longest-lived budgets first."""
    a = TenantBudget(
        tenant_id="alpha",
        monthly_usd_limit=100.0,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    b = TenantBudget(
        tenant_id="beta",
        monthly_usd_limit=200.0,
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
        updated_at=datetime(2026, 3, 1, tzinfo=UTC),
    )
    await storage.upsert_tenant_budget(a)
    await storage.upsert_tenant_budget(b)
    rows = await storage.list_tenant_budgets()
    assert [r.tenant_id for r in rows] == ["alpha", "beta"]


@pytest.mark.unit
async def test_sum_cost_current_month_zero_with_no_runs(storage) -> None:
    """No runs → 0.0 (not None, not error). Default path for fresh tenants."""
    assert await storage.sum_tenant_cost_current_month("nobody") == 0.0


@pytest.mark.unit
async def test_sum_cost_current_month_aggregates_only_this_month(storage) -> None:
    """Cost from this month counts; cost from prior months does NOT.
    The whole point of a *monthly* budget is that it resets."""
    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month = month_start - timedelta(days=15)

    # Two runs this month for tenant alpha; one last month (must not count).
    this_month_1 = _run_for_tenant(
        tenant_id="alpha", cost=0.50, created_at=month_start + timedelta(days=1)
    )
    this_month_2 = _run_for_tenant(
        tenant_id="alpha", cost=0.25, created_at=month_start + timedelta(days=2)
    )
    prior_month = _run_for_tenant(tenant_id="alpha", cost=99.99, created_at=last_month)
    # Different tenant in current month — must not count for alpha.
    other_tenant = _run_for_tenant(
        tenant_id="beta", cost=10.0, created_at=month_start + timedelta(days=1)
    )

    for r in (this_month_1, this_month_2, prior_month, other_tenant):
        await storage.save_run(r)

    total = await storage.sum_tenant_cost_current_month("alpha")
    assert total == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# 2. Executor enforcement
# ---------------------------------------------------------------------------


@pytest.fixture
def pricing() -> PricingTable:
    return load_pricing()


@pytest.fixture
async def storage_with_runs() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def tracer() -> NullTracer:
    return NullTracer()


@pytest.mark.unit
async def test_executor_allows_run_when_no_budget_row(
    tmp_path: Path,
    pricing: PricingTable,
    storage_with_runs: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """No tenant_budgets row = unlimited (v0.x-compat). The executor
    runs normally; no errors, no special-case logging."""
    bundle = load_agent(scaffold_agent(tmp_path / "demo", name="demo"))
    executor = Executor(
        provider=MockProvider(response='{"message": "ok"}'),
        pricing=pricing,
        storage=storage_with_runs,
        tracer=tracer,
        tenant_id="t1",
    )
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "success"


@pytest.mark.unit
async def test_executor_allows_run_when_budget_is_none(
    tmp_path: Path,
    pricing: PricingTable,
    storage_with_runs: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """Explicit row with monthly_usd_limit=None is the 'cleared' case —
    cap is unlimited; runs proceed."""
    await storage_with_runs.upsert_tenant_budget(
        TenantBudget(tenant_id="t1", monthly_usd_limit=None)
    )
    bundle = load_agent(scaffold_agent(tmp_path / "demo", name="demo"))
    executor = Executor(
        provider=MockProvider(response='{"message": "ok"}'),
        pricing=pricing,
        storage=storage_with_runs,
        tracer=tracer,
        tenant_id="t1",
    )
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "success"


@pytest.mark.unit
async def test_executor_aborts_when_spend_meets_or_exceeds_budget(
    tmp_path: Path,
    pricing: PricingTable,
    storage_with_runs: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """Tenant spent $1.00 this month with a $1.00 cap → next run
    aborts BEFORE any provider call. Failure persisted with
    ``tenant_budget_exceeded`` type."""
    # Pre-populate runs totaling $1.00 this month.
    now = datetime.now(UTC)
    for _ in range(4):
        await storage_with_runs.save_run(_run_for_tenant(tenant_id="t1", cost=0.25, created_at=now))
    await storage_with_runs.upsert_tenant_budget(
        TenantBudget(tenant_id="t1", monthly_usd_limit=1.00)
    )

    class ShouldNeverBeCalled(MockProvider):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def complete(self, request):  # type: ignore[override]
            self.calls += 1
            return await super().complete(request)

    provider = ShouldNeverBeCalled()
    bundle = load_agent(scaffold_agent(tmp_path / "demo", name="demo"))
    executor = Executor(
        provider=provider,
        pricing=pricing,
        storage=storage_with_runs,
        tracer=tracer,
        tenant_id="t1",
    )
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))

    assert response.status == "error"
    assert response.error is not None
    assert response.error.type == FailureType.TENANT_BUDGET_EXCEEDED.value
    # No provider call fired — budget check short-circuits.
    assert provider.calls == 0
    # Failure persisted to the failures table for audit.
    assert len(storage_with_runs.failures) == 1
    assert storage_with_runs.failures[0].failure_type == FailureType.TENANT_BUDGET_EXCEEDED.value


@pytest.mark.unit
async def test_executor_includes_helpful_operator_pointer(
    tmp_path: Path,
    pricing: PricingTable,
    storage_with_runs: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """The error message must include the CLI command to fix it. Self-
    fixing error messages are the v1.0 stage 3 + 4 pattern."""
    await storage_with_runs.save_run(
        _run_for_tenant(tenant_id="t1", cost=5.0, created_at=datetime.now(UTC))
    )
    await storage_with_runs.upsert_tenant_budget(
        TenantBudget(tenant_id="t1", monthly_usd_limit=1.0)
    )
    bundle = load_agent(scaffold_agent(tmp_path / "demo", name="demo"))
    executor = Executor(
        provider=MockProvider(),
        pricing=pricing,
        storage=storage_with_runs,
        tracer=tracer,
        tenant_id="t1",
    )
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.error is not None
    assert "movate tenants set-budget" in response.error.message
    # Also surfaces the actual numbers so the operator sees "how much over".
    assert "$5.00" in response.error.message
    assert "$1.00" in response.error.message


@pytest.mark.unit
async def test_executor_proceeds_when_spend_is_under_budget(
    tmp_path: Path,
    pricing: PricingTable,
    storage_with_runs: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """Spent $0.50 of a $1.00 cap → run proceeds normally. The check
    is ``>=``, not ``>``: exactly-at-budget DOES block."""
    await storage_with_runs.save_run(
        _run_for_tenant(tenant_id="t1", cost=0.50, created_at=datetime.now(UTC))
    )
    await storage_with_runs.upsert_tenant_budget(
        TenantBudget(tenant_id="t1", monthly_usd_limit=1.00)
    )
    bundle = load_agent(scaffold_agent(tmp_path / "demo", name="demo"))
    executor = Executor(
        provider=MockProvider(response='{"message": "ok"}'),
        pricing=pricing,
        storage=storage_with_runs,
        tracer=tracer,
        tenant_id="t1",
    )
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "success"


# ---------------------------------------------------------------------------
# 3. CLI integration — `movate tenants set-budget|show|list|clear-budget`
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_db(tmp_path: Path, monkeypatch) -> Path:
    """Point `movate` at a per-test sqlite file via ``MOVATE_DB``."""
    db = tmp_path / "movate.db"
    monkeypatch.setenv("MOVATE_DB", str(db))
    return db


@pytest.mark.unit
def test_cli_set_budget_persists_to_storage(cli_db) -> None:
    """``movate tenants set-budget`` writes the row + the next ``show``
    reads it back."""
    tid = uuid4().hex
    r = runner.invoke(cli_app, ["tenants", "set-budget", tid, "--monthly-usd", "500"])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert "$500.00/month" in r.stderr

    r = runner.invoke(cli_app, ["tenants", "show", tid])
    assert r.exit_code == 0
    assert "$500.00" in r.stdout
    assert "$0.0000" in r.stdout  # no spend yet


@pytest.mark.unit
def test_cli_clear_budget_makes_tenant_unlimited(cli_db) -> None:
    """``clear-budget`` flips the cap to None; ``show`` reports unlimited."""
    tid = uuid4().hex
    runner.invoke(cli_app, ["tenants", "set-budget", tid, "--monthly-usd", "100"])
    r = runner.invoke(cli_app, ["tenants", "clear-budget", tid])
    assert r.exit_code == 0
    assert "unlimited" in r.stderr

    r = runner.invoke(cli_app, ["tenants", "show", tid])
    assert "unlimited" in r.stdout


@pytest.mark.unit
def test_cli_show_for_unknown_tenant_reports_no_row(cli_db) -> None:
    """A tenant with no budget row is the default (v0.x-compat). Show
    surfaces ``unlimited (no row)`` so the operator knows it's not a
    configured exemption — it's just unset."""
    r = runner.invoke(cli_app, ["tenants", "show", "never-configured"])
    assert r.exit_code == 0
    assert "no row" in r.stdout


@pytest.mark.unit
def test_cli_list_shows_every_configured_budget(cli_db) -> None:
    """``movate tenants list`` enumerates every row with the
    color-coded status (ok / warning / paused)."""
    a, b, c = uuid4().hex, uuid4().hex, uuid4().hex
    runner.invoke(cli_app, ["tenants", "set-budget", a, "--monthly-usd", "100"])
    runner.invoke(cli_app, ["tenants", "set-budget", b, "--monthly-usd", "50"])
    runner.invoke(cli_app, ["tenants", "set-budget", c, "--monthly-usd", "0"])  # paused

    r = runner.invoke(cli_app, ["tenants", "list"])
    assert r.exit_code == 0
    # All three tenants appear (we use the first 8 chars since Rich
    # tables may truncate long strings depending on terminal width).
    assert a[:8] in r.stdout
    assert b[:8] in r.stdout
    assert c[:8] in r.stdout
    # The zero-cap one is marked paused.
    assert "paused" in r.stdout


@pytest.mark.unit
def test_cli_set_budget_rejects_negative(cli_db) -> None:
    tid = uuid4().hex
    r = runner.invoke(cli_app, ["tenants", "set-budget", tid, "--monthly-usd", "-10"])
    assert r.exit_code == 2
    assert "must be >= 0" in r.stderr
