"""Unit tests for the run estimator (Cost Prediction).

``estimate_run`` predicts cost + latency for an agent run WITHOUT executing
it. These tests pin the honest-estimate contract:

* tokens_in reflects the ASSEMBLED prompt (longer prompt → higher estimate).
* tokens_out_expected uses the historical mean when history exists, else
  falls back to the agent's ``max_tokens``.
* cost bands (min/expected/max) are the pricing-table math over the token
  bands.
* latency is the historical p50/p95 when runs exist, ``unavailable`` otherwise.
* budget_check compares cost_usd_max against the agent's per-run budget.
* the estimator NEVER calls the LLM (a spy provider proves it).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from movate.core.loader import load_agent
from movate.core.models import (
    JobStatus,
    Metrics,
    RunRecord,
    TokenUsage,
)
from movate.core.run_estimator import estimate_run
from movate.testing import scaffold_agent
from movate.testing.doubles import InMemoryStorage

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


def _run_record(
    *,
    agent: str,
    tenant_id: str,
    output_tokens: int,
    latency_ms: int,
    status: JobStatus = JobStatus.SUCCESS,
    n: int = 0,
) -> RunRecord:
    """A minimal successful RunRecord carrying the metrics the estimator reads."""
    return RunRecord(
        run_id=f"run-{agent}-{n}",
        job_id=f"job-{agent}-{n}",
        tenant_id=tenant_id,
        agent=agent,
        agent_version="0.1.0",
        prompt_hash="hash",
        provider="openai/gpt-4o-mini-2024-07-18",
        provider_version="",
        pricing_version="test",
        status=status,
        input={"text": "x"},
        output={"message": "y"},
        metrics=Metrics(
            latency_ms=latency_ms,
            tokens=TokenUsage(input=100, output=output_tokens),
            cost_usd=0.001,
        ),
        created_at=datetime.now(UTC) - timedelta(minutes=n),
    )


# ---------------------------------------------------------------------------
# tokens_in reflects the assembled prompt
# ---------------------------------------------------------------------------


async def test_tokens_in_reflects_assembled_prompt(tmp_path: Path) -> None:
    """A longer input → a longer assembled prompt → a higher tokens_in.

    The estimator renders the prompt via the SAME ``bundle.render_prompt``
    path the executor uses, so the input text is interpolated into the
    system prompt and counted."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    bundle = load_agent(agent_dir)
    storage = await _storage()

    short = await estimate_run(bundle, {"text": "hi"}, storage=storage)
    long = await estimate_run(
        bundle, {"text": "word " * 500}, storage=storage
    )

    assert long.predicted.tokens_in > short.predicted.tokens_in
    assert short.predicted.tokens_in > 0
    assert short.basis.prompt_tokens_method.startswith("assembled")


# ---------------------------------------------------------------------------
# tokens_out_expected: historical mean vs max_tokens fallback
# ---------------------------------------------------------------------------


async def test_out_expected_falls_back_to_max_tokens_without_history(tmp_path: Path) -> None:
    """No history → tokens_out_expected == max_tokens, method=max_tokens_fallback."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    bundle = load_agent(agent_dir)
    storage = await _storage()

    est = await estimate_run(bundle, {"text": "hi"}, storage=storage, tenant_id="local")

    assert est.basis.out_expected_method == "max_tokens_fallback"
    # The default template pins max_tokens: 1024.
    assert est.predicted.tokens_out_expected == 1024
    assert est.predicted.tokens_out_max == 1024
    assert est.basis.sample_size == 0


async def test_out_expected_uses_historical_mean_with_history(tmp_path: Path) -> None:
    """With history → tokens_out_expected is the MEAN of historical output tokens."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    bundle = load_agent(agent_dir)
    storage = await _storage()
    tenant = "local"
    # Mean of [100, 200, 300] = 200.
    for i, out in enumerate((100, 200, 300)):
        await storage.save_run(
            _run_record(agent="demo", tenant_id=tenant, output_tokens=out, latency_ms=500, n=i)
        )

    est = await estimate_run(bundle, {"text": "hi"}, storage=storage, tenant_id=tenant)

    assert est.basis.out_expected_method == "historical_mean"
    assert est.predicted.tokens_out_expected == 200
    assert est.basis.sample_size == 3
    # max band still comes from max_tokens, distinct from the mean.
    assert est.predicted.tokens_out_max == 1024


# ---------------------------------------------------------------------------
# cost bands
# ---------------------------------------------------------------------------


