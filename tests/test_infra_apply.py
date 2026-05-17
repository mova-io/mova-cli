"""``mdk infra apply`` — wraps Bicep deploy + auto-chains bootstrap-seed.

Closes the "first-deploy is two commands" gap from the auth-flow
polish work. Tests cover:

* Happy path: az deployment + bootstrap-seed chain both succeed →
  exit 0, summary line shows ``ok=true seeded=true``.
* ``--dry-run``: no az calls, no seed call, summary shows ``dry_run=true``.
* ``--no-seed``: az runs, bootstrap-seed is skipped, summary shows
  ``seeded=false ok=true``.
* az failure: bootstrap-seed is NOT called, exit 1, ``ok=false``.
* Pre-existing seed in KV: friendly skip, exit 0, ``seeded=false``.
* Missing target / missing keyvault / missing az / missing bicep:
  exit 2 with actionable error.
"""

from __future__ import annotations

import subprocess as _real_subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _isolate(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(home / ".movate" / "config.yaml"))
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(home / ".movate" / "credentials"))
    monkeypatch.delenv("MDK_DEV_KEY", raising=False)


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


def _materialize_bicep(tmp_path: Path) -> None:
    """Create the bicep template + parameters file the command's
    existence checks look for. The actual contents don't matter
    because the subprocess is mocked."""
    infra_dir = tmp_path / "infra" / "azure"
    infra_dir.mkdir(parents=True, exist_ok=True)
    (infra_dir / "main.bicep").write_text("// stub bicep\n")
    (infra_dir / "main.dev.bicepparam").write_text("// stub bicepparam\n")


