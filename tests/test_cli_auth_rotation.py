"""CLI: ``mdk auth rotate-key`` (grace window), ``list-keys`` pre-expiry
warning marker, and ``mdk auth revoke-all`` (bulk, confirmed) — ADR 013 D5.

Local-storage paths only (no --target), exercised through the same Typer
app the operator hits. Storage lands in tmp_path via the ``isolated_db``
fixture (MOVATE_DB + HOME redirected).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.storage import build_storage

runner = CliRunner(mix_stderr=False)


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "movate.db"
    monkeypatch.setenv("MOVATE_DB", str(db))
    monkeypatch.setenv("HOME", str(tmp_path))
    return db


def _save_record(record) -> None:
    async def _go() -> None:
        storage = build_storage()
        await storage.init()
        try:
            await storage.save_api_key(record)
        finally:
            await storage.close()

    asyncio.run(_go())


def _get_record(key_id: str):
    async def _go():
        storage = build_storage()
        await storage.init()
        try:
            return await storage.get_api_key(key_id)
        finally:
            await storage.close()

    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# rotate-key
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rotate_prints_new_key_once_and_grace_expiry(isolated_db: Path) -> None:
    tenant_id = uuid4().hex
    minted = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="ci", scopes=["read"])
    _save_record(minted.record)

    result = runner.invoke(app, ["auth", "rotate-key", minted.record.key_id, "--grace", "1h", "-y"])
    assert result.exit_code == 0, result.stdout + result.stderr
    # New full key printed once on stdout.
    assert "mvt_live_" in result.stdout
    # Grace-window note on stderr.
    assert "grace window" in result.stderr.lower()
    assert "save this now" in result.stderr.lower() or "shown again" in result.stderr.lower()

    # Old key now has an expiry set (grace window armed), and is NOT revoked.
    old = _get_record(minted.record.key_id)
    assert old is not None
    assert old.revoked_at is None  # zero-downtime: still valid, just time-boxed
    assert old.expires_at is not None
    assert old.expires_at > datetime.now(UTC)


@pytest.mark.unit
def test_rotate_unknown_key_errors(isolated_db: Path) -> None:
    result = runner.invoke(app, ["auth", "rotate-key", "NOPE", "-y"])
    assert result.exit_code != 0
    assert "not found" in result.stderr.lower()


@pytest.mark.unit
def test_rotate_rejects_bad_grace(isolated_db: Path) -> None:
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE)
    _save_record(minted.record)
    result = runner.invoke(
        app, ["auth", "rotate-key", minted.record.key_id, "--grace", "banana", "-y"]
    )
    assert result.exit_code != 0
    assert "invalid --grace" in result.stderr.lower()


# ---------------------------------------------------------------------------
# list-keys pre-expiry warning marker
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_keys_flags_near_expiry_with_warning(isolated_db: Path) -> None:
    tenant_id = uuid4().hex
    minted = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, ttl_days=90)
    # Force a near-expiry (within the 7-day threshold).
    near = minted.record.model_copy(update={"expires_at": datetime.now(UTC) + timedelta(days=2)})
    _save_record(near)

    result = runner.invoke(app, ["auth", "list-keys", "--tenant-id", tenant_id])
    assert result.exit_code == 0, result.stdout
    # The ⚠ marker appears for the near-expiry key.
    assert "⚠" in result.stdout


@pytest.mark.unit
def test_list_keys_no_warning_for_far_expiry(isolated_db: Path) -> None:
    tenant_id = uuid4().hex
    minted = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, ttl_days=90)
    _save_record(minted.record)  # ~90 days out
    result = runner.invoke(app, ["auth", "list-keys", "--tenant-id", tenant_id])
    assert result.exit_code == 0
    # No per-row ⚠ on a far-out key (the legend line uses ⚠ but only when
    # printed; the row itself must not be flagged). Assert the row date is
    # plain by checking the key shows and ⚠ isn't adjacent to its date.
    # Simpler + robust: the far key's expiry date string is present without
    # the warning style — we assert the marker count is 0 in the data rows
    # by checking ⚠ only appears (if at all) in the trailing legend hint,
    # which goes to stderr.
    assert "⚠" not in result.stdout


# ---------------------------------------------------------------------------
# revoke-all (bulk)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_revoke_all_confirms_and_reports_count(isolated_db: Path) -> None:
    tenant_id = "tttttttt" + uuid4().hex
    keys = [mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE) for _ in range(3)]
    for m in keys:
        _save_record(m.record)

    result = runner.invoke(app, ["auth", "revoke-all", "--tenant-id", tenant_id, "-y"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "revoked 3" in (result.stdout + result.stderr).lower()

    # All three are now revoked.
    for m in keys:
        rec = _get_record(m.record.key_id)
        assert rec is not None
        assert rec.revoked_at is not None


@pytest.mark.unit
def test_revoke_all_spares_excepted_key(isolated_db: Path) -> None:
    tenant_id = "tttttttt" + uuid4().hex
    spare = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE)
    other = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE)
    _save_record(spare.record)
    _save_record(other.record)

    result = runner.invoke(
        app,
        [
            "auth",
            "revoke-all",
            "--tenant-id",
            tenant_id,
            "--except",
            spare.record.key_id,
            "-y",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "revoked 1" in (result.stdout + result.stderr).lower()
    assert _get_record(spare.record.key_id).revoked_at is None
    assert _get_record(other.record.key_id).revoked_at is not None


@pytest.mark.unit
def test_revoke_all_requires_tenant_without_target(isolated_db: Path) -> None:
    result = runner.invoke(app, ["auth", "revoke-all", "-y"])
    assert result.exit_code != 0
    assert "tenant-id" in result.stderr.lower()


@pytest.mark.unit
def test_revoke_all_aborts_without_confirmation(isolated_db: Path) -> None:
    """Destructive: a non-TTY invocation without -y must NOT proceed."""
    tenant_id = "tttttttt" + uuid4().hex
    m = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE)
    _save_record(m.record)
    # No -y, no TTY input → confirm_destructive aborts.
    result = runner.invoke(app, ["auth", "revoke-all", "--tenant-id", tenant_id])
    assert result.exit_code != 0
    # The key is still active (nothing was revoked).
    assert _get_record(m.record.key_id).revoked_at is None
