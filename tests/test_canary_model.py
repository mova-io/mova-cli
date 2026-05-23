"""CanaryConfig model — field bounds + extra=forbid (ADR 016 D3).

Mirrors tests/test_trigger_model.py / test_job_schedule_model.py: validate the
``weight`` 0-100 bound, the ``extra="forbid"`` strictness, and the sensible
defaults (weight 0 = kill switch / dormant, sticky on, assisted promote).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from movate.core.models import CanaryConfig


def _make(**overrides) -> CanaryConfig:
    base: dict = {
        "tenant_id": "tenant-a",
        "agent": "faq-agent",
        "challenger_version": "2026.5.23.1",
    }
    base.update(overrides)
    return CanaryConfig(**base)


@pytest.mark.unit
def test_minimal_construction_defaults() -> None:
    c = _make()
    assert c.weight == 0  # dormant / kill switch by default
    assert c.sticky is True
    assert c.enabled is True
    assert c.auto_promote is False
    assert c.eval_gate is None
    assert c.champion_version is None  # → registry latest
    assert c.created_at is not None
    assert c.updated_at is not None


@pytest.mark.unit
@pytest.mark.parametrize("weight", [0, 1, 50, 99, 100])
def test_weight_in_range_accepted(weight: int) -> None:
    assert _make(weight=weight).weight == weight


@pytest.mark.unit
@pytest.mark.parametrize("weight", [-1, 101, 1000, -100])
def test_weight_out_of_range_rejected(weight: int) -> None:
    with pytest.raises(ValidationError):
        _make(weight=weight)


@pytest.mark.unit
def test_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        _make(unexpected_field="boom")


@pytest.mark.unit
def test_challenger_version_required() -> None:
    with pytest.raises(ValidationError):
        CanaryConfig(tenant_id="t", agent="a")  # type: ignore[call-arg]


@pytest.mark.unit
def test_full_construction_round_trips() -> None:
    c = _make(
        champion_version="2026.5.22.1",
        weight=25,
        sticky=False,
        enabled=False,
        auto_promote=True,
        eval_gate=0.9,
        created_by="key-1",
    )
    assert c.champion_version == "2026.5.22.1"
    assert c.weight == 25
    assert c.sticky is False
    assert c.enabled is False
    assert c.auto_promote is True
    assert c.eval_gate == 0.9
    assert c.created_by == "key-1"