class _FakeCompleted:
    def __init__(
        self, returncode: int = 0, stdout: str = "", stderr: str = ""
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_az(
    monkeypatch: pytest.MonkeyPatch,
    *,
    az_on_path: bool = True,
    deployment_rc: int = 0,
    secret_show_finds_existing: bool = False,
    secret_set_rc: int = 0,
) -> dict[str, list[list[str]]]:
    """Patch shutil.which + subprocess.run to mock the three az calls
    that `mdk infra apply` (with chain) makes:

    1. ``az deployment group create`` — the bicep apply.
    2. ``az keyvault secret show`` — bootstrap-seed's existence probe.
    3. ``az keyvault secret set`` — bootstrap-seed's upload.
    """
    captures: dict[str, list[list[str]]] = {"calls": []}

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> _FakeCompleted:
        captures["calls"].append(list(cmd))
        if cmd[:4] == ["az", "deployment", "group", "create"]:
            return _FakeCompleted(returncode=deployment_rc)
        if cmd[:4] == ["az", "keyvault", "secret", "show"]:
            if secret_show_finds_existing:
                return _FakeCompleted(
                    returncode=0,
                    stdout="https://example.vault.azure.net/secrets/bootstrap-api-key/abc\n",
                )
            return _FakeCompleted(returncode=1, stderr="SecretNotFound")
        if cmd[:4] == ["az", "keyvault", "secret", "set"]:
            return _FakeCompleted(returncode=secret_set_rc)
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(
        "shutil.which",
        lambda cmd: "/usr/local/bin/az" if (az_on_path and cmd == "az") else None,
    )
    _ = _real_subprocess  # imported for side-effect parity with other tests
    return captures


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_apply_happy_path_runs_deployment_then_chains_bootstrap_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: az deployment succeeds, bootstrap-seed probe finds
    no pre-existing secret, mint+upload+save all succeed. Summary line
    reports ``ok=true seeded=true``."""
    _isolate(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    _materialize_bicep(tmp_path)
    monkeypatch.chdir(tmp_path)
    captures = _patch_az(monkeypatch)

    result = runner.invoke(
        app,
        ["infra", "apply", "dev", "--keyvault", "movate-dev-kv-mvt"],
        env={"COLUMNS": "200"},
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    # Three az calls: deployment, secret show (probe), secret set.
    az_calls = [c for c in captures["calls"] if c[0] == "az"]
    assert any(c[:4] == ["az", "deployment", "group", "create"] for c in az_calls)
    assert any(c[:4] == ["az", "keyvault", "secret", "show"] for c in az_calls)
    assert any(c[:4] == ["az", "keyvault", "secret", "set"] for c in az_calls)
    # Summary line emitted with the seeded flag set.
    combined = (result.stdout + result.stderr).replace("\n", " ")
    assert "mdk_infra_summary:" in combined
    assert "target=dev" in combined
    assert "seeded=true" in combined
    assert "ok=true" in combined
    # Local credentials store got the new key.
    creds = (tmp_path / ".movate" / "credentials").read_text()
    # The 8-char tenant prefix is `demotena` (first 8 of `demotenant`).
    assert "MDK_DEV_KEY=mvt_live_demotena_" in creds


@pytest.mark.unit
def test_apply_passes_target_subscription_rg_and_param_file_to_az(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The az deployment command must include the target's
    subscription + RG + the .bicepparam derived from azure_env."""
    _isolate(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    _materialize_bicep(tmp_path)
    monkeypatch.chdir(tmp_path)
    captures = _patch_az(monkeypatch)

    runner.invoke(
        app,
        ["infra", "apply", "dev", "--keyvault", "movate-dev-kv-mvt"],
        env={"COLUMNS": "200"},
    )

    deploy_call = next(
        c for c in captures["calls"] if c[:4] == ["az", "deployment", "group", "create"]
    )
    assert "--subscription" in deploy_call
    assert "00000000-0000-0000-0000-000000000001" in deploy_call
    assert "-g" in deploy_call
    assert "movate-dev-rg" in deploy_call
    assert "-n" in deploy_call
    assert "main" in deploy_call  # default deployment name
    # Default bicep + param paths derived from azure_env=dev.
    assert any("infra/azure/main.bicep" in p for p in deploy_call)
    assert any("infra/azure/main.dev.bicepparam" in p for p in deploy_call)


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_apply_dry_run_makes_no_az_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dry-run prints the command preview but executes nothing."""
    _isolate(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    _materialize_bicep(tmp_path)
    monkeypatch.chdir(tmp_path)
    captures = _patch_az(monkeypatch)

    result = runner.invoke(
        app,
        [
            "infra",
            "apply",
            "dev",
            "--keyvault",
            "movate-dev-kv-mvt",
            "--dry-run",
        ],
        env={"COLUMNS": "200"},
    )

    assert result.exit_code == 0
    assert captures["calls"] == [], "no subprocess should be invoked in dry-run"
    combined = (result.stdout + result.stderr).replace("\n", " ")
    assert "mdk_infra_summary:" in combined
    assert "dry_run=true" in combined
    # Operator sees the command preview + the chain-into note.
    assert "az deployment group create" in combined
    assert "bootstrap-seed" in combined


# ---------------------------------------------------------------------------
# --no-seed
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_apply_no_seed_skips_the_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--no-seed`` runs the Bicep deploy and stops — no probe, no
    mint, no upload. The summary reports seeded=false ok=true."""
    _isolate(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    _materialize_bicep(tmp_path)
    monkeypatch.chdir(tmp_path)
    captures = _patch_az(monkeypatch)

    result = runner.invoke(
        app,
        ["infra", "apply", "dev", "--no-seed"],
        env={"COLUMNS": "200"},
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    # Exactly one az call: deployment.
    az_calls = [c for c in captures["calls"] if c[0] == "az"]
    assert len(az_calls) == 1
    assert az_calls[0][:4] == ["az", "deployment", "group", "create"]
    combined = (result.stdout + result.stderr).replace("\n", " ")
    assert "seeded=false" in combined
    assert "ok=true" in combined


@pytest.mark.unit
def test_apply_no_seed_does_not_require_keyvault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``--keyvault`` requirement is gated on the seed chain — with
    ``--no-seed`` the operator owns secret population separately, so
    the flag is optional."""
    _isolate(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    _materialize_bicep(tmp_path)
    monkeypatch.chdir(tmp_path)
    _patch_az(monkeypatch)

    result = runner.invoke(
        app,
        ["infra", "apply", "dev", "--no-seed"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_apply_az_deployment_failure_exits_1_without_chaining_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Bicep deploy fails, bootstrap-seed must NOT run — we don't
    want to mint a key into a half-provisioned environment. Exit 1,
    summary shows ok=false."""
    _isolate(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    _materialize_bicep(tmp_path)
    monkeypatch.chdir(tmp_path)
    captures = _patch_az(monkeypatch, deployment_rc=2)

    result = runner.invoke(
        app,
        ["infra", "apply", "dev", "--keyvault", "movate-dev-kv-mvt"],
        env={"COLUMNS": "200"},
    )

    assert result.exit_code == 1
    az_calls = [c for c in captures["calls"] if c[0] == "az"]
    # Only the failed deployment call — no seed-related calls fired.
    assert len(az_calls) == 1
    assert az_calls[0][:4] == ["az", "deployment", "group", "create"]
    combined = (result.stdout + result.stderr).replace("\n", " ")
    assert "ok=false" in combined
    assert "az deployment group create failed" in combined


@pytest.mark.unit
def test_apply_existing_bootstrap_secret_skips_mint_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running apply on an already-bootstrapped env should not
    error — the probe finds the existing secret, we skip the mint,
    and exit 0 with seeded=false (since we didn't seed THIS run)."""
    _isolate(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    _materialize_bicep(tmp_path)
    monkeypatch.chdir(tmp_path)
    captures = _patch_az(monkeypatch, secret_show_finds_existing=True)

    result = runner.invoke(
        app,
        ["infra", "apply", "dev", "--keyvault", "movate-dev-kv-mvt"],
        env={"COLUMNS": "200"},
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    az_calls = [c for c in captures["calls"] if c[0] == "az"]
    # Two calls: deployment + secret show; no set (the mint was skipped).
    assert any(c[:4] == ["az", "deployment", "group", "create"] for c in az_calls)
    assert any(c[:4] == ["az", "keyvault", "secret", "show"] for c in az_calls)
    assert not any(c[:4] == ["az", "keyvault", "secret", "set"] for c in az_calls)
    combined = (result.stdout + result.stderr).replace("\n", " ")
    assert "already exists" in combined
    assert "pull-runtime-key dev" in combined
    assert "seeded=false" in combined
    assert "ok=true" in combined


@pytest.mark.unit
def test_apply_missing_keyvault_without_no_seed_errors_with_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    _materialize_bicep(tmp_path)
    monkeypatch.chdir(tmp_path)
    _patch_az(monkeypatch)

    result = runner.invoke(
        app, ["infra", "apply", "dev"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 2
    combined = (result.stdout + result.stderr).replace("\n", " ")
    assert "--keyvault is required" in combined
    assert "--no-seed" in combined


@pytest.mark.unit
def test_apply_unknown_target_exits_2_with_registered_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(tmp_path, monkeypatch)
    _write_user_config(tmp_path)

    result = runner.invoke(
        app,
        ["infra", "apply", "ghost", "--keyvault", "kv"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
    combined = (result.stdout + result.stderr).replace("\n", " ")
    assert "unknown target" in combined.lower()
    assert "dev" in combined  # the registered one named in the hint


@pytest.mark.unit
def test_apply_missing_bicep_template_errors_with_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the bicep template isn't found (operator running outside
    the source tree), surface a clear pointer to --bicep."""
    _isolate(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    # Note: do NOT call _materialize_bicep — that's what we're testing.
    monkeypatch.chdir(tmp_path)
    _patch_az(monkeypatch)

    result = runner.invoke(
        app,
        ["infra", "apply", "dev", "--keyvault", "movate-dev-kv-mvt"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
    combined = (result.stdout + result.stderr).replace("\n", " ")
    assert "bicep template not found" in combined
    assert "--bicep" in combined


@pytest.mark.unit
def test_apply_missing_az_cli_exits_with_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    _materialize_bicep(tmp_path)
    monkeypatch.chdir(tmp_path)
    _patch_az(monkeypatch, az_on_path=False)

    result = runner.invoke(
        app,
        ["infra", "apply", "dev", "--keyvault", "movate-dev-kv-mvt"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
    combined = (result.stdout + result.stderr).replace("\n", " ")
    assert "install-azure-cli" in combined
