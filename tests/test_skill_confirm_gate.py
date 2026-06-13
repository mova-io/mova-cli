"""ADR 105 — agent-loop tool governance: the confirm/HITL gate.

Covers SkillPolicy.confirm_side_effects semantics and the Executor's per-call
approval enforcement (fail-closed when no approver is wired).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from movate.core.config import SkillPolicy
from movate.core.executor import Executor
from movate.core.models import SkillSideEffects
from movate.core.skill_backend.base import SkillError
from movate.providers.mock import MockProvider
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import InMemoryStorage, NullTracer

RO = SkillSideEffects.READ_ONLY
MUT = SkillSideEffects.MUTATES_STATE


@pytest.fixture
def pricing() -> PricingTable:
    return load_pricing()


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


# ---------------------------------------------------------------------------
# SkillPolicy semantics
# ---------------------------------------------------------------------------


def test_requires_confirm() -> None:
    p = SkillPolicy(confirm_side_effects=[MUT])
    assert p.requires_confirm(MUT) is True
    assert p.requires_confirm(RO) is False
    # Default policy never requires confirm.
    assert SkillPolicy().requires_confirm(MUT) is False


def test_confirm_category_passes_the_deny_gate() -> None:
    # allowed=[read-only], confirm=[mutates-state]: a mutating skill is NOT a
    # deny violation (the confirm gate, not the allowlist, governs it).
    p = SkillPolicy(allowed_side_effects=[RO], confirm_side_effects=[MUT])
    assert p.check_skill("writer", MUT) is None
    # A category that's neither allowed nor confirm is still denied.
    assert p.check_skill("fs", SkillSideEffects.FILESYSTEM) is not None


# ---------------------------------------------------------------------------
# Executor enforcement
# ---------------------------------------------------------------------------


def _skill(name: str, se: SkillSideEffects) -> SimpleNamespace:
    return SimpleNamespace(spec=SimpleNamespace(name=name, side_effects=se))


def _executor(
    *, pricing: PricingTable, storage: InMemoryStorage, policy: SkillPolicy, approve=None
) -> Executor:
    return Executor(
        provider=MockProvider(),
        pricing=pricing,
        storage=storage,
        tracer=NullTracer(),
        skill_policy=policy,
        approve=approve,
    )


def test_non_confirm_category_is_noop(pricing: PricingTable, storage: InMemoryStorage) -> None:
    ex = _executor(pricing=pricing, storage=storage, policy=SkillPolicy(confirm_side_effects=[MUT]))
    # read-only skill → no approval needed, no raise, even with no callback.
    ex._enforce_confirm(_skill("lookup", RO), {})


def test_confirm_without_callback_is_failclosed(
    pricing: PricingTable, storage: InMemoryStorage
) -> None:
    ex = _executor(pricing=pricing, storage=storage, policy=SkillPolicy(confirm_side_effects=[MUT]))
    with pytest.raises(SkillError, match="requires human approval"):
        ex._enforce_confirm(_skill("delete-repo", MUT), {"repo": "x"})


def test_confirm_approved_passes(pricing: PricingTable, storage: InMemoryStorage) -> None:
    seen: dict = {}

    def approve(name: str, se: str, inp: dict) -> bool:
        seen.update(name=name, se=se, inp=inp)
        return True

    ex = _executor(
        pricing=pricing,
        storage=storage,
        policy=SkillPolicy(confirm_side_effects=[MUT]),
        approve=approve,
    )
    ex._enforce_confirm(_skill("delete-repo", MUT), {"repo": "x"})  # no raise
    assert seen == {"name": "delete-repo", "se": "mutates-state", "inp": {"repo": "x"}}


def test_confirm_declined_raises(pricing: PricingTable, storage: InMemoryStorage) -> None:
    ex = _executor(
        pricing=pricing,
        storage=storage,
        policy=SkillPolicy(confirm_side_effects=[MUT]),
        approve=lambda *_: False,
    )
    with pytest.raises(SkillError, match="not approved"):
        ex._enforce_confirm(_skill("delete-repo", MUT), {})
