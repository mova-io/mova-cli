"""`mdk auth bootstrap-seed` — mint, upload, and save the one-time seed key.

Wraps three side effects:

1. ``mint_api_key()`` produces a fresh ``mvt_<env>_<tenant>_<keyid>_<secret>``
   token, matching the runtime's seed-bootstrap parser.
2. ``az keyvault secret set`` uploads it as ``bootstrap-api-key``,
   which the Container App's Bicep references via ``MOVATE_SEED_API_KEY``.
3. ``CredentialsStore.set()`` stashes the same value locally so
   ``mdk deploy --target <name>`` works immediately.

Tests below mock the subprocess + verify each side effect lands.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _write_user_config(home: Path) -> None:
    cfg_dir = home / ".movate"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(
        "active: dev\n"
        "targets:\n"
        "  dev:\n"
        "    url: https://movate-dev-api.example.azurecontainerapps.io\n"
        "    key_env: MDK_DEV_KEY\n"
        "    azure_subscription: 00000000-0000-0000-0000-000000000001\n"
        "    azure_resource_group: movate-dev-rg\n"
        "    azure_env: dev\n"
    )


def _isolate_credentials(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(home / ".movate" / "config.yaml"))
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(home / ".movate" / "credentials"))
    monkeypatch.delenv("MDK_DEV_KEY", raising=False)


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_az(
    monkeypatch: pytest.MonkeyPatch,
    *,
    az_on_path: bool = True,
    probe_finds_existing: bool = False,
    secret_set_rc: int = 0,
    secret_set_stderr: str = "",
) -> dict[str, list[list[str]]]:
    """Patch ``shutil.which`` + ``subprocess.run`` for the two az calls
    bootstrap-seed makes: ``az keyvault secret show`` (existence probe)
    and ``az keyvault secret set`` (upload).

    Returns a captures dict the test can inspect to verify the right
    args were passed.
    """
    captures: dict[str, list[list[str]]] = {"calls": []}

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> _FakeCompleted:
        captures["calls"].append(list(cmd))
        # Existence probe: `az keyvault secret show ... --name bootstrap-api-key`
        if cmd[:4] == ["az", "keyvault", "secret", "show"]:
            if probe_finds_existing:
                return _FakeCompleted(
                    returncode=0,
                    stdout="https://example.vault.azure.net/secrets/bootstrap-api-key/abc\n",
                    stderr="",
                )
            return _FakeCompleted(returncode=1, stdout="", stderr="SecretNotFound")
        # Upload: `az keyvault secret set ... --name bootstrap-api-key --value <key>`
        if cmd[:4] == ["az", "keyvault", "secret", "set"]:
            return _FakeCompleted(returncode=secret_set_rc, stderr=secret_set_stderr)
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(
        "shutil.which",
        lambda cmd: "/usr/local/bin/az" if (az_on_path and cmd == "az") else None,
    )
    return captures


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bootstrap_seed_mints_uploads_and_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: secret-show probe says missing → mint → upload →
    save locally. The full minted key never appears in argv we capture
    (it's the ONE place we expect a secret to be passed inline)."""
    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    captures = _patch_az(monkeypatch)

    result = runner.invoke(
        app,
        ["auth", "bootstrap-seed", "dev", "--keyvault", "movate-dev-kv-mvt"],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    # Probe + upload — two az calls.
    assert len(captures["calls"]) == 2
    assert captures["calls"][0][:4] == ["az", "keyvault", "secret", "show"]
    assert captures["calls"][1][:4] == ["az", "keyvault", "secret", "set"]
    # The upload call references the right vault + secret name.
    set_call = captures["calls"][1]
    assert "movate-dev-kv-mvt" in set_call
    assert "bootstrap-api-key" in set_call
    # The minted key was passed via --value. It's expected to be in
    # the argv (subprocess is the boundary); we verify it has the
    # right format rather than that it's hidden.
    value_idx = set_call.index("--value")
    minted_key = set_call[value_idx + 1]
    assert minted_key.startswith("mvt_live_demotena_"), minted_key
    # And the same value got saved to the credentials store.
    creds = (tmp_path / ".movate" / "credentials").read_text()
    assert f"MDK_DEV_KEY={minted_key}" in creds


@pytest.mark.unit
def test_bootstrap_seed_blocks_when_secret_already_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --force, an existing bootstrap-api-key secret is a
    safety stop — operators don't accidentally rotate a shared
    environment's seed key. The error names the way to recover the
    existing value locally."""
    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    captures = _patch_az(monkeypatch, probe_finds_existing=True)

    result = runner.invoke(
        app,
        ["auth", "bootstrap-seed", "dev", "--keyvault", "movate-dev-kv-mvt"],
    )

    assert result.exit_code == 2, result.stdout + result.stderr
    # Only the probe call, no upload.
    assert len(captures["calls"]) == 1
    assert captures["calls"][0][:4] == ["az", "keyvault", "secret", "show"]
    # The error names a recovery command so the operator can pull the
    # existing value locally without rotating it. Post-PR-#158 the
    # recommended recovery is `pull-runtime-key`, which reads from
    # Key Vault and writes to local creds in one step (was: a manual
    # `az keyvault secret show` piped into `save-runtime-key`).
    assert "pull-runtime-key dev" in result.stderr
    assert "already exists" in result.stderr
    # And nothing was written locally.
    creds_path = tmp_path / ".movate" / "credentials"
    if creds_path.exists():
        assert "MDK_DEV_KEY=" not in creds_path.read_text()


@pytest.mark.unit
def test_bootstrap_seed_force_overwrites_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--force`` skips the existence probe and runs the upload
    unconditionally — used for deliberate rotation."""
    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    captures = _patch_az(monkeypatch, probe_finds_existing=True)

    result = runner.invoke(
        app,
        ["auth", "bootstrap-seed", "dev", "--keyvault", "movate-dev-kv-mvt", "--force"],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    # No probe call with --force — straight to upload.
    set_calls = [c for c in captures["calls"] if c[:4] == ["az", "keyvault", "secret", "set"]]
    show_calls = [c for c in captures["calls"] if c[:4] == ["az", "keyvault", "secret", "show"]]
    assert len(show_calls) == 0
    assert len(set_calls) == 1


@pytest.mark.unit
def test_bootstrap_seed_az_secret_set_failure_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``az keyvault secret set`` returns non-zero (wrong RBAC,
    vault not found, etc.) the command exits 2 + surfaces the stderr
    so the operator can triage."""
    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    _patch_az(
        monkeypatch,
        secret_set_rc=1,
        secret_set_stderr="ERROR: forbidden — caller lacks Secrets Officer role",
    )

    result = runner.invoke(
        app,
        ["auth", "bootstrap-seed", "dev", "--keyvault", "movate-dev-kv-mvt"],
    )

    assert result.exit_code == 2
    assert "az keyvault secret set failed" in result.stderr
    assert "Secrets Officer" in result.stderr


@pytest.mark.unit
def test_bootstrap_seed_missing_az_cli_exits_with_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    _patch_az(monkeypatch, az_on_path=False)

    result = runner.invoke(
        app,
        ["auth", "bootstrap-seed", "dev", "--keyvault", "movate-dev-kv-mvt"],
    )

    assert result.exit_code == 2
    assert "az" in result.stderr.lower()
    assert "install-azure-cli" in result.stderr


@pytest.mark.unit
def test_bootstrap_seed_unknown_target_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)

    result = runner.invoke(
        app,
        ["auth", "bootstrap-seed", "nope", "--keyvault", "movate-dev-kv-mvt"],
    )

    assert result.exit_code == 2
    assert "unknown target" in result.stderr.lower()
    assert "nope" in result.stderr


@pytest.mark.unit
def test_bootstrap_seed_invalid_env_value_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--env`` only accepts the values in :class:`ApiKeyEnv`."""
    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    _patch_az(monkeypatch)

    result = runner.invoke(
        app,
        [
            "auth",
            "bootstrap-seed",
            "dev",
            "--keyvault",
            "movate-dev-kv-mvt",
            "--env",
            "bogus",
        ],
    )

    assert result.exit_code == 2
    assert "--env" in result.stderr
