"""``movate bench --baseline <bench_id>`` diff math.

Sister to :mod:`movate.core.baseline` (which handles eval baselines).
Bench baselines differ from eval baselines in shape:

* An ``EvalRecord`` has flat aggregates (``mean_score``, ``pass_rate``,
  etc.) → one delta per aggregate.
* A ``BenchRecord`` is **per-model**: each ``BenchModelRow`` carries
  its own score / cost / latency. The interesting diff is therefore
  per-model AND a roll-up at the top.

We compute one :class:`BenchModelDelta` per model that's present in
both baseline + current (matched by provider string). Models that
appear in only one side surface as ``added``/``removed`` sets — the
operator can decide whether to extend the baseline or accept the
divergence.

Regression detection follows the same model as eval: any model whose
score drops past ``--regression-tolerance`` is a regression. Cost +
latency deltas are surfaced for awareness but don't gate by default
(operators care about quality regression; cost regression is
typically tracked separately).
"""

from __future__ import annotations

from dataclasses import dataclass

from movate.core.models import BenchModelRow, BenchRecord


@dataclass(frozen=True)
class BenchModelDelta:
    """Per-model diff between two :class:`BenchModelRow` rows.

    All deltas are ``current - baseline``. Positive scores delta =
    improvement; positive cost / latency delta = regression (cost or
    latency went UP).
    """

    provider: str
    baseline: BenchModelRow
    current: BenchModelRow

    @property
    def score_delta(self) -> float | None:
        """``None`` when either side had no judge configured. We treat
        "no baseline score" as "no opinion" and skip score regression
        detection for that row."""
        if self.baseline.score is None or self.current.score is None:
            return None
        return round(self.current.score - self.baseline.score, 6)

    @property
    def cost_mean_delta(self) -> float:
        return round(self.current.cost_mean_usd - self.baseline.cost_mean_usd, 6)

    @property
    def latency_p50_delta(self) -> int:
        return self.current.latency_p50_ms - self.baseline.latency_p50_ms

    @property
    def latency_p95_delta(self) -> int:
        return self.current.latency_p95_ms - self.baseline.latency_p95_ms

    def is_regression(self, *, tolerance: float = 0.0) -> bool:
        """Score dropped by more than ``tolerance``. ``tolerance`` is in
        absolute score units (0.0-1.0)."""
        sd = self.score_delta
        if sd is None:
            return False
        return sd < -tolerance


@dataclass
class BenchBaselineDiff:
    """Full diff between two :class:`BenchRecord` rows.

    Composes per-model :class:`BenchModelDelta` instances + tracks
    models added or removed between baseline and current.
    """

    baseline: BenchRecord
    current: BenchRecord
    matched: list[BenchModelDelta]
    added: list[str]  # providers present in current, absent from baseline
    removed: list[str]  # providers present in baseline, absent from current

    @property
    def total_cost_delta(self) -> float:
        return round(self.current.total_cost_usd - self.baseline.total_cost_usd, 6)

    @property
    def input_changed(self) -> bool:
        """``True`` when the bench was run against a different input than
        the baseline. The diff math still works, but the comparison is
        less meaningful — surface this to the operator."""
        return self.baseline.input_hash != self.current.input_hash

    @property
    def baseline_age_seconds(self) -> float:
        return (self.current.created_at - self.baseline.created_at).total_seconds()

    def is_regression(self, *, tolerance: float = 0.0) -> bool:
        """Any model regressed past ``tolerance``."""
        return any(m.is_regression(tolerance=tolerance) for m in self.matched)

    def regressing_models(self, *, tolerance: float = 0.0) -> list[BenchModelDelta]:
        """The subset of matched models that regressed past tolerance.
        Empty list means no regression — useful for CI summaries."""
        return [m for m in self.matched if m.is_regression(tolerance=tolerance)]


def compute_bench_baseline_diff(baseline: BenchRecord, current: BenchRecord) -> BenchBaselineDiff:
    """Return a :class:`BenchBaselineDiff`. Asserts agent identity matches.

    Matching uses the ``provider`` string as the join key — same
    provider in baseline + current → matched delta. Missing on either
    side → in the ``added``/``removed`` lists. We don't try to match
    by tag/family or any fuzzy logic; if the operator changed
    providers they get a clean "removed/added" signal.
    """
    if baseline.agent != current.agent:
        raise ValueError(
            f"baseline agent {baseline.agent!r} differs from current "
            f"{current.agent!r}; comparing across agents is meaningless"
        )

    baseline_by_provider = {m.provider: m for m in baseline.models}
    current_by_provider = {m.provider: m for m in current.models}

    matched: list[BenchModelDelta] = []
    for provider, current_row in current_by_provider.items():
        if provider in baseline_by_provider:
            matched.append(
                BenchModelDelta(
                    provider=provider,
                    baseline=baseline_by_provider[provider],
                    current=current_row,
                )
            )

    added = sorted(set(current_by_provider) - set(baseline_by_provider))
    removed = sorted(set(baseline_by_provider) - set(current_by_provider))

    return BenchBaselineDiff(
        baseline=baseline,
        current=current,
        matched=matched,
        added=added,
        removed=removed,
    )


__all__ = [
    "BenchBaselineDiff",
    "BenchModelDelta",
    "compute_bench_baseline_diff",
]
