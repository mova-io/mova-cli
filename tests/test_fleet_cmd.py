"""``mdk fleet`` — read-only cross-target health + agent inventory (+ logs).

Covers the two subcommands without any real I/O:

* ``fleet status`` — fans out concurrent /healthz + /api/v1/agents probes
  across every configured target. We route the lazily-imported
  ``httpx.AsyncClient`` through a ``MockTransport`` that answers per-URL,
  so we can assert: two healthy targets both render with the right
  version + agent count, the active one is marked, an unreachable target
  degrades to "unreachable" without failing the command, and ``--json``
  emits a well-formed array.
* ``fleet logs`` — mocks the shared ``deploy._run_az`` runner so no real
  ``az`` runs; asserts the derived ``movate-{env}-api`` app name + ``--tail``
  reach the command, and that a target with no Azure config errors out
  (exit 2) before any ``az`` call.

Mirrors the config + monkeypatch setup from
``test_kb_management_remote_target.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _write_two_target_config(home: Path) -> None:
    """Two targets (dev active, prod not), each with a distinct URL +
    key_env so the status probe routing is unambiguous."""
    cfg_dir = home / ".movate"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(
        "active: dev\n"
        "targets:\n"
        "  dev:\n"
        "    url: https://dev.example.com\n"
        "    key_env: MDK_DEV_KEY\n"
        "    azure_env: dev\n"
        "  prod:\n"
        "    url: https://prod.example.com\n"
        "    key_env: MDK_PROD_KEY\n"
        "    azure_subscription: sub-123\n"
        "    azure_resource_group: movate-prod-rg\n"
        "    azure_acr_name: movateprodacr\n"
        "    azure_env: prod\n"
    )


def _common_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_two_target_config(tmp_path)
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / ".movate" / "config.yaml"))
    # Isolate from the developer's real ~/.movate/credentials autoload.
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / "nocreds"))
    monkeypatch.setenv("MDK_DEV_KEY", "mvt_dev_secret")
    monkeypatch.setenv("MDK_PROD_KEY", "mvt_prod_secret")


def _make_async_client_factory(handler: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the lazily-imported ``httpx.AsyncClient`` through a MockTransport.

    The fleet status path uses ``httpx.AsyncClient`` (concurrent gather), so
    we inject an ``AsyncMockTransport`` built from a sync handler — httpx's
    ``MockTransport`` works for both sync + async clients.
    """
    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport  # type: ignore[assignment]
        return real_async_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("httpx.AsyncClient", factory)


# ---------------------------------------------------------------------------
# fleet status — happy path (2 healthy targets)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_status_two_healthy_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)
    seen_auth: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        if path == "/healthz":
            version = "2026.5.1.1" if host == "dev.example.com" else "2026.5.2.3"
            return httpx.Response(200, json={"status": "ok", "version": version})
        if path == "/api/v1/agents":
            seen_auth[host] = request.headers.get("authorization")
            if host == "dev.example.com":
                return httpx.Response(
                    200,
                    json={
                        "agents": [
                            {"name": "faq", "version": "1"},
                            {"name": "triage", "version": "1"},
                        ],
                        "count": 2,
                    },
                )
            return httpx.Response(
                200,
                json={"agents": [{"name": "billing", "version": "1"}], "count": 1},
            )
        return httpx.Response(404)

    _make_async_client_factory(handler, monkeypatch)

    result = runner.invoke(app, ["fleet", "status"], env={"COLUMNS": "240"})
    assert result.exit_code == 0, result.stdout + result.stderr
    out = result.stdout
    # Both targets present.
    assert "dev" in out
    assert "prod" in out
    # Versions surface.
    assert "2026.5.1.1" in out
    assert "2026.5.2.3" in out
    # Agent counts surface (2 for dev, 1 for prod) with names.
    assert "faq" in out
    assert "billing" in out
    # Active target (dev) carries the ● marker.
    assert "●" in out
    # Each authenticated agents probe carried that target's bearer.
    assert seen_auth["dev.example.com"] == "Bearer mvt_dev_secret"
    assert seen_auth["prod.example.com"] == "Bearer mvt_prod_secret"


# ---------------------------------------------------------------------------
# fleet status — one target unreachable
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_status_one_unreachable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        # prod is dead — raise a connection error for every prod request.
        if host == "prod.example.com":
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "version": "2026.5.1.1"})
        if request.url.path == "/api/v1/agents":
            return httpx.Response(200, json={"agents": [{"name": "faq"}], "count": 1})
        return httpx.Response(404)

    _make_async_client_factory(handler, monkeypatch)

    result = runner.invoke(app, ["fleet", "status"], env={"COLUMNS": "240"})
    # A dead target must NOT crash the command or change its exit code.
    assert result.exit_code == 0, result.stdout + result.stderr
    out = result.stdout
    # The healthy target still renders.
    assert "dev" in out
    assert "2026.5.1.1" in out
    # The dead one is shown as unreachable rather than omitted/crashing.
    assert "unreachable" in out


