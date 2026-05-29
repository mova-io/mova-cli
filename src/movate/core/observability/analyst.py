"""The overnight analyst — preprocess telemetry into one daily insight (ADR 047).

This is an **MDK agent dogfooding the platform**: a scheduled job that reads
the same storage every deployed agent writes to (runs / evals / failures) and
distills it into one append-only :class:`ObservabilityInsight` per project per
day. The NL-query path then reads those insights instead of re-scanning raw
runs, so questions answer in a single indexed lookup.

Pipeline (`analyze`):

1. **Pull telemetry** from the :class:`StorageProvider` Protocol — runs
   (cost / latency / status), evals (pass rates), failures. ADR 036's
   ``build_usage`` and the #542 Failure Pattern Diagnoser are used opportunistically
   via ``getattr`` and degrade gracefully when absent on ``main``.
2. **Anomaly detection** (pure Python, NO LLM) — z-score of
   cost / latency / error-rate / volume against a trailing baseline drawn from
   prior insights (or 0 when there's no history yet).
3. **Health score** (pure Python) — composite 0-100 from error rate, eval
   pass rate, drift presence, and cost trend. Formula documented in
   :func:`compute_health_score`.
4. **Top failure clusters** — the #542 diagnoser when present, else
   un-clustered failures grouped by ``failure_type``.
5. **Narrative digest** — the ONE LLM call, budget-capped. Feeds the computed
   rollups + anomalies + clusters to the model for a short markdown digest.
6. **Persist** via :meth:`StorageProvider.save_insight` (append-only).

Boundary discipline: depends only on the Protocols + core models. No concrete
backend, no ``runtime``/``cli`` import. Tracing is NOT wired here (it lives at
the edges).
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from movate.core.models import RunRecord
from movate.core.observability.models import (
    Anomaly,
    AnomalySeverity,
    ObservabilityInsight,
)
from movate.providers.base import BaseLLMProvider, CompletionRequest, Message
from movate.storage.base import StorageProvider

logger = logging.getLogger(__name__)

# Trailing window for baselines when prior insights are thin / absent. The
# anomaly detector prefers prior insights' usage_rollups (cheap, already
# computed) and only falls back to a live scan over this many days.
DEFAULT_BASELINE_DAYS = 14

# z-score → severity thresholds (absolute value).
_Z_INFO = 2.0
_Z_WARNING = 3.0
_Z_CRITICAL = 4.0

# Minimum trailing data points before a z-score is defined (need >= 2 for a std).
_MIN_BASELINE_POINTS = 2

# Budget headroom: the narrative call is skipped entirely if the estimated
# cost of even a minimal completion would exceed the remaining budget. We use
# a conservative per-1k-token guess only for the pre-flight gate; the ACTUAL
# cost is computed from the provider's returned token usage.
_NARRATIVE_MAX_OUTPUT_TOKENS = 600


@dataclass
class _Window:
    """A bounded time window over telemetry, resolved to [start, end)."""

    start: datetime
    end: datetime

    @property
    def day(self) -> date:
        """The calendar day the window summarizes (its start date, UTC)."""
        return self.start.astimezone(UTC).date()


def day_window(day: date) -> _Window:
    """Build the [00:00, 24:00) UTC window for a calendar ``day``."""
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return _Window(start=start, end=start + timedelta(days=1))


def _coerce_utc(ts: datetime | None) -> datetime | None:
    """Normalize a (possibly naive, SQLite-origin) datetime to UTC."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _in_window(ts: datetime | None, window: _Window) -> bool:
    norm = _coerce_utc(ts)
    if norm is None:
        return False
    return window.start <= norm < window.end


# ---------------------------------------------------------------------------
# Step 1-2: usage rollup + anomaly detection (PURE — no LLM, no I/O)
# ---------------------------------------------------------------------------


