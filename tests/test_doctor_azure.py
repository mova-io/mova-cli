"""``movate doctor --target <name>`` — Azure preflight checks.

Same testing strategy as ``tests/test_deploy.py``: the underlying
checks shell out to ``az``, so we mock ``subprocess.run`` + ``shutil.which``
+ ``httpx.Client`` to deterministically flip each branch.

Coverage:

* ``run_azure_preflight`` — every short-circuit (no az / no login /
  missing config / wrong sub) and the happy path with mocked az
  responses + mocked /healthz.
* CLI integration — ``movate doctor --target prod`` renders the
  Azure section; missing target surfaces a clean error; the
  ``movate doctor`` (no --target) hot-path is untouched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from movate.cli._azure_doctor import run_azure_preflight
from movate.cli.main import app as cli_app
from movate.core.user_config import (
    TargetConfig,
    UserConfig,
    save_user_config,
)

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _target(**overrides) -> TargetConfig:
    """Fully-configured target — happy path defaults; override for negative cases."""
    base = {
        "url": "https://movate-prod-api.example.com",
        "key_env": "MOVATE_PROD_KEY",
        "azure_subscription": "00000000-0000-0000-0000-000000000000",
        "azure_resource_group": "movate-prod-rg",
        "azure_acr_name": "movateprodacr",
        "azure_env": "prod",
    }
    base.update(overrides)
    return TargetConfig(**base)


def _fake_az(handlers: dict[str, tuple[int, str]]):
    """Build a fake ``subprocess.run`` that picks a response by command shape.

    ``handlers`` maps a substring (matched against the joined command)
    to ``(returncode, stdout)``. The first key whose substring is in
    the command string wins. Default returncode=1 / empty stdout
    (= "not found").
    """

    def _run(cmd, *_args, **_kwargs):
        joined = " ".join(cmd)
        for needle, (rc, stdout) in handlers.items():
            if needle in joined:
                return subprocess.CompletedProcess(args=cmd, returncode=rc, stdout=stdout)
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="")

    return _run


def _patch_az_present(monkeypatch) -> None:
    import shutil  # noqa: PLC0415

    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name == "az" else None,
    )


def _patch_healthz_ok(monkeypatch, version: str = "0.5.0") -> None:
    """Make every ``httpx.Client().get`` return a 200 with the given version."""

    def handler(request):
        return httpx.Response(200, json={"status": "ok", "version": version})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("movate.cli._azure_doctor.httpx.Client", factory)


# ---------------------------------------------------------------------------
# run_azure_preflight — pure-function unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_preflight_no_az_short_circuits(monkeypatch) -> None:
    """No ``az`` on PATH: one row reporting it, then stop. Avoids cascading
    false-positives (sub mismatch, RG missing) from the same root cause."""
    import shutil  # noqa: PLC0415

    monkeypatch.setattr(shutil, "which", lambda name: None)
    rows = run_azure_preflight("prod", _target())
    assert len(rows) == 1
    assert rows[0].name == "az CLI"
    assert rows[0].status == "missing"
    assert "install" in rows[0].detail.lower()


@pytest.mark.unit
def test_preflight_no_login_short_circuits(monkeypatch) -> None:
    """``az account show`` fails → flag it and stop. Every later check
    needs a logged-in session, so reporting them all as red is noise."""
    _patch_az_present(monkeypatch)
    monkeypatch.setattr(
        "movate.cli._azure_doctor.subprocess.run",
        _fake_az({}),  # everything returns rc=1, stdout=""
    )

    rows = run_azure_preflight("prod", _target())
    assert len(rows) == 2  # az CLI ok, az login missing
    assert rows[0].status == "ok"
    assert rows[1].name == "az login"
    assert rows[1].status == "missing"


@pytest.mark.unit
def test_preflight_missing_azure_config_short_circuits(monkeypatch) -> None:
    """Target without ``azure_subscription`` / etc. fails the config check
    BEFORE we waste az subprocess calls."""
    _patch_az_present(monkeypatch)
    monkeypatch.setattr(
        "movate.cli._azure_doctor.subprocess.run",
        _fake_az(
            {
                "account show": (0, '{"id": "sub-id", "tenantId": "tenant-id"}'),
            }
        ),
    )
    target = _target(azure_acr_name=None)

    rows = run_azure_preflight("prod", target)
    config_row = next(r for r in rows if r.name == "target azure config")
    assert config_row.status == "missing"
    assert "azure_acr_name" in config_row.detail
    assert "config add-target" in config_row.detail


@pytest.mark.unit
def test_preflight_subscription_mismatch_short_circuits(monkeypatch) -> None:
    """The current ``az`` session is on a different sub than the target.
    Operator pointer tells them which subscription to switch to."""
    _patch_az_present(monkeypatch)
    monkeypatch.setattr(
        "movate.cli._azure_doctor.subprocess.run",
        _fake_az(
            {
                "account show": (
                    0,
                    '{"id": "OTHER-SUB-ID", "tenantId": "tenant-id"}',
                ),
            }
        ),
    )
    rows = run_azure_preflight("prod", _target())
    sub_row = next(r for r in rows if r.name == "subscription match")
    assert sub_row.status == "missing"
    assert "az account set" in sub_row.detail
    # Doesn't try to look up the RG with the wrong sub.
    assert not any(r.name == "resource group" for r in rows)


@pytest.mark.unit
def test_preflight_missing_rg_reported(monkeypatch) -> None:
    """RG doesn't exist: missing-row + operator pointer at the bootstrap
    script. Don't cascade into ACR / Container Apps checks (they'd
    all fail with the same noise)."""
    _patch_az_present(monkeypatch)
    monkeypatch.setattr(
        "movate.cli._azure_doctor.subprocess.run",
        _fake_az(
            {
                "account show": (
                    0,
                    '{"id": "00000000-0000-0000-0000-000000000000", "tenantId": "tenant-id"}',
                ),
                # `group show` returns rc=1 by default → missing.
            }
        ),
    )
    rows = run_azure_preflight("prod", _target())
    rg_row = next(r for r in rows if r.name == "resource group")
    assert rg_row.status == "missing"
    assert "azure-bootstrap.sh" in rg_row.detail


@pytest.mark.unit
def test_preflight_happy_path_reports_every_layer(monkeypatch) -> None:
    """Everything green: every section ok, /healthz reachable with
    the version we expect."""
    _patch_az_present(monkeypatch)
    monkeypatch.setattr(
        "movate.cli._azure_doctor.subprocess.run",
        _fake_az(
            {
                "account show": (
                    0,
                    '{"id": "00000000-0000-0000-0000-000000000000", "tenantId": "tenant-id"}',
                ),
                "group show": (0, '{"name": "movate-prod-rg", "location": "eastus2"}'),
                "acr show": (
                    0,
                    '{"name": "movateprodacr", "sku": {"name": "Basic"}}',
                ),
                "containerapp show": (
                    0,
                    '{"properties": {"template": {"containers": '
                    '[{"image": "movateprodacr.azurecr.io/movate:0.5.0-abc1234"}]}}}',
                ),
            }
        ),
    )
    _patch_healthz_ok(monkeypatch, version="0.5.0")

    rows = run_azure_preflight("prod", _target())
    statuses = {r.name: r.status for r in rows}
    assert statuses["az CLI"] == "ok"
    assert statuses["az login"] == "ok"
    assert statuses["target azure config"] == "ok"
    assert statuses["subscription match"] == "ok"
    assert statuses["resource group"] == "ok"
    assert statuses["ACR"] == "ok"
    assert statuses["containerapp api"] == "ok"
    assert statuses["containerapp worker"] == "ok"
    assert statuses["/healthz"] == "ok"
    # The image tag is surfaced so operators know what's deployed.
    api_row = next(r for r in rows if r.name == "containerapp api")
    assert "0.5.0-abc1234" in api_row.detail


@pytest.mark.unit
def test_preflight_healthz_unreachable_reported(monkeypatch) -> None:
    """ACA infrastructure exists but the runtime is down. Distinct from
    "missing" — error so the operator sees the contrast."""
    _patch_az_present(monkeypatch)
    monkeypatch.setattr(
        "movate.cli._azure_doctor.subprocess.run",
        _fake_az(
            {
                "account show": (
                    0,
                    '{"id": "00000000-0000-0000-0000-000000000000", "tenantId": "tenant-id"}',
                ),
                "group show": (0, '{"name": "movate-prod-rg"}'),
                "acr show": (0, '{"name": "movateprodacr", "sku": {"name": "Basic"}}'),
                "containerapp show": (0, '{"properties": {"template": {"containers": [{}]}}}'),
            }
        ),
    )

    def fail_handler(request):
        raise httpx.ConnectError("boom")

    # Capture the real Client before patching so the lambda doesn't
    # recurse into itself (we're patching at the module attribute, so
    # the lambda body would otherwise call the patched version too).
    real_client = httpx.Client
    transport = httpx.MockTransport(fail_handler)
    monkeypatch.setattr(
        "movate.cli._azure_doctor.httpx.Client",
        lambda *args, **kwargs: real_client(*args, transport=transport, **kwargs),
    )

    rows = run_azure_preflight("prod", _target())
    healthz_row = next(r for r in rows if r.name == "/healthz")
    assert healthz_row.status == "error"
    assert "unreachable" in healthz_row.detail


# ---------------------------------------------------------------------------
# CLI integration — `movate doctor --target prod`
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_doctor_without_target_unchanged(tmp_path: Path, monkeypatch) -> None:
    """`movate doctor` (no --target) doesn't touch any Azure code path.
    Preserves the existing fast local-only behavior."""
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / "cfg.yaml"))
    result = runner.invoke(cli_app, ["doctor"])
    assert result.exit_code == 0
    # No Azure preflight section.
    assert "azure preflight" not in result.stdout.lower()


@pytest.mark.unit
def test_cli_doctor_with_target_renders_azure_table(tmp_path: Path, monkeypatch) -> None:
    """`movate doctor --target prod` renders the Azure preflight section."""
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / "cfg.yaml"))
    save_user_config(
        UserConfig(
            targets={"prod": _target()},
            active="prod",
        )
    )
    # Make az appear absent so we get a clean, deterministic short-circuit.
    import shutil  # noqa: PLC0415

    monkeypatch.setattr(shutil, "which", lambda name: None)

    result = runner.invoke(cli_app, ["doctor", "--target", "prod"])
    assert result.exit_code == 0
    out = result.stdout.lower()
    assert "azure preflight" in out
    assert "prod" in out
    # The first row is "az CLI" — verify it surfaces the missing-az
    # finding (not a stack trace).
    assert "az cli" in out


@pytest.mark.unit
def test_cli_doctor_with_unknown_target_reports_resolver_error(tmp_path: Path, monkeypatch) -> None:
    """An unknown --target name: clean error message, exit 0 (the local
    doctor still ran successfully; only the Azure section is skipped)."""
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / "cfg.yaml"))
    save_user_config(UserConfig(targets={}, active=None))

    result = runner.invoke(cli_app, ["doctor", "--target", "ghost"])
    # Doctor itself doesn't fail — it's a diagnostic tool; we report
    # the resolver error inline and let the rest of the output stand.
    assert result.exit_code == 0
    assert "azure preflight skipped" in result.stdout.lower()


# ---------------------------------------------------------------------------
# _check_auth_roundtrip — operator's saved bearer roundtrip
# ---------------------------------------------------------------------------


def _patch_auth_roundtrip(monkeypatch, status_code: int) -> None:
    """Patch httpx.Client inside _azure_doctor to return ``status_code``
    on the auth-roundtrip GET. ``_patch_healthz_ok`` already returns 200
    for /healthz + /ready; this composes on top of it so the
    auth-roundtrip row gets the status we want."""

    def handler(request):
        if request.url.path == "/api/v1/agents":
            return httpx.Response(status_code, json={"agents": []})
        # /healthz, /ready
        if request.url.path == "/ready":
            return httpx.Response(
                200,
                json={
                    "status": "ready",
                    "version": "0.5.0",
                    "checks": {"storage": "ok"},
                    "storage_backend": "postgres",
                    "storage_durable": True,
                },
            )
        return httpx.Response(200, json={"status": "ok", "version": "0.5.0"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("movate.cli._azure_doctor.httpx.Client", factory)


@pytest.mark.unit
def test_auth_roundtrip_missing_env_var_reports_recovery_command(
    monkeypatch,
) -> None:
    """The operator hasn't saved the bearer locally yet — surface the
    one-command recovery path so they know exactly what to run."""
    monkeypatch.delenv("MOVATE_PROD_KEY", raising=False)
    _patch_az_present(monkeypatch)
    monkeypatch.setattr(
        "movate.cli._azure_doctor.subprocess.run",
        _fake_az(
            {
                "account show": (
                    0,
                    '{"id": "00000000-0000-0000-0000-000000000000", "tenantId": "tenant-id"}',
                ),
                "group show": (0, '{"name": "movate-prod-rg", "location": "eastus2"}'),
                "acr show": (0, '{"name": "movateprodacr", "sku": {"name": "Basic"}}'),
                "containerapp show": (
                    0,
                    '{"properties": {"template": {"containers": '
                    '[{"image": "movateprodacr.azurecr.io/movate:0.5.0-abc1234"}]}}}',
                ),
            }
        ),
    )
    _patch_auth_roundtrip(monkeypatch, status_code=200)

    rows = run_azure_preflight("prod", _target())
    auth_row = next(r for r in rows if r.name == "auth roundtrip")
    assert auth_row.status == "missing"
    assert "MOVATE_PROD_KEY" in auth_row.detail
    assert "mdk auth pull-runtime-key" in auth_row.detail


@pytest.mark.unit
def test_auth_roundtrip_200_reports_ok(monkeypatch) -> None:
    """Bearer set + runtime accepts it = green row. This is what every
    healthy environment looks like."""
    monkeypatch.setenv("MOVATE_PROD_KEY", "mvt_live_demotena_keyid_secret")
    _patch_az_present(monkeypatch)
    monkeypatch.setattr(
        "movate.cli._azure_doctor.subprocess.run",
        _fake_az(
            {
                "account show": (
                    0,
                    '{"id": "00000000-0000-0000-0000-000000000000", "tenantId": "tenant-id"}',
                ),
                "group show": (0, '{"name": "movate-prod-rg", "location": "eastus2"}'),
                "acr show": (0, '{"name": "movateprodacr", "sku": {"name": "Basic"}}'),
                "containerapp show": (
                    0,
                    '{"properties": {"template": {"containers": '
                    '[{"image": "movateprodacr.azurecr.io/movate:0.5.0-abc1234"}]}}}',
                ),
            }
        ),
    )
    _patch_auth_roundtrip(monkeypatch, status_code=200)

    rows = run_azure_preflight("prod", _target())
    auth_row = next(r for r in rows if r.name == "auth roundtrip")
    assert auth_row.status == "ok"
    assert "saved bearer accepted" in auth_row.detail


@pytest.mark.unit
def test_auth_roundtrip_401_reports_stale_bearer_with_truncated_prefix(
    monkeypatch,
) -> None:
    """Bearer set but runtime rejects it = the single most useful red
    row in the doctor. Shows the first 16 chars (enough to spot
    'wrong tenant', 'old key from yesterday') without leaking the
    full secret to logs. Detail names the recovery command."""
    monkeypatch.setenv(
        "MOVATE_PROD_KEY", "mvt_live_demotena_0123456789abcdef_DEADBEEF"
    )
    _patch_az_present(monkeypatch)
    monkeypatch.setattr(
        "movate.cli._azure_doctor.subprocess.run",
        _fake_az(
            {
                "account show": (
                    0,
                    '{"id": "00000000-0000-0000-0000-000000000000", "tenantId": "tenant-id"}',
                ),
                "group show": (0, '{"name": "movate-prod-rg", "location": "eastus2"}'),
                "acr show": (0, '{"name": "movateprodacr", "sku": {"name": "Basic"}}'),
                "containerapp show": (
                    0,
                    '{"properties": {"template": {"containers": '
                    '[{"image": "movateprodacr.azurecr.io/movate:0.5.0-abc1234"}]}}}',
                ),
            }
        ),
    )
    _patch_auth_roundtrip(monkeypatch, status_code=401)

    rows = run_azure_preflight("prod", _target())
    auth_row = next(r for r in rows if r.name == "auth roundtrip")
    assert auth_row.status == "error"
    # Bearer prefix shown — first 16 chars only.
    assert "mvt_live_demoten" in auth_row.detail
    # Tail (the actual secret) NOT shown.
    assert "DEADBEEF" not in auth_row.detail
    # Recovery command surfaced.
    assert "mdk auth pull-runtime-key" in auth_row.detail


@pytest.mark.unit
def test_auth_roundtrip_network_error_reports_unreachable(monkeypatch) -> None:
    """Transport-level error (DNS, TLS, etc.) is distinct from a 401 —
    the bearer might be fine, the network isn't."""
    monkeypatch.setenv("MOVATE_PROD_KEY", "mvt_live_demotena_keyid_secret")
    _patch_az_present(monkeypatch)
    monkeypatch.setattr(
        "movate.cli._azure_doctor.subprocess.run",
        _fake_az(
            {
                "account show": (
                    0,
                    '{"id": "00000000-0000-0000-0000-000000000000", "tenantId": "tenant-id"}',
                ),
                "group show": (0, '{"name": "movate-prod-rg", "location": "eastus2"}'),
                "acr show": (0, '{"name": "movateprodacr", "sku": {"name": "Basic"}}'),
                "containerapp show": (
                    0,
                    '{"properties": {"template": {"containers": '
                    '[{"image": "movateprodacr.azurecr.io/movate:0.5.0-abc1234"}]}}}',
                ),
            }
        ),
    )

    def handler(request):
        # /healthz + /ready succeed so we reach the auth check; the
        # auth GET is the one that blows up.
        if request.url.path == "/api/v1/agents":
            raise httpx.ConnectError("network unreachable")
        if request.url.path == "/ready":
            return httpx.Response(
                200,
                json={
                    "status": "ready",
                    "version": "0.5.0",
                    "checks": {"storage": "ok"},
                    "storage_backend": "postgres",
                    "storage_durable": True,
                },
            )
        return httpx.Response(200, json={"status": "ok", "version": "0.5.0"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("movate.cli._azure_doctor.httpx.Client", factory)

    rows = run_azure_preflight("prod", _target())
    auth_row = next(r for r in rows if r.name == "auth roundtrip")
    assert auth_row.status == "error"
    assert "ConnectError" in auth_row.detail or "unreachable" in auth_row.detail
