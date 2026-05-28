"""Failure-source adapter for the diagnoser (ADR 043 D1).

Pulls failures from the existing storage Protocol and normalizes them
into the :class:`movate.core.diagnoser.Failure` shape the diagnoser
consumes. Keeps the storage-touching code OUT of
:mod:`movate.core.diagnoser` so the diagnoser stays a pure transform
(testable with hand-built failure lists) and the storage Protocol
stays unchanged (no new methods for the diagnose phase).

Four signal streams, each gated by a flag in the
:class:`movate.runtime.schemas.DiagnoseRequest`:

* **failed runs** — ``RunRecord`` rows whose ``status`` is anything
  but ``success`` AND within the window. Pulled via the existing
  ``list_runs(agent=..., tenant_id=..., status=...)`` Protocol method;
  this is the primary signal source — non-success runs are what an
  operator usually wants to fix.

* **eval failures** — ``EvalRecord`` rows whose ``pass_rate < threshold``
  in the window. Persisted ``EvalRecord`` does NOT carry per-case
  detail (only aggregate ``pass_rate`` / ``mean_score``), so the
  diagnoser sees one Failure per failing eval RUN, not per failing
  CASE — a known schema constraint we flag in the PR.

* **drift detections** — derived from the eval history by running
  :func:`movate.core.drift.detect_drift` pairwise (latest vs. its
  predecessor) for each agent within the window. There is no
  dedicated drift-signal store in the codebase today; drift is
  *computed* on demand from ``EvalRecord`` deltas. This module mirrors
  that pattern — see ADR 016 D5.

* **canary misses** — pulled from the canary config + recent runs
  for the challenger version (a "miss" = a non-success run on the
  challenger). Best-effort: when no canary is configured the source
  contributes zero failures rather than raising.

Tenant scoping is enforced at the storage layer — every read passes
``tenant_id`` straight through; this module never touches another
tenant's rows.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from movate.core.diagnoser import Failure, FailureSource
from movate.core.drift import detect_drift, select_baseline
from movate.core.models import EvalRecord, JobStatus, RunRecord
from movate.storage.base import StorageProvider

logger = logging.getLogger(__name__)

# Bounded fetch caps — keep one diagnose call from scanning the entire
# table for a high-volume tenant. The diagnoser further caps the prompt
# at :data:`movate.core.diagnoser.MAX_FAILURES_PER_PROMPT`.
_RUN_FETCH_CAP = 500
_EVAL_FETCH_CAP = 50
# Threshold for marking an eval as a "failure" when the request asks
# for eval failures. Mirrors the EvalRecord.pass_rate scale (0-1).
# Conservative: anything < 1.0 has at least one case that missed.
DEFAULT_EVAL_PASS_RATE_FLOOR = 1.0
# Minimum eval history for drift computation: need at least one current
# eval + one baseline candidate. Module-level so the drift collector
# doesn't carry a function-local magic number.
_MIN_HISTORY_FOR_DRIFT = 2


async def collect_failures(
    storage: StorageProvider,
    *,
    agent: str,
    tenant_id: str,
    window_days: int = 30,
    include_runs: bool = True,
    include_eval_failures: bool = True,
    include_drift_detections: bool = True,
    include_canary_misses: bool = True,
    eval_pass_rate_floor: float = DEFAULT_EVAL_PASS_RATE_FLOOR,
) -> list[Failure]:
    """Pull recent failures from every enabled source into one flat list.

    Tenant-scoped + bounded: each source has its own fetch cap so a
    pathological tenant can't blow the diagnoser's prompt budget on a
    single source. The diagnoser is responsible for the final cap
    (``MAX_FAILURES_PER_PROMPT``).

    Returns an empty list when nothing matches in the window — the
    runtime's diagnose endpoint surfaces that as ``clusters: []`` with
    zero cost.
    """
    cutoff = datetime.now(UTC) - timedelta(days=max(1, window_days))
    failures: list[Failure] = []

    if include_runs:
        failures.extend(
            await _collect_failed_runs(
                storage,
                agent=agent,
                tenant_id=tenant_id,
                cutoff=cutoff,
            )
        )

    if include_eval_failures:
        failures.extend(
            await _collect_eval_failures(
                storage,
                agent=agent,
                tenant_id=tenant_id,
                cutoff=cutoff,
                pass_rate_floor=eval_pass_rate_floor,
            )
        )

    if include_drift_detections:
        failures.extend(
            await _collect_drift_detections(
                storage,
                agent=agent,
                tenant_id=tenant_id,
                cutoff=cutoff,
            )
        )

    if include_canary_misses:
        failures.extend(
            await _collect_canary_misses(
                storage,
                agent=agent,
                tenant_id=tenant_id,
                cutoff=cutoff,
            )
        )

    return failures


# ---------------------------------------------------------------------------
# Per-source pulls
# ---------------------------------------------------------------------------


async def _collect_failed_runs(
    storage: StorageProvider,
    *,
    agent: str,
    tenant_id: str,
    cutoff: datetime,
) -> list[Failure]:
    """Failed runs in the window — every status that isn't ``success``.

    Iterates over the non-success statuses one by one because the
    storage Protocol's ``list_runs`` filter takes a single status
    value (a small constraint of the existing surface — flagged in the
    PR description). Each call is tenant-scoped and capped.
    """
    failures: list[Failure] = []
    non_success = (
        JobStatus.ERROR,
        JobStatus.SAFETY_BLOCKED,
        JobStatus.DEAD_LETTER,
        JobStatus.CANCELLED,
    )
    seen_run_ids: set[str] = set()
    for status in non_success:
        runs = await storage.list_runs(
            agent=agent,
            tenant_id=tenant_id,
            status=status.value,
            limit=_RUN_FETCH_CAP,
        )
        for r in runs:
            if r.created_at < cutoff:
                continue
            if r.run_id in seen_run_ids:
                continue
            seen_run_ids.add(r.run_id)
            failures.append(_run_to_failure(r))
    return failures


def _run_to_failure(r: RunRecord) -> Failure:
    summary = (
        f"run {r.run_id[:12]} status={r.status.value}{' error=' + r.error.type if r.error else ''}"
    )
    return Failure(
        id=r.run_id,
        source=FailureSource.RUN,
        summary=summary,
        created_at=r.created_at,
        input=r.input,
        output=r.output,
        error=(f"{r.error.type}: {r.error.message}" if r.error else None),
        extra={
            "status": r.status.value,
            "agent_version": r.agent_version,
            "provider": r.provider,
            "latency_ms": r.metrics.latency_ms,
            "cost_usd": r.metrics.cost_usd,
        },
    )


async def _collect_eval_failures(
    storage: StorageProvider,
    *,
    agent: str,
    tenant_id: str,
    cutoff: datetime,
    pass_rate_floor: float,
) -> list[Failure]:
    """Eval-run records whose ``pass_rate < pass_rate_floor`` in the window.

    Schema constraint: persisted :class:`EvalRecord` carries aggregates
    only — no per-case failures. So we surface one Failure per failing
    eval RUN, not per failing CASE. The cluster summary will mention
    aggregate metrics; a deeper drill-down would require ADR 043 to
    extend EvalRecord with case-level persistence (out of scope here).
    """
    evals = await storage.list_evals(agent=agent, tenant_id=tenant_id, limit=_EVAL_FETCH_CAP)
    failures: list[Failure] = []
    for e in evals:
        if e.created_at < cutoff:
            continue
        if e.pass_rate >= pass_rate_floor:
            continue
        failures.append(_eval_to_failure(e))
    return failures


def _eval_to_failure(e: EvalRecord) -> Failure:
    summary = (
        f"eval {e.eval_id[:12]} pass_rate={e.pass_rate:.3f} "
        f"mean={e.mean_score:.3f} (n={e.sample_count})"
    )
    return Failure(
        id=e.eval_id,
        source=FailureSource.EVAL,
        summary=summary,
        created_at=e.created_at,
        input={},
        output=None,
        error=(
            f"pass_rate {e.pass_rate:.3f} below threshold "
            f"{e.threshold:.3f}; mean_score {e.mean_score:.3f}"
        ),
        extra={
            "agent_version": e.agent_version,
            "dataset_hash": e.dataset_hash,
            "sample_count": e.sample_count,
            "threshold": e.threshold,
            "dimension_means": e.dimension_means,
        },
    )


async def _collect_drift_detections(
    storage: StorageProvider,
    *,
    agent: str,
    tenant_id: str,
    cutoff: datetime,
) -> list[Failure]:
    """Compute drift detections in the window.

    No dedicated drift-signal store exists today (ADR 016 D5 detects
    drift on the fly from ``EvalRecord`` history). We mirror that
    pattern here: walk the agent's eval history newest-first, run
    :func:`detect_drift` for each eval vs. its baseline (the prior
    eval), and surface every ``regressed=True`` outcome as one
    Failure. Best-effort: a drift computation failure logs and skips
    rather than failing the whole diagnose.
    """
    evals = await storage.list_evals(agent=agent, tenant_id=tenant_id, limit=_EVAL_FETCH_CAP)
    if len(evals) < _MIN_HISTORY_FOR_DRIFT:
        return []

    failures: list[Failure] = []
    for current in evals:
        if current.created_at < cutoff:
            continue
        baseline = select_baseline(current=current, candidates=evals, baseline_id=None)
        if baseline is None:
            continue
        try:
            result = detect_drift(current, baseline, tolerance=0.05)
        except Exception:
            logger.warning(
                "diagnoser_drift_compute_failed eval_id=%s agent=%s",
                current.eval_id,
                agent,
                exc_info=True,
            )
            continue
        if not result.regressed:
            continue
        failures.append(_drift_to_failure(current, result))
    return failures


def _drift_to_failure(current: EvalRecord, result: object) -> Failure:
    # ``result`` is a :class:`movate.core.drift.DriftResult`; we read
    # only its public summary + headline deltas so this module doesn't
    # have to pin the DriftResult import shape across drift evolutions.
    deltas: dict[str, float] = {
        "mean_score": getattr(result, "mean_score_delta", 0.0),
        "pass_rate": getattr(result, "pass_rate_delta", 0.0),
    }
    dim_deltas = getattr(result, "dimension_deltas", {}) or {}
    if isinstance(dim_deltas, dict):
        deltas.update({str(k): float(v) for k, v in dim_deltas.items()})

    summary = f"drift on {current.eval_id[:12]}: {getattr(result, 'summary', lambda: '')()}"
    return Failure(
        id=current.eval_id,
        source=FailureSource.DRIFT,
        summary=summary,
        created_at=current.created_at,
        input={},
        output=None,
        error="; ".join(f"{m} Δ={d:+.4f}" for m, d in deltas.items()),
        extra={
            "agent_version": current.agent_version,
            "regressed_metrics": list(getattr(result, "regressed_metrics", []) or []),
            "regressed_dimensions": list(getattr(result, "regressed_dimensions", []) or []),
            "deltas": deltas,
        },
    )


async def _collect_canary_misses(
    storage: StorageProvider,
    *,
    agent: str,
    tenant_id: str,
    cutoff: datetime,
) -> list[Failure]:
    """Failed runs on the canary CHALLENGER version.

    No-op when no canary is configured for ``(agent, tenant_id)``
    (the overwhelming common case). When a canary IS configured, the
    challenger's failed runs are surfaced separately with
    :data:`FailureSource.CANARY` so a cluster's example mix tells an
    operator whether the issue is canary-specific or fleet-wide.

    Best-effort: storage / canary-resolver errors log and return ``[]``
    rather than failing the whole diagnose.
    """
    try:
        config = await storage.get_canary_config(agent, tenant_id=tenant_id)
    except Exception:
        logger.warning(
            "diagnoser_canary_lookup_failed agent=%s tenant=%s",
            agent,
            tenant_id,
            exc_info=True,
        )
        return []
    if config is None:
        return []
    challenger_version = getattr(config, "challenger_version", None)
    if not challenger_version:
        return []

    failures: list[Failure] = []
    non_success = (JobStatus.ERROR, JobStatus.SAFETY_BLOCKED, JobStatus.DEAD_LETTER)
    seen: set[str] = set()
    for status in non_success:
        runs = await storage.list_runs(
            agent=agent,
            tenant_id=tenant_id,
            status=status.value,
            limit=_RUN_FETCH_CAP,
        )
        for r in runs:
            if r.created_at < cutoff:
                continue
            if r.agent_version != challenger_version:
                continue
            if r.run_id in seen:
                continue
            seen.add(r.run_id)
            f = _run_to_failure(r)
            failures.append(
                Failure(
                    id=f.id,
                    source=FailureSource.CANARY,
                    summary=f"canary {f.summary}",
                    created_at=f.created_at,
                    input=f.input,
                    output=f.output,
                    error=f.error,
                    extra={**f.extra, "canary_version": challenger_version},
                )
            )
    return failures
