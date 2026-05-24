"""Pure canary / champion-challenger routing (ADR 016 D3).

This module is the **heart** of the canary feature and is deliberately
*pure* — no DB, no HTTP, no clock. Given a :class:`movate.core.models.CanaryConfig`
(or ``None``) and a ``thread_id``, :func:`choose_version` returns *which agent
version a run should execute*: the champion (``None`` → registry latest, or a
pinned ``champion_version``) or the ``challenger_version``.

The single most important invariant:

    **No config → return ``None`` → the run path is byte-for-byte today's
    behavior.**

``resolve_agent_bundle(version=None)`` already means "latest", and the
async enqueue path leaves ``JobRecord.target_version = None`` for a no-canary
job — so a returned ``None`` threads through untouched. Every other branch
(disabled, kill-switch ``weight == 0``) also returns the champion, so dialing a
canary back to 0 instantly restores the no-canary behavior.

Two selection strategies:

* **Sticky (default)** — when ``sticky`` is set AND a ``thread_id`` is
  available, the side is a *deterministic* function of the thread id:
  ``_bucket(thread_id) < weight`` → challenger. This keeps a multi-turn
  conversation pinned to one side (no champion↔challenger flip mid-thread) and
  needs no stored state — the same thread always hashes to the same bucket.
  The hash is :func:`hashlib.sha256` (stable across processes / Python runs,
  unlike the salted built-in ``hash``), reduced to ``[0, 100)``.
* **Weighted** — otherwise (no ``thread_id``, or ``sticky`` off) an
  independent draw: ``rng.random() * 100 < weight`` → challenger. A caller may
  inject a seeded :class:`random.Random` for deterministic tests; production
  passes ``None`` and gets the module default.

Stdlib only (``hashlib`` + ``random``) — no new dependency, portable (ADR 001).
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from movate.core.models import CanaryConfig, JobStatus

if TYPE_CHECKING:
    from movate.storage.base import StorageProvider

__all__ = [
    "SideStats",
    "aggregate_side",
    "bucket_for_thread",
    "choose_version",
    "is_active",
    "rolled_back_config",
    "should_auto_rollback",
]


def is_active(config: CanaryConfig | None) -> bool:
    """Whether ``config`` would actually route any traffic to a challenger.

    A canary is *active* only when it exists, is enabled, and has a non-zero
    weight. A disabled canary or one at ``weight == 0`` (the kill switch) is
    inert — it routes 100% to the champion — so callers that only need to know
    "is anything being split?" can short-circuit on this.
    """
    return config is not None and config.enabled and config.weight > 0


def should_auto_rollback(
    config: CanaryConfig | None,
    *,
    regressed: bool,
    evaluated_version: str | None,
) -> bool:
    """Whether a drift regression should auto-trip the canary kill switch.

    Pure decision function for ADR 016 D5's *opt-in* auto-rollback: when a
    scheduled-eval drift check finds a regression on the **challenger**, an
    operator who has opted in wants traffic reverted to the champion instantly
    (``weight`` → 0). Returns ``True`` only when **every** guard holds:

    * ``config`` exists, is ``enabled``, and is ``auto_rollback`` (the opt-in
      — default-off means alert-only, ADR 016 D5);
    * the drift check ``regressed``;
    * the eval that regressed was the ``challenger_version`` — a regression on
      the champion (or any other version) is *not* a reason to kill a canary;
    * ``config.weight > 0`` — there is live challenger traffic to pull back; a
      canary already at the kill switch needs no rollback (idempotent / no-op).

    No DB, no clock, no I/O — trivially unit-testable; the dispatch hook calls
    this then persists :func:`rolled_back_config` on ``True``.
    """
    if config is None or not config.enabled or not config.auto_rollback:
        return False
    if not regressed:
        return False
    if evaluated_version is None or evaluated_version != config.challenger_version:
        return False
    return config.weight > 0


def rolled_back_config(config: CanaryConfig) -> CanaryConfig:
    """Return a copy of ``config`` with the kill switch tripped (``weight`` → 0).

    The rollback action itself: route 100% of traffic back to the champion by
    zeroing the weight, refreshing ``updated_at``. Champion and challenger
    pins are preserved (rollback is a *pointer* move, never a version delete —
    the challenger stays in the registry and the config so it can be
    re-investigated or re-enabled). Mirrors the manual ``mdk canary off`` /
    ``weight=0`` kill switch — :func:`choose_version` already routes a
    ``weight == 0`` config 100% to the champion.
    """
    return config.model_copy(update={"weight": 0, "updated_at": datetime.now(UTC)})


def bucket_for_thread(thread_id: str) -> int:
    """Map a ``thread_id`` to a stable bucket in ``[0, 100)``.

    Deterministic across processes and Python runs (unlike the built-in
    ``hash``, which is salted per-process) so the same conversation lands on
    the same canary side everywhere — the property sticky routing needs.
    """
    digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
    return int(digest, 16) % 100


def choose_version(
    config: CanaryConfig | None,
    *,
    thread_id: str | None,
    rng: random.Random | None = None,
) -> str | None:
    """Pick the agent version a run should execute (champion vs challenger).

    Returns:

    * ``None`` — run the champion-by-default (registry latest). This is the
      no-canary / kill-switch / disabled path; returning ``None`` makes the
      run path identical to pre-canary behavior.
    * ``config.champion_version`` — the champion, *pinned* to a specific
      version (only when the config pins one and champion is chosen).
    * ``config.challenger_version`` — the challenger received this run.

    Selection:

    1. ``config is None`` / ``not config.enabled`` / ``config.weight == 0`` →
       champion (``champion_version`` if pinned, else ``None``).
    2. Sticky (``config.sticky`` and a ``thread_id``) → deterministic
       :func:`bucket_for_thread` ``< weight`` → challenger, else champion.
    3. Otherwise a weighted random draw via ``rng`` (or the module default).

    ``weight == 100`` always selects the challenger; ``weight`` in ``(0, 100)``
    splits per the strategy above.
    """
    # No canary / disabled / kill switch → champion. The pinned champion (if
    # any) or None (→ latest). This branch is the back-compat guarantee.
    if not is_active(config):
        return config.champion_version if config is not None else None

    # ``is_active`` proved config is not None; help the type checker.
    assert config is not None

    to_challenger: bool
    if config.sticky and thread_id is not None:
        # Deterministic per-thread bucketing — same thread, same side, every
        # turn. No stored state needed.
        to_challenger = bucket_for_thread(thread_id) < config.weight
    else:
        # Independent weighted draw. Inject a seeded Random for deterministic
        # tests; production passes None.
        draw = (rng or random).random() * 100.0
        to_challenger = draw < config.weight

    if to_challenger:
        return config.challenger_version
    return config.champion_version


@dataclass(frozen=True)
class SideStats:
    """Aggregated live quality for ONE agent_version slice (champion OR challenger).

    Pure data — no view/wire coupling — so both the runtime API and the CLI
    can build their own response shapes from it. ``version`` is the slice key
    (``None`` only for the champion-by-latest side when nothing is pinned and
    the registry can't resolve a latest).
    """

    version: str | None
    run_count: int
    success_count: int
    error_count: int
    thumbs_up: int
    thumbs_down: int
    feedback_count: int

    @property
    def success_rate(self) -> float:
        return (self.success_count / self.run_count) if self.run_count else 0.0

    @property
    def thumbs_up_rate(self) -> float:
        return (self.thumbs_up / self.feedback_count) if self.feedback_count else 0.0


async def aggregate_side(
    storage: StorageProvider,
    *,
    agent: str,
    tenant_id: str,
    version: str | None,
    limit: int = 1000,
) -> SideStats:
    """Aggregate runs + feedback for one ``agent_version`` slice (ADR 016 D3).

    Slices ``list_runs(agent, tenant)`` by ``agent_version == version`` (the
    canary slice key — every run is already version-tagged, ADR 014), counts
    runs / successes / errors, then joins ``list_feedback`` by ``run_id`` to
    count 👍/👎. ``version=None`` means "every run for the agent" (the
    champion-by-latest side when no concrete version is resolvable).

    Pure application logic over the :class:`StorageProvider` Protocol (no
    concrete backend), reused by the compare endpoint and ``mdk canary
    compare``. Live feedback/error slicing is the must-have; an eval-based
    slice is a documented follow-up.
    """
    runs = await storage.list_runs(agent=agent, tenant_id=tenant_id, limit=limit)
    sliced = [r for r in runs if r.agent_version == version] if version is not None else list(runs)
    run_ids = {r.run_id for r in sliced}
    success_count = sum(1 for r in sliced if r.status == JobStatus.SUCCESS)
    error_count = sum(1 for r in sliced if r.status in (JobStatus.ERROR, JobStatus.SAFETY_BLOCKED))
    feedback = await storage.list_feedback(agent=agent, tenant_id=tenant_id, limit=limit)
    relevant = [f for f in feedback if f.run_id in run_ids]
    thumbs_up = sum(1 for f in relevant if f.score >= 1)
    thumbs_down = sum(1 for f in relevant if f.score == -1)
    return SideStats(
        version=version,
        run_count=len(sliced),
        success_count=success_count,
        error_count=error_count,
        thumbs_up=thumbs_up,
        thumbs_down=thumbs_down,
        feedback_count=len(relevant),
    )
