"""ADR 093 Phase 2 (PR 3) — the executor runs the GovernanceEngine in *shadow*.

The contract: the engine is built from the same policies the executor already
holds and runs alongside the legacy checks at each edge (model / runtime / skill
/ cost), emitting the uniform governance audit trail in WARN mode — so a
would-be deny is *recorded, never enforced*. The legacy ``PolicyViolationError``
raises stay authoritative; the hot path is byte-for-byte unchanged when no
policy is configured.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from movate.core.config import ModelPolicy, RuntimePolicy, SkillPolicy
from movate.core.executor import Executor
from movate.core.models import AgentRuntime, SkillSideEffects
from movate.governance.gate import GateKind
from movate.providers.mock import MockProvider
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import InMemoryStorage, NullTracer


@pytest.fixture
def pricing() -> PricingTable:
    return load_pricing()


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


def _executor(
    *,
    pricing: PricingTable,
    storage: InMemoryStorage,
    policy: ModelPolicy | None = None,
    runtime_policy: RuntimePolicy | None = None,
    skill_policy: SkillPolicy | None = None,
) -> Executor:
    return Executor(
        provider=MockProvider(),
        pricing=pricing,
        storage=storage,
        tracer=NullTracer(),
        policy=policy,
        runtime_policy=runtime_policy,
        skill_policy=skill_policy,
    )


# ---------------------------------------------------------------------------
# Construction — zero overhead when nothing is configured (the compat anchor)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_shadow_engine_is_none_when_all_policies_permissive(
    pricing: PricingTable, storage: InMemoryStorage
) -> None:
    # The byte-for-byte contract: with no policy configured the executor builds
    # no engine, so the per-edge shadow calls all short-circuit to a no-op.
    ex = _executor(pricing=pricing, storage=storage)
    assert ex._governance is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "kwargs",
    [
        {"policy": ModelPolicy(allowed_providers=["azure"])},
        {"policy": ModelPolicy(max_cost_per_run_usd=0.10)},
        {"runtime_policy": RuntimePolicy(allowed=[AgentRuntime.LITELLM])},
        {"skill_policy": SkillPolicy(allowed_side_effects=[SkillSideEffects.READ_ONLY])},
    ],
)
async def test_shadow_engine_built_when_any_policy_restrictive(
    pricing: PricingTable, storage: InMemoryStorage, kwargs: dict[str, object]
) -> None:
    ex = _executor(pricing=pricing, storage=storage, **kwargs)  # type: ignore[arg-type]
    assert ex._governance is not None


# ---------------------------------------------------------------------------
# Emission — the shadow records a would-be deny but never raises (warn mode)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_shadow_records_model_deny_without_raising(
    pricing: PricingTable, storage: InMemoryStorage, caplog: pytest.LogCaptureFixture
) -> None:
    # A model OUTSIDE the allowlist would be denied — but the shadow only
    # records it (warn mode); _govern_shadow must not raise.
    ex = _executor(
        pricing=pricing, storage=storage, policy=ModelPolicy(allowed_providers=["azure"])
    )
    spec = SimpleNamespace(name="rag-qa")
    with caplog.at_level(logging.INFO, logger="movate.audit"):
        ex._govern_shadow(GateKind.MODEL, spec, model="openai/gpt-4o")  # type: ignore[arg-type]
    # An audit event was emitted for the governance.model gate (the deny was
    # downgraded to a recorded warn).
    assert any("governance.model" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
async def test_shadow_allow_is_not_audited(
    pricing: PricingTable, storage: InMemoryStorage, caplog: pytest.LogCaptureFixture
) -> None:
    # A compliant model produces an ALLOW — not audited by default, so the
    # trail stays signal-dense.
    ex = _executor(
        pricing=pricing, storage=storage, policy=ModelPolicy(allowed_providers=["openai"])
    )
    spec = SimpleNamespace(name="rag-qa")
    with caplog.at_level(logging.INFO, logger="movate.audit"):
        ex._govern_shadow(GateKind.MODEL, spec, model="openai/gpt-4o")  # type: ignore[arg-type]
    assert not any("governance.model" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
async def test_shadow_noop_when_engine_absent(
    pricing: PricingTable, storage: InMemoryStorage, caplog: pytest.LogCaptureFixture
) -> None:
    # No policy ⇒ no engine ⇒ _govern_shadow is a pure no-op (no audit, no raise).
    ex = _executor(pricing=pricing, storage=storage)
    spec = SimpleNamespace(name="rag-qa")
    with caplog.at_level(logging.INFO, logger="movate.audit"):
        ex._govern_shadow(GateKind.MODEL, spec, model="anything/at-all")  # type: ignore[arg-type]
    assert not any("governance" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
async def test_shadow_swallows_internal_errors(
    pricing: PricingTable, storage: InMemoryStorage
) -> None:
    # The failure-mode rule: a shadow bug must never surface to the run. Feed a
    # context-attribute access that would blow up inside a gate and assert the
    # helper still returns cleanly. (A spec with no ``name`` attribute would
    # raise inside _govern_shadow before the guard — but the broad except
    # swallows it.)
    ex = _executor(
        pricing=pricing, storage=storage, policy=ModelPolicy(allowed_providers=["azure"])
    )

    class _Boom:
        @property
        def name(self) -> str:
            raise RuntimeError("boom")

    # Must not propagate.
    ex._govern_shadow(GateKind.MODEL, _Boom(), model="openai/gpt-4o")  # type: ignore[arg-type]
