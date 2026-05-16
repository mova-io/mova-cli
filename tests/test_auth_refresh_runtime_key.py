"""PR #111 — `mdk auth refresh-runtime-key <target>` one-shot recovery.

The two-step `create-key` → `save-runtime-key` flow has an annoying
failure mode: when the runtime is redeployed (or its revision is
recycled) the JWT secret rotates, which invalidates every previously-
minted key. The operator's saved bearer starts returning 401 and they
have to manually `az containerapp exec` → mint → copy → save.

This command wraps all four steps. Tests here mock the `az` subprocess
calls so the suite stays hermetic.

Tested here:

1. Happy path — derives the Container App name, runs `az`, parses the
   minted key out of stdout/stderr, saves it to the credentials store.
2. Custom `--container-app` override skips the auto-derive.
3. Missing `azure_resource_group` → exit 2 with a clear error.
4. Missing `azure_env` (and no override) → exit 2.
5. `az` not on PATH → exit 2.
6. `az containerapp exec` non-zero exit → exit 2 + the exec's stderr surfaced.
7. No `mvt_*` token in az output → exit 2 + raw output surfaced for debug.
8. _extract_mvt_key helper — picks the first valid `mvt_<...>_<...>`
   token out of arbitrary text + ignores partial matches.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.auth import _extract_mvt_key
from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _write_user_config(home: Path) -> None:
    """Stash a config.yaml with a `dev` target wired to the canonical
    Azure addressing the real bootstrap deploys use."""
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


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    account_set_rc: int = 0,
    exec_rc: int = 0,
    exec_stdout: str = "",
    exec_stderr: str = "",
    az_on_path: bool = True,
) -> dict[str, object]:
    """Patch shutil.which + subprocess.run inside movate.cli.auth.

    Returns a captures dict the test can inspect to verify the right
    args were passed to az.
    """
    import subprocess as _real_subprocess  # noqa: PLC0415

    captures: dict[str, object] = {"calls": []}

    class _FakeCompleted:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        captures["calls"].append(list(cmd))
        # First call: `az account set ...` (only when target has a subscription)
        if len(cmd) >= 3 and cmd[:3] == ["az", "account", "set"]:
            if account_set_rc != 0:
                raise _real_subprocess.CalledProcessError(
                    account_set_rc, cmd, stderr=b"account set failed"
                )
            return _FakeCompleted(returncode=0, stdout="", stderr="")
        # Second call: `az containerapp exec ...`
        if len(cmd) >= 3 and cmd[:3] == ["az", "containerapp", "exec"]:
            return _FakeCompleted(returncode=exec_rc, stdout=exec_stdout, stderr=exec_stderr)
        return _FakeCompleted(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    # auth.py's `import subprocess` inside the function picks up the
    # patched stdlib module since we monkeypatch the canonical location.
    monkeypatch.setattr(
        "shutil.which", lambda cmd: "/usr/local/bin/az" if (az_on_path and cmd == "az") else None
    )
    return captures


def _isolate_credentials(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point credentials + config at tmp so the test doesn't touch the
    real ~/.movate. CredentialsStore caches its default path at module
    import via `Path.home()`, so we use the MOVATE_CREDENTIALS_PATH
    env override rather than relying on $HOME indirection."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(home / ".movate" / "config.yaml"))
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(home / ".movate" / "credentials"))
    monkeypatch.delenv("MDK_DEV_KEY", raising=False)


# ---------------------------------------------------------------------------
# _extract_mvt_key
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExtractMvtKey:
    def test_finds_canonical_key_in_text(self) -> None:
        """Five-segment `mvt_<env>_<tenant>_<keyid>_<secret>` shape
        in the middle of arbitrary `az exec` output."""
        text = (
            "INFO: connecting to pod...\n"
            "secret: mvt_live_demo_kid001_aBcDeFgHiJkL\n"
            "save this now — never shown again\n"
        )
        assert _extract_mvt_key(text) == "mvt_live_demo_kid001_aBcDeFgHiJkL"

    def test_finds_test_env_keys(self) -> None:
        assert _extract_mvt_key("key: mvt_test_acme_K1_secretXYZ") == "mvt_test_acme_K1_secretXYZ"

    def test_tenant_with_hyphens_allowed(self) -> None:
        """Tenant ids commonly have hyphens (UUID slug, ACME-bot-2)."""
        text = "mvt_live_acme-bot-2_K1_secret"
        assert _extract_mvt_key(text) == "mvt_live_acme-bot-2_K1_secret"

    def test_returns_none_when_no_match(self) -> None:
        assert _extract_mvt_key("no key here") is None
        # Partial — missing the secret segment.
        assert _extract_mvt_key("mvt_live_demo_K1") is None
        # Wrong prefix.
        assert _extract_mvt_key("not_mvt_live_demo_K1_secret") is None


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_refresh_happy_path_writes_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E2E happy path: target resolved → az containerapp exec mocked →
    minted key parsed → saved to credentials store."""
    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    captures = _patch_subprocess(
        monkeypatch,
        exec_stdout="key_id: kid_abc\n",
        exec_stderr="secret: mvt_live_demo_kid_abc_S3CRET\nsave this now — never shown again\n",
    )

    result = runner.invoke(app, ["auth", "refresh-runtime-key", "dev"])
    assert result.exit_code == 0, result.stdout + result.stderr
    # az was invoked with the right addressing.
    calls = captures["calls"]
    # First call: az account set --subscription <id>
    assert calls[0][0:3] == ["az", "account", "set"]
    assert "00000000-0000-0000-0000-000000000001" in calls[0]
    # Second call: az containerapp exec --command "mdk auth create-key ..."
    assert calls[1][0:3] == ["az", "containerapp", "exec"]
    assert "-g" in calls[1] and "movate-dev-rg" in calls[1]
    assert "-n" in calls[1] and "movate-dev-api" in calls[1]  # auto-derived
    assert "--command" in calls[1]
    # The inner command includes the right tenant + env + --quiet.
    inner = next(part for part in calls[1] if "mdk auth create-key" in part)
    assert "--tenant-id demo" in inner
    assert "--env live" in inner
    assert "--quiet" in inner

    # And the credentials file has the minted key.
    creds = (tmp_path / ".movate" / "credentials").read_text()
    assert "MDK_DEV_KEY=mvt_live_demo_kid_abc_S3CRET" in creds
    # `success()` writes to stderr, not stdout.
    assert "minted + saved fresh runtime key" in result.stderr