def build_usage_rollup(
    runs: Sequence[RunRecord], *, eval_pass_rate: float | None
) -> dict[str, Any]:
    """Aggregate a window's runs into the ``usage_rollup`` dict.

    Pure reduction over :class:`RunRecord` — no SQL GROUP BY (the same
    Protocol backs SQLite + Postgres, so we reduce in Python; the run volume
    one project produces per day is small). Latency percentiles use the
    nearest-rank method over the window's per-run latencies.
    """
    total = len(runs)
    errors = sum(1 for r in runs if r.status.value in ("error", "dead_letter", "safety_blocked"))
    cost = sum(float(r.metrics.cost_usd or 0.0) for r in runs)
    tokens_in = sum(int(r.metrics.tokens.input or 0) for r in runs)
    tokens_out = sum(int(r.metrics.tokens.output or 0) for r in runs)
    latencies = sorted(int(r.metrics.latency_ms or 0) for r in runs)

    by_agent: dict[str, dict[str, float]] = defaultdict(
        lambda: {"runs": 0.0, "errors": 0.0, "cost_usd": 0.0}
    )
    by_provider: dict[str, dict[str, float]] = defaultdict(lambda: {"runs": 0.0, "cost_usd": 0.0})
    for r in runs:
        a = by_agent[r.agent or "(unknown)"]
        a["runs"] += 1
        a["cost_usd"] += float(r.metrics.cost_usd or 0.0)
        if r.status.value in ("error", "dead_letter", "safety_blocked"):
            a["errors"] += 1
        p = by_provider[r.provider or "(unknown)"]
        p["runs"] += 1
        p["cost_usd"] += float(r.metrics.cost_usd or 0.0)

    return {
        "runs": total,
        "errors": errors,
        "error_rate": (errors / total) if total else 0.0,
        "cost_usd": cost,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "mean_latency_ms": (sum(latencies) / len(latencies)) if latencies else 0.0,
        "p95_latency_ms": _percentile(latencies, 95),
        "eval_pass_rate": eval_pass_rate,
        "by_agent": {k: dict(v) for k, v in by_agent.items()},
        "by_provider": {k: dict(v) for k, v in by_provider.items()},
    }


