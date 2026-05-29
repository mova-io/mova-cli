"""Backend-agnostic agent-health aggregation (ADR 031 D3 / ADR 032 D2).

The **pure rollup** behind two surfaces:

* ``mdk report`` (``cli/report_cmd.py``) — the offline, no-infra CLI summary.
* ``GET /api/v1/report`` + ``GET /api/v1/agents/{name}/metrics``
  (``runtime/app.py``) — the in-product monitor feed the Mova iO front end
  renders (ADR 032 D2).

Both call-sites fetch the same persisted records — runs (ADR 024 per-step cost
+ latency on :class:`~movate.core.models.RunRecord`) and eval summaries
(:class:`~movate.core.models.EvalRecord`) — through the
:class:`~movate.storage.base.StorageProvider` Protocol, then reduce them here.
This module is the single source of truth for the rollup so the CLI and the API
can never drift (CLAUDE.md rule 4: factor, don't duplicate).

Design rules:

* **Pure / backend-agnostic.** No I/O, no ``cli`` import, no concrete storage
  backend — it depends only on :mod:`movate.core.models`. This is what lets the
  runtime (which must not import ``cli`` — ``cli ⊥ runtime``) reuse it.
* **Aggregation in-memory.** Postgres + SQLite share the same Protocol, so we
  reduce in Python rather than writing two backend-specific ``GROUP BY``\\ s.
* **Degrades gracefully.** Empty input → a zeroed :class:`Report` (never a
  divide-by-zero / crash); records missing cost/latency contribute ``0`` /
  drop out rather than poisoning the distribution.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from movate.core.models import EvalRecord, JobStatus, RunRecord

# Run statuses that count as a "failure" for the top-failing rollup. Mirrors
# the terminal-non-success set the logs/runs commands treat as failed.
FAILED_STATUSES: frozenset[JobStatus] = frozenset(
    {
        JobStatus.ERROR,
        JobStatus.SAFETY_BLOCKED,
        JobStatus.DEAD_LETTER,
    }
)


# ---------------------------------------------------------------------------
# Aggregation dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LatencyPercentiles:
    """p50 / p95 / p99 of a set of run latencies (ms).

    ``None`` for every field when there were no runs with a recorded
    latency — the caller renders "N/A" rather than ``0`` so a missing
    signal is distinguishable from a genuinely instant run.
    """

    p50: float | None = None
    p95: float | None = None
    p99: float | None = None
    count: int = 0


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile over an already-sorted, non-empty list.

    Uses the nearest-rank method (no interpolation) so a tiny sample
    (the common offline case) yields a real observed value rather than a
    synthetic interpolated one. ``pct`` in [0, 100].
    """
    if not sorted_values:
        raise ValueError("percentile of empty sequence")
    if len(sorted_values) == 1:
        return sorted_values[0]
    # Nearest-rank: rank = ceil(pct/100 * N), 1-based, clamped to [1, N].
    rank = max(1, min(len(sorted_values), -(-int(pct * len(sorted_values)) // 100)))
    return sorted_values[rank - 1]


def _latency_percentiles(runs: list[RunRecord]) -> LatencyPercentiles:
    """Compute p50/p95/p99 of run latency over ``runs``.

    Runs whose ``metrics.latency_ms`` is missing / non-positive are dropped
    (older records may carry no latency) — they don't contribute a bogus
    ``0`` to the distribution. Empty input → all-``None``.
    """
    latencies = sorted(
        float(r.metrics.latency_ms)
        for r in runs
        if r.metrics.latency_ms and r.metrics.latency_ms > 0
    )
    if not latencies:
        return LatencyPercentiles(count=0)
    return LatencyPercentiles(
        p50=_percentile(latencies, 50),
        p95=_percentile(latencies, 95),
        p99=_percentile(latencies, 99),
        count=len(latencies),
    )


@dataclass(frozen=True)
class AgentRollup:
    """The full rollup for one agent (or workflow) name.

    Pass-rate fields are ``None`` when the agent has no eval runs (cost /
    latency come from agent *runs*, which may exist without any eval).
    """

    name: str
    # --- runs / cost ---
    runs: int = 0
    failed_runs: int = 0
    total_cost_usd: float = 0.0
    latency: LatencyPercentiles = field(default_factory=LatencyPercentiles)
    last_run_at: str = ""
    # --- evals / pass-rate ---
    eval_runs: int = 0
    latest_pass_rate: float | None = None
    mean_pass_rate: float | None = None
    latest_eval_at: str = ""

    @property
    def mean_cost_usd(self) -> float:
        return self.total_cost_usd / self.runs if self.runs else 0.0

    @property
    def failure_rate(self) -> float:
        return self.failed_runs / self.runs if self.runs else 0.0


@dataclass(frozen=True)
class FailingCase:
    """One recurring failing input across runs, for the top-failures table."""

    case: str
    """A short, stable rendering of the failing input (the 'case id')."""
    failures: int
    agents: list[str]
    last_error: str = ""


def _case_key(run: RunRecord) -> str:
    """A stable, compact key for a run's input — the 'case id' we cluster on.

    Prefers an explicit ``case``/``id``/``case_id`` field if the agent's
    input carries one (eval-style inputs often do); otherwise falls back to
    a compact JSON rendering of the whole input. Truncated so the table
    stays readable — clustering identical inputs is the point, not showing
    the full payload.
    """
    payload = run.input or {}
    if isinstance(payload, dict):
        for field_name in ("case_id", "case", "id"):
            val = payload.get(field_name)
            if isinstance(val, str | int) and str(val).strip():
                return str(val).strip()[:120]
    try:
        rendered = json.dumps(payload, sort_keys=True, default=str)
    except (TypeError, ValueError):
        rendered = str(payload)
    return rendered[:120]


def _top_failing_cases(runs: list[RunRecord], *, limit: int = 5) -> list[FailingCase]:
    """Cluster non-success runs by input and rank by failure count.

    The persisted store keeps eval *summaries* (``EvalRecord``), not per-case
    eval detail, so the offline per-instance failure signal is failing
    *runs*: we group them by a stable input key and surface the most
    frequently failing inputs. Empty / all-success input → empty list.
    """
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"failures": 0, "agents": set(), "last_error": ""}
    )
    for run in runs:
        if run.status not in FAILED_STATUSES:
            continue
        bucket = buckets[_case_key(run)]
        bucket["failures"] += 1
        bucket["agents"].add(run.agent or "(unknown agent)")
        if run.error is not None and run.error.message:
            bucket["last_error"] = run.error.message
    cases = [
        FailingCase(
            case=key,
            failures=b["failures"],
            agents=sorted(b["agents"]),
            last_error=b["last_error"],
        )
        for key, b in buckets.items()
    ]
    # Most failures first; ties broken by case string for deterministic output.
    cases.sort(key=lambda c: (-c.failures, c.case))
    return cases[:limit]


@dataclass(frozen=True)
class Report:
    """The complete rollup — what ``--json`` serialises and the table renders."""

    total_runs: int
    total_failed_runs: int
    total_cost_usd: float
    overall_latency: LatencyPercentiles
    total_eval_runs: int
    overall_latest_pass_rate: float | None
    agents: list[AgentRollup]
    top_failing_cases: list[FailingCase]
    window_days: int = 0
    agent_filter: str | None = None


def _filter_runs_by_since(runs: list[RunRecord], days: int) -> list[RunRecord]:
    """Drop runs older than ``days`` ago (UTC). ``days <= 0`` → keep all.

    Coerces naive datetimes (some SQLite rows hand them back naive) to UTC
    before comparing, so the window never raises on a mixed set.
    """
    if days <= 0:
        return runs
    cutoff = datetime.now(UTC) - timedelta(days=days)
    out: list[RunRecord] = []
    for run in runs:
        ts = run.created_at
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if ts >= cutoff:
            out.append(run)
    return out


def _filter_evals_by_since(evals: list[EvalRecord], days: int) -> list[EvalRecord]:
    """Window helper for eval summaries — same contract as the run variant."""
    if days <= 0:
        return evals
    cutoff = datetime.now(UTC) - timedelta(days=days)
    out: list[EvalRecord] = []
    for ev in evals:
        ts = ev.created_at
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if ts >= cutoff:
            out.append(ev)
    return out


def build_report(
    runs: list[RunRecord],
    evals: list[EvalRecord],
    *,
    window_days: int = 0,
    agent_filter: str | None = None,
    top_n: int = 5,
) -> Report:
    """Reduce the raw run + eval records into the per-agent + overall rollup.

    Pure: no I/O. ``runs`` and ``evals`` are already store-fetched +
    time-windowed by the caller. An agent appears in the rollup if it has
    *either* runs or evals — so an agent you've only eval'd (no ad-hoc runs)
    still shows its pass-rate, and vice versa.
    """
    # --- per-agent run aggregation ---
    run_buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "runs": 0,
            "failed": 0,
            "cost": 0.0,
            "latencies": [],
            "last_run_at": "",
        }
    )
    for run in runs:
        name = run.agent or "(unknown agent)"
        b = run_buckets[name]
        b["runs"] += 1
        if run.status in FAILED_STATUSES:
            b["failed"] += 1
        # ``metrics`` is a required field on RunRecord; ``cost_usd`` /
        # ``latency_ms`` default to 0 on older records, so a missing signal
        # degrades to "no contribution" rather than a crash.
        b["cost"] += float(run.metrics.cost_usd or 0.0)
        lat = run.metrics.latency_ms
        if lat and lat > 0:
            b["latencies"].append(float(lat))
        created_iso = run.created_at.isoformat() if run.created_at else ""
        b["last_run_at"] = max(b["last_run_at"], created_iso)

    # --- per-agent eval aggregation (evals are returned newest-first) ---
    eval_buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"eval_runs": 0, "pass_rates": [], "latest_pass_rate": None, "latest_at": ""}
    )
    for ev in evals:
        name = ev.agent or "(unknown agent)"
        b = eval_buckets[name]
        b["eval_runs"] += 1
        b["pass_rates"].append(float(ev.pass_rate))
        created_iso = ev.created_at.isoformat() if ev.created_at else ""
        # First time we see this agent in a newest-first list = latest eval.
        if created_iso >= b["latest_at"]:
            b["latest_at"] = created_iso
            b["latest_pass_rate"] = float(ev.pass_rate)

    names = sorted(set(run_buckets) | set(eval_buckets))
    agents: list[AgentRollup] = []
    for name in names:
        rb = run_buckets.get(name)
        eb = eval_buckets.get(name)
        latencies = sorted(rb["latencies"]) if rb else []
        latency = (
            LatencyPercentiles(
                p50=_percentile(latencies, 50),
                p95=_percentile(latencies, 95),
                p99=_percentile(latencies, 99),
                count=len(latencies),
            )
            if latencies
            else LatencyPercentiles(count=0)
        )
        pass_rates = eb["pass_rates"] if eb else []
        agents.append(
            AgentRollup(
                name=name,
                runs=rb["runs"] if rb else 0,
                failed_runs=rb["failed"] if rb else 0,
                total_cost_usd=rb["cost"] if rb else 0.0,
                latency=latency,
                last_run_at=rb["last_run_at"] if rb else "",
                eval_runs=eb["eval_runs"] if eb else 0,
                latest_pass_rate=eb["latest_pass_rate"] if eb else None,
                mean_pass_rate=(sum(pass_rates) / len(pass_rates)) if pass_rates else None,
                latest_eval_at=eb["latest_at"] if eb else "",
            )
        )
    # Highest spend first — that's usually what the operator scans for.
    agents.sort(key=lambda a: a.total_cost_usd, reverse=True)

    total_failed = sum(1 for r in runs if r.status in FAILED_STATUSES)
    # Overall latest pass-rate = pass-rate of the single most recent eval run.
    overall_latest_pass_rate: float | None = None
    if evals:
        latest_eval = max(
            evals,
            key=lambda e: e.created_at if e.created_at else datetime.min.replace(tzinfo=UTC),
        )
        overall_latest_pass_rate = float(latest_eval.pass_rate)

    return Report(
        total_runs=len(runs),
        total_failed_runs=total_failed,
        total_cost_usd=sum(float(r.metrics.cost_usd or 0.0) for r in runs),
        overall_latency=_latency_percentiles(runs),
        total_eval_runs=len(evals),
        overall_latest_pass_rate=overall_latest_pass_rate,
        agents=agents,
        top_failing_cases=_top_failing_cases(runs, limit=top_n),
        window_days=window_days,
        agent_filter=agent_filter,
    )