@pytest.mark.unit
def test_refresh_container_app_override_skips_derive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--container-app overrides the auto-derive (`movate-dev-api`).
    Useful for non-standard ACA naming."""
    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    captures = _patch_subprocess(monkeypatch, exec_stderr="secret: mvt_live_demo_K_X\n")

    result = runner.invoke(
        app,
        ["auth", "refresh-runtime-key", "dev", "--container-app", "custom-api-app"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    exec_call = captures["calls"][1]
    # Custom name used, NOT the derived one.
    assert "custom-api-app" in exec_call
    assert "movate-dev-api" not in exec_call


@pytest.mark.unit
def test_refresh_custom_tenant_and_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--tenant + --label thread through to the inner mdk command."""
    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    captures = _patch_subprocess(monkeypatch, exec_stderr="secret: mvt_test_acme_K_S\n")

    result = runner.invoke(
        app,
        [
            "auth",
            "refresh-runtime-key",
            "dev",
            "--tenant",
            "customer-acme",
            "--env",
            "test",
            "--label",
            "local-laptop",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    inner = next(part for part in captures["calls"][1] if "mdk auth create-key" in part)
    assert "--tenant-id customer-acme" in inner
    assert "--env test" in inner
    assert "--label local-laptop" in inner


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_refresh_unknown_target_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    result = runner.invoke(app, ["auth", "refresh-runtime-key", "nonexistent"])
    assert result.exit_code == 2
    assert "unknown target" in result.stderr


@pytest.mark.unit
def test_refresh_missing_azure_rg_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Target with no `azure_resource_group` → exit 2; the user gets
    a clear hint pointing at the fallback (manual `save-runtime-key`)."""
    _isolate_credentials(tmp_path, monkeypatch)
    (tmp_path / ".movate").mkdir(parents=True)
    (tmp_path / ".movate" / "config.yaml").write_text(
        "active: dev\ntargets:\n  dev:\n    url: https://x.example.com\n    key_env: MDK_DEV_KEY\n"
    )
    result = runner.invoke(app, ["auth", "refresh-runtime-key", "dev"])
    assert result.exit_code == 2
    assert "azure_resource_group" in result.stderr
    assert "save-runtime-key" in result.stderr  # fallback hint


@pytest.mark.unit
def test_refresh_missing_azure_env_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Target with azure_resource_group but no azure_env → exit 2
    unless --container-app is passed. The error tells the operator
    both recovery paths."""
    _isolate_credentials(tmp_path, monkeypatch)
    (tmp_path / ".movate").mkdir(parents=True)
    (tmp_path / ".movate" / "config.yaml").write_text(
        "active: dev\n"
        "targets:\n"
        "  dev:\n"
        "    url: https://x.example.com\n"
        "    key_env: MDK_DEV_KEY\n"
        "    azure_resource_group: movate-rg\n"
    )
    result = runner.invoke(app, ["auth", "refresh-runtime-key", "dev"])
    assert result.exit_code == 2
    assert "azure_env" in result.stderr
    assert "--container-app" in result.stderr


@pytest.mark.unit
def test_refresh_az_not_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When `az` isn't installed, exit 2 + point at the manual recovery."""
    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    _patch_subprocess(monkeypatch, az_on_path=False)
    result = runner.invoke(app, ["auth", "refresh-runtime-key", "dev"])
    assert result.exit_code == 2
    assert "az" in result.stderr.lower()
    # Hint at the fallback path.
    assert "save-runtime-key" in result.stderr


@pytest.mark.unit
def test_refresh_az_exec_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-zero exit from `az containerapp exec` → exit 2 + the exec's
    stderr surfaced for the operator to triage (wrong RBAC, wrong RG,
    Container App doesn't exist, etc.)."""
    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    _patch_subprocess(
        monkeypatch,
        exec_rc=1,
        exec_stderr="ERROR: container app 'movate-dev-api' not found in RG 'movate-dev-rg'",
    )
    result = runner.invoke(app, ["auth", "refresh-runtime-key", "dev"])
    assert result.exit_code == 2
    assert "az containerapp exec failed" in result.stderr
    assert "not found in RG" in result.stderr


@pytest.mark.unit
def test_refresh_no_mvt_key_in_output_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When `az exec` returns 0 but no `mvt_*` token is in the output
    (Azure CLI version mismatch, stdout buffering quirk, etc.), exit
    2 + dump the raw output so the operator can debug."""
    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    _patch_subprocess(
        monkeypatch,
        exec_stdout="some random non-key output\n",
        exec_stderr="more output without a token\n",
    )
    result = runner.invoke(app, ["auth", "refresh-runtime-key", "dev"])
    assert result.exit_code == 2
    assert "could not find a `mvt_*` key" in result.stderr
    # Raw output surfaced so the operator can debug.
    assert "some random non-key output" in result.stderr
    # And the manual fallback is suggested.
    assert "save-runtime-key" in result.stderr
