"""Webhook subscriptions + delivery log — storage round-trip + tenant
isolation (ADR 035 D2).

Three backends in scope via the shared ``storage`` fixture in
``conftest.py``: ``InMemoryStorage``, ``SqliteProvider``, and
``PostgresProvider`` (skipped when ``MOVATE_PG_TEST_URL`` is unset).
Mirrors :mod:`tests.test_storage_events`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from movate.core.webhooks import (
    WILDCARD_KIND,
    WebhookAttempt,
    WebhookSubscription,
)


def _make_sub(
    *,
    tenant_id: str = "tenant-a",
    url: str = "https://example.com/hook",
    kind_filter: list[str] | None = None,
    enabled: bool = True,
) -> WebhookSubscription:
    return WebhookSubscription(
        tenant_id=tenant_id,
        url=url,
        kind_filter=kind_filter if kind_filter is not None else [WILDCARD_KIND],
        enabled=enabled,
    )


# ---------------------------------------------------------------------------
# CRUD round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_default_off_no_rows(storage) -> None:
    """An untouched table returns an empty list (no D2 sentinel rows)."""
    assert await storage.list_webhooks("tenant-a") == []


@pytest.mark.unit
async def test_create_and_get_round_trip(storage) -> None:
    """A created subscription round-trips through get_webhook unchanged."""
    sub = _make_sub(kind_filter=["run.completed", "eval.failed"])
    await storage.create_webhook(sub)
    got = await storage.get_webhook("tenant-a", sub.id)
    assert got is not None
    assert got.id == sub.id
    assert got.url == sub.url
    assert got.kind_filter == ["run.completed", "eval.failed"]
    assert got.secret == sub.secret
    assert got.enabled is True
    assert got.failure_count == 0


@pytest.mark.unit
async def test_list_returns_only_enabled_by_default(storage) -> None:
    enabled = _make_sub()
    disabled = _make_sub(enabled=False)
    await storage.create_webhook(enabled)
    await storage.create_webhook(disabled)
    rows = await storage.list_webhooks("tenant-a")
    assert {r.id for r in rows} == {enabled.id}
    all_rows = await storage.list_webhooks("tenant-a", enabled_only=False)
    assert {r.id for r in all_rows} == {enabled.id, disabled.id}


@pytest.mark.unit
async def test_update_enabled_in_place(storage) -> None:
    sub = _make_sub()
    await storage.create_webhook(sub)
    updated = await storage.update_webhook("tenant-a", sub.id, enabled=False)
    assert updated is not None
    assert updated.enabled is False
    refetched = await storage.get_webhook("tenant-a", sub.id)
    assert refetched is not None
    assert refetched.enabled is False


@pytest.mark.unit
async def test_update_failure_count(storage) -> None:
    sub = _make_sub()
    await storage.create_webhook(sub)
    updated = await storage.update_webhook("tenant-a", sub.id, failure_count=3)
    assert updated is not None
    assert updated.failure_count == 3


@pytest.mark.unit
async def test_delete_returns_true_on_hit_false_on_miss(storage) -> None:
    sub = _make_sub()
    await storage.create_webhook(sub)
    assert await storage.delete_webhook("tenant-a", sub.id) is True
    assert await storage.delete_webhook("tenant-a", sub.id) is False
    assert await storage.get_webhook("tenant-a", sub.id) is None


# ---------------------------------------------------------------------------
# Tenant isolation — wrong-tenant lookups return None / no-op
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_get_webhook_wrong_tenant_returns_none(storage) -> None:
    """A wrong-tenant lookup is indistinguishable from a missing one."""
    sub = _make_sub(tenant_id="tenant-a")
    await storage.create_webhook(sub)
    # tenant-b can't see tenant-a's webhook.
    assert await storage.get_webhook("tenant-b", sub.id) is None


@pytest.mark.unit
async def test_list_webhooks_tenant_scoped(storage) -> None:
    a = _make_sub(tenant_id="tenant-a")
    b = _make_sub(tenant_id="tenant-b")
    await storage.create_webhook(a)
    await storage.create_webhook(b)
    a_rows = await storage.list_webhooks("tenant-a")
    b_rows = await storage.list_webhooks("tenant-b")
    assert {r.id for r in a_rows} == {a.id}
    assert {r.id for r in b_rows} == {b.id}


@pytest.mark.unit
async def test_delete_wrong_tenant_is_noop(storage) -> None:
    sub = _make_sub(tenant_id="tenant-a")
    await storage.create_webhook(sub)
    deleted = await storage.delete_webhook("tenant-b", sub.id)
    assert deleted is False
    # Original tenant still sees the row.
    assert await storage.get_webhook("tenant-a", sub.id) is not None


# ---------------------------------------------------------------------------
# Attempts log
# ---------------------------------------------------------------------------


def _make_attempt(
    *,
    webhook_id: str = "wh-1",
    tenant_id: str = "tenant-a",
    event_id: str = "ev-1",
    error_kind: str = "ok",
    status_code: int | None = 200,
    attempt_n: int = 1,
    attempted_at: datetime | None = None,
) -> WebhookAttempt:
    return WebhookAttempt(
        webhook_id=webhook_id,
        tenant_id=tenant_id,
        event_id=event_id,
        attempted_at=attempted_at or datetime.now(UTC),
        status_code=status_code,
        response_excerpt="ok",
        error_kind=error_kind,
        attempt_n=attempt_n,
    )


@pytest.mark.unit
async def test_record_and_list_attempts(storage) -> None:
    """Recorded attempts round-trip, newest-first."""
    t0 = datetime.now(UTC) - timedelta(minutes=10)
    a1 = _make_attempt(event_id="ev-1", attempted_at=t0)
    a2 = _make_attempt(event_id="ev-2", attempted_at=t0 + timedelta(minutes=1))
    a3 = _make_attempt(event_id="ev-3", attempted_at=t0 + timedelta(minutes=2))
    for a in (a1, a2, a3):
        await storage.record_webhook_attempt(a)
    rows = await storage.list_webhook_attempts("tenant-a")
    assert [r.event_id for r in rows] == ["ev-3", "ev-2", "ev-1"]


@pytest.mark.unit
async def test_list_attempts_filter_by_webhook_id(storage) -> None:
    a1 = _make_attempt(webhook_id="wh-1", event_id="ev-1")
    a2 = _make_attempt(webhook_id="wh-2", event_id="ev-2")
    await storage.record_webhook_attempt(a1)
    await storage.record_webhook_attempt(a2)
    rows = await storage.list_webhook_attempts("tenant-a", webhook_id="wh-1")
    assert [r.event_id for r in rows] == ["ev-1"]


@pytest.mark.unit
async def test_list_attempts_tenant_scoped(storage) -> None:
    a = _make_attempt(tenant_id="tenant-a", event_id="ev-a")
    b = _make_attempt(tenant_id="tenant-b", event_id="ev-b")
    await storage.record_webhook_attempt(a)
    await storage.record_webhook_attempt(b)
    a_rows = await storage.list_webhook_attempts("tenant-a")
    b_rows = await storage.list_webhook_attempts("tenant-b")
    assert [r.event_id for r in a_rows] == ["ev-a"]
    assert [r.event_id for r in b_rows] == ["ev-b"]


@pytest.mark.unit
async def test_attempts_log_keeps_error_rows(storage) -> None:
    """Non-2xx + max_retries terminal rows round-trip in the log."""
    a_err = _make_attempt(
        event_id="ev-err",
        error_kind="http_error",
        status_code=500,
        attempt_n=2,
    )
    a_term = _make_attempt(
        event_id="ev-err",
        error_kind="max_retries",
        status_code=500,
        attempt_n=4,
    )
    await storage.record_webhook_attempt(a_err)
    await storage.record_webhook_attempt(a_term)
    rows = await storage.list_webhook_attempts("tenant-a")
    kinds = {r.error_kind for r in rows}
    assert kinds == {"http_error", "max_retries"}


# ---------------------------------------------------------------------------
# Per-webhook cursor
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_cursor_default_none(storage) -> None:
    sub = _make_sub()
    await storage.create_webhook(sub)
    assert await storage.get_webhook_cursor("tenant-a", sub.id) is None


@pytest.mark.unit
async def test_cursor_set_and_get(storage) -> None:
    sub = _make_sub()
    await storage.create_webhook(sub)
    await storage.set_webhook_cursor("tenant-a", sub.id, "ev-100")
    assert await storage.get_webhook_cursor("tenant-a", sub.id) == "ev-100"
    # Upsert overwrites in place.
    await storage.set_webhook_cursor("tenant-a", sub.id, "ev-200")
    assert await storage.get_webhook_cursor("tenant-a", sub.id) == "ev-200"


@pytest.mark.unit
async def test_cursor_tenant_isolated(storage) -> None:
    sub_a = _make_sub(tenant_id="tenant-a")
    sub_b = _make_sub(tenant_id="tenant-b")
    await storage.create_webhook(sub_a)
    await storage.create_webhook(sub_b)
    await storage.set_webhook_cursor("tenant-a", sub_a.id, "ev-A")
    await storage.set_webhook_cursor("tenant-b", sub_b.id, "ev-B")
    assert await storage.get_webhook_cursor("tenant-a", sub_a.id) == "ev-A"
    assert await storage.get_webhook_cursor("tenant-b", sub_b.id) == "ev-B"
    # Cross-tenant get returns None (the webhook id doesn't belong to
    # this tenant, so the cursor read can't surface a value).
    assert await storage.get_webhook_cursor("tenant-b", sub_a.id) is None
