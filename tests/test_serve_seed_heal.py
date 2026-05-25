"""Runtime bootstrap-key seed self-heal — ``_seed_bootstrap_key``.

Live-Azure finding: the seed was historically insert-only, so a bootstrap
key seeded by an OLDER image (before fleet-admin seeding / before the #61
scope fix) kept its stale/narrow scope forever across redeploys. A
``.11``-seeded key with ``["admin"]`` (no ``read``) 403'd on
``read``-scoped endpoints even on a fixed image, because the
``effective_scopes`` fleet-admin expansion only helps rows whose stored
scope IS fleet-admin — it can't widen a row seeded with a narrow scope.

The fix makes the seed self-heal a stale bootstrap-key scope on startup:
keep idempotent no-op when the row already grants fleet-admin, otherwise
rewrite the row's ``scopes`` to ``["fleet-admin"]`` in place, preserving
secret_hash / salt / tenant_id / env / created_at (the key value is
unchanged — never re-hashed/re-salted).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from movate.cli.serve import _seed_bootstrap_key
from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.testing import InMemoryStorage


async def _store_with_seed(*, scopes: list[str] | None) -> tuple[InMemoryStorage, str, str]:
    """Mint a real seed key, persist its record with ``scopes``, and return
    ``(storage, full_key, key_id)``. ``scopes`` is set verbatim on the
    persisted row to simulate what an older image seeded."""
    storage = InMemoryStorage()
    await storage.init()
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="seed")
    record = minted.record.model_copy(update={"scopes": list(scopes or [])})
    await storage.save_api_key(record)
    return storage, minted.full_key, record.key_id


# ---------------------------------------------------------------------------
# Heal a stale / narrow scope
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("narrow", [["admin"], ["read"], ["read", "run"], []])
async def test_existing_narrow_scope_healed_to_fleet_admin(
    narrow: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bootstrap row seeded by an older image with a narrow scope (or no
    explicit scope at all) is healed in place to ``["fleet-admin"]``."""
    storage, full_key, key_id = await _store_with_seed(scopes=narrow)
    before = await storage.get_api_key(key_id)
    assert before is not None
    monkeypatch.setenv("MOVATE_SEED_API_KEY", full_key)

    await _seed_bootstrap_key(storage)

    after = await storage.get_api_key(key_id)
    assert after is not None
    # Scope healed to fleet-admin.
    assert after.scopes == ["fleet-admin"]
    # Secret material + identity columns preserved (never re-hashed/re-salted).
    assert after.secret_hash == before.secret_hash
    assert after.salt == before.salt
    assert after.tenant_id == before.tenant_id
    assert after.env == before.env
    assert after.created_at == before.created_at


@pytest.mark.unit
async def test_heal_does_not_insert_a_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Healing updates the SAME row — no second row is appended (the seed
    is insert-only, so a re-save would have errored)."""
    storage, full_key, key_id = await _store_with_seed(scopes=["admin"])
    monkeypatch.setenv("MOVATE_SEED_API_KEY", full_key)

    await _seed_bootstrap_key(storage)

    matching = [k for k in storage.api_keys if k.key_id == key_id]
    assert len(matching) == 1


# ---------------------------------------------------------------------------
# Already fleet-admin → idempotent no-op
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_existing_fleet_admin_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row already carrying ``["fleet-admin"]`` is left byte-for-byte
    unchanged (no write) on redeploy."""
    storage, full_key, key_id = await _store_with_seed(scopes=["fleet-admin"])
    before = await storage.get_api_key(key_id)
    assert before is not None
    monkeypatch.setenv("MOVATE_SEED_API_KEY", full_key)

    await _seed_bootstrap_key(storage)

    after = await storage.get_api_key(key_id)
    assert after == before


@pytest.mark.unit
async def test_existing_fleet_admin_mixed_scopes_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fleet-admin mixed with other scopes is still a fleet-admin grant
    (membership check, representation-agnostic) → no heal, unchanged."""
    storage, full_key, key_id = await _store_with_seed(scopes=["read", "fleet-admin"])
    before = await storage.get_api_key(key_id)
    assert before is not None
    monkeypatch.setenv("MOVATE_SEED_API_KEY", full_key)

    await _seed_bootstrap_key(storage)

    after = await storage.get_api_key(key_id)
    assert after == before


# ---------------------------------------------------------------------------
# No existing row → insert with fleet-admin (today's behavior, unchanged)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_no_existing_row_inserts_fleet_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh deploy (no row yet) inserts the bootstrap key with
    ``["fleet-admin"]`` — the pre-existing insert path is unchanged."""
    storage = InMemoryStorage()
    await storage.init()
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="seed")
    monkeypatch.setenv("MOVATE_SEED_API_KEY", minted.full_key)

    await _seed_bootstrap_key(storage)

    inserted = await storage.get_api_key(minted.record.key_id)
    assert inserted is not None
    assert inserted.scopes == ["fleet-admin"]
    # The insert path derives tenant_id from the parsed key's tenant_prefix
    # (the 8-char segment in the key string), not the full mint-time tenant_id.
    assert inserted.tenant_id == minted.record.tenant_id[:8]
    assert inserted.env == minted.record.env


# ---------------------------------------------------------------------------
# Unset / malformed env → no-op
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_unset_env_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``MOVATE_SEED_API_KEY`` → nothing seeded, nothing healed."""
    storage = InMemoryStorage()
    await storage.init()
    monkeypatch.delenv("MOVATE_SEED_API_KEY", raising=False)

    await _seed_bootstrap_key(storage)

    assert storage.api_keys == []


@pytest.mark.unit
async def test_malformed_env_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed key string is skipped (no row touched)."""
    storage = InMemoryStorage()
    await storage.init()
    monkeypatch.setenv("MOVATE_SEED_API_KEY", "not-a-valid-movate-key")

    await _seed_bootstrap_key(storage)

    assert storage.api_keys == []
