"""Canary storage — CRUD round-trip + tenant isolation, plus the additive
``JobRecord.target_version`` column round-trip (ADR 016 D3).

Three backends in scope via the shared ``storage`` fixture in conftest.py:
``InMemoryStorage``, ``SqliteProvider``, and ``PostgresProvider`` (skipped when
``MOVATE_PG_TEST_URL`` is unset). Mirrors tests/test_job_schedule_storage.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from movate.core.models import CanaryConfig, JobKind, JobRecord


def _make_canary(
    *,
    tenant_id: str = "tenant-a",
    agent: str = "faq-agent",
    challenger_version: str = "2026.5.23.1",
    weight: int = 10,
    created_at: datetime | None = None,
) -> CanaryConfig:
    return CanaryConfig(
        tenant_id=tenant_id,
        agent=agent,
        challenger_version=challenger_version,
        champion_version="2026.5.22.1",
        weight=weight,
        sticky=True,
        enabled=True,
        auto_promote=True,
        eval_gate=0.9,
        auto_rollback=True,
        created_by="key-xyz",
        created_at=created_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Default-off + round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_default_off_no_rows(storage) -> None:
    assert await storage.list_canary_configs(tenant_id="tenant-a") == []
    assert await storage.get_canary_config("anything", tenant_id="tenant-a") is None


@pytest.mark.unit
async def test_save_and_get_round_trip(storage) -> None:
    c = _make_canary()
    await storage.save_canary_config(c)
    got = await storage.get_canary_config("faq-agent", tenant_id="tenant-a")
    assert got is not None
    assert got.agent == "faq-agent"
    assert got.tenant_id == "tenant-a"
    assert got.challenger_version == "2026.5.23.1"
    assert got.champion_version == "2026.5.22.1"
    assert got.weight == 10
    assert got.sticky is True
    assert got.enabled is True
    assert got.auto_promote is True
    assert got.eval_gate == 0.9
    assert got.auto_rollback is True  # ADR 016 D5 column round-trips
    assert got.created_by == "key-xyz"


@pytest.mark.unit
async def test_save_upserts_on_tenant_agent(storage) -> None:
    await storage.save_canary_config(_make_canary(weight=10))
    await storage.save_canary_config(_make_canary(weight=50, challenger_version="2026.5.24.1"))
    rows = await storage.list_canary_configs(tenant_id="tenant-a")
    assert len(rows) == 1
    assert rows[0].weight == 50
    assert rows[0].challenger_version == "2026.5.24.1"


@pytest.mark.unit
async def test_champion_version_none_round_trips(storage) -> None:
    c = CanaryConfig(
        tenant_id="tenant-a",
        agent="faq-agent",
        challenger_version="v2",
        champion_version=None,
        weight=5,
    )
    await storage.save_canary_config(c)
    got = await storage.get_canary_config("faq-agent", tenant_id="tenant-a")
    assert got is not None
    assert got.champion_version is None
    assert got.eval_gate is None
    assert got.auto_rollback is False  # default-off persists (alert-only)


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_get_is_tenant_scoped(storage) -> None:
    await storage.save_canary_config(_make_canary(tenant_id="tenant-a"))
    assert await storage.get_canary_config("faq-agent", tenant_id="tenant-a") is not None
    assert await storage.get_canary_config("faq-agent", tenant_id="tenant-b") is None


@pytest.mark.unit
async def test_list_is_tenant_scoped(storage) -> None:
    await storage.save_canary_config(_make_canary(tenant_id="tenant-a", agent="foo"))
    await storage.save_canary_config(_make_canary(tenant_id="tenant-a", agent="bar"))
    await storage.save_canary_config(_make_canary(tenant_id="tenant-b", agent="foo"))
    only_a = await storage.list_canary_configs(tenant_id="tenant-a")
    assert {c.agent for c in only_a} == {"foo", "bar"}
    only_b = await storage.list_canary_configs(tenant_id="tenant-b")
    assert {c.agent for c in only_b} == {"foo"}


@pytest.mark.unit
async def test_list_all_tenants(storage) -> None:
    await storage.save_canary_config(_make_canary(tenant_id="tenant-a", agent="foo"))
    await storage.save_canary_config(_make_canary(tenant_id="tenant-b", agent="bar"))
    rows = await storage.list_canary_configs(tenant_id=None)
    assert {(c.tenant_id, c.agent) for c in rows} == {
        ("tenant-a", "foo"),
        ("tenant-b", "bar"),
    }


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_delete_returns_true_then_false(storage) -> None:
    await storage.save_canary_config(_make_canary())
    assert await storage.delete_canary_config("faq-agent", tenant_id="tenant-a") is True
    assert await storage.get_canary_config("faq-agent", tenant_id="tenant-a") is None
    assert await storage.delete_canary_config("faq-agent", tenant_id="tenant-a") is False


@pytest.mark.unit
async def test_delete_is_tenant_scoped(storage) -> None:
    await storage.save_canary_config(_make_canary(tenant_id="tenant-a"))
    assert await storage.delete_canary_config("faq-agent", tenant_id="tenant-b") is False
    assert await storage.get_canary_config("faq-agent", tenant_id="tenant-a") is not None


# ---------------------------------------------------------------------------
# JobRecord.target_version — additive nullable column round-trip
# ---------------------------------------------------------------------------


def _make_job(*, target_version: str | None) -> JobRecord:
    return JobRecord(
        job_id=str(uuid4()),
        tenant_id="tenant-a",
        kind=JobKind.AGENT,
        target="faq-agent",
        input={"text": "hi"},
        target_version=target_version,
    )


@pytest.mark.unit
async def test_job_target_version_round_trips(storage) -> None:
    job = _make_job(target_version="2026.5.23.1")
    await storage.save_job(job)
    got = await storage.get_job(job.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.target_version == "2026.5.23.1"


@pytest.mark.unit
async def test_job_target_version_defaults_none(storage) -> None:
    """A pre-canary job (no target_version) reads back as None — the unchanged path."""
    job = _make_job(target_version=None)
    await storage.save_job(job)
    got = await storage.get_job(job.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.target_version is None


@pytest.mark.unit
async def test_job_target_version_survives_claim(storage) -> None:
    """The worker's claim path must preserve target_version (it resolves that version)."""
    job = _make_job(target_version="2026.5.23.1")
    await storage.save_job(job)
    claimed = await storage.claim_next_job(tenant_id="tenant-a")
    assert claimed is not None
    assert claimed.target_version == "2026.5.23.1"
