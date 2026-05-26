"""`mdk auth pull-runtime-key` — fetch the bootstrap key from Key Vault.

Recovery path when ``~/.movate/credentials`` has been cleared (new
laptop, etc.) and the Container App's ``bootstrap-api-key`` is the
canonical source of truth. One `az keyvault secret show` + one
`CredentialsStore.set` — no chicken-and-egg.

Tests mock the `az` subprocess so the suite stays hermetic.
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
    secret_value: str = "mvt_live_demotena_kid123abc_secretdataXYZ",
    secret_show_rc: int = 0,
    secret_show_stderr: str = "",
) -> dict[str, list[list[str]]]:
    captures: dict[str, list[list[str]]] = {"calls": []}

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> _FakeCompleted:
        captures["calls"].append(list(cmd))
        if cmd[:4] == ["az", "keyvault", "secret", "show"]:
            if secret_show_rc != 0:
                return _FakeCompleted(returncode=secret_show_rc, stderr=secret_show_stderr)
            return _FakeCompleted(returncode=0, stdout=secret_value + "\n")
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
def test_pull_runtime_key_reads_kv_and_writes_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: az returns the secret value, we save it locally."""
    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    captures = _patch_az(monkeypatch)

    result = runner.invoke(
        app,
        ["auth", "pull-runtime-key", "dev", "--keyvault", "movate-dev-kv-mvt"],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    # Exactly one az call: the secret show.
    assert len(captures["calls"]) == 1
    show_call = captures["calls"][0]
    assert show_call[:4] == ["az", "keyvault", "secret", "show"]
    assert "movate-dev-kv-mvt" in show_call
    assert "bootstrap-api-key" in show_call  # default secret name
    # And the same value got saved to the credentials store.
    creds = (tmp_path / ".movate" / "credentials").read_text()
    assert "MDK_DEV_KEY=mvt_live_demotena_kid123abc_secretdataXYZ" in creds


@pytest.mark.unit
def test_pull_runtime_key_strips_trailing_newline_from_tsv_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``az ... --query value -o tsv`` always emits a trailing newline.
    Make sure we strip it — otherwise the saved value would be subtly
    wrong (matches against the runtime by chance because the regex
    might tolerate it, but cleaner to handle here)."""
    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    _patch_az(
        monkeypatch,
        secret_value="mvt_live_demotena_kid123abc_secretdataXYZ\n\n",
    )

    result = runner.invoke(
        app,
        ["auth", "pull-runtime-key", "dev", "--keyvault", "movate-dev-kv-mvt"],
    )

    assert result.exit_code == 0
    creds = (tmp_path / ".movate" / "credentials").read_text()
    # Newlines stripped from saved value.
    assert "MDK_DEV_KEY=mvt_live_demotena_kid123abc_secretdataXYZ\n" in creds
    assert "MDK_DEV_KEY=mvt_live_demotena_kid123abc_secretdataXYZ\n\n" not in creds


@pytest.mark.unit
def test_pull_runtime_key_custom_secret_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--secret-name`` overrides the default for ops who use a
    non-canonical name (e.g. when running a second runtime alongside
    the bootstrap one)."""
    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    captures = _patch_az(monkeypatch)

    result = runner.invoke(
        app,
        [
            "auth",
            "pull-runtime-key",
            "dev",
            "--keyvault",
            "movate-dev-kv-mvt",
            "--secret-name",
            "second-runtime-key",
        ],
    )

    assert result.exit_code == 0
    assert "second-runtime-key" in captures["calls"][0]
    assert "bootstrap-api-key" not in captures["calls"][0]


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pull_runtime_key_az_failure_surfaces_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Common KV failures (SecretNotFound, RBAC denied) surface the
    underlying stderr so the operator knows whether the secret name,
    vault, or permission is the problem."""
    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    _patch_az(
        monkeypatch,
        secret_show_rc=1,
        secret_show_stderr=(
            "ERROR: (SecretNotFound) A secret with (name/id) bootstrap-api-key was not found"
        ),
    )

    result = runner.invoke(
        app,
        ["auth", "pull-runtime-key", "dev", "--keyvault", "movate-dev-kv-mvt"],
    )

    assert result.exit_code == 2
    assert "az keyvault secret show failed" in result.stderr
    assert "SecretNotFound" in result.stderr
    # Recovery hint: run bootstrap-seed first.
    assert "mdk auth bootstrap-seed dev" in result.stderr


@pytest.mark.unit
def test_pull_runtime_key_rejects_non_mvt_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If something other than a movate bearer is in the secret (got
    overwritten with random text, etc.), fail loudly rather than
    silently saving garbage that would 401 on first deploy."""
    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    _patch_az(monkeypatch, secret_value="not-a-movate-key")

    result = runner.invoke(
        app,
        ["auth", "pull-runtime-key", "dev", "--keyvault", "movate-dev-kv-mvt"],
    )

    assert result.exit_code == 2
    # Rich may wrap the error message — match a substring that won't
    # break across a soft-wrap boundary.
    combined_no_newlines = result.stderr.replace("\n", " ")
    assert "doesn't look like a" in combined_no_newlines
    assert "movate bearer" in combined_no_newlines
    # Nothing got saved.
    creds_path = tmp_path / ".movate" / "credentials"
    if creds_path.exists():
        assert "MDK_DEV_KEY=" not in creds_path.read_text()


@pytest.mark.unit
def test_pull_runtime_key_missing_az_cli_exits_with_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    _patch_az(monkeypatch, az_on_path=False)

    result = runner.invoke(
        app,
        ["auth", "pull-runtime-key", "dev", "--keyvault", "movate-dev-kv-mvt"],
    )

    assert result.exit_code == 2
    assert "az" in result.stderr.lower()
    assert "install-azure-cli" in result.stderr


@pytest.mark.unit
def test_pull_runtime_key_unknown_target_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)

    result = runner.invoke(
        app,
        ["auth", "pull-runtime-key", "ghost", "--keyvault", "movate-dev-kv-mvt"],
    )

    assert result.exit_code == 2
    assert "unknown target" in result.stderr.lower()
    assert "ghost" in result.stderr