def report_to_json(report: Report) -> dict[str, Any]:
    """Machine-readable shape for ``--json`` / the HTTP response — stable.

    Dataclasses serialise cleanly via ``asdict``; ``FailingCase.agents`` is
    already a list and the nested ``LatencyPercentiles`` flattens to a dict.
    The runtime endpoints (ADR 032 D2) wrap this same shape in a typed
    Pydantic ``response_model`` so the OpenAPI spec stays rich.
    """
    return {
        "agent_filter": report.agent_filter,
        "window_days": report.window_days,
        "totals": {
            "runs": report.total_runs,
            "failed_runs": report.total_failed_runs,
            "cost_usd": report.total_cost_usd,
            "eval_runs": report.total_eval_runs,
            "latest_pass_rate": report.overall_latest_pass_rate,
            "latency_ms": asdict(report.overall_latency),
        },
        "agents": [
            {
                "name": a.name,
                "runs": a.runs,
                "failed_runs": a.failed_runs,
                "failure_rate": a.failure_rate,
                "total_cost_usd": a.total_cost_usd,
                "mean_cost_usd": a.mean_cost_usd,
                "latency_ms": asdict(a.latency),
                "last_run_at": a.last_run_at,
                "eval_runs": a.eval_runs,
                "latest_pass_rate": a.latest_pass_rate,
                "mean_pass_rate": a.mean_pass_rate,
                "latest_eval_at": a.latest_eval_at,
            }
            for a in report.agents
        ],
        "top_failing_cases": [
            {
                "case": c.case,
                "failures": c.failures,
                "agents": c.agents,
                "last_error": c.last_error,
            }
            for c in report.top_failing_cases
        ],
    }


