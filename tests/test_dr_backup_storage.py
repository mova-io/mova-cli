"""DR backup/restore — storage export/import round-trip (item 26).

Exercises :func:`movate.core.dr_backup.export_state` /
:func:`~movate.core.dr_backup.import_state` (via the Protocol's
``export_state`` / ``import_state`` methods) across all three backends:
``InMemoryStorage``, ``SqliteProvider``, and ``PostgresProvider`` (skipped when
``MOVATE_PG_TEST_URL`` is unset).

Unlike most storage tests this needs TWO fresh stores of the same backend (a
seeded SOURCE + an empty TARGET) to prove a snapshot round-trips into a clean
deployment, so it builds its own backend pair instead of the shared single
``storage`` fixture.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from movate.core.auth import mint_api_key
from movate.core.dr_backup import SCHEMA_VERSION, SnapshotError
from movate.core.models import (
    AgentBundleRecord,
    ApiKeyEnv,
    CanaryConfig,
    EvalSchedule,
    JobKind,
    JobSchedule,
    TenantProviderKey,
)
from movate.storage.base import StorageProvider
from movate.storage.sqlite import SqliteProvider
from movate.testing import InMemoryStorage

_BACKENDS = [
    "memory",
    "sqlite",
    pytest.param("postgres", marks=pytest.mark.postgres),
]


def _pg_url() -> str | None:
    return os.environ.get("MOVATE_PG_TEST_URL")


async def _make_store(backend: str, path: Path, *, tag: str) -> StorageProvider:
    """Build + init one fresh store of ``backend``. ``tag`` keeps the two
    sqlite files (source/target) distinct under one tmp_path."""
    store: StorageProvider
    if backend == "memory":
        store = InMemoryStorage()
    elif backend == "sqlite":
        store = SqliteProvider(db_path=path / f"{tag}.db")
    else:  # postgres
        url = _pg_url()
        if url is None:
            pytest.skip("MOVATE_PG_TEST_URL not set; skipping postgres backend")
        from movate.storage.postgres import PostgresProvider  # noqa: PLC0415

        store = PostgresProvider(dsn=url)
    await store.init()
    return store


async def _wipe_pg(store: StorageProvider) -> None:
    pool = store._db  # type: ignore[attr-defined]
    await pool.execute(
        "TRUNCATE TABLE agent_bundles, api_keys, canary_configs, eval_schedules, "
        "job_schedules, tenant_provider_keys RESTART IDENTITY CASCADE"
    )


_StorePair = tuple[StorageProvider, StorageProvider]


@pytest.fixture(params=_BACKENDS)
async def store_pair(request, tmp_path: Path) -> AsyncIterator[_StorePair]:
    """Yield ``(source, target)`` — two fresh, hermetic stores of one backend."""
    backend = request.param
    # Postgres shares one server: source + target are the SAME physical DB, so
    # we can't truly use two. For postgres we seed, export, wipe, then import
    # into the now-empty same store — equivalent to a fresh-target restore.
    if backend == "postgres":
        store = await _make_store(backend, tmp_path, tag="pg")
        await _wipe_pg(store)
        try:
            yield (store, store)
        finally:
            await _wipe_pg(store)
            await store.close()
        return

    source = await _make_store(backend, tmp_path, tag="source")
    target = await _make_store(backend, tmp_path, tag="target")
    try:
        yield (source, target)
    finally:
        await source.close()
        await target.close()


# ---------------------------------------------------------------------------
# Seed helpers — one row per in-scope entity.
# ---------------------------------------------------------------------------


async def _seed(store: StorageProvider) -> dict[str, object]:
    """Populate a store with one of each in-scope entity. Returns the seeded
    api-key record so the test can assert hash/salt preservation."""
    now = datetime.now(UTC)

    # Two agent-bundle VERSIONS of the same agent → proves all versions export.
    await store.save_agent_bundle(
        AgentBundleRecord(
            name="faq-agent",
            tenant_id="tenant-a",
            version="2026.5.1.1",
            content_hash="hash-v1",
            files={"agent.yaml": "name: faq-agent\nversion: 2026.5.1.1\n"},
            created_at=now,
        )
    )
    await store.save_agent_bundle(
        AgentBundleRecord(
            name="faq-agent",
            tenant_id="tenant-a",
            version="2026.5.2.1",
            content_hash="hash-v2",
            files={"agent.yaml": "name: faq-agent\nversion: 2026.5.2.1\n", "prompt.md": "Hi"},
            created_at=now,
        )
    )

    minted = mint_api_key(tenant_id="tenant-a", env=ApiKeyEnv.LIVE, label="ci-bot")
    await store.save_api_key(minted.record)

    await store.save_canary_config(
        CanaryConfig(
            tenant_id="tenant-a",
            agent="faq-agent",
            challenger_version="2026.5.2.1",
            champion_version="2026.5.1.1",
            weight=20,
        )
    )
    await store.save_eval_schedule(
        EvalSchedule(tenant_id="tenant-a", agent="faq-agent", cadence_seconds=3600)
    )
    await store.save_job_schedule(
        JobSchedule(
            tenant_id="tenant-a",
            name="nightly",
            kind=JobKind.AGENT,
            target="faq-agent",
            cadence_seconds=86400,
        )
    )
    await store.save_tenant_provider_key(
        TenantProviderKey(
            tenant_id="tenant-a",
            provider="openai",
            ciphertext="gAAAAAB-fake-fernet-token",
            fingerprint="…AbCd",
        )
    )
    return {"api_key": minted.record}


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_export_shape_versioned(store_pair) -> None:
    source, _ = store_pair
    await _seed(source)
    snap = await source.export_state()
    assert snap["schema_version"] == SCHEMA_VERSION
    assert "exported_at" in snap
    entities = snap["entities"]
    assert len(entities["agent_bundles"]) == 2  # both versions
    assert len(entities["api_keys"]) == 1
    assert len(entities["canary_configs"]) == 1
    assert len(entities["eval_schedules"]) == 1
    assert len(entities["job_schedules"]) == 1
    assert len(entities["tenant_provider_keys"]) == 1


@pytest.mark.unit
async def test_round_trip_into_fresh_store(store_pair) -> None:
    source, target = store_pair
    await _seed(source)
    snap = await source.export_state()

    # Postgres uses one physical store: wipe it before importing so the
    # import lands into an empty target (a fresh-deployment restore).
    if source is target:
        await _wipe_pg(target)

    result = await target.import_state(snap)
    assert result.imported["agent_bundles"] == 2
    assert result.imported["api_keys"] == 1
    assert result.imported["canary_configs"] == 1
    assert result.imported["eval_schedules"] == 1
    assert result.imported["job_schedules"] == 1
    assert result.imported["tenant_provider_keys"] == 1
    assert result.total_skipped == 0

    # Key fields survive the round-trip.
    versions = await target.list_agent_versions("faq-agent", tenant_id="tenant-a")
    assert {b.version for b in versions} == {"2026.5.1.1", "2026.5.2.1"}
    v2 = await target.get_agent_bundle("faq-agent", tenant_id="tenant-a", version="2026.5.2.1")
    assert v2 is not None
    assert v2.files["prompt.md"] == "Hi"

    canary = await target.get_canary_config("faq-agent", tenant_id="tenant-a")
    assert canary is not None and canary.weight == 20

    es = await target.get_eval_schedule("faq-agent", tenant_id="tenant-a")
    assert es is not None and es.cadence_seconds == 3600

    js = await target.get_job_schedule("nightly", tenant_id="tenant-a")
    assert js is not None and js.target == "faq-agent"

    pk = await target.get_tenant_provider_key("openai", tenant_id="tenant-a")
    assert pk is not None
    assert pk.ciphertext == "gAAAAAB-fake-fernet-token"  # ciphertext exported verbatim
    assert pk.fingerprint == "…AbCd"


@pytest.mark.unit
async def test_api_key_hash_and_salt_preserved(store_pair) -> None:
    """Exported api-key rows keep secret_hash + salt, so existing keys keep
    working after restore (the plaintext was never stored)."""
    source, target = store_pair
    seeded = await _seed(source)
    original = seeded["api_key"]
    snap = await source.export_state()
    if source is target:
        await _wipe_pg(target)
    await target.import_state(snap)

    restored = await target.get_api_key(original.key_id)  # type: ignore[attr-defined]
    assert restored is not None
    assert restored.secret_hash == original.secret_hash  # type: ignore[attr-defined]
    assert restored.salt == original.salt  # type: ignore[attr-defined]
    assert restored.tenant_id == "tenant-a"
    assert restored.env == ApiKeyEnv.LIVE


@pytest.mark.unit
async def test_reimport_skip_existing_is_idempotent(store_pair) -> None:
    source, target = store_pair
    await _seed(source)
    snap = await source.export_state()
    if source is target:
        await _wipe_pg(target)

    first = await target.import_state(snap)  # skip-existing default
    assert first.total_imported == 7

    # Re-import the same snapshot — nothing new, everything skipped.
    second = await target.import_state(snap)
    assert second.total_imported == 0
    assert second.total_skipped == 7


@pytest.mark.unit
async def test_overwrite_mode_resaves(store_pair) -> None:
    source, target = store_pair
    await _seed(source)
    snap = await source.export_state()
    if source is target:
        await _wipe_pg(target)
    await target.import_state(snap)

    # Bump the canary weight in the target, then overwrite-restore: the
    # snapshot's value wins.
    bumped = await target.get_canary_config("faq-agent", tenant_id="tenant-a")
    assert bumped is not None
    await target.save_canary_config(bumped.model_copy(update={"weight": 99}))

    result = await target.import_state(snap, mode="overwrite")
    assert result.total_skipped == 0
    restored = await target.get_canary_config("faq-agent", tenant_id="tenant-a")
    assert restored is not None and restored.weight == 20  # snapshot value re-applied

    # Agent versions are not duplicated by an overwrite re-import.
    versions = await target.list_agent_versions("faq-agent", tenant_id="tenant-a")
    assert len(versions) == 2


@pytest.mark.unit
async def test_cross_tenant_export_covers_all_tenants(store_pair) -> None:
    """The export must capture every tenant's rows, not just one."""
    source, _target = store_pair
    await _seed(source)  # tenant-a
    await source.save_agent_bundle(
        AgentBundleRecord(
            name="other",
            tenant_id="tenant-b",
            version="1.0.0",
            content_hash="h",
            files={"agent.yaml": "name: other\n"},
        )
    )
    await source.save_tenant_provider_key(
        TenantProviderKey(
            tenant_id="tenant-b",
            provider="anthropic",
            ciphertext="gAAAAAB-tenant-b",
            fingerprint="…WxYz",
        )
    )
    snap = await source.export_state()
    tenants = {b["tenant_id"] for b in snap["entities"]["agent_bundles"]}
    assert tenants == {"tenant-a", "tenant-b"}
    pk_tenants = {k["tenant_id"] for k in snap["entities"]["tenant_provider_keys"]}
    assert pk_tenants == {"tenant-a", "tenant-b"}


