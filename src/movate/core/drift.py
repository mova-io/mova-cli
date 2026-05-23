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
``pass_rate`` — so those are the "dimensions" drift watches. (Per-dimension
means, e.g. faithfulness vs. accuracy, are computed in-engine but not yet
persisted on ``EvalRecord``; once they are, this module extends to a
per-dim drift check with no caller change.) Detection is pure + side-effect
free so it's trivially unit-testable; alerting is wired separately at the
worker edge via :func:`alert_on_drift`.
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

    ``regressed`` is the headline boolean the worker alerts on.
    ``regressed_metrics`` names which metric(s) dropped past tolerance
    (``mean_score`` and/or ``pass_rate``) so the alert can be specific.
    All deltas are ``current - baseline`` (negative = a drop). When there
    is no baseline (first-ever eval for the agent), ``baseline`` is ``None``
    and ``regressed`` is ``False`` — no false alarm on a cold start.
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

    @property
    def has_baseline(self) -> bool:
        return self.baseline is not None

    def summary(self) -> str:
        """One-line operator-readable summary for logs / CLI."""
        if self.baseline is None:
            return (
                f"no baseline for {self.agent!r} — recording "
                f"{self.current.eval_id} as the first eval (no drift check)"
            )
        verdict = "REGRESSION" if self.regressed else "OK"
        ds = " [dataset changed]" if self.dataset_changed else ""
        return (
            f"{verdict} {self.agent!r}: mean_score Δ={self.mean_score_delta:+.4f} "
            f"pass_rate Δ={self.pass_rate_delta:+.4f} "
            f"(tolerance ±{self.tolerance:.2f}; baseline={self.baseline.eval_id} "
            f"score={self.baseline.mean_score:.4f} → {self.current.mean_score:.4f})"
            f"{ds}"
        )


def detect_drift(
    current: EvalRecord,
    baseline: EvalRecord | None,
    *,
    tolerance: float = 0.05,
) -> DriftResult:
    """Compare ``current`` against ``baseline``; return a :class:`DriftResult`.

    A regression fires when ``mean_score`` OR ``pass_rate`` drops by more
    than ``tolerance`` (absolute, in 0.0-1.0 score units). ``tolerance=0.0``
    means any drop is a regression; a small positive tolerance (default
    0.05) absorbs LLM-judge sampling noise on the scheduled path.

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

    return DriftResult(
        agent=current.agent,
        tolerance=tolerance,
        baseline=baseline,
        current=current,
        mean_score_delta=diff.mean_score_delta,
        pass_rate_delta=diff.pass_rate_delta,
        regressed=bool(regressed_metrics),
        regressed_metrics=regressed_metrics,
        dataset_changed=diff.dataset_changed,
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
    # Structured log event — stable key/value shape for log-based alerting.
    logger.warning(
        "eval_drift_detected agent=%s tenant=%s eval_id=%s baseline_eval_id=%s "
        "mean_score_baseline=%.6f mean_score_current=%.6f mean_score_delta=%+.6f "
        "pass_rate_baseline=%.6f pass_rate_current=%.6f pass_rate_delta=%+.6f "
        "tolerance=%.4f regressed_metrics=%s dataset_changed=%s",
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
            f"Regressed:  {', '.join(result.regressed_metrics)}\n"
        )
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
