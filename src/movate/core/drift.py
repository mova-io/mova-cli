"""Eval drift detection — compare a fresh eval against a baseline (ADR 016 D2).

The continuous-eval loop runs the eval suite on a cadence (see
:mod:`movate.core.scheduler`), persists an :class:`EvalRecord`, then asks:
*did quality regress vs. a baseline?* This module is the comparator.

It builds on :mod:`movate.core.baseline` (the existing aggregate
:class:`BaselineDiff`) and adds:

* a structured :class:`DriftResult` (``regressed`` + per-metric deltas +
  which metrics regressed) that the worker can log + alert on;
* a baseline-selection helper that picks the **prior eval for the agent**
  when no baseline is pinned — the simplest durable baseline (ADR 016 D2:
  "the prior eval for that agent or a designated baseline eval_id").

``EvalRecord`` persists two aggregate quality metrics — ``mean_score`` and
``pass_rate`` — which drift always watches. As of item 24 it *also* persists
per-dimension means (``EvalRecord.dimension_means``, e.g. faithfulness vs.
accuracy vs. safety). When BOTH the current and baseline records carry that
map, :func:`detect_drift` additionally compares each *shared* dimension and
flags a **per-dimension regression** when any one drops past ``tolerance`` —
catching a single-dimension quality slide that holds the aggregate steady.
When either record lacks ``dimension_means`` (a legacy / exact-match eval),
the per-dimension check is skipped and behaviour is byte-for-byte the
aggregate-only path. Detection is pure + side-effect free so it's trivially
unit-testable; alerting is wired separately at the worker edge via
:func:`alert_on_drift`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from movate.core.baseline import BaselineDiff, compute_baseline_diff
from movate.core.models import EvalRecord

if TYPE_CHECKING:
    from movate.core.notify import NotificationDispatcher

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DriftResult:
    """Structured outcome of comparing one eval against a baseline.

    ``regressed`` is the headline boolean the worker alerts on. It is ``True``
    when EITHER the aggregate check (``mean_score`` / ``pass_rate``) OR any
    per-dimension check (item 24) regresses past ``tolerance``.
    ``regressed_metrics`` names which aggregate metric(s) dropped
    (``mean_score`` and/or ``pass_rate``) so the alert can be specific.
    All deltas are ``current - baseline`` (negative = a drop). When there
    is no baseline (first-ever eval for the agent), ``baseline`` is ``None``
    and ``regressed`` is ``False`` — no false alarm on a cold start.

    Per-dimension fields (item 24, additive — empty for legacy records or when
    either side lacks ``dimension_means``):

    * ``dimension_deltas`` — ``{dim: current - baseline}`` over the dimensions
      *both* records scored.
    * ``regressed_dimensions`` — the subset of ``dimension_deltas`` that
      dropped past ``tolerance`` (the dim names, sorted worst-first).
    """

    agent: str
    tolerance: float
    baseline: EvalRecord | None
    current: EvalRecord
    mean_score_delta: float = 0.0
    pass_rate_delta: float = 0.0
    regressed: bool = False
    regressed_metrics: list[str] = field(default_factory=list)
    dataset_changed: bool = False
    dimension_deltas: dict[str, float] = field(default_factory=dict)
    regressed_dimensions: list[str] = field(default_factory=list)

    @property
    def has_baseline(self) -> bool:
        return self.baseline is not None

    @property
    def worst_dimension(self) -> str | None:
        """Name of the dimension that dropped the most past tolerance, if any.

        ``None`` when no per-dimension regression fired. Drives the
        operator-facing summary + alert so the single most damaging
        dimension is called out by name.
        """
        if not self.regressed_dimensions:
            return None
        return min(self.regressed_dimensions, key=lambda d: self.dimension_deltas.get(d, 0.0))

    def summary(self) -> str:
        """One-line operator-readable summary for logs / CLI."""
        if self.baseline is None:
            return (
                f"no baseline for {self.agent!r} — recording "
                f"{self.current.eval_id} as the first eval (no drift check)"
            )
        verdict = "REGRESSION" if self.regressed else "OK"
        ds = " [dataset changed]" if self.dataset_changed else ""
        # item 24: name the worst-regressing dimension when a per-dimension
        # check fired — the aggregate Δs can look fine while one dim slid.
        worst = self.worst_dimension
        dim = ""
        if worst is not None:
            dim = f" worst dim={worst} Δ={self.dimension_deltas[worst]:+.4f}"
        return (
            f"{verdict} {self.agent!r}: mean_score Δ={self.mean_score_delta:+.4f} "
            f"pass_rate Δ={self.pass_rate_delta:+.4f} "
            f"(tolerance ±{self.tolerance:.2f}; baseline={self.baseline.eval_id} "
            f"score={self.baseline.mean_score:.4f} → {self.current.mean_score:.4f})"
            f"{dim}{ds}"
        )


def _compute_dimension_drift(
    current: EvalRecord,
    baseline: EvalRecord,
    *,
    tolerance: float,
) -> tuple[dict[str, float], list[str]]:
    """Per-dimension drift over the dimensions *both* records scored (item 24).

    Returns ``(dimension_deltas, regressed_dimensions)``:

    * ``dimension_deltas`` — ``{dim: round(current - baseline, 6)}`` for every
      dimension present in *both* ``dimension_means`` maps. A dimension only
      one side scored is skipped — a delta against a missing baseline is
      meaningless, and a dataset that simply added a dimension shouldn't read
      as a regression.
    * ``regressed_dimensions`` — the subset whose delta dropped past
      ``tolerance`` (same semantics as the aggregate check: ``delta <
      -tolerance``), sorted worst-first.

    When either record lacks ``dimension_means`` (``None``) both outputs are
    empty → the caller's ``regressed`` decision reduces to the aggregate-only
    path, byte-for-byte the pre-item-24 behaviour.
    """
    cur_means = current.dimension_means
    base_means = baseline.dimension_means
    if not cur_means or not base_means:
        return {}, []

    shared = cur_means.keys() & base_means.keys()
    deltas = {dim: round(cur_means[dim] - base_means[dim], 6) for dim in shared}
    regressed = sorted(
        (dim for dim, delta in deltas.items() if delta < -tolerance),
        key=lambda d: deltas[d],
    )
    return deltas, regressed


def detect_drift(
    current: EvalRecord,
    baseline: EvalRecord | None,
    *,
    tolerance: float = 0.05,
) -> DriftResult:
    """Compare ``current`` against ``baseline``; return a :class:`DriftResult`.

    A regression fires when ``mean_score`` OR ``pass_rate`` drops by more
    than ``tolerance`` (absolute, in 0.0-1.0 score units), OR — as of item 24
    — when any *per-dimension* mean both records scored drops past the same
    ``tolerance``. ``tolerance=0.0`` means any drop is a regression; a small
    positive tolerance (default 0.05) absorbs LLM-judge sampling noise on the
    scheduled path.

    The per-dimension check is skipped (empty deltas, no per-dim regression)
    whenever either record lacks ``dimension_means`` — so a legacy /
    exact-match eval falls back to aggregate-only, byte-for-byte the old
    behaviour. This lets a single-dimension regression (e.g. faithfulness
    slid while the aggregate held steady) trip the same drift → alert →
    (opt-in) rollback path the aggregate metrics already use.

    ``baseline=None`` (no prior eval) → ``regressed=False`` and empty
    ``regressed_metrics`` — a cold start never alerts.
    """
    if baseline is None:
        return DriftResult(
            agent=current.agent,
            tolerance=tolerance,
            baseline=None,
            current=current,
        )

    diff: BaselineDiff = compute_baseline_diff(baseline, current)
    regressed_metrics: list[str] = []
    if diff.mean_score_delta < -tolerance:
        regressed_metrics.append("mean_score")
    if diff.pass_rate_delta < -tolerance:
        regressed_metrics.append("pass_rate")

    dimension_deltas, regressed_dimensions = _compute_dimension_drift(
        current, baseline, tolerance=tolerance
    )

    return DriftResult(
        agent=current.agent,
        tolerance=tolerance,
        baseline=baseline,
        current=current,
        mean_score_delta=diff.mean_score_delta,
        pass_rate_delta=diff.pass_rate_delta,
        # item 24: regress on EITHER an aggregate metric OR any per-dimension
        # drop. Empty regressed_dimensions (legacy / no shared dims) leaves
        # this equal to the aggregate-only result.
        regressed=bool(regressed_metrics) or bool(regressed_dimensions),
        regressed_metrics=regressed_metrics,
        dataset_changed=diff.dataset_changed,
        dimension_deltas=dimension_deltas,
        regressed_dimensions=regressed_dimensions,
    )


def select_baseline(
    *,
    current: EvalRecord,
    candidates: list[EvalRecord],
    baseline_id: str | None = None,
) -> EvalRecord | None:
    """Pick the baseline ``current`` should be compared against.

    ``candidates`` is the agent's eval history (e.g. ``list_evals(agent=...)``),
    which may or may not include ``current`` itself.

    Selection (ADR 016 D2):

    1. If ``baseline_id`` is pinned, use that exact eval (never ``current``).
    2. Otherwise the **prior eval for this agent** — the newest candidate
       that isn't ``current`` and is strictly older than ``current``.
    3. If neither exists (first eval), ``None`` — no drift check.

    Comparing only against the same agent keeps the diff meaningful;
    ``compute_baseline_diff`` also asserts agent identity as a guard.
    """
    same_agent = [c for c in candidates if c.agent == current.agent]

    if baseline_id is not None:
        return next(
            (c for c in same_agent if c.eval_id == baseline_id and c.eval_id != current.eval_id),
            None,
        )

    prior = [
        c for c in same_agent if c.eval_id != current.eval_id and c.created_at <= current.created_at
    ]
    if not prior:
        return None
    # Newest of the priors (defends against unordered candidate lists).
    return max(prior, key=lambda c: c.created_at)


async def alert_on_drift(
    result: DriftResult,
    *,
    notifier: NotificationDispatcher | None = None,
    notify_email: str | None = None,
) -> bool:
    """Fire a drift alert when ``result.regressed`` — log event + notifier.

    Always emits a structured ``eval_drift_detected`` log event on a
    regression (so a log-based alerting pipeline catches it even without
    SMTP). When a ``notifier`` is supplied, also dispatches an alert via
    :meth:`NotificationDispatcher.notify_alert` (Console logs the intent;
    SMTP delivers if ``notify_email`` is set). Never raises — alerting is
    courtesy, the persisted ``EvalRecord`` is the source of truth.

    Returns ``True`` iff an alert was fired (i.e. a regression occurred).
    No regression (or no baseline) → no alert, returns ``False``.
    """
    if not result.regressed or result.baseline is None:
        return False

    baseline = result.baseline
    current = result.current
    # item 24: append the per-dimension regression detail additively — the
    # existing key/value pairs are unchanged so log-based alerting that parses
    # the old shape keeps working; ``regressed_dimensions`` is empty for
    # legacy / aggregate-only regressions.
    regressed_dims = ",".join(
        f"{dim}{result.dimension_deltas.get(dim, 0.0):+.6f}" for dim in result.regressed_dimensions
    )
    # Structured log event — stable key/value shape for log-based alerting.
    logger.warning(
        "eval_drift_detected agent=%s tenant=%s eval_id=%s baseline_eval_id=%s "
        "mean_score_baseline=%.6f mean_score_current=%.6f mean_score_delta=%+.6f "
        "pass_rate_baseline=%.6f pass_rate_current=%.6f pass_rate_delta=%+.6f "
        "tolerance=%.4f regressed_metrics=%s regressed_dimensions=%s dataset_changed=%s",
        result.agent,
        current.tenant_id,
        current.eval_id,
        baseline.eval_id,
        baseline.mean_score,
        current.mean_score,
        result.mean_score_delta,
        baseline.pass_rate,
        current.pass_rate,
        result.pass_rate_delta,
        result.tolerance,
        ",".join(result.regressed_metrics),
        regressed_dims,
        result.dataset_changed,
    )

    if notifier is not None:
        subject = f"[movate] eval drift — {result.agent} regressed"
        body = (
            f"Scheduled eval for agent {result.agent!r} regressed past tolerance "
            f"±{result.tolerance:.2f}.\n\n"
            f"New eval:   {current.eval_id} "
            f"(mean_score={current.mean_score:.4f}, pass_rate={current.pass_rate:.4f})\n"
            f"Baseline:   {baseline.eval_id} "
            f"(mean_score={baseline.mean_score:.4f}, pass_rate={baseline.pass_rate:.4f})\n"
            f"Delta:      mean_score {result.mean_score_delta:+.4f}, "
            f"pass_rate {result.pass_rate_delta:+.4f}\n"
            f"Regressed:  {', '.join(result.regressed_metrics) or '(no aggregate metric)'}\n"
        )
        # item 24: call out the per-dimension regressions when any fired. The
        # aggregate metrics above can read fine while one quality dimension
        # (faithfulness, coverage, safety, …) silently slid — that's the case
        # this surfaces, worst dimension first.
        if result.regressed_dimensions:
            dim_lines = "\n".join(
                f"  - {dim}: {result.dimension_deltas[dim]:+.4f}"
                for dim in result.regressed_dimensions
            )
            body += f"Dimensions: regressed past tolerance —\n{dim_lines}\n"
        if result.dataset_changed:
            body += "\nNote: the dataset hash changed since the baseline.\n"
        body += "\n— movate continuous eval (ADR 016 D2)\n"
        try:
            await notifier.notify_alert(subject=subject, body=body, email=notify_email)
        except Exception:
            # Never let an alert sink the worker.
            logger.warning(
                "drift_alert_dispatch_raised agent=%s eval_id=%s",
                result.agent,
                current.eval_id,
                exc_info=True,
            )

    return True


__all__ = [
    "DriftResult",
    "alert_on_drift",
    "detect_drift",
    "select_baseline",
]