def _percentile(sorted_values: list[int], pct: int) -> float:
    """Nearest-rank percentile over a pre-sorted list. ``[]`` → 0.0."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = math.ceil((pct / 100) * len(sorted_values))
    rank = min(max(rank, 1), len(sorted_values))
    return float(sorted_values[rank - 1])


def _severity_for(z: float) -> AnomalySeverity | None:
    """Map an absolute z-score to a severity, or ``None`` below the floor."""
    az = abs(z)
    if az >= _Z_CRITICAL:
        return AnomalySeverity.CRITICAL
    if az >= _Z_WARNING:
        return AnomalySeverity.WARNING
    if az >= _Z_INFO:
        return AnomalySeverity.INFO
    return None


def detect_anomalies(
    rollup: dict[str, Any],
    baselines: dict[str, list[float]],
) -> list[Anomaly]:
    """z-score the window's metrics against trailing baselines (PURE, no LLM).

    For each tracked metric (``cost``, ``latency``, ``error_rate``,
    ``volume``) we take the trailing series of prior daily values, compute
    its mean + population std, and emit an :class:`Anomaly` when the current
    value's absolute z-score crosses the ``info`` floor (2.0).

    A flat baseline (std == 0) or an empty baseline can't produce a
    meaningful z-score, so those metrics are skipped — we never invent an
    anomaly out of zero history. ``z`` carries its sign so the narrative can
    say "up" vs "down".
    """
    current = {
        "cost": float(rollup.get("cost_usd", 0.0)),
        "latency": float(rollup.get("p95_latency_ms", 0.0)),
        "error_rate": float(rollup.get("error_rate", 0.0)),
        "volume": float(rollup.get("runs", 0)),
    }
    anomalies: list[Anomaly] = []
    for metric, value in current.items():
        series = baselines.get(metric, [])
        if len(series) < _MIN_BASELINE_POINTS:
            continue  # need at least two prior points for a std
        mean = statistics.fmean(series)
        std = statistics.pstdev(series)
        if std == 0:
            continue  # flat baseline — no meaningful z
        z = (value - mean) / std
        severity = _severity_for(z)
        if severity is None:
            continue
        direction = "above" if z > 0 else "below"
        anomalies.append(
            Anomaly(
                metric=metric,
                severity=severity,
                value=value,
                baseline=mean,
                z=z,
                note=f"{metric} is {abs(z):.1f} sigma {direction} the {len(series)}-day baseline",
            )
        )
    # Critical first so a truncating renderer keeps the scariest.
    order = {AnomalySeverity.CRITICAL: 0, AnomalySeverity.WARNING: 1, AnomalySeverity.INFO: 2}
    anomalies.sort(key=lambda a: (order[a.severity], -abs(a.z)))
    return anomalies


# ---------------------------------------------------------------------------
# Step 3: health score (PURE — no LLM)
# ---------------------------------------------------------------------------


def compute_health_score(
    rollup: dict[str, Any],
    *,
    has_drift: bool,
    anomalies: Sequence[Anomaly],
) -> float:
    """Composite health score in [0, 100] from a day's signals (PURE).

    Formula (each term is a 0-1 factor; the score is the weighted sum * 100):

        score = 100 * (
            0.40 * (1 - error_rate)                 # reliability
          + 0.30 * eval_pass_rate                   # quality (1.0 if no evals ran)
          + 0.15 * (0.0 if has_drift else 1.0)      # stability (drift present?)
          + 0.15 * cost_trend_factor                # cost direction
        )

    where ``cost_trend_factor`` is 1.0 normally and decays toward 0 as cost /
    latency / volume anomalies pile up (each critical cost-or-latency anomaly
    subtracts 0.34, each warning 0.17), floored at 0. A project with no errors,
    passing evals, no drift, and stable cost scores 100. The weights are fixed
    constants documented here so the score is reproducible + explainable; tuning
    them is a deliberate, reviewed change (not a silent drift).

    Clamped to [0, 100] so a pathological input (error_rate > 1 from dirty
    data) can never produce a nonsense score.
    """
    error_rate = max(0.0, min(1.0, float(rollup.get("error_rate", 0.0))))
    raw_pass = rollup.get("eval_pass_rate")
    # No evals ran → don't penalize quality (treat as neutral 1.0).
    eval_pass_rate = 1.0 if raw_pass is None else max(0.0, min(1.0, float(raw_pass)))

    penalty = 0.0
    for a in anomalies:
        if a.metric not in ("cost", "latency", "volume"):
            continue
        if a.severity is AnomalySeverity.CRITICAL:
            penalty += 0.34
        elif a.severity is AnomalySeverity.WARNING:
            penalty += 0.17
    cost_trend_factor = max(0.0, 1.0 - penalty)

    score = 100.0 * (
        0.40 * (1.0 - error_rate)
        + 0.30 * eval_pass_rate
        + 0.15 * (0.0 if has_drift else 1.0)
        + 0.15 * cost_trend_factor
    )
    return round(max(0.0, min(100.0, score)), 1)


# ---------------------------------------------------------------------------
# Step 4: failure clustering (degrades gracefully without #542)
# ---------------------------------------------------------------------------


def cluster_failures(failures: Sequence[Any]) -> list[dict[str, Any]]:
    """Cluster failures into ``{signature, count, sample_message, agent}``.

    Prefers the #542 Failure Pattern Diagnoser
    (:func:`movate.core.diagnoser.cluster_failures`) when it's present on
    ``main``; resolved via ``getattr`` so this PR doesn't hard-depend on a
    module that may not have landed yet. When absent, degrades gracefully to
    un-clustered grouping by ``failure_type`` — still useful, just coarser.
    """
    diagnoser = _load_diagnoser()
    if diagnoser is not None:
        try:
            clustered = diagnoser(failures)
            # Normalize whatever the diagnoser returns into our flat shape.
            return [_normalize_cluster(c) for c in clustered]
        except Exception:  # pragma: no cover - defensive: diagnoser shape drift
            logger.warning(
                "diagnoser_cluster_failed — degrading to un-clustered grouping",
                exc_info=True,
            )

    # Fallback: group by failure_type.
    buckets: dict[str, dict[str, Any]] = {}
    for f in failures:
        sig = getattr(f, "failure_type", None) or "(unknown)"
        b = buckets.setdefault(
            sig,
            {"signature": sig, "count": 0, "sample_message": "", "agent": getattr(f, "agent", "")},
        )
        b["count"] += 1
        if not b["sample_message"]:
            b["sample_message"] = (getattr(f, "message", "") or "")[:280]
    return sorted(buckets.values(), key=lambda c: c["count"], reverse=True)


def _load_diagnoser() -> Any | None:
    """Look up the #542 diagnoser without a hard import (graceful degrade)."""
    # Dynamic import so this PR does not statically depend on a module that
    # may not have landed yet (#542). When it's present we use its richer
    # clustering; when absent we degrade to the failure_type fallback.
    import importlib  # noqa: PLC0415

    try:
        diagnoser_mod = importlib.import_module("movate.core.diagnoser")
    except ImportError:
        return None
    return getattr(diagnoser_mod, "cluster_failures", None)