# ---------------------------------------------------------------------------
# fleet status --json
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_status_json_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "prod.example.com":
            raise httpx.ConnectError("down", request=request)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "version": "2026.5.1.1"})
        if request.url.path == "/api/v1/agents":
            return httpx.Response(
                200, json={"agents": [{"name": "faq"}, {"name": "triage"}], "count": 2}
            )
        return httpx.Response(404)

    _make_async_client_factory(handler, monkeypatch)

    result = runner.invoke(app, ["fleet", "status", "--json"], env={"COLUMNS": "240"})
    assert result.exit_code == 0, result.stdout + result.stderr

    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) == 2
    by_name = {row["target"]: row for row in data}
    assert set(by_name) == {"dev", "prod"}

    # Expected keys per target.
    expected_keys = {
        "target",
        "active",
        "env",
        "url",
        "reachable",
        "status",
        "version",
        "authorized",
        "agent_count",
        "agents",
        "error",
    }
    assert expected_keys <= set(by_name["dev"])

    dev = by_name["dev"]
    assert dev["active"] is True
    assert dev["reachable"] is True
    assert dev["version"] == "2026.5.1.1"
    assert dev["agent_count"] == 2
    assert dev["agents"] == ["faq", "triage"]

    prod = by_name["prod"]
    assert prod["active"] is False
    assert prod["reachable"] is False
    # Tokens never leak into the machine-readable output.
    assert "mvt_dev_secret" not in result.stdout
    assert "mvt_prod_secret" not in result.stdout


# ---------------------------------------------------------------------------
# fleet status — bare `mdk fleet` defaults to status
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bare_fleet_runs_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "version": "2026.5.1.1"})
        if request.url.path == "/api/v1/agents":
            return httpx.Response(200, json={"agents": [], "count": 0})
        return httpx.Response(404)

    _make_async_client_factory(handler, monkeypatch)

    result = runner.invoke(app, ["fleet"], env={"COLUMNS": "240"})
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "movate fleet" in result.stdout  # the status table title


# ---------------------------------------------------------------------------
# fleet logs — no Azure config
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_logs_no_azure_config_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)

    # `dev` target has only azure_env (no subscription/rg) → must error.
    called: list[list[str]] = []
    monkeypatch.setattr(
        "movate.cli.deploy._run_az",
        lambda cmd, *, what: called.append(cmd) or "",  # type: ignore[func-returns-value]
    )

    result = runner.invoke(app, ["fleet", "logs", "dev"], env={"COLUMNS": "240"})
    assert result.exit_code == 2
    out = (result.stdout + result.stderr).lower()
    assert "azure" in out
    # Must fail BEFORE any az call.
    assert called == []


# ---------------------------------------------------------------------------
# fleet logs — with Azure config invokes az containerapp logs show
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_logs_with_azure_invokes_az(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)

    captured: dict[str, object] = {}

    def fake_run_az(cmd: list[str], *, what: str) -> str:
        captured["cmd"] = cmd
        captured["what"] = what
        return "2026-05-22 log line one\n2026-05-22 log line two\n"

    monkeypatch.setattr("movate.cli.deploy._run_az", fake_run_az)
    # Pretend `az` is installed so the which() guard passes.
    monkeypatch.setattr("movate.cli.fleet_cmd.shutil.which", lambda _name: "/usr/bin/az")

    result = runner.invoke(app, ["fleet", "logs", "prod", "--tail", "100"], env={"COLUMNS": "240"})
    assert result.exit_code == 0, result.stdout + result.stderr

    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    # Read-only az subcommand only.
    assert cmd[:4] == ["az", "containerapp", "logs", "show"]
    # Derived app name: movate-{env}-api.
    assert "movate-prod-api" in cmd
    # --tail forwarded.
    assert "--tail" in cmd
    assert cmd[cmd.index("--tail") + 1] == "100"
    # Resource group + subscription from the target config.
    assert "movate-prod-rg" in cmd
    assert "sub-123" in cmd
    # Log output surfaced to the operator.
    assert "log line one" in result.stdout


@pytest.mark.unit
def test_logs_default_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "movate.cli.deploy._run_az",
        lambda cmd, *, what: captured.update(cmd=cmd) or "",  # type: ignore[func-returns-value]
    )
    monkeypatch.setattr("movate.cli.fleet_cmd.shutil.which", lambda _name: "/usr/bin/az")

    result = runner.invoke(app, ["fleet", "logs", "prod"], env={"COLUMNS": "240"})
    assert result.exit_code == 0, result.stdout + result.stderr
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    # Default tail is 50.
    assert cmd[cmd.index("--tail") + 1] == "50"
