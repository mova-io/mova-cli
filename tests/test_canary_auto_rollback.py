"""Pure auto-rollback helpers — movate.core.canary (ADR 016 D5).

``should_auto_rollback`` is the decision: a drift regression on the *challenger*
trips the kill switch ONLY when an operator opted in (``auto_rollback`` on).
``rolled_back_config`` is the action: a copy with ``weight`` → 0 (champion +
challenger pins preserved — rollback is the kill switch, never a delete).

Mirrors tests/test_canary_routing.py / test_canary_model.py. The #1 safety
invariant (ADR 016 D5): default-off → alert-only → ``should_auto_rollback`` is
``False``.
"""

from __future__ import annotations

import pytest

from movate.core.canary import rolled_back_config, should_auto_rollback
from movate.core.models import CanaryConfig


def _cfg(**overrides) -> CanaryConfig:
    base: dict = {
        "tenant_id": "t",
        "agent": "a",
        "challenger_version": "chal",
        "champion_version": "champ",
        "weight": 25,
        "enabled": True,
        "auto_rollback": True,
    }
    base.update(overrides)
    return CanaryConfig(**base)


# ---------------------------------------------------------------------------
# should_auto_rollback — the opt-in decision (truth table)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_off_by_default_never_rolls_back() -> None:
    """ADR 016 D5 safety default: auto_rollback off → alert-only → False."""
    cfg = _cfg(auto_rollback=False)
    assert should_auto_rollback(cfg, regressed=True, evaluated_version="chal") is False


@pytest.mark.unit
def test_on_regressed_challenger_version_rolls_back() -> None:
    """Opt-in + regression on the live challenger → trip the kill switch."""
    cfg = _cfg(auto_rollback=True, weight=25)
    assert should_auto_rollback(cfg, regressed=True, evaluated_version="chal") is True


@pytest.mark.unit
def test_regression_on_champion_version_does_not_roll_back() -> None:
    """A regression on the champion (not the challenger) is not a rollback reason."""
    cfg = _cfg(auto_rollback=True, weight=25, champion_version="champ")
    assert should_auto_rollback(cfg, regressed=True, evaluated_version="champ") is False


@pytest.mark.unit
def test_no_regression_does_not_roll_back() -> None:
    """No regression → nothing to revert, even opted-in on the challenger."""
    cfg = _cfg(auto_rollback=True, weight=25)
    assert should_auto_rollback(cfg, regressed=False, evaluated_version="chal") is False


@pytest.mark.unit
def test_already_at_kill_switch_does_not_roll_back() -> None:
    """weight already 0 → no live challenger traffic → idempotent no-op (False)."""
    cfg = _cfg(auto_rollback=True, weight=0)
    assert should_auto_rollback(cfg, regressed=True, evaluated_version="chal") is False


@pytest.mark.unit
def test_disabled_canary_does_not_roll_back() -> None:
    """A disabled canary routes 100% champion already → no rollback needed."""
    cfg = _cfg(auto_rollback=True, weight=25, enabled=False)
    assert should_auto_rollback(cfg, regressed=True, evaluated_version="chal") is False


@pytest.mark.unit
def test_none_config_does_not_roll_back() -> None:
    """No canary → nothing to roll back (the run path never had a canary)."""
    assert should_auto_rollback(None, regressed=True, evaluated_version="chal") is False


@pytest.mark.unit
def test_none_evaluated_version_does_not_roll_back() -> None:
    """An eval with no recorded version can't be attributed to the challenger."""
    cfg = _cfg(auto_rollback=True, weight=25)
    assert should_auto_rollback(cfg, regressed=True, evaluated_version=None) is False


# ---------------------------------------------------------------------------
# rolled_back_config — the action (kill switch, pins preserved)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rolled_back_config_zeroes_weight() -> None:
    cfg = _cfg(weight=25)
    rolled = rolled_back_config(cfg)
    assert rolled.weight == 0


@pytest.mark.unit
def test_rolled_back_config_preserves_champion_and_challenger() -> None:
    cfg = _cfg(weight=40, champion_version="champ", challenger_version="chal")
    rolled = rolled_back_config(cfg)
    # Rollback is a pointer move (kill switch), never a version delete.
    assert rolled.champion_version == "champ"
    assert rolled.challenger_version == "chal"
    assert rolled.enabled is True
    assert rolled.auto_rollback is True


@pytest.mark.unit
def test_rolled_back_config_does_not_mutate_original() -> None:
    cfg = _cfg(weight=25)
    rolled = rolled_back_config(cfg)
    assert cfg.weight == 25  # original untouched
    assert rolled is not cfg
    assert rolled.updated_at >= cfg.updated_at