# ---------------------------------------------------------------------------
# Usage metering (ADR 036 D1)
#
# Per-tenant rollup of *requests*, *tokens*, and *cost* over a time window —
# the billing-visibility companion to the agent-health :func:`build_report`.
# Reuses the same in-memory aggregation pattern + the per-run records already
# captured (ADR 024). No new measurement plumbing; this is purely a reducer
# over runs the storage layer hands back. Quota *enforcement* (ADR 036 D2) is
# out of scope here — D1 ships only the metering signal + read endpoint.
#
# Design rules:
#
# * Pure / backend-agnostic — same as :func:`build_report`. ``cli ⊥ runtime``.
# * Empty input → a zeroed :class:`Usage` (never a divide-by-zero / crash).
# * Records missing token / cost / provider degrade to "no contribution"
#   rather than poisoning the rollup (older rows may have ``0`` / ``""``).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageRollup:
    """A single grouped usage row (one agent, one provider, or the whole window).

    ``key`` carries the grouping value (agent name / provider id / etc.) so the
    same dataclass renders every breakdown. Empty string ``""`` is the
    deliberate sentinel for "no signal" (e.g. an older run that didn't record a
    provider) — the front end can render it as "(unknown)" without ambiguity.
    """

    key: str
    requests: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class Usage:
    """Per-tenant usage rollup over a time window (ADR 036 D1).

    The HTTP response (``GET /api/v1/usage``) and any future ``mdk usage`` CLI
    render this same shape. ``by_agent`` / ``by_provider`` are the optional
    breakdowns — included by default so the front end can drill in without a
    second round-trip; pass empty lists to suppress.
    """

    tenant_id: str
    window_days: int
    agent_filter: str | None
    totals: UsageRollup
    by_agent: list[UsageRollup]
    by_provider: list[UsageRollup]


