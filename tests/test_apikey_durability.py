"""Durable API-key store (#122): keys survive a runtime "restart".

The recurring failure mode: a runtime seeds / mints an API key, the
container recycles, and every previously-valid key returns 401 because
the ApiKeyRecord table lived in an ephemeral in-pod SQLite file (or was
never re-seeded). These tests pin the durability contract directly at
the storage seam:

1. A key persisted by one provider instance is still readable by a
   FRESH provider instance bound to the SAME backend — the "restart".
   Exercised for persistent SQLite (a real on-disk file) and, when
   ``MOVATE_PG_TEST_URL`` is set, Postgres.
2. The cold-start re-seed (``_seed_bootstrap_key``) is idempotent and
   self-heals a concurrent insert race (multi-pod fresh deploy) instead
   of crashing startup.

These complement ``test_serve_seed_heal.py`` (scope-heal semantics over
the in-memory store) by proving the *cross-instance persistence* the
fix is actually about.
"""

from __future__ import annotations

import base64
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from movate.cli.serve import _seed_bootstrap_key
from movate.core.auth import (
    SCOPE_FLEET_ADMIN,
    ApiKeyEnv,
    hash_secret,
    mint_api_key,
    parse_api_key,
)
from movate.core.models import ApiKeyRecord
from movate.storage.sqlite import SqliteProvider
from movate.testing import InMemoryStorage


def _pg_test_url() -> str | None:
    return os.environ.get("MOVATE_PG_TEST_URL")


def _seed_record_from(full_key: str) -> ApiKeyRecord:
    """Build the ApiKeyRecord the runtime seed would persist for a key
    string — mirrors ``_seed_bootstrap_key``'s insert path."""
    parsed = parse_api_key(full_key)
    salt = base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode("ascii")
    return ApiKeyRecord(
        key_id=parsed.key_id,
        tenant_id=parsed.tenant_prefix,
        env=parsed.env,
        secret_hash=hash_secret(parsed.secret, salt),
        salt=salt,
        label="seed",
        created_at=datetime.now(UTC),
        scopes=[SCOPE_FLEET_ADMIN],
    )


# ---------------------------------------------------------------------------
# Cross-instance persistence — the "restart"
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_sqlite_key_survives_restart_on_persistent_path(tmp_path: Path) -> None:
    """A key written to a persistent SQLite FILE is still authenticable
    after the provider is closed and a NEW provider opens the same path —
    the local-dev analogue of a container restart."""
    db_path = tmp_path / "durable.db"
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="seed")
    record = _seed_record_from(minted.full_key)

    # First "boot": persist then shut down.
    first = SqliteProvider(db_path=db_path)
    await first.init()
    await first.save_api_key(record)
    await first.close()

    # Second "boot": a brand-new instance on the SAME file.
    second = SqliteProvider(db_path=db_path)
    await second.init()
    try:
        loaded = await second.get_api_key(record.key_id)
    finally:
        await second.close()

    assert loaded is not None, "key vanished across restart — not durable"
    assert loaded.key_id == record.key_id
    assert loaded.secret_hash == record.secret_hash
    assert SCOPE_FLEET_ADMIN in loaded.scopes


@pytest.mark.postgres
async def test_postgres_key_survives_restart() -> None:
    """Gated on MOVATE_PG_TEST_URL. A key written by one PostgresProvider
    instance is readable by a fresh instance (new pool) against the same
    DB — Postgres is the durable backend the fix steers production to."""
    url = _pg_test_url()
    if url is None:
        pytest.skip("MOVATE_PG_TEST_URL not set; skipping postgres durability test")
    from movate.storage.postgres import PostgresProvider  # noqa: PLC0415

    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="seed")
    record = _seed_record_from(minted.full_key)

    first = PostgresProvider(dsn=url)
    await first.init()
    try:
        # Clean any prior row for this key_id (hermetic).
        await first.save_api_key(record)
    finally:
        await first.close()

    second = PostgresProvider(dsn=url)
    await second.init()
    try:
        loaded = await second.get_api_key(record.key_id)
        assert loaded is not None, "key vanished across restart on Postgres"
        assert loaded.secret_hash == record.secret_hash
        assert SCOPE_FLEET_ADMIN in loaded.scopes
    finally:
        # Cleanup so reruns stay hermetic.
        await second.revoke_api_key(record.key_id, tenant_id=record.tenant_id)
        await second.close()


# ---------------------------------------------------------------------------
# Cold-start re-seed durability across a restart
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_reseed_after_restart_is_idempotent_no_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running the seed against a backend that already has the key
    (a redeploy / restart) does NOT insert a second row and leaves the
    grant intact — the self-healing-but-idempotent contract."""
    db_path = tmp_path / "reseed.db"
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="seed")
    monkeypatch.setenv("MOVATE_SEED_API_KEY", minted.full_key)

    # Boot 1: seed into a fresh DB.
    first = SqliteProvider(db_path=db_path)
    await first.init()
    await _seed_bootstrap_key(first)
    await first.close()

    # Boot 2: same file, seed again (simulated restart).
    second = SqliteProvider(db_path=db_path)
    await second.init()
    try:
        await _seed_bootstrap_key(second)
        rows = await second.list_api_keys(include_revoked=True)
        matching = [k for k in rows if k.key_id == minted.record.key_id]
        assert len(matching) == 1, "re-seed must not duplicate the bootstrap row"
        assert SCOPE_FLEET_ADMIN in matching[0].scopes
    finally:
        await second.close()


@pytest.mark.unit
async def test_reseed_tolerates_concurrent_insert_race(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multi-pod fresh deploy: two pods both pass the get_api_key()
    no-row check, then race the insert. ``save_api_key`` is insert-only,
    so the loser hits a uniqueness violation — the seed must treat a
    now-present fleet-admin row as success, not crash startup."""
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="seed")
    monkeypatch.setenv("MOVATE_SEED_API_KEY", minted.full_key)

    storage = InMemoryStorage()
    await storage.init()

    # Simulate the winning pod having ALREADY inserted the row between
    # this pod's get_api_key() check and its save_api_key() call: make
    # save_api_key raise as if on a duplicate key, with the row present.
    real_save = storage.save_api_key
    raised = {"n": 0}

    async def racing_save(record: ApiKeyRecord) -> None:
        # Persist via the real path so the post-failure re-read finds it,
        # then raise to mimic the DB's uniqueness violation on the loser.
        await real_save(record)
        raised["n"] += 1
        raise RuntimeError("duplicate key value violates unique constraint")

    monkeypatch.setattr(storage, "save_api_key", racing_save)

    # Must NOT raise — the seed self-heals on the concurrent insert.
    await _seed_bootstrap_key(storage)

    assert raised["n"] == 1, "the racing save should have been attempted once"
    loaded = await storage.get_api_key(minted.record.key_id)
    assert loaded is not None
    assert SCOPE_FLEET_ADMIN in loaded.scopes


@pytest.mark.unit
async def test_reseed_reraises_when_row_absent_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine save failure (NOT a benign race — the row is still
    absent afterward) must re-raise so a real storage fault surfaces
    loudly instead of being swallowed."""
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="seed")
    monkeypatch.setenv("MOVATE_SEED_API_KEY", minted.full_key)

    storage = InMemoryStorage()
    await storage.init()

    async def failing_save(record: ApiKeyRecord) -> None:
        raise RuntimeError("disk full / connection reset")

    monkeypatch.setattr(storage, "save_api_key", failing_save)

    with pytest.raises(RuntimeError, match="disk full"):
        await _seed_bootstrap_key(storage)
