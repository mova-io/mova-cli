"""The overnight analyst (ADR 047) — anomaly z-scores, health formula, digest.

Covers:

* z-score anomaly detection over a trailing baseline (and that a flat / empty
  baseline emits nothing — no anomaly invented from zero history).
* the documented health-score formula (perfect day → 100; errors / drift /
  cost anomalies degrade it predictably).
* the narrative digest is the ONE budget-capped LLM call (mocked), and is
  skipped cleanly when budget is 0 or no LLM is given.
* graceful degradation when the #542 diagnoser is absent (un-clustered
  fallback by failure_type).
* end-to-end ``analyze`` persists exactly one append-only insight.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from movate.core.models import (
    ErrorInfo,
    FailureRecord,
    JobStatus,
    Metrics,
    RunRecord,
    TokenUsage,
)
from movate.core.observability import analyst
from movate.core.observability.models import AnomalySeverity
from movate.providers.base import CompletionRequest, CompletionResponse
from movate.testing import InMemoryStorage

DAY = date(2026, 5, 20)


# ---------------------------------------------------------------------------
# A controllable LLM stub (records calls, returns a fixed text + token usage).
# ---------------------------------------------------------------------------


@dataclass
class _StubLLM:
    name: str = "stub"
    version: str = "1"
    reply: str = "Yesterday: 10 runs, healthy. Watch: cost up."
    calls: list[CompletionRequest] = field(default_factory=list)

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls.append(request)
        return CompletionResponse(text=self.reply, tokens=TokenUsage(input=100, output=20))


def _run(
    *,
    tenant_id: str = "t1",
    run_id: str,
    agent: str = "triage",
    status: JobStatus = JobStatus.SUCCESS,
    cost: float = 0.01,
    latency_ms: int = 100,
    when: datetime,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        job_id=f"j-{run_id}",
        tenant_id=tenant_id,
        agent=agent,
        agent_version="0.1.0",
        prompt_hash="h",
        provider="openai/gpt-4o-mini",
        provider_version="v1",
        pricing_version="2026-05",
        status=status,
        input={"q": "x"},
        output={"a": "y"} if status == JobStatus.SUCCESS else None,
        error=ErrorInfo(type="Timeout", message="boom") if status != JobStatus.SUCCESS else None,
        metrics=Metrics(
            cost_usd=cost, latency_ms=latency_ms, tokens=TokenUsage(input=10, output=5)
        ),
        created_at=when,
    )


# ---------------------------------------------------------------------------
# Anomaly detection (pure)
# ---------------------------------------------------------------------------


def test_detect_anomalies_zscore_flags_cost_spike() -> None:
    # Baseline cost ~1.0 with tiny variance; today is 10.0 → huge z-score.
    rollup = {"cost_usd": 10.0, "p95_latency_ms": 100.0, "error_rate": 0.0, "runs": 50}
    baselines = {
        "cost": [1.0, 1.1, 0.9, 1.0, 1.05],
        "latency": [100.0, 101.0, 99.0],
        "error_rate": [0.0, 0.0, 0.0],
        "volume": [50.0, 51.0, 49.0],
    }
    anomalies = analyst.detect_anomalies(rollup, baselines)
    cost = next(a for a in anomalies if a.metric == "cost")
    assert cost.z > 4.0  # way above baseline
    assert cost.severity is AnomalySeverity.CRITICAL
    assert cost.value == 10.0
    assert "above" in cost.note


def test_detect_anomalies_empty_or_flat_baseline_emits_nothing() -> None:
    rollup = {"cost_usd": 10.0, "p95_latency_ms": 100.0, "error_rate": 0.0, "runs": 50}
    # Empty baseline → no anomalies (cold start).
    assert analyst.detect_anomalies(rollup, {}) == []
    # Flat baseline (std == 0) → no meaningful z → no anomalies.
    flat = {"cost": [1.0, 1.0, 1.0], "latency": [], "error_rate": [], "volume": []}
    assert analyst.detect_anomalies(rollup, flat) == []


def test_detect_anomalies_within_baseline_is_quiet() -> None:
    rollup = {"cost_usd": 1.0, "p95_latency_ms": 100.0, "error_rate": 0.0, "runs": 50}
    baselines = {
        "cost": [1.0, 1.1, 0.9, 1.0, 1.05],
        "latency": [100.0, 101.0, 99.0],
        "error_rate": [0.0, 0.0, 0.0],
        "volume": [50.0, 51.0, 49.0],
    }
    assert analyst.detect_anomalies(rollup, baselines) == []


# ---------------------------------------------------------------------------
# Health score (pure, documented formula)
# ---------------------------------------------------------------------------


def test_health_score_perfect_day_is_100() -> None:
    rollup = {"error_rate": 0.0, "eval_pass_rate": 1.0}
    assert analyst.compute_health_score(rollup, has_drift=False, anomalies=[]) == 100.0


def test_health_score_no_evals_does_not_penalize_quality() -> None:
    # eval_pass_rate None → treated as neutral 1.0 (no evals ran).
    rollup = {"error_rate": 0.0, "eval_pass_rate": None}
    assert analyst.compute_health_score(rollup, has_drift=False, anomalies=[]) == 100.0


def test_health_score_errors_and_drift_degrade() -> None:
    # error_rate 0.5 costs 0.40 * 0.5 = 0.20; drift costs 0.15 → 100*(0.40*0.5
    # + 0.30*1.0 + 0.15*0 + 0.15*1) = 100*(0.2 + 0.3 + 0 + 0.15) = 65.0
    rollup = {"error_rate": 0.5, "eval_pass_rate": 1.0}
    score = analyst.compute_health_score(rollup, has_drift=True, anomalies=[])
    assert score == 65.0


def test_health_score_clamped_to_bounds() -> None:
    # Pathological error_rate > 1 (dirty data) clamps; score stays >= 0.
    rollup = {"error_rate": 5.0, "eval_pass_rate": 0.0}
    score = analyst.compute_health_score(rollup, has_drift=True, anomalies=[])
    assert 0.0 <= score <= 100.0


# ---------------------------------------------------------------------------
# Failure clustering (degrades without #542)
# ---------------------------------------------------------------------------


def test_cluster_failures_degrades_without_diagnoser() -> None:
    # #542 diagnoser is not on main → fallback groups by failure_type.
    failures = [
        FailureRecord(
            failure_id="f1",
            run_id="r1",
            tenant_id="t1",
            agent="a",
            failure_type="Timeout",
            message="timed out once",
            retryable=True,
        ),
        FailureRecord(
            failure_id="f2",
            run_id="r2",
            tenant_id="t1",
            agent="a",
            failure_type="Timeout",
            message="timed out again",
            retryable=True,
        ),
        FailureRecord(
            failure_id="f3",
            run_id="r3",
            tenant_id="t1",
            agent="b",
            failure_type="RateLimit",
            message="429",
            retryable=True,
        ),
    ]
    clusters = analyst.cluster_failures(failures)
    by_sig = {c["signature"]: c for c in clusters}
    assert by_sig["Timeout"]["count"] == 2
    assert by_sig["RateLimit"]["count"] == 1
    # Sorted by count desc.
    assert clusters[0]["signature"] == "Timeout"


# ---------------------------------------------------------------------------
# Narrative digest = the ONE budget-capped LLM call
# ---------------------------------------------------------------------------


async def test_analyze_makes_exactly_one_llm_call() -> None:
    storage = InMemoryStorage()
    await storage.init()
    base = datetime(2026, 5, 20, 9, 0, tzinfo=UTC)
    for i in range(5):
        await storage.save_run(_run(run_id=f"r{i}", when=base + timedelta(minutes=i)))

    llm = _StubLLM(reply="Yesterday: 5 runs, all green.")
    insight = await analyst.analyze("t1", "proj", DAY, storage=storage, llm=llm, budget_usd=1.0)

    assert len(llm.calls) == 1  # the digest is the only LLM spend
    assert insight.narrative_digest == "Yesterday: 5 runs, all green."
    assert insight.usage_rollup["runs"] == 5


async def test_analyze_budget_zero_skips_llm() -> None:
    storage = InMemoryStorage()
    await storage.init()
    base = datetime(2026, 5, 20, 9, 0, tzinfo=UTC)
    await storage.save_run(_run(run_id="r1", when=base))

    llm = _StubLLM()
    insight = await analyst.analyze("t1", "proj", DAY, storage=storage, llm=llm, budget_usd=0.0)
    assert llm.calls == []  # budget 0 → no call
    assert insight.narrative_digest == ""
    # Pure-Python stages still ran.
    assert insight.usage_rollup["runs"] == 1
    assert insight.health_score == 100.0


async def test_analyze_no_llm_still_produces_structured_insight() -> None:
    storage = InMemoryStorage()
    await storage.init()
    base = datetime(2026, 5, 20, 9, 0, tzinfo=UTC)
    await storage.save_run(_run(run_id="r1", when=base, status=JobStatus.ERROR))

    insight = await analyst.analyze("t1", "proj", DAY, storage=storage, llm=None)
    assert insight.narrative_digest == ""
    assert insight.usage_rollup["errors"] == 1
    assert insight.usage_rollup["error_rate"] == 1.0


# ---------------------------------------------------------------------------
# End-to-end persistence (append-only)
# ---------------------------------------------------------------------------


async def test_analyze_persists_one_appendonly_insight() -> None:
    storage = InMemoryStorage()
    await storage.init()
    base = datetime(2026, 5, 20, 9, 0, tzinfo=UTC)
    await storage.save_run(_run(run_id="r1", when=base))

    await analyst.analyze("t1", "proj", DAY, storage=storage, llm=_StubLLM(), budget_usd=1.0)
    await analyst.analyze("t1", "proj", DAY, storage=storage, llm=_StubLLM(), budget_usd=1.0)
    # Two analyze runs for the same day → two append-only rows...
    assert len(storage.insights) == 2
    # ...but the read collapses to the latest.
    rows = await storage.list_insights("t1", project_id="proj")
    assert len([r for r in rows if r.date == DAY]) == 1


async def test_analyze_uses_prior_insights_as_baseline() -> None:
    """Anomaly detection draws its trailing baseline from prior insights.

    Seed each prior day with a few cheap runs (so the baseline has a non-zero,
    low-variance cost series), then give the current day a clear cost spike →
    a cost anomaly fires against the prior insights' baseline.
    """
    storage = InMemoryStorage()
    await storage.init()

    # Seed 5 prior days, each with a couple of CHEAP runs → a stable, non-zero,
    # slightly-varying cost baseline (non-zero std so the z-score is defined)
    # that the live analyze() persists as prior insights.
    for offset in range(1, 6):
        prior_day = DAY - timedelta(days=offset)
        prior_base = datetime(prior_day.year, prior_day.month, prior_day.day, 9, 0, tzinfo=UTC)
        jitter = 0.001 * offset  # small day-to-day variance → non-zero std
        await storage.save_run(_run(run_id=f"p{offset}a", cost=0.01 + jitter, when=prior_base))
        await storage.save_run(
            _run(run_id=f"p{offset}b", cost=0.012, when=prior_base + timedelta(minutes=1))
        )
        await analyst.analyze("t1", "proj", prior_day, storage=storage, llm=None, budget_usd=0.0)

    # The current day spikes cost well above the ~0.022/day baseline.
    base = datetime(2026, 5, 20, 9, 0, tzinfo=UTC)
    for i in range(3):
        await storage.save_run(_run(run_id=f"spike{i}", cost=5.0, when=base + timedelta(minutes=i)))

    insight = await analyst.analyze("t1", "proj", DAY, storage=storage, llm=None, budget_usd=0.0)
    metrics = {a.metric for a in insight.typed_anomalies()}
    assert "cost" in metrics
