"""CLI — ``mdk keys set|list|delete`` (BYOK, ADR 018).

Covers:

* ``mdk keys set`` stores a provider key under the local tenant, encrypted at
  rest, printing only a masked fingerprint (never the value).
* The stored row holds a ciphertext (not the plaintext) + a masked tail.
* ``mdk keys list`` shows the provider + fingerprint, never the value.
* ``mdk keys delete`` removes it (friendly no-op when missing).
* ``mdk keys set`` fails cleanly when MOVATE_PROVIDER_KEY_SECRET is unset.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.provider_keys import decrypt_provider_key
from movate.storage import build_storage

runner = CliRunner(mix_stderr=False)

_FERNET_KEY = Fernet.generate_key()
_SECRET = "sk-cli-secret-WXYZ"


@pytest.fixture
def local_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "local.db"
    monkeypatch.setenv("MOVATE_DB", str(db_path))
    monkeypatch.setenv("MOVATE_TRACER", "silent")
    monkeypatch.setenv("MOVATE_PROVIDER_KEY_SECRET", _FERNET_KEY.decode())
    return db_path


async def _list_keys() -> list:
    storage = build_storage()
    await storage.init()
    try:
        return await storage.list_tenant_provider_keys(tenant_id="local")
    finally:
        await storage.close()


@pytest.mark.unit
def test_set_stores_encrypted_and_prints_fingerprint(local_env: Path) -> None:
    r = runner.invoke(app, ["keys", "set", "openai", "--api-key", _SECRET])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert "openai" in r.stdout
    assert "…WXYZ" in r.stdout
    # The plaintext is NEVER echoed.
    assert _SECRET not in r.stdout
    assert _SECRET not in r.stderr

    rows = asyncio.run(_list_keys())
    assert len(rows) == 1
    k = rows[0]
    assert k.provider == "openai"
    assert k.fingerprint == "…WXYZ"
    # Stored ciphertext is NOT the plaintext, but decrypts back to it.
    assert _SECRET not in k.ciphertext
    assert decrypt_provider_key(k.ciphertext, fernet=Fernet(_FERNET_KEY)) == _SECRET


@pytest.mark.unit
def test_set_normalizes_provider_prefix(local_env: Path) -> None:
    r = runner.invoke(app, ["keys", "set", "openai/gpt-4o", "--api-key", "sk-1234"])
    assert r.exit_code == 0, r.stdout + r.stderr
    rows = asyncio.run(_list_keys())
    assert rows[0].provider == "openai"


@pytest.mark.unit
def test_list_shows_provider_no_value(local_env: Path) -> None:
    runner.invoke(app, ["keys", "set", "anthropic", "--api-key", "sk-anthropic-1234"])
    lst = runner.invoke(app, ["keys", "list"])
    assert lst.exit_code == 0, lst.stdout + lst.stderr
    assert "anthropic" in lst.stdout
    assert "…1234" in lst.stdout
    assert "sk-anthropic-1234" not in lst.stdout


@pytest.mark.unit
def test_delete_removes_key(local_env: Path) -> None:
    runner.invoke(app, ["keys", "set", "openai", "--api-key", "sk-abcd"])
    r = runner.invoke(app, ["keys", "delete", "openai"])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert "deleted" in r.stdout
    assert asyncio.run(_list_keys()) == []
    # Friendly no-op on a second delete.
    again = runner.invoke(app, ["keys", "delete", "openai"])
    assert again.exit_code == 0
    assert "nothing to delete" in again.stdout


@pytest.mark.unit
def test_set_without_secret_env_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOVATE_DB", str(tmp_path / "local.db"))
    monkeypatch.setenv("MOVATE_TRACER", "silent")
    monkeypatch.delenv("MOVATE_PROVIDER_KEY_SECRET", raising=False)
    monkeypatch.delenv("MDK_PROVIDER_KEY_SECRET", raising=False)
    r = runner.invoke(app, ["keys", "set", "openai", "--api-key", "sk-x"])
    assert r.exit_code == 2
    assert "MOVATE_PROVIDER_KEY_SECRET" in r.stderr