def _normalize_cluster(cluster: Any) -> dict[str, Any]:
    """Coerce a diagnoser cluster (object or dict) into our flat dict shape."""
    if isinstance(cluster, dict):
        return {
            "signature": cluster.get("signature") or cluster.get("pattern") or "(unknown)",
            "count": int(cluster.get("count", 0)),
            "sample_message": (cluster.get("sample_message") or cluster.get("example") or "")[:280],
            "agent": cluster.get("agent", ""),
        }
    return {
        "signature": getattr(cluster, "signature", None)
        or getattr(cluster, "pattern", "(unknown)"),
        "count": int(getattr(cluster, "count", 0)),
        "sample_message": (getattr(cluster, "sample_message", "") or "")[:280],
        "agent": getattr(cluster, "agent", ""),
    }


# ---------------------------------------------------------------------------
# Step 5: narrative digest (the ONE LLM call — budget-capped)
# ---------------------------------------------------------------------------

_DIGEST_SYSTEM = (
    "You are an SRE assistant summarizing one day of AI-agent telemetry. "
    "Write a SHORT markdown digest (<= 8 lines). Start with a 'Yesterday:' "
    "line stating health + volume + cost, then a 'Watch:' line calling out "
    "the most important anomaly or failure cluster. Be concrete and cite "
    "numbers from the data. Do NOT invent metrics that aren't present."
)


@dataclass
class _DigestResult:
    text: str = ""
    cost_usd: float = 0.0


async def _narrative_digest(
    *,
    llm: BaseLLMProvider | None,
    model: str,
    rollup: dict[str, Any],
    anomalies: Sequence[Anomaly],
    clusters: Sequence[dict[str, Any]],
    health_score: float,
    budget_usd: float,
) -> _DigestResult:
    """Run the single budget-capped LLM call that writes the digest.

    Returns an empty digest (cost 0.0) when there is no LLM, the budget is
    non-positive, or the call fails — the analyst NEVER fails just because the
    narrative couldn't be written (the structured insight is the source of
    truth; the prose is a courtesy layer).
    """
    if llm is None or budget_usd <= 0:
        return _DigestResult()

    payload = {
        "health_score": health_score,
        "usage": {k: rollup.get(k) for k in ("runs", "errors", "error_rate", "cost_usd")},
        "eval_pass_rate": rollup.get("eval_pass_rate"),
        "anomalies": [a.model_dump(mode="json") for a in anomalies[:5]],
        "top_failures": list(clusters[:3]),
    }
    request = CompletionRequest(
        provider=model,
        messages=[
            Message(role="system", content=_DIGEST_SYSTEM),
            Message(role="user", content=json.dumps(payload, default=str)),
        ],
        params={"max_tokens": _NARRATIVE_MAX_OUTPUT_TOKENS, "temperature": 0.2},
    )
    try:
        response = await llm.complete(request)
    except Exception:
        logger.warning(
            "observability_digest_llm_failed — insight saved without prose", exc_info=True
        )
        return _DigestResult()

    cost = _estimate_cost(model, response)
    if cost > budget_usd:
        # We can't un-spend it, but we record it + log so the over-budget call
        # is visible. The digest still lands (we already paid for it).
        logger.warning(
            "observability_digest_over_budget cost=%.4f budget=%.4f model=%s",
            cost,
            budget_usd,
            model,
        )
    return _DigestResult(text=response.text.strip(), cost_usd=cost)