async def test_cost_bands_ordered_and_from_pricing_table(tmp_path: Path) -> None:
    """min <= expected <= max, and min is tokens_in only (no output cost)."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    bundle = load_agent(agent_dir)
    storage = await _storage()

    est = await estimate_run(bundle, {"text": "hello world"}, storage=storage)

    p = est.predicted
    assert p.cost_usd_min <= p.cost_usd_expected <= p.cost_usd_max
    # min is input-only: gpt-4o-mini input is $0.00015/1k.
    expected_min = round(p.tokens_in / 1000.0 * 0.00015, 6)
    assert p.cost_usd_min == expected_min
    # max adds the full max_tokens output band.
    assert p.cost_usd_max > p.cost_usd_min


# ---------------------------------------------------------------------------
# latency: historical vs unavailable
# ---------------------------------------------------------------------------


async def test_latency_unavailable_without_history(tmp_path: Path) -> None:
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    bundle = load_agent(agent_dir)
    storage = await _storage()

    est = await estimate_run(bundle, {"text": "hi"}, storage=storage)

    assert est.basis.latency_method == "unavailable"
    assert est.predicted.latency_ms_p50 is None
    assert est.predicted.latency_ms_p95 is None


async def test_latency_present_with_history(tmp_path: Path) -> None:
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    bundle = load_agent(agent_dir)
    storage = await _storage()
    tenant = "local"
    for i, lat in enumerate((100, 200, 300, 400, 500, 600, 700, 800, 900, 1000)):
        await storage.save_run(
            _run_record(agent="demo", tenant_id=tenant, output_tokens=50, latency_ms=lat, n=i)
        )

    est = await estimate_run(bundle, {"text": "hi"}, storage=storage, tenant_id=tenant)

    assert est.basis.latency_method == "historical_p50p95"
    assert est.predicted.latency_ms_p50 is not None
    assert est.predicted.latency_ms_p95 is not None
    # p95 of a 100..1000 ramp is at/near the top; p50 below it.
    assert est.predicted.latency_ms_p95 >= est.predicted.latency_ms_p50
    assert est.predicted.latency_ms_p95 >= 900


# ---------------------------------------------------------------------------
# budget_check
# ---------------------------------------------------------------------------


async def test_budget_check_within(tmp_path: Path) -> None:
    """A cheap run (tiny input, gpt-4o-mini) is within the 0.50 per-run budget."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    bundle = load_agent(agent_dir)
    storage = await _storage()

    est = await estimate_run(bundle, {"text": "hi"}, storage=storage)

    # Default template budget is 0.50.
    assert est.budget_check.per_run_budget_usd == 0.50
    assert est.budget_check.within_per_run_budget is True
    assert est.predicted.cost_usd_max <= 0.50


async def test_budget_check_over_when_budget_tiny(tmp_path: Path) -> None:
    """Lower the agent's per-run budget below cost_usd_max → flagged OVER."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    bundle = load_agent(agent_dir)
    storage = await _storage()
    # Shrink the budget on the loaded spec to force the over-budget branch.
    bundle.spec.budget.max_cost_usd_per_run = 0.0000001

    est = await estimate_run(bundle, {"text": "hi"}, storage=storage)

    assert est.budget_check.within_per_run_budget is False
    assert est.predicted.cost_usd_max > est.budget_check.per_run_budget_usd


# ---------------------------------------------------------------------------
# tenant scoping
# ---------------------------------------------------------------------------


async def test_history_is_tenant_scoped(tmp_path: Path) -> None:
    """Another tenant's runs MUST NOT inform this tenant's estimate."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    bundle = load_agent(agent_dir)
    storage = await _storage()
    # Other tenant has rich history; our tenant has none.
    for i in range(5):
        await storage.save_run(
            _run_record(agent="demo", tenant_id="other", output_tokens=42, latency_ms=123, n=i)
        )

    est = await estimate_run(bundle, {"text": "hi"}, storage=storage, tenant_id="local")

    # No history for "local" → fallback, not the other tenant's mean.
    assert est.basis.sample_size == 0
    assert est.basis.out_expected_method == "max_tokens_fallback"
    assert est.basis.latency_method == "unavailable"


# ---------------------------------------------------------------------------
# no LLM is ever called
# ---------------------------------------------------------------------------


class _SpyProvider:
    """A provider whose every call records that it was invoked. The
    estimator must touch NONE of these — it predicts, never executes."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete(self, *args: object, **kwargs: object) -> object:  # pragma: no cover
        self.calls.append("complete")
        raise AssertionError("estimate_run must NEVER call the LLM")

    async def stream(self, *args: object, **kwargs: object) -> object:  # pragma: no cover
        self.calls.append("stream")
        raise AssertionError("estimate_run must NEVER stream from the LLM")


async def test_estimator_never_calls_llm(tmp_path: Path) -> None:
    """Wire a spy provider into an Executor and pass it to the estimator;
    the estimate completes and the spy records ZERO calls. A non-RAG agent
    never touches the executor's retrieval seam at all."""
    from movate.core.executor import Executor  # noqa: PLC0415
    from movate.providers.pricing import load_pricing  # noqa: PLC0415
    from movate.testing.doubles import NullTracer  # noqa: PLC0415

    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    bundle = load_agent(agent_dir)
    storage = await _storage()
    spy = _SpyProvider()
    executor = Executor(
        provider=spy,  # type: ignore[arg-type]
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
        tenant_id="local",
    )

    est = await estimate_run(
        bundle,
        {"text": "hi"},
        storage=storage,
        tenant_id="local",
        executor=executor,
        estimate_retrieval=True,  # even with retrieval on, a non-RAG agent skips it
    )

    assert spy.calls == []
    assert est.agent_name == "demo"
    assert est.retrieval_embedded is False


async def test_estimator_does_not_persist_a_run(tmp_path: Path) -> None:
    """No RunRecord / JobRecord is written by an estimate."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    bundle = load_agent(agent_dir)
    storage = await _storage()

    before = len(await storage.list_runs(tenant_id="local", limit=1000))
    await estimate_run(bundle, {"text": "hi"}, storage=storage, tenant_id="local")
    after = len(await storage.list_runs(tenant_id="local", limit=1000))

    assert before == after == 0


# ---------------------------------------------------------------------------
# response shape
# ---------------------------------------------------------------------------


async def test_estimate_reports_model_and_agent(tmp_path: Path) -> None:
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    bundle = load_agent(agent_dir)
    storage = await _storage()

    est = await estimate_run(bundle, {"text": "hi"}, storage=storage)

    assert est.agent_name == "demo"
    assert est.model == "openai/gpt-4o-mini-2024-07-18"