# ---------------------------------------------------------------------------
# Malformed / version errors
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_import_missing_schema_version_errors(store_pair) -> None:
    _, target = store_pair
    with pytest.raises(SnapshotError, match="schema_version"):
        await target.import_state({"entities": {}})


@pytest.mark.unit
async def test_import_future_schema_version_refused(store_pair) -> None:
    _, target = store_pair
    bad = {"schema_version": SCHEMA_VERSION + 1, "entities": {}}
    with pytest.raises(SnapshotError, match="newer than this build"):
        await target.import_state(bad)


@pytest.mark.unit
async def test_import_unknown_mode_errors(store_pair) -> None:
    _, target = store_pair
    snap = {"schema_version": SCHEMA_VERSION, "entities": {}}
    with pytest.raises(SnapshotError, match="unknown import mode"):
        await target.import_state(snap, mode="nuke-everything")


@pytest.mark.unit
async def test_import_unknown_entity_noted_not_crashed(store_pair) -> None:
    """A snapshot from a hypothetical newer minor with an extra entity imports
    best-effort, noting the unrecognised key rather than crashing."""
    _, target = store_pair
    snap = {
        "schema_version": SCHEMA_VERSION,
        "entities": {"agent_bundles": [], "future_entity": [{"x": 1}]},
    }
    result = await target.import_state(snap)
    assert result.unknown == ["future_entity"]
    assert result.total_imported == 0