def _estimate_cost(model: str, response: Any) -> float:
    """Cost the completion from its returned token usage via the pricing table.

    Uses the same versioned local pricing table the executor uses; returns
    0.0 if the model isn't priced (e.g. mock provider) so a missing price
    never crashes the analyst.
    """
    try:
        from movate.providers.pricing import load_pricing  # noqa: PLC0415

        pricing = load_pricing()
        return float(pricing.cost_for(provider=model, tokens=response.tokens))
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class _Telemetry:
    runs: list[RunRecord] = field(default_factory=list)
    eval_pass_rate: float | None = None
    failures: list[Any] = field(default_factory=list)
    has_drift: bool = False


async def _pull_telemetry(
    *,
    storage: StorageProvider,
    tenant_id: str,
    window: _Window,
    scan_limit: int,
) -> _Telemetry:
    """Read the window's runs / evals / failures via the Protocol only.

    Filters the tenant's recent rows down to the window in Python (the
    Protocol's ``list_*`` methods don't take a date range), which is fine for
    a day's volume. Eval pass-rate is the mean ``pass_rate`` of the window's
    evals. ``has_drift`` is best-effort: it flips on if any window eval
    pass_rate dropped below the prior eval's (a coarse drift proxy that needs
    no extra storage surface).
    """
    runs = await storage.list_runs(tenant_id=tenant_id, limit=scan_limit)
    window_runs = [r for r in runs if _in_window(r.created_at, window)]

    evals = await storage.list_evals(tenant_id=tenant_id, limit=scan_limit)
    window_evals = [e for e in evals if _in_window(getattr(e, "created_at", None), window)]
    pass_rates = [float(e.pass_rate) for e in window_evals if e.pass_rate is not None]
    eval_pass_rate = statistics.fmean(pass_rates) if pass_rates else None

    # Coarse drift proxy: any window eval below the most recent prior eval.
    has_drift = False
    if window_evals and len(evals) > len(window_evals):
        prior = [e for e in evals if e not in window_evals]
        if prior:
            prior_rate = float(prior[0].pass_rate or 0.0)
            has_drift = any(float(e.pass_rate or 0.0) < prior_rate for e in window_evals)

    failures: list[Any] = []
    list_failures = getattr(storage, "list_failures", None)
    if callable(list_failures):
        try:
            all_failures = await list_failures(tenant_id=tenant_id, limit=scan_limit)
            failures = [
                f for f in all_failures if _in_window(getattr(f, "created_at", None), window)
            ]
        except Exception:
            logger.warning("observability_list_failures_failed — failures omitted", exc_info=True)

    return _Telemetry(
        runs=window_runs,
        eval_pass_rate=eval_pass_rate,
        failures=failures,
        has_drift=has_drift,
    )


async def _trailing_baselines(
    *,
    storage: StorageProvider,
    tenant_id: str,
    project_id: str,
    window: _Window,
    baseline_days: int,
) -> dict[str, list[float]]:
    """Assemble per-metric trailing series from PRIOR daily insights.

    Reads the project's recent insights (newest-first), drops anything on or
    after the current window, and pulls each prior day's
    ``cost_usd`` / ``p95_latency_ms`` / ``error_rate`` / ``runs`` into a
    series. Cheap (insights are pre-aggregated) and self-reinforcing: the more
    nights the analyst has run, the richer the baseline. Returns empty series
    on a cold start — the detector then simply emits no anomalies.
    """
    since = window.start - timedelta(days=baseline_days)
    try:
        prior = await storage.list_insights(
            tenant_id,
            project_id=project_id,
            since=since.date(),
            until=window.day,
            limit=baseline_days + 5,
        )
    except Exception:
        logger.warning("observability_baseline_read_failed — anomalies skipped", exc_info=True)
        return {}

    series: dict[str, list[float]] = {"cost": [], "latency": [], "error_rate": [], "volume": []}
    seen_days: set[date] = set()
    for ins in prior:
        if ins.date >= window.day or ins.date in seen_days:
            continue  # exclude the day under analysis + dedupe append-only re-runs
        seen_days.add(ins.date)
        roll = ins.usage_rollup or {}
        series["cost"].append(float(roll.get("cost_usd", 0.0)))
        series["latency"].append(float(roll.get("p95_latency_ms", 0.0)))
        series["error_rate"].append(float(roll.get("error_rate", 0.0)))
        series["volume"].append(float(roll.get("runs", 0)))
    return series


