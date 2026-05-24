"""Key rotation + lifecycle (ADR 013 D5) — pure helper, storage, middleware.

Three layers, asserted independently:

1. **Pure helper** (``core/auth.rotate_key_record``): successor inherits
   env/scopes/label; grace clamping; old-key expiry = now + grace.
2. **Storage** (``set_api_key_expiry`` + ``revoke_all_api_keys``): round-trip
   over the shared ``storage`` fixture (memory + sqlite + postgres-skip-guard);
   tenant scoping; bulk-revoke spares the excepted key + counts correctly.
3. **Middleware expiry enforcement** (the load-bearing point): a key past
   ``expires_at`` → 401; inside its window → 200. The whole grace-window
   rotation model depends on this, so it's proven directly through the
   FastAPI auth dependency.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import (
    KEY_DEFAULT_ROTATION_GRACE_SECONDS,
    KEY_MAX_ROTATION_GRACE_SECONDS,
    ROTATED_LABEL_SUFFIX,
    mint_api_key,
    rotate_key_record,
)
from movate.core.models import ApiKeyEnv
from movate.runtime import build_app
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# 1. Pure helper — rotate_key_record
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rotate_successor_inherits_env_scopes_label() -> None:
    old = mint_api_key(
        tenant_id=uuid4().hex,
        env=ApiKeyEnv.TEST,
        label="ci-bot",
        scopes=["read", "run", "admin"],
    ).record
    rotated = rotate_key_record(old)
    new = rotated.minted.record
    assert new.env == ApiKeyEnv.TEST
    assert sorted(new.scopes) == ["admin", "read", "run"]
    assert new.tenant_id == old.tenant_id
    # Label inherited + suffixed so the two are distinguishable.
    assert new.label == f"ci-bot{ROTATED_LABEL_SUFFIX}"
    # A fresh, different key id + secret.
    assert new.key_id != old.key_id
    assert rotated.minted.full_key.startswith("mvt_test_")


@pytest.mark.unit
def test_rotate_empty_scopes_stay_empty() -> None:
    """A scopeless key rotates to a scopeless successor (never widened)."""
    old = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE).record
    new = rotate_key_record(old).minted.record
    assert new.scopes == []


@pytest.mark.unit
def test_rotate_old_expiry_is_now_plus_grace() -> None:
    old = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE).record
    now = datetime(2026, 1, 1, tzinfo=UTC)
    rotated = rotate_key_record(old, grace_seconds=3600, now=now)
    assert rotated.old_expires_at == now + timedelta(seconds=3600)


@pytest.mark.unit
def test_rotate_default_grace_is_24h() -> None:
    old = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE).record
    now = datetime(2026, 1, 1, tzinfo=UTC)
    rotated = rotate_key_record(old, now=now)
    assert rotated.old_expires_at == now + timedelta(seconds=KEY_DEFAULT_ROTATION_GRACE_SECONDS)


@pytest.mark.unit
def test_rotate_grace_is_capped() -> None:
    old = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE).record
    now = datetime(2026, 1, 1, tzinfo=UTC)
    rotated = rotate_key_record(old, grace_seconds=10**9, now=now)
    assert rotated.old_expires_at == now + timedelta(seconds=KEY_MAX_ROTATION_GRACE_SECONDS)


@pytest.mark.unit
def test_rotate_negative_grace_clamped_to_zero() -> None:
    old = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE).record
    now = datetime(2026, 1, 1, tzinfo=UTC)
    rotated = rotate_key_record(old, grace_seconds=-99, now=now)
    assert rotated.old_expires_at == now


@pytest.mark.unit
def test_rotate_does_not_double_suffix_label() -> None:
    """Rotating an already-rotated key doesn't stack the suffix."""
    old = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label=f"x{ROTATED_LABEL_SUFFIX}"
    ).record
    new = rotate_key_record(old).minted.record
    assert new.label == f"x{ROTATED_LABEL_SUFFIX}"


# ---------------------------------------------------------------------------
# 2. Storage — set_api_key_expiry + revoke_all_api_keys
#    (shared ``storage`` fixture: memory + sqlite + postgres skip-guard)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_set_api_key_expiry_round_trips(storage) -> None:
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, ttl_days=0)
    await storage.save_api_key(minted.record)
    new_expiry = datetime(2030, 6, 1, tzinfo=UTC)
    await storage.set_api_key_expiry(
        minted.record.key_id, tenant_id=minted.record.tenant_id, expires_at=new_expiry
    )
    got = await storage.get_api_key(minted.record.key_id)
    assert got is not None
    assert got.expires_at == new_expiry


@pytest.mark.unit
async def test_set_api_key_expiry_tenant_scoped(storage) -> None:
    """A different tenant can't move another tenant's expiry."""
    minted = mint_api_key(tenant_id="aaaaaaaa" + uuid4().hex, env=ApiKeyEnv.LIVE, ttl_days=90)
    await storage.save_api_key(minted.record)
    original = (await storage.get_api_key(minted.record.key_id)).expires_at
    await storage.set_api_key_expiry(
        minted.record.key_id,
        tenant_id="bbbbbbbb" + uuid4().hex,  # wrong tenant
        expires_at=datetime(2030, 6, 1, tzinfo=UTC),
    )
    got = await storage.get_api_key(minted.record.key_id)
    assert got is not None
    assert got.expires_at == original  # unchanged


