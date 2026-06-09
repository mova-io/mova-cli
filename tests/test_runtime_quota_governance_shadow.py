"""ADR 093 Phase 2 (PR 4) — the quota middleware runs the QUOTA gate in *shadow*.

The contract mirrors the executor shadow (PR 3): the engine is built from the
same :class:`QuotaConfig` the admission edge already enforces and emits the
uniform ``governance.quota`` audit trail in WARN mode — recording a breach,
never blocking. The legacy ``check_quota`` 429 path stays authoritative, and the
returned :class:`QuotaDecision` is byte-for-byte unaffected by the shadow.

Tested at the ``_compute_decision`` seam (the cache-miss path where the usage
rollups are freshly computed — exactly where the shadow fires).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from movate.core.models import JobStatus, Metrics, RunRecord, TokenUsage
from movate.core.quotas import QuotaConfig, QuotaMode, TenantQuota
from movate.runtime.middleware import _build_quota_shadow, _compute_decision
from movate.testing import InMemoryStorage

_TENANT = "acme"


def _run(
    *, run_id: str, tokens_in: int = 100, tokens_out: int = 50, cost: float = 0.05
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        job_id=f"j-{run_id}",
        tenant_id=_TENANT,
        agent="triage",
        agent_version="0.1.0",
        prompt_hash="hash",
        provider="openai/gpt-4o-mini",
        provider_version="v1",
        pricing_version="2026-05",
        status=JobStatus.SUCCESS,
        input={"q": "x"},
        output={"a": "y"},
        metrics=Metrics(
            cost_usd=cost,
            latency_ms=100,
            tokens=TokenUsage(input=tokens_in, output=tokens_out),
            provider="openai/gpt-4o-mini",
        ),
        created_at=datetime.now(UTC),
    )


async def _storage_with(*runs: RunRecord) -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    for r in runs:
        await s.save_run(r)
    return s


# ---------------------------------------------------------------------------
# Construction — pass-through when quotas are unconfigured
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_quota_shadow_engine_is_none_without_config() -> None:
    assert _build_quota_shadow(None) is None
    assert _build_quota_shadow(QuotaConfig(tenants=[TenantQuota(tenant_id=_TENANT)])) is not None


# ---------------------------------------------------------------------------
# Emission — records a breach, never blocks; conformant with the legacy decision
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_shadow_records_quota_breach_and_matches_legacy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # One 150-token run trips a 100-token daily ceiling (>= boundary).
    storage = await _storage_with(_run(run_id="r1"))
    row = TenantQuota(tenant_id=_TENANT, daily_token_limit=100, mode=QuotaMode.DENY)
    engine = _build_quota_shadow(QuotaConfig(tenants=[row]))
    assert engine is not None

    with caplog.at_level(logging.INFO, logger="movate.audit"):
        decision = await _compute_decision(storage, tenant_id=_TENANT, quota=row, governance=engine)

    # Legacy decision is authoritative + unaffected: deny mode over a ceiling.
    assert decision.allow is False
    assert "daily_tokens" in decision.over
    # The shadow recorded the breach as a governance.quota audit event.
    assert any("governance.quota" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
async def test_shadow_under_ceiling_is_not_audited(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Well under the ceiling ⇒ ALLOW ⇒ not audited (the trail stays signal-dense).
    storage = await _storage_with(_run(run_id="r1", tokens_in=5, tokens_out=5))
    row = TenantQuota(tenant_id=_TENANT, daily_token_limit=10_000, mode=QuotaMode.DENY)
    engine = _build_quota_shadow(QuotaConfig(tenants=[row]))
    assert engine is not None

    with caplog.at_level(logging.INFO, logger="movate.audit"):
        decision = await _compute_decision(storage, tenant_id=_TENANT, quota=row, governance=engine)

    assert decision.allow is True
    assert not any("governance.quota" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
async def test_shadow_does_not_change_the_returned_decision() -> None:
    # The shadow is strictly observational: the QuotaDecision must be identical
    # with and without the engine wired in.
    storage = await _storage_with(_run(run_id="r1"))
    row = TenantQuota(tenant_id=_TENANT, daily_token_limit=100, mode=QuotaMode.DENY)
    engine = _build_quota_shadow(QuotaConfig(tenants=[row]))

    without = await _compute_decision(storage, tenant_id=_TENANT, quota=row)
    with_shadow = await _compute_decision(storage, tenant_id=_TENANT, quota=row, governance=engine)
    assert without == with_shadow


@pytest.mark.unit
async def test_shadow_warn_mode_row_still_records_breach(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The legacy per-tenant WARN mode allows (allow=True) but the breach is real;
    # the gate emits a raw deny that the engine's universal mode downgrades to a
    # recorded warn — so the governance trail still captures it.
    storage = await _storage_with(_run(run_id="r1"))
    row = TenantQuota(tenant_id=_TENANT, daily_token_limit=100, mode=QuotaMode.WARN)
    engine = _build_quota_shadow(QuotaConfig(tenants=[row]))
    assert engine is not None

    with caplog.at_level(logging.INFO, logger="movate.audit"):
        decision = await _compute_decision(storage, tenant_id=_TENANT, quota=row, governance=engine)

    assert decision.allow is True  # warn mode passes through
    assert any("governance.quota" in r.getMessage() for r in caplog.records)
