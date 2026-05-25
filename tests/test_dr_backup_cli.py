"""CLI — ``mdk export state`` / ``mdk import state`` DR escape hatch (item 26).

Covers:

* ``mdk export state <file>`` writes a versioned JSON backup of the seeded
  control-plane state; ``mdk import state <file>`` restores it into a fresh DB
  and reports per-entity counts.
* gzip is auto-detected by a ``.gz`` suffix.
* a malformed / old-``schema_version`` file produces a clean error (exit 2),
  not a traceback.

Both commands run DB-direct against the env-selected backend; the ``local_db``
fixture points ``MOVATE_DB`` at a tmp sqlite file (the SOURCE), and the restore
test points it at a second tmp file (the TARGET) before importing.
"""

from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.auth import mint_api_key
from movate.core.models import (
    AgentBundleRecord,
    ApiKeyEnv,
    CanaryConfig,
    TenantProviderKey,
)
from movate.storage import build_storage

runner = CliRunner(mix_stderr=False)


@pytest.fixture
def env_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOVATE_TRACER", "silent")


async def _seed() -> None:
    """Seed the currently-selected (MOVATE_DB) storage with one of each entity.
    The caller sets ``MOVATE_DB`` (via monkeypatch) before invoking."""
    storage = build_storage()
    await storage.init()
    try:
        await storage.save_agent_bundle(
            AgentBundleRecord(
                name="faq-agent",
                tenant_id="tenant-a",
                version="1.0.0",
                content_hash="h",
                files={"agent.yaml": "name: faq-agent\n"},
            )
        )
        minted = mint_api_key(tenant_id="tenant-a", env=ApiKeyEnv.LIVE, label="ci")
        await storage.save_api_key(minted.record)
        await storage.save_canary_config(
            CanaryConfig(
                tenant_id="tenant-a",
                agent="faq-agent",
                challenger_version="1.0.0",
                weight=5,
            )
        )
        await storage.save_tenant_provider_key(
            TenantProviderKey(
                tenant_id="tenant-a",
                provider="openai",
                ciphertext="gAAAAAB-ct",
                fingerprint="…AbCd",
            )
        )
    finally:
        await storage.close()


async def _count_agents() -> int:
    """Count agent bundles in the currently-selected (MOVATE_DB) storage."""
    storage = build_storage()
    await storage.init()
    try:
        return len(await storage.list_all_agent_bundles())
    finally:
        await storage.close()


@pytest.mark.unit
def test_export_then_import_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_quiet: None
) -> None:
    source_db = tmp_path / "source.db"
    target_db = tmp_path / "target.db"
    backup = tmp_path / "backup.json"

    # Seed the SOURCE db and export from it.
    monkeypatch.setenv("MOVATE_DB", str(source_db))
    asyncio.run(_seed())

    r = runner.invoke(app, ["export", "state", str(backup)])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert backup.is_file()
    snap = json.loads(backup.read_text())
    assert snap["schema_version"] == 1
    assert len(snap["entities"]["agent_bundles"]) == 1
    assert len(snap["entities"]["api_keys"]) == 1

    # Import into a FRESH target db.
    monkeypatch.setenv("MOVATE_DB", str(target_db))
    imp = runner.invoke(app, ["import", "state", str(backup)])
    assert imp.exit_code == 0, imp.stdout + imp.stderr
    assert "imported 4" in imp.stderr or "imported 4" in imp.stdout

    assert asyncio.run(_count_agents()) == 1

    # Re-import is idempotent (skip-existing default) → 0 new.
    again = runner.invoke(app, ["import", "state", str(backup)])
    assert again.exit_code == 0
    assert "imported 0" in again.stderr or "imported 0" in again.stdout


@pytest.mark.unit
def test_export_gzip_auto_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_quiet: None
) -> None:
    source_db = tmp_path / "source.db"
    backup = tmp_path / "backup.json.gz"
    monkeypatch.setenv("MOVATE_DB", str(source_db))
    asyncio.run(_seed())

    r = runner.invoke(app, ["export", "state", str(backup)])
    assert r.exit_code == 0, r.stdout + r.stderr
    # The file is real gzip (decompresses to the JSON snapshot).
    snap = json.loads(gzip.decompress(backup.read_bytes()))
    assert snap["schema_version"] == 1

    # And import reads it back transparently.
    target_db = tmp_path / "target.db"
    monkeypatch.setenv("MOVATE_DB", str(target_db))
    imp = runner.invoke(app, ["import", "state", str(backup), "--format", "json"])
    assert imp.exit_code == 0, imp.stdout + imp.stderr
    summary = json.loads(imp.stdout)
    assert summary["total_imported"] == 4


@pytest.mark.unit
def test_export_refuses_overwrite_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_quiet: None
) -> None:
    source_db = tmp_path / "source.db"
    backup = tmp_path / "backup.json"
    backup.write_text("{}")
    monkeypatch.setenv("MOVATE_DB", str(source_db))
    asyncio.run(_seed())

    r = runner.invoke(app, ["export", "state", str(backup)])
    assert r.exit_code == 2
    assert "already exists" in r.stderr

    forced = runner.invoke(app, ["export", "state", str(backup), "--force"])
    assert forced.exit_code == 0, forced.stdout + forced.stderr


@pytest.mark.unit
def test_import_malformed_file_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_quiet: None
) -> None:
    target_db = tmp_path / "target.db"
    monkeypatch.setenv("MOVATE_DB", str(target_db))

    bad = tmp_path / "bad.json"
    bad.write_text("this is not json {{{")
    r = runner.invoke(app, ["import", "state", str(bad)])
    assert r.exit_code == 2
    assert "not valid JSON" in r.stderr


@pytest.mark.unit
def test_import_future_schema_version_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_quiet: None
) -> None:
    target_db = tmp_path / "target.db"
    monkeypatch.setenv("MOVATE_DB", str(target_db))

    future = tmp_path / "future.json"
    future.write_text(json.dumps({"schema_version": 999, "entities": {}}))
    r = runner.invoke(app, ["import", "state", str(future)])
    assert r.exit_code == 2
    assert "newer than this build" in r.stderr


@pytest.mark.unit
def test_import_missing_file_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_quiet: None
) -> None:
    target_db = tmp_path / "target.db"
    monkeypatch.setenv("MOVATE_DB", str(target_db))
    r = runner.invoke(app, ["import", "state", str(tmp_path / "nope.json")])
    assert r.exit_code == 2
    assert "no such backup file" in r.stderr