def build_usage(
    runs: list[RunRecord],
    *,
    tenant_id: str,
    window_days: int = 0,
    agent_filter: str | None = None,
    include_by_agent: bool = True,
    include_by_provider: bool = True,
) -> Usage:
    """Reduce ``runs`` into per-tenant usage counters + optional breakdowns.

    Pure: no I/O, no time-windowing (caller pre-filters with
    :func:`_filter_runs_by_since`, same as :func:`build_report`). Empty
    ``runs`` → a zeroed :class:`Usage` (200 path; never a 500).

    Counters:

    * ``requests`` — count of runs in the window. Every persisted ``RunRecord``
      contributes regardless of ``status`` (a safety-blocked / error run *was*
      a request; billing has to see it).
    * ``tokens_in`` / ``tokens_out`` — sum of ``metrics.tokens.input`` /
      ``metrics.tokens.output``. ``cached_input`` is intentionally NOT counted
      (it's the provider's cache hit, not new tokenization).
    * ``cost_usd`` — sum of ``metrics.cost_usd``. Already includes per-turn
      LLM cost + per-skill cost (ADR 024 — skills add to the same field).

    Note on cost: this is the **estimated** cost from ``pricing.yaml`` at run
    time, NOT the actual provider invoice. The ADR (036 §Risks) calls out the
    estimate↔actual gap — D3 billing export will document it. We surface the
    estimate as-is here.
    """
    totals_requests = 0
    totals_tokens_in = 0
    totals_tokens_out = 0
    totals_cost = 0.0

    # Per-grouping accumulators. Use dict to preserve discovery + sort later.
    by_agent_buckets: dict[str, dict[str, float]] = defaultdict(
        lambda: {"requests": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
    )
    by_provider_buckets: dict[str, dict[str, float]] = defaultdict(
        lambda: {"requests": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
    )

    for run in runs:
        metrics = run.metrics
        tokens_in = int(metrics.tokens.input or 0) if metrics.tokens else 0
        tokens_out = int(metrics.tokens.output or 0) if metrics.tokens else 0
        cost = float(metrics.cost_usd or 0.0)

        totals_requests += 1
        totals_tokens_in += tokens_in
        totals_tokens_out += tokens_out
        totals_cost += cost

        if include_by_agent:
            agent_key = run.agent or ""
            ab = by_agent_buckets[agent_key]
            ab["requests"] += 1
            ab["tokens_in"] += tokens_in
            ab["tokens_out"] += tokens_out
            ab["cost_usd"] += cost

        if include_by_provider:
            # Prefer the per-call ``metrics.provider`` (set by the executor for
            # the actual model used); fall back to the record's top-level
            # ``provider`` field. Empty string = older record without a
            # captured provider — surface it as a distinct bucket rather than
            # silently dropping it (failure-mode rule).
            provider_key = metrics.provider or run.provider or ""
            pb = by_provider_buckets[provider_key]
            pb["requests"] += 1
            pb["tokens_in"] += tokens_in
            pb["tokens_out"] += tokens_out
            pb["cost_usd"] += cost

    def _sort_rollups(buckets: dict[str, dict[str, float]]) -> list[UsageRollup]:
        # Highest cost first — operator/billing scans for top spend; ties
        # broken by key for deterministic output.
        out = [
            UsageRollup(
                key=k,
                requests=int(v["requests"]),
                tokens_in=int(v["tokens_in"]),
                tokens_out=int(v["tokens_out"]),
                cost_usd=float(v["cost_usd"]),
            )
            for k, v in buckets.items()
        ]
        out.sort(key=lambda r: (-r.cost_usd, r.key))
        return out

    return Usage(
        tenant_id=tenant_id,
        window_days=window_days,
        agent_filter=agent_filter,
        totals=UsageRollup(
            key=tenant_id,
            requests=totals_requests,
            tokens_in=totals_tokens_in,
            tokens_out=totals_tokens_out,
            cost_usd=totals_cost,
        ),
        by_agent=_sort_rollups(by_agent_buckets) if include_by_agent else [],
        by_provider=_sort_rollups(by_provider_buckets) if include_by_provider else [],
    )


__all__ = [
    "FAILED_STATUSES",
    "AgentRollup",
    "FailingCase",
    "LatencyPercentiles",
    "Report",
    "Usage",
    "UsageRollup",
    "build_report",
    "build_usage",
    "report_to_json",
]
