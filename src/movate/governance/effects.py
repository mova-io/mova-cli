"""Per-run governance-effect collection (ADR 096 — ``governance_effect``).

The ``observability_facts`` table carries one ``governance_effect`` column:
the **most severe** effect (``deny`` > ``warn`` > ``allow``) any governance
gate recorded while a run executed, or ``NULL`` when no gate evaluated. The
:class:`movate.governance.engine.GovernanceEngine` is the single place
decisions are made, but it has no notion of "a run" — the run boundary is
owned by the execution edges (``runtime/dispatch.py``, the Temporal
activities). This module bridges the two without coupling either side:

* :func:`governance_effect_scope` — a context manager the **edges** open
  around one run. Decisions the engine records while the scope is active
  fold into the scope's most-severe effect. Contextvar-based, so it follows
  the run across ``await`` points and into tasks the run spawns (parallel
  workflow nodes) with zero threading of a handle through ``core``.
* :func:`record_scope_effect` — called by the engine (fail-soft) for every
  decision where at least one gate evaluated. A no-op when no scope is
  active, so the engine's behavior outside an instrumented edge is
  byte-for-byte unchanged.
* the **run-effect registry** (:func:`record_run_effect` /
  :func:`peek_run_effect` / :func:`consume_run_effect`) — a small
  process-local map ``workflow_run_id → effect`` for the Temporal path,
  where the gates fire in one activity invocation but the fact is written
  by another (the persist/pause activities). Best-effort by design: if a
  workflow's activities land on a *different* worker process than its
  persist activity (multi-worker pools), the effect is simply absent there
  — the fact's ``governance_effect`` stays NULL rather than wrong, and the
  storage upsert never lets a NULL overwrite a previously recorded value.

Pure stdlib — this module keeps the governance seam free of ``core`` /
``runtime`` imports (CLAUDE.md §6).
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

#: Severity order — higher is more severe (mirrors ``gate._EFFECT_RANK``).
_SEVERITY: dict[str, int] = {"allow": 0, "warn": 1, "deny": 2}

#: Registry size bound: a worker whose workflows crash before their terminal
#: persist activity would otherwise leak one entry per run, forever. 4096
#: in-flight workflow runs per worker process is far beyond any real pool;
#: past it the OLDEST entry is evicted (its fact honestly stays NULL).
_MAX_TRACKED_RUNS = 4096


def most_severe(*effects: str | None) -> str | None:
    """Fold effects with most-severe-wins (``deny`` > ``warn`` > ``allow``).

    ``None`` / unknown strings are skipped; all-skipped ⇒ ``None`` (no gate
    evaluated — the fact column's honest NULL).
    """
    winner: str | None = None
    for effect in effects:
        if effect not in _SEVERITY:
            continue
        if winner is None or _SEVERITY[effect] > _SEVERITY[winner]:
            winner = effect
    return winner


class GovernanceEffectScope:
    """Accumulates the most severe effect recorded while the scope is active."""

    __slots__ = ("_worst",)

    def __init__(self) -> None:
        self._worst: str | None = None

    def record(self, effect: str) -> None:
        self._worst = most_severe(self._worst, effect)

    @property
    def effect(self) -> str | None:
        """``deny`` / ``warn`` / ``allow``, or ``None`` if nothing recorded."""
        return self._worst


_ACTIVE_SCOPE: ContextVar[GovernanceEffectScope | None] = ContextVar(
    "movate_governance_effect_scope", default=None
)


@contextmanager
def governance_effect_scope() -> Iterator[GovernanceEffectScope]:
    """Open a per-run collection scope (edges only — one scope per run).

    Inner code (the executor, the workflow runner, tasks they spawn) needs no
    handle: the engine finds the active scope via the contextvar. Scopes do
    not nest meaningfully — an inner scope shadows the outer for its duration,
    which is fine because only the execution edges open one.
    """
    scope = GovernanceEffectScope()
    token = _ACTIVE_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _ACTIVE_SCOPE.reset(token)


def record_scope_effect(effect: str) -> None:
    """Record one decision's effect into the active scope (engine-side hook).

    No-op when no scope is active or the effect string is unknown — the
    engine's hot path outside an instrumented edge is unchanged.
    """
    scope = _ACTIVE_SCOPE.get()
    if scope is not None:
        scope.record(effect)


# ---------------------------------------------------------------------------
# Run-effect registry — the Temporal cross-activity bridge.
# ---------------------------------------------------------------------------

_RUN_EFFECTS: OrderedDict[str, str] = OrderedDict()


def record_run_effect(run_id: str, effect: str | None) -> None:
    """Merge ``effect`` into the registry entry for ``run_id`` (severity-wins).

    ``None`` / unknown effects are ignored. Bounded: past
    :data:`_MAX_TRACKED_RUNS` the oldest entry is evicted (fail-soft — its
    fact keeps an honest NULL rather than a wrong value).
    """
    if not run_id or effect not in _SEVERITY:
        return
    merged = most_severe(_RUN_EFFECTS.get(run_id), effect)
    if merged is None:  # pragma: no cover — unreachable (effect validated above)
        return
    _RUN_EFFECTS[run_id] = merged
    _RUN_EFFECTS.move_to_end(run_id)
    while len(_RUN_EFFECTS) > _MAX_TRACKED_RUNS:
        _RUN_EFFECTS.popitem(last=False)


def peek_run_effect(run_id: str) -> str | None:
    """Read the run's effect WITHOUT consuming it (pause facts — the run
    continues, the terminal persist still needs the entry)."""
    return _RUN_EFFECTS.get(run_id)


def consume_run_effect(run_id: str) -> str | None:
    """Pop the run's effect (terminal facts — the run is done, free the slot)."""
    return _RUN_EFFECTS.pop(run_id, None)


__all__ = [
    "GovernanceEffectScope",
    "consume_run_effect",
    "governance_effect_scope",
    "most_severe",
    "peek_run_effect",
    "record_run_effect",
    "record_scope_effect",
]
