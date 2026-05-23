"""Pure canary routing — movate.core.canary.choose_version (ADR 016 D3).

The heart of the feature. The single most important assertion: NO config →
``None`` → the run path is byte-for-byte today's behavior. Plus disabled /
kill-switch → champion, weight 100 → challenger, sticky determinism, and a
weighted distribution that matches the configured weight within tolerance.
"""

from __future__ import annotations

import random

import pytest

from movate.core.canary import bucket_for_thread, choose_version, is_active
from movate.core.models import CanaryConfig


def _cfg(**overrides) -> CanaryConfig:
    base: dict = {
        "tenant_id": "t",
        "agent": "a",
        "challenger_version": "chal",
    }
    base.update(overrides)
    return CanaryConfig(**base)


# ---------------------------------------------------------------------------
# The #1 invariant — no config / inert config → champion (→ None → latest)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_config_returns_none() -> None:
    """No canary → None → resolve latest → pre-canary behavior, byte-for-byte."""
    assert choose_version(None, thread_id=None) is None
    assert choose_version(None, thread_id="thread-1") is None


@pytest.mark.unit
def test_disabled_routes_to_champion() -> None:
    # Disabled, even at weight 100 + pinned challenger, never picks challenger.
    assert choose_version(_cfg(weight=100, enabled=False), thread_id="t1") is None
    # With a pinned champion, returns that pin (not None).
    assert (
        choose_version(_cfg(weight=100, enabled=False, champion_version="champ"), thread_id="t1")
        == "champ"
    )


@pytest.mark.unit
def test_kill_switch_weight_zero_routes_to_champion() -> None:
    assert choose_version(_cfg(weight=0), thread_id="t1") is None
    assert choose_version(_cfg(weight=0, champion_version="champ"), thread_id=None) == "champ"


@pytest.mark.unit
def test_is_active() -> None:
    assert is_active(None) is False
    assert is_active(_cfg(weight=0)) is False
    assert is_active(_cfg(weight=50, enabled=False)) is False
    assert is_active(_cfg(weight=1)) is True


# ---------------------------------------------------------------------------
# Full traffic → challenger
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_weight_100_always_challenger() -> None:
    cfg = _cfg(weight=100)
    # Sticky + thread.
    assert choose_version(cfg, thread_id="anything") == "chal"
    # Non-sticky weighted draw — always under 100.
    assert choose_version(_cfg(weight=100, sticky=False), thread_id=None) == "chal"


# ---------------------------------------------------------------------------
# Sticky determinism
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sticky_same_thread_same_side() -> None:
    cfg = _cfg(weight=50)
    first = choose_version(cfg, thread_id="thread-xyz")
    # Same thread, many calls → identical decision every time (no re-roll).
    for _ in range(50):
        assert choose_version(cfg, thread_id="thread-xyz") == first


@pytest.mark.unit
def test_sticky_bucket_is_deterministic_across_processes() -> None:
    # The bucket is a stable sha256 reduction, not the salted built-in hash.
    b = bucket_for_thread("thread-xyz")
    assert 0 <= b < 100
    assert bucket_for_thread("thread-xyz") == b


@pytest.mark.unit
def test_sticky_splits_threads_across_sides() -> None:
    """Over many distinct threads at 50%, both sides get traffic."""
    cfg = _cfg(weight=50)
    sides = {choose_version(cfg, thread_id=f"thread-{i}") for i in range(200)}
    # Champion side is None (no pin); challenger is "chal".
    assert sides == {None, "chal"}


@pytest.mark.unit
def test_no_thread_id_falls_back_to_weighted_even_when_sticky() -> None:
    # Sticky but no thread → weighted draw. Seed it so it's deterministic.
    cfg = _cfg(weight=100, sticky=True)
    assert choose_version(cfg, thread_id=None, rng=random.Random(0)) == "chal"
    cfg0 = _cfg(weight=0, sticky=True, champion_version="champ")
    assert choose_version(cfg0, thread_id=None) == "champ"


# ---------------------------------------------------------------------------
# Weighted distribution ≈ the configured weight (seeded → deterministic)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("weight", [10, 25, 50, 75, 90])
def test_weighted_distribution_matches_weight(weight: int) -> None:
    cfg = _cfg(weight=weight, sticky=False)
    rng = random.Random(1234)
    n = 20_000
    challengers = sum(1 for _ in range(n) if choose_version(cfg, thread_id=None, rng=rng) == "chal")
    observed = challengers / n * 100
    # Tolerance: within 2 percentage points of the configured weight.
    assert abs(observed - weight) < 2.0, f"observed {observed:.2f}% vs weight {weight}%"


@pytest.mark.unit
def test_champion_side_returns_pin_when_set() -> None:
    cfg = _cfg(weight=0, champion_version="champ-pin")
    assert choose_version(cfg, thread_id="t1") == "champ-pin"
