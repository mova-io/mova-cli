"""Trigger storage — save/get/get-by-id/list/delete/touch + tenant isolation.

ADR 017 D2. Mirrors tests/test_job_schedule_storage.py: the same three
backends via the shared ``storage`` fixture in conftest.py — ``InMemoryStorage``,
``SqliteProvider``, and ``PostgresProvider`` (skipped when ``MOVATE_PG_TEST_URL``
is unset).

Asserts the additive table is default-off (no rows until written), upserts on
``(tenant_id, name)``, round-trips every field (including ``input_defaults``),
is tenant-scoped on the management path (no leak), resolves by the PUBLIC
``trigger_id`` WITHOUT a tenant on the fire path, and that ``touch`` stamps
``last_fired_at``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from movate.core.models import JobKind, Trigger


def _make_trigger(
    *,
    tenant_id: str = "tenant-a",
    name: str = "zendesk",
    trigger_id: str = "trig-1",
    kind: JobKind = JobKind.AGENT,
    target: str = "triage-agent",
    enabled: bool = True,
    created_at: datetime | None = None,
) -> Trigger:
    return Trigger(
        tenant_id=tenant_id,
        name=name,
        trigger_id=trigger_id,
        kind=kind,
        target=target,
        secret_hash="abc123",
        salt="saltsalt",
        input_defaults={"source": "zendesk", "n": 3},
        enabled=enabled,
        created_by="key-xyz",
        created_at=created_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Default-off + round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_default_off_no_rows(storage) -> None:
    assert await storage.list_triggers(tenant_id="tenant-a") == []
    assert await storage.get_trigger("anything", tenant_id="tenant-a") is None
    assert await storage.get_trigger_by_id("nope") is None


@pytest.mark.unit
async def test_save_and_get_round_trip(storage) -> None:
    t = _make_trigger(kind=JobKind.WORKFLOW, target="returns-pipeline")
    await storage.save_trigger(t)
    got = await storage.get_trigger("zendesk", tenant_id="tenant-a")
    assert got is not None
    assert got.name == "zendesk"
    assert got.tenant_id == "tenant-a"
    assert got.trigger_id == "trig-1"
    assert got.kind == JobKind.WORKFLOW
    assert got.target == "returns-pipeline"
    assert got.secret_hash == "abc123"
    assert got.salt == "saltsalt"
    assert got.input_defaults == {"source": "zendesk", "n": 3}
    assert got.enabled is True
    assert got.created_by == "key-xyz"
    assert got.last_fired_at is None


@pytest.mark.unit
async def test_get_by_id_is_not_tenant_scoped(storage) -> None:
    """The fire path resolves by public id with NO tenant context."""
    await storage.save_trigger(_make_trigger(tenant_id="tenant-a", trigger_id="pub-xyz"))
    got = await storage.get_trigger_by_id("pub-xyz")
    assert got is not None
    assert got.tenant_id == "tenant-a"  # carries its own tenant
    assert got.name == "zendesk"


@pytest.mark.unit
async def test_save_upserts_on_tenant_name(storage) -> None:
    await storage.save_trigger(_make_trigger(target="a", trigger_id="id-1"))
    await storage.save_trigger(_make_trigger(target="b", trigger_id="id-2", kind=JobKind.WORKFLOW))
    rows = await storage.list_triggers(tenant_id="tenant-a")
    assert len(rows) == 1
    assert rows[0].target == "b"
    assert rows[0].kind == JobKind.WORKFLOW
    assert rows[0].trigger_id == "id-2"


# ---------------------------------------------------------------------------
# Tenant isolation (management path)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_get_is_tenant_scoped(storage) -> None:
    await storage.save_trigger(_make_trigger(tenant_id="tenant-a"))
    assert await storage.get_trigger("zendesk", tenant_id="tenant-a") is not None
    assert await storage.get_trigger("zendesk", tenant_id="tenant-b") is None


@pytest.mark.unit
async def test_list_is_tenant_scoped(storage) -> None:
    await storage.save_trigger(_make_trigger(tenant_id="tenant-a", name="foo", trigger_id="a1"))
    await storage.save_trigger(_make_trigger(tenant_id="tenant-a", name="bar", trigger_id="a2"))
    await storage.save_trigger(_make_trigger(tenant_id="tenant-b", name="foo", trigger_id="b1"))
    only_a = await storage.list_triggers(tenant_id="tenant-a")
    assert {t.name for t in only_a} == {"foo", "bar"}
    only_b = await storage.list_triggers(tenant_id="tenant-b")
    assert {t.name for t in only_b} == {"foo"}


@pytest.mark.unit
async def test_list_all_tenants_for_operator(storage) -> None:
    await storage.save_trigger(_make_trigger(tenant_id="tenant-a", name="foo", trigger_id="a1"))
    await storage.save_trigger(_make_trigger(tenant_id="tenant-b", name="bar", trigger_id="b1"))
    rows = await storage.list_triggers(tenant_id=None)
    assert {(t.tenant_id, t.name) for t in rows} == {
        ("tenant-a", "foo"),
        ("tenant-b", "bar"),
    }


# ---------------------------------------------------------------------------
# Delete + touch
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_delete_returns_true_then_false(storage) -> None:
    await storage.save_trigger(_make_trigger())
    assert await storage.delete_trigger("zendesk", tenant_id="tenant-a") is True
    assert await storage.get_trigger("zendesk", tenant_id="tenant-a") is None
    assert await storage.delete_trigger("zendesk", tenant_id="tenant-a") is False


@pytest.mark.unit
async def test_delete_is_tenant_scoped(storage) -> None:
    await storage.save_trigger(_make_trigger(tenant_id="tenant-a"))
    assert await storage.delete_trigger("zendesk", tenant_id="tenant-b") is False
    assert await storage.get_trigger("zendesk", tenant_id="tenant-a") is not None


@pytest.mark.unit
async def test_touch_stamps_last_fired_at(storage) -> None:
    await storage.save_trigger(_make_trigger(trigger_id="pub-1"))
    when = datetime.now(UTC) - timedelta(seconds=5)
    await storage.touch_trigger("pub-1", last_fired_at=when)
    got = await storage.get_trigger_by_id("pub-1")
    assert got is not None
    assert got.last_fired_at is not None
    assert abs((got.last_fired_at - when).total_seconds()) < 1.0


@pytest.mark.unit
async def test_touch_missing_trigger_is_noop(storage) -> None:
    await storage.touch_trigger("ghost", last_fired_at=datetime.now(UTC))
    assert await storage.get_trigger_by_id("ghost") is None
