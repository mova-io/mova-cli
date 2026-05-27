"""Session cost budgeting for the authoring copilot/autopilot (D7e, #136).

The planner and the "improve my agent" autopilot make LLM calls (proposals).
Each call has a real monetary cost — derived from the provider's reported
:class:`~movate.core.models.TokenUsage` against the **canonical** packaged
pricing table (:mod:`movate.providers.pricing`), exactly as ADR 024 derives
per-call ``cost_usd`` everywhere else. This module accumulates that cost across
one ``mdk dev`` / autopilot session and enforces an optional **budget cap**:

* a warning is printed as cumulative spend approaches the cap, and
* the *next* LLM call is refused (raising :class:`BudgetExceededError`) once the
  cap is reached — the planner checks the budget **before** it calls the model,
  so enforcement never leaves a half-applied action: it stops at a call
  boundary, before any propose/apply work begins.

The accumulator is a control-plane object (``cli`` ⊥ ``runtime``): it never
reaches into the runtime store. It is a plain, dependency-free value object that
the planner records into and the CLI surfaces.

With **no cap set** (the default) behavior is unchanged: the tracker still
accumulates cost (so the session total can be shown) but never refuses a call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from movate.core.models import TokenUsage
    from movate.providers.pricing import PricingTable

# Default fraction of the cap at which an approaching-budget warning fires.
DEFAULT_WARN_FRACTION = 0.8


class BudgetExceededError(Exception):
    """Raised when an LLM call would run after the session budget is spent.

    The planner raises this **before** making the model call (and before any
    apply), so the session stops cleanly at a call boundary — no half-applied
    action, no partial spend past the cap. The copilot catches it and reports a
    clear message rather than crashing the ``mdk dev`` loop.
    """


@dataclass
class CostBudget:
    """An optional cap on cumulative LLM spend within one authoring session.

    Attributes
    ----------
    cap_usd:
        The hard cap in USD. ``None`` (the default) means **no cap** — the
        session never refuses a call; the tracker still accumulates for
        reporting.
    warn_fraction:
        Fraction of ``cap_usd`` at which an approaching-budget warning is
        emitted (default 0.8 → warn at 80%). Ignored when ``cap_usd`` is None.
    """

    cap_usd: float | None = None
    warn_fraction: float = DEFAULT_WARN_FRACTION

    def __post_init__(self) -> None:
        if self.cap_usd is not None and self.cap_usd < 0:
            raise ValueError("budget cap_usd must be >= 0")
        if not (0.0 < self.warn_fraction <= 1.0):
            raise ValueError("budget warn_fraction must be in (0, 1]")

    @property
    def enabled(self) -> bool:
        """True when a cap is set (enforcement active)."""
        return self.cap_usd is not None


def cost_of_tokens(pricing: PricingTable, *, provider: str, tokens: TokenUsage) -> float:
    """Best-effort per-call cost from token usage via the canonical pricing table.

    Reuses :meth:`PricingTable.cost_for` (the same surface the executor and the
    eval/reflection paths use — ADR 024). A model missing from the table (a
    ``KeyError``) yields ``0.0`` rather than raising: an unknown price must not
    crash the copilot, it just isn't counted against the budget (and is noted by
    the caller). This mirrors how :mod:`movate.core.reflection` degrades.
    """
    try:
        return pricing.cost_for(provider=provider, tokens=tokens)
    except Exception:
        return 0.0


@dataclass
class SessionCostTracker:
    """Accumulates LLM cost across a session and enforces an optional cap.

    The planner calls :meth:`check_before_call` *before* each model call (it
    raises :class:`BudgetExceededError` when the cap is already spent) and
    :meth:`record` *after*, with the call's derived ``cost_usd``. The CLI reads
    :attr:`total_usd` / :attr:`calls` to surface session spend.

    Construct with ``budget=CostBudget()`` (no cap) for the default,
    backward-compatible behavior: it accumulates but never refuses.
    """

    budget: CostBudget = field(default_factory=CostBudget)
    total_usd: float = 0.0
    calls: int = 0
    _warned: bool = field(default=False, repr=False)

    def remaining_usd(self) -> float | None:
        """USD left before the cap, or ``None`` when no cap is set."""
        if self.budget.cap_usd is None:
            return None
        return max(0.0, self.budget.cap_usd - self.total_usd)

    def would_exceed(self) -> bool:
        """True when the cap is set and already met/exceeded (next call refused)."""
        return self.budget.cap_usd is not None and self.total_usd >= self.budget.cap_usd

    def check_before_call(self) -> None:
        """Refuse the next LLM call once the cap is spent (raise before calling).

        Called by the planner immediately before a model call. When the budget
        is already exhausted this raises :class:`BudgetExceededError` so the
        session stops at the call boundary — nothing is proposed or applied.
        A no-op when no cap is set.
        """
        if self.would_exceed():
            cap = self.budget.cap_usd
            raise BudgetExceededError(
                f"session LLM budget exhausted: spent ${self.total_usd:.4f} of the "
                f"${cap:.4f} cap across {self.calls} call(s). Raise the cap "
                f"(--budget / `mdk config set copilot.budget_usd`) or start a new session."
            )

    def record(self, cost_usd: float) -> None:
        """Add a completed call's cost to the running total.

        Non-negative costs only; a negative is clamped to 0 (defensive — a
        pricing miscompute must not let spend run backwards). Does NOT enforce
        the cap itself: enforcement is at :meth:`check_before_call`, so an
        already-started call always completes (no half-applied action).
        """
        self.total_usd += max(0.0, cost_usd)
        self.calls += 1

    def approaching_cap(self) -> bool:
        """True the FIRST time spend crosses the warn threshold (one-shot).

        Lets the CLI print an approaching-budget warning exactly once as the
        session nears the cap, rather than on every call. Always False when no
        cap is set.
        """
        cap = self.budget.cap_usd
        if cap is None or cap <= 0:
            return False
        if self._warned:
            return False
        if self.total_usd >= cap * self.budget.warn_fraction:
            self._warned = True
            return True
        return False


__all__ = [
    "DEFAULT_WARN_FRACTION",
    "BudgetExceededError",
    "CostBudget",
    "SessionCostTracker",
    "cost_of_tokens",
]
