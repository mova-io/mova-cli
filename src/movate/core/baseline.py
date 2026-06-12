"""Eval baseline diff — compare a current eval run against a stored baseline.

Closes the regression-detection loop. Workflow:

1. ``movate eval ./agent`` runs the eval suite and persists an
   :class:`EvalRecord` with a fresh ``eval_id``.
2. Engineer reads the ``eval_id`` from the CLI output (printed in both
   the Rich table and the JSON dump).
3. After making code / prompt / model changes, run
   ``movate eval ./agent --baseline <eval_id>``.
4. Diff is rendered alongside the new eval; CLI exits non-zero when the
   current run regressed past the configured tolerance.

v0.4 ships aggregate diff (mean_score, pass_rate, sample_count). Per-case
diff requires per-case persistence and lands in v0.4.1+ when datasets are
big enough that aggregate isn't enough.

Dataset hash mismatches are flagged but never fail the gate — adding a
new eval case is a normal workflow.
"""

from __future__ import annotations

from dataclasses import dataclass

from movate.core.models import EvalRecord


@dataclass
class BaselineDiff:
    """Aggregate diff between two :class:`EvalRecord` rows.

    All deltas are ``current - baseline`` (positive = improvement).
    """

    baseline: EvalRecord
    current: EvalRecord

    @property
    def mean_score_delta(self) -> float:
        return round(self.current.mean_score - self.baseline.mean_score, 6)

    @property
    def pass_rate_delta(self) -> float:
        return round(self.current.pass_rate - self.baseline.pass_rate, 6)

    @property
    def sample_count_delta(self) -> int:
        return self.current.sample_count - self.baseline.sample_count

    @property
    def cost_delta(self) -> float:
        return round(self.current.total_cost_usd - self.baseline.total_cost_usd, 6)

    @property
    def dataset_changed(self) -> bool:
        return self.baseline.dataset_hash != self.current.dataset_hash

    @property
    def prompt_changed(self) -> bool | None:
        """Whether the prompt template changed between baseline and current.

        ``None`` when either side predates ADR 102 (no ``prompt_hash``
        persisted) — rendered as "unknown", never guessed. Informational
        only: like ``dataset_changed``, it never enters
        :meth:`is_regression` — changing a prompt and regressing is
        exactly the case the gate must keep failing.
        """
        if self.baseline.prompt_hash is None or self.current.prompt_hash is None:
            return None
        return self.baseline.prompt_hash != self.current.prompt_hash

    @property
    def baseline_age_seconds(self) -> float:
        return (self.current.created_at - self.baseline.created_at).total_seconds()

    def is_regression(self, *, tolerance: float = 0.0) -> bool:
        """True iff mean_score or pass_rate dropped by more than ``tolerance``.

        ``tolerance`` is in absolute score units (0.0-1.0). Default 0.0
        means any drop counts as a regression - strict CI default.
        Bumping tolerance up to ~0.05 is a reasonable knob for noisy
        LLM-as-judge runs.
        """
        if self.mean_score_delta < -tolerance:
            return True
        return self.pass_rate_delta < -tolerance


def compute_baseline_diff(baseline: EvalRecord, current: EvalRecord) -> BaselineDiff:
    """Return a :class:`BaselineDiff`. Asserts agent identity matches."""
    if baseline.agent != current.agent:
        raise ValueError(
            f"baseline agent {baseline.agent!r} differs from current "
            f"{current.agent!r}; comparing across agents is meaningless"
        )
    return BaselineDiff(baseline=baseline, current=current)


def format_delta(value: float, *, percent: bool = False) -> str:
    """Render ``value`` as a signed string with a colour-friendly hint.

    Returned strings carry no Rich markup — callers decide how to dress
    the colour. Format is ``+0.123`` / ``-0.045`` / ``0.000``.
    """
    if percent:
        return f"{value * 100:+.1f}%"
    return f"{value:+.4f}"


def regression_summary(diff: BaselineDiff, *, tolerance: float) -> str:
    """One-line summary suitable for CI logs."""
    changed = diff.prompt_changed
    changed_word = "unknown" if changed is None else ("yes" if changed else "no")
    prompt_note = f" prompt_changed={changed_word}"
    if diff.is_regression(tolerance=tolerance):
        return (
            f"REGRESSION mean_score Δ={format_delta(diff.mean_score_delta)} "
            f"pass_rate Δ={format_delta(diff.pass_rate_delta)} "
            f"(tolerance ±{tolerance:.2f})" + prompt_note
        )
    return (
        f"OK mean_score Δ={format_delta(diff.mean_score_delta)} "
        f"pass_rate Δ={format_delta(diff.pass_rate_delta)}" + prompt_note
    )


__all__ = [
    "BaselineDiff",
    "compute_baseline_diff",
    "format_delta",
    "regression_summary",
]