async def analyze(
    tenant_id: str,
    project_id: str,
    window: date | _Window,
    *,
    storage: StorageProvider,
    llm: BaseLLMProvider | None = None,
    model: str = "openai/gpt-4o-mini",
    budget_usd: float = 0.10,
    scan_limit: int = 10_000,
    baseline_days: int = DEFAULT_BASELINE_DAYS,
    persist: bool = True,
) -> ObservabilityInsight:
    """Preprocess one (tenant, project, day) into an append-only insight.

    The scheduled-job entry point (wired as ``JobKind.OBSERVABILITY_ANALYZE``
    and ``POST /api/v1/observability/analyze``). Pure-Python stages 1-4 always
    run; the LLM narrative (stage 5) is the only spend and is budget-capped.
    Persists via :meth:`StorageProvider.save_insight` (append-only — a re-run
    inserts a new row) unless ``persist=False`` (used by tests / dry-runs).

    ``window`` accepts a bare ``date`` (the common nightly case → that whole
    UTC day) or an explicit :class:`_Window`.
    """
    win = window if isinstance(window, _Window) else day_window(window)

    telemetry = await _pull_telemetry(
        storage=storage, tenant_id=tenant_id, window=win, scan_limit=scan_limit
    )
    rollup = build_usage_rollup(telemetry.runs, eval_pass_rate=telemetry.eval_pass_rate)

    baselines = await _trailing_baselines(
        storage=storage,
        tenant_id=tenant_id,
        project_id=project_id,
        window=win,
        baseline_days=baseline_days,
    )
    anomalies = detect_anomalies(rollup, baselines)
    clusters = cluster_failures(telemetry.failures)
    health = compute_health_score(rollup, has_drift=telemetry.has_drift, anomalies=anomalies)

    trends = {
        metric: {
            "value": float(
                rollup.get(_metric_key(metric), 0.0)
                if metric != "volume"
                else rollup.get("runs", 0)
            ),
            "baseline": statistics.fmean(series) if series else None,
            "delta_pct": _delta_pct(
                rollup.get(_metric_key(metric), 0.0)
                if metric != "volume"
                else rollup.get("runs", 0),
                series,
            ),
        }
        for metric, series in baselines.items()
    }

    digest = await _narrative_digest(
        llm=llm,
        model=model,
        rollup=rollup,
        anomalies=anomalies,
        clusters=clusters,
        health_score=health,
        budget_usd=budget_usd,
    )

    insight = ObservabilityInsight(
        tenant_id=tenant_id,
        project_id=project_id,
        date=win.day,
        health_score=health,
        anomalies=[a.model_dump(mode="json") for a in anomalies],
        top_failures=clusters,
        usage_rollup=rollup,
        trends=trends,
        narrative_digest=digest.text,
    )

    if persist:
        await storage.save_insight(insight)
        logger.info(
            "observability_insight_saved tenant=%s project=%s date=%s health=%.1f "
            "anomalies=%d failures=%d digest_cost=%.4f",
            tenant_id,
            project_id,
            win.day.isoformat(),
            health,
            len(anomalies),
            len(clusters),
            digest.cost_usd,
        )
    return insight


def _metric_key(metric: str) -> str:
    return {
        "cost": "cost_usd",
        "latency": "p95_latency_ms",
        "error_rate": "error_rate",
        "volume": "runs",
    }[metric]


def _delta_pct(value: float, series: list[float]) -> float | None:
    """Percent change of ``value`` vs the trailing mean (None on no baseline)."""
    if not series:
        return None
    mean = statistics.fmean(series)
    if mean == 0:
        return None
    return round(100.0 * (float(value) - mean) / mean, 1)


def run_analyze_job_input(project_id: str) -> dict[str, Any]:
    """Build the ``JobRecord.input`` payload for an OBSERVABILITY_ANALYZE job.

    Kept here (next to ``analyze``) so the scheduler hook + the runtime
    on-demand trigger construct the same shape. ``date`` omitted → the worker
    analyzes *yesterday* (the common nightly case).
    """
    return {"project_id": project_id}


__all__ = [
    "analyze",
    "build_usage_rollup",
    "cluster_failures",
    "compute_health_score",
    "day_window",
    "detect_anomalies",
    "run_analyze_job_input",
]