# ---------------------------------------------------------------------------
# pull_runtime_key_inline — the programmatic helper `mdk deploy`'s bearer
# auto-recovery calls to pull the guaranteed-trusted bootstrap key (returns a
# tuple + raises PullRuntimeKeyError, instead of printing + typer.Exit).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pull_runtime_key_inline_returns_key_and_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from movate.cli.auth import pull_runtime_key_inline  # noqa: PLC0415

    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    _patch_az(monkeypatch)

    key, env_var = pull_runtime_key_inline("dev", keyvault="movate-dev-kv-mvt")

    assert key == "mvt_live_demotena_kid123abc_secretdataXYZ"
    assert env_var == "MDK_DEV_KEY"
    creds = (tmp_path / ".movate" / "credentials").read_text()
    assert "MDK_DEV_KEY=mvt_live_demotena_kid123abc_secretdataXYZ" in creds


@pytest.mark.unit
def test_pull_runtime_key_inline_raises_on_az_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from movate.cli.auth import PullRuntimeKeyError, pull_runtime_key_inline  # noqa: PLC0415

    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    _patch_az(monkeypatch, secret_show_rc=1, secret_show_stderr="ERROR: (SecretNotFound)")

    with pytest.raises(PullRuntimeKeyError) as exc:
        pull_runtime_key_inline("dev", keyvault="movate-dev-kv-mvt")
    assert "az keyvault secret show failed" in str(exc.value)


@pytest.mark.unit
def test_pull_runtime_key_inline_raises_on_non_mvt_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from movate.cli.auth import PullRuntimeKeyError, pull_runtime_key_inline  # noqa: PLC0415

    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    _patch_az(monkeypatch, secret_value="not-a-movate-key")

    with pytest.raises(PullRuntimeKeyError) as exc:
        pull_runtime_key_inline("dev", keyvault="movate-dev-kv-mvt")
    assert "movate bearer" in str(exc.value)
    # Nothing got saved.
    creds_path = tmp_path / ".movate" / "credentials"
    if creds_path.exists():
        assert "MDK_DEV_KEY=" not in creds_path.read_text()


# ---------------------------------------------------------------------------
# refresh_runtime_key_inline — the in-pod `az containerapp exec mdk auth
# create-key` mint. The deploy 401 auto-recovery calls this with an admin-
# capable scope so the minted bearer can actually perform admin uploads.
# ---------------------------------------------------------------------------


def _patch_exec_mint(
    monkeypatch: pytest.MonkeyPatch,
    *,
    minted_key: str = "mvt_live_demotena_kidEXEC123_secretEXECdataABCDE",
) -> dict[str, list[list[str]]]:
    """Mock `az account set` + `az containerapp exec` for the in-pod mint.

    The exec returns the minted key on stderr (mirrors create-key --quiet,
    which prints the secret to stderr). Captures every az argv so a test can
    inspect the inner `mdk auth create-key` command for forwarded scopes."""
    captures: dict[str, list[list[str]]] = {"calls": []}

    class _Done:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> _Done:
        captures["calls"].append(list(cmd))
        if cmd[:3] == ["az", "containerapp", "exec"]:
            return _Done(returncode=0, stdout="", stderr=f"secret: {minted_key}\n")
        return _Done(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/local/bin/az" if cmd == "az" else None)
    return captures


@pytest.mark.unit
def test_refresh_runtime_key_inline_forwards_scope_to_inner_create_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deploy recovery path passes `scopes=["fleet-admin"]`; the in-pod
    `mdk auth create-key` must receive a matching `--scope fleet-admin` so the
    minted bearer is admin-capable (deploy uploads need `admin`)."""
    from movate.cli.auth import refresh_runtime_key_inline  # noqa: PLC0415

    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    captures = _patch_exec_mint(monkeypatch)

    key, env_var = refresh_runtime_key_inline("dev", scopes=["fleet-admin"])

    assert key == "mvt_live_demotena_kidEXEC123_secretEXECdataABCDE"
    assert env_var == "MDK_DEV_KEY"
    exec_call = next(c for c in captures["calls"] if c[:3] == ["az", "containerapp", "exec"])
    inner = next(part for part in exec_call if "mdk auth create-key" in part)
    assert "--scope fleet-admin" in inner


@pytest.mark.unit
def test_refresh_runtime_key_inline_default_omits_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default callers (interactive `refresh-runtime-key`) pass no scopes — the
    inner create-key must NOT gain a `--scope` flag, preserving the legacy
    read,run,eval tenant-key behaviour."""
    from movate.cli.auth import refresh_runtime_key_inline  # noqa: PLC0415

    _isolate_credentials(tmp_path, monkeypatch)
    _write_user_config(tmp_path)
    captures = _patch_exec_mint(monkeypatch)

    refresh_runtime_key_inline("dev")

    exec_call = next(c for c in captures["calls"] if c[:3] == ["az", "containerapp", "exec"])
    inner = next(part for part in exec_call if "mdk auth create-key" in part)
    assert "--scope" not in inner
