"""Client-side OIDC token provider (ADR 012 D4).

When a target opts into ``auth: oidc``, ``_resolve_target_bearer`` obtains a
short-lived JWT from an :class:`OidcTokenProvider` (default: the Azure CLI)
instead of reading the static ``MDK_<T>_KEY`` env var. The default ``auth:
key`` path is unchanged.

No real ``az`` is invoked — ``subprocess.run`` / ``shutil.which`` are mocked.

Coverage:
* ``auth='oidc'`` → ``_resolve_target_bearer`` shells out to ``az account
  get-access-token`` and returns the token (no env var read);
* the ``--resource`` / ``--scope`` argv is built from the target config;
* ``auth='key'`` (default) path is unchanged — still reads ``key_env``;
* missing ``az`` → actionable :class:`OidcTokenError`;
* a target with ``auth='oidc'`` but no resource/scope → actionable error;
* non-zero ``az`` exit → actionable error.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import typer

from movate.core.oidc_provider import AzureCliTokenProvider, OidcTokenError
from movate.core.user_config import TargetConfig

_TOKEN = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJtZSJ9.sig"


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["az"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _write_user_config(home: Path, *, auth: str, extra: str = "") -> None:
    cfg_dir = home / ".movate"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(
        "active: dev\n"
        "targets:\n"
        "  dev:\n"
        "    url: https://fake.example.com\n"
        "    key_env: MDK_DEV_KEY\n"
        f"    auth: {auth}\n" + extra
    )


# ---------------------------------------------------------------------------
# AzureCliTokenProvider — argv + error surfaces (mocked subprocess)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAzureCliTokenProvider:
    def test_resource_argv_and_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/az")
        captured: dict[str, list[str]] = {}

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            captured["cmd"] = cmd
            return _completed(0, stdout=_TOKEN + "\n")

        monkeypatch.setattr("subprocess.run", fake_run)
        target = TargetConfig(
            url="https://x", key_env="MDK_DEV_KEY", auth="oidc", oidc_resource="api://movate"
        )
        token = AzureCliTokenProvider().get_token("dev", target)
        assert token == _TOKEN
        assert captured["cmd"][:3] == ["az", "account", "get-access-token"]
        assert "--resource" in captured["cmd"]
        assert "api://movate" in captured["cmd"]
        assert "--scope" not in captured["cmd"]

    def test_scope_takes_precedence_over_resource(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/az")
        captured: dict[str, list[str]] = {}

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            captured["cmd"] = cmd
            return _completed(0, stdout=_TOKEN)

        monkeypatch.setattr("subprocess.run", fake_run)
        target = TargetConfig(
            url="https://x",
            key_env="MDK_DEV_KEY",
            auth="oidc",
            oidc_resource="api://movate",
            oidc_scope="api://movate/.default",
        )
        AzureCliTokenProvider().get_token("dev", target)
        assert "--scope" in captured["cmd"]
        assert "api://movate/.default" in captured["cmd"]
        assert "--resource" not in captured["cmd"]

    def test_missing_az_raises_actionable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda name: None)
        target = TargetConfig(
            url="https://x", key_env="MDK_DEV_KEY", auth="oidc", oidc_resource="api://movate"
        )
        with pytest.raises(OidcTokenError) as exc:
            AzureCliTokenProvider().get_token("dev", target)
        assert "az" in str(exc.value).lower()

    def test_no_resource_or_scope_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/az")
        target = TargetConfig(url="https://x", key_env="MDK_DEV_KEY", auth="oidc")
        with pytest.raises(OidcTokenError) as exc:
            AzureCliTokenProvider().get_token("dev", target)
        assert "oidc_resource" in str(exc.value)

    def test_nonzero_exit_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/az")
        monkeypatch.setattr(
            "subprocess.run",
            lambda cmd, **kw: _completed(1, stderr="Please run 'az login'"),
        )
        target = TargetConfig(
            url="https://x", key_env="MDK_DEV_KEY", auth="oidc", oidc_resource="api://movate"
        )
        with pytest.raises(OidcTokenError) as exc:
            AzureCliTokenProvider().get_token("dev", target)
        assert "az login" in str(exc.value)


# ---------------------------------------------------------------------------
# _resolve_target_bearer — routing on TargetConfig.auth
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResolveTargetBearer:
    def test_oidc_target_uses_provider_not_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from movate.cli.kb_cmd import _resolve_target_bearer  # noqa: PLC0415

        _write_user_config(tmp_path, auth="oidc", extra="    oidc_resource: api://movate\n")
        monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / ".movate" / "config.yaml"))
        # Deliberately leave MDK_DEV_KEY UNSET — the oidc path must not read it.
        monkeypatch.delenv("MDK_DEV_KEY", raising=False)
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/az")
        monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _completed(0, stdout=_TOKEN))

        name, _cfg, base_url, bearer = _resolve_target_bearer("dev")
        assert name == "dev"
        assert base_url == "https://fake.example.com"
        assert bearer == _TOKEN

    def test_key_target_unchanged(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from movate.cli.kb_cmd import _resolve_target_bearer  # noqa: PLC0415

        _write_user_config(tmp_path, auth="key")
        monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / ".movate" / "config.yaml"))
        monkeypatch.setenv("MDK_DEV_KEY", "mvt_live_tenantxx_KEYID12345_secret")
        # If the key path wrongly shelled out to az, this would blow up.
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call az for auth=key")),
        )

        name, _cfg, _base_url, bearer = _resolve_target_bearer("dev")
        assert name == "dev"
        assert bearer == "mvt_live_tenantxx_KEYID12345_secret"

    def test_default_auth_is_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A target with NO auth field defaults to "key" — backward compatible.
        cfg_dir = tmp_path / ".movate"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config.yaml").write_text(
            "active: dev\n"
            "targets:\n"
            "  dev:\n"
            "    url: https://fake.example.com\n"
            "    key_env: MDK_DEV_KEY\n"
        )
        from movate.cli.kb_cmd import _resolve_target_bearer  # noqa: PLC0415

        monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg_dir / "config.yaml"))
        monkeypatch.setenv("MDK_DEV_KEY", "mvt_live_tenantxx_KEYID12345_secret")
        _name, _cfg, _url, bearer = _resolve_target_bearer("dev")
        assert bearer == "mvt_live_tenantxx_KEYID12345_secret"

    def test_oidc_missing_az_exits_2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from movate.cli.kb_cmd import _resolve_target_bearer  # noqa: PLC0415

        _write_user_config(tmp_path, auth="oidc", extra="    oidc_resource: api://movate\n")
        monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / ".movate" / "config.yaml"))
        monkeypatch.setattr("shutil.which", lambda name: None)
        with pytest.raises(typer.Exit) as exc:
            _resolve_target_bearer("dev")
        assert exc.value.exit_code == 2
