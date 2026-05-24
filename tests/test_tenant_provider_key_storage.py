"""Tenant provider-key storage — save/get/list/delete + tenant isolation (ADR 018).

Runs across all three backends via the shared ``storage`` fixture in
conftest.py — ``InMemoryStorage``, ``SqliteProvider``, and ``PostgresProvider``
(skipped when ``MOVATE_PG_TEST_URL`` is unset).

Asserts the additive table is default-off (no rows until written), upserts on
``(tenant_id, provider)`` (rotation in place), round-trips every field, is
tenant-scoped (no leak), and that the stored row carries only the ciphertext +
masked fingerprint — never plaintext.
"""

from __future__ import annotations

import pytest

from movate.core.models import TenantProviderKey


def _make_key(
    *,
    tenant_id: str = "tenant-a",
    provider: str = "openai",
    ciphertext: str = "gAAAAAB-ciphertext-token",
    fingerprint: str = "…AbCd",
) -> TenantProviderKey:
    return TenantProviderKey(
        tenant_id=tenant_id,
        provider=provider,
        ciphertext=ciphertext,
        fingerprint=fingerprint,
        created_by="key-xyz",
    )


@pytest.mark.unit
async def test_default_off_no_rows(storage) -> None:
    assert await storage.list_tenant_provider_keys(tenant_id="tenant-a") == []
    assert await storage.get_tenant_provider_key("openai", tenant_id="tenant-a") is None


@pytest.mark.unit
async def test_save_and_get_round_trip(storage) -> None:
    await storage.save_tenant_provider_key(_make_key())
    got = await storage.get_tenant_provider_key("openai", tenant_id="tenant-a")
    assert got is not None
    assert got.tenant_id == "tenant-a"
    assert got.provider == "openai"
    assert got.ciphertext == "gAAAAAB-ciphertext-token"
    assert got.fingerprint == "…AbCd"
    assert got.created_by == "key-xyz"


@pytest.mark.unit
async def test_save_upserts_on_tenant_provider(storage) -> None:
    """A re-set rotates the key in place — one row per (tenant, provider)."""
    await storage.save_tenant_provider_key(_make_key(ciphertext="ct-1", fingerprint="…1111"))
    await storage.save_tenant_provider_key(_make_key(ciphertext="ct-2", fingerprint="…2222"))
    rows = await storage.list_tenant_provider_keys(tenant_id="tenant-a")
    assert len(rows) == 1
    assert rows[0].ciphertext == "ct-2"
    assert rows[0].fingerprint == "…2222"


@pytest.mark.unit
async def test_get_is_tenant_scoped(storage) -> None:
    await storage.save_tenant_provider_key(_make_key(tenant_id="tenant-a"))
    assert await storage.get_tenant_provider_key("openai", tenant_id="tenant-a") is not None
    # No leak across tenants.
    assert await storage.get_tenant_provider_key("openai", tenant_id="tenant-b") is None


@pytest.mark.unit
async def test_list_is_tenant_scoped(storage) -> None:
    await storage.save_tenant_provider_key(_make_key(tenant_id="tenant-a", provider="openai"))
    await storage.save_tenant_provider_key(_make_key(tenant_id="tenant-a", provider="anthropic"))
    await storage.save_tenant_provider_key(_make_key(tenant_id="tenant-b", provider="openai"))
    only_a = await storage.list_tenant_provider_keys(tenant_id="tenant-a")
    assert {k.provider for k in only_a} == {"openai", "anthropic"}
    only_b = await storage.list_tenant_provider_keys(tenant_id="tenant-b")
    assert {k.provider for k in only_b} == {"openai"}


@pytest.mark.unit
async def test_delete_returns_true_then_false(storage) -> None:
    await storage.save_tenant_provider_key(_make_key())
    assert await storage.delete_tenant_provider_key("openai", tenant_id="tenant-a") is True
    assert await storage.get_tenant_provider_key("openai", tenant_id="tenant-a") is None
    assert await storage.delete_tenant_provider_key("openai", tenant_id="tenant-a") is False


@pytest.mark.unit
async def test_delete_is_tenant_scoped(storage) -> None:
    await storage.save_tenant_provider_key(_make_key(tenant_id="tenant-a"))
    assert await storage.delete_tenant_provider_key("openai", tenant_id="tenant-b") is False
    assert await storage.get_tenant_provider_key("openai", tenant_id="tenant-a") is not None