@pytest.mark.unit
async def test_set_api_key_expiry_noop_on_revoked(storage) -> None:
    """A revoked key is dead regardless; we never re-arm its expiry."""
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, ttl_days=90)
    await storage.save_api_key(minted.record)
    await storage.revoke_api_key(minted.record.key_id, tenant_id=minted.record.tenant_id)
    original = (await storage.get_api_key(minted.record.key_id)).expires_at
    await storage.set_api_key_expiry(
        minted.record.key_id,
        tenant_id=minted.record.tenant_id,
        expires_at=datetime(2040, 1, 1, tzinfo=UTC),
    )
    got = await storage.get_api_key(minted.record.key_id)
    assert got is not None
    assert got.expires_at == original  # unchanged — revoked keys aren't re-armed


@pytest.mark.unit
async def test_revoke_all_revokes_active_keys_and_counts(storage) -> None:
    tenant = "tttttttt" + uuid4().hex
    keys = [mint_api_key(tenant_id=tenant, env=ApiKeyEnv.LIVE) for _ in range(3)]
    for m in keys:
        await storage.save_api_key(m.record)
    count = await storage.revoke_all_api_keys(tenant_id=tenant)
    assert count == 3
    for m in keys:
        got = await storage.get_api_key(m.record.key_id)
        assert got is not None
        assert got.revoked_at is not None


@pytest.mark.unit
async def test_revoke_all_spares_excepted_key(storage) -> None:
    tenant = "tttttttt" + uuid4().hex
    spare = mint_api_key(tenant_id=tenant, env=ApiKeyEnv.LIVE)
    other = mint_api_key(tenant_id=tenant, env=ApiKeyEnv.LIVE)
    await storage.save_api_key(spare.record)
    await storage.save_api_key(other.record)
    count = await storage.revoke_all_api_keys(tenant_id=tenant, except_key_id=spare.record.key_id)
    assert count == 1  # only `other`
    assert (await storage.get_api_key(spare.record.key_id)).revoked_at is None
    assert (await storage.get_api_key(other.record.key_id)).revoked_at is not None


@pytest.mark.unit
async def test_revoke_all_is_tenant_scoped(storage) -> None:
    tenant_a = "aaaaaaaa" + uuid4().hex
    tenant_b = "bbbbbbbb" + uuid4().hex
    a = mint_api_key(tenant_id=tenant_a, env=ApiKeyEnv.LIVE)
    b = mint_api_key(tenant_id=tenant_b, env=ApiKeyEnv.LIVE)
    await storage.save_api_key(a.record)
    await storage.save_api_key(b.record)
    count = await storage.revoke_all_api_keys(tenant_id=tenant_a)
    assert count == 1
    assert (await storage.get_api_key(b.record.key_id)).revoked_at is None  # B untouched


@pytest.mark.unit
async def test_revoke_all_idempotent_second_run_is_zero(storage) -> None:
    tenant = "tttttttt" + uuid4().hex
    m = mint_api_key(tenant_id=tenant, env=ApiKeyEnv.LIVE)
    await storage.save_api_key(m.record)
    assert await storage.revoke_all_api_keys(tenant_id=tenant) == 1
    assert await storage.revoke_all_api_keys(tenant_id=tenant) == 0


# ---------------------------------------------------------------------------
# 3. Middleware expiry enforcement (LOAD-BEARING)
#    The grace-window model only works if an expired key is rejected.
# ---------------------------------------------------------------------------


@pytest.fixture
async def mw_storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def mw_client(mw_storage: InMemoryStorage) -> TestClient:
    return TestClient(build_app(mw_storage))


async def _save(storage: InMemoryStorage, record) -> None:
    await storage.save_api_key(record)


@pytest.mark.unit
def test_expired_key_is_rejected_401(mw_client: TestClient, mw_storage: InMemoryStorage) -> None:
    """A key whose ``expires_at`` is in the past → 401 (load-bearing)."""
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, ttl_days=90)
    expired = minted.record.model_copy(
        update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
    )
    import asyncio  # noqa: PLC0415

    asyncio.get_event_loop().run_until_complete(_save(mw_storage, expired))
    resp = mw_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {minted.full_key}"})
    assert resp.status_code == 401


@pytest.mark.unit
def test_unexpired_key_is_accepted_200(mw_client: TestClient, mw_storage: InMemoryStorage) -> None:
    """A key well inside its window authenticates normally (regression guard:
    the expiry check must not reject still-valid keys)."""
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, ttl_days=90)
    import asyncio  # noqa: PLC0415

    asyncio.get_event_loop().run_until_complete(_save(mw_storage, minted.record))
    resp = mw_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {minted.full_key}"})
    assert resp.status_code == 200
