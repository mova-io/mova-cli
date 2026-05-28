"""``mdk project`` — comprehensive polish layer over the per-endpoint PRs.

Covers the cross-command UX guarantees the parity layer adds on top of
the per-endpoint PRs landing tonight:

* ``--json`` on every read command (parity with ``mdk fleet``,
  ``mdk runs list``).
* Next-step hint on every write command (parity with ``mdk init`` /
  ``mdk add``).
* ``--target`` plumbing — per-command flag, falling back to the
  top-level ``-t`` / ``MDK_TARGET`` env var, then the active config
  target.
* The pre-call ``echo_remote_context`` line on stderr is suppressed
  under ``--json`` (clean machine output).

Mocks the runtime via ``httpx.MockTransport`` so the tests don't need
the per-endpoint PRs' server routes present — the CLI parity layer is
a thin wrapper over the JSON-over-HTTP API and we assert its shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _write_config(home: Path) -> None:
    """Two-target config: dev (active) + prod. Same shape as test_fleet_cmd."""
    cfg_dir = home / ".movate"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(
        "active: dev\n"
        "targets:\n"
        "  dev:\n"
        "    url: https://dev.example.com\n"
        "    key_env: MDK_DEV_KEY\n"
        "  prod:\n"
        "    url: https://prod.example.com\n"
        "    key_env: MDK_PROD_KEY\n"
    )


def _setup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path)
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / ".movate" / "config.yaml"))
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / "nocreds"))
    monkeypatch.setenv("MDK_DEV_KEY", "dev-key")
    monkeypatch.setenv("MDK_PROD_KEY", "prod-key")


def _route(handler: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the lazy ``httpx.AsyncClient`` through a MockTransport."""
    real = httpx.AsyncClient
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport  # type: ignore[assignment]
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("httpx.AsyncClient", factory)


# ---------------------------------------------------------------------------
# `mdk project list` — read with --json parity
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_project_list_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_env(tmp_path, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/projects"
        assert request.headers.get("authorization") == "Bearer dev-key"
        return httpx.Response(
            200,
            json={
                "projects": [
                    {"name": "alpha", "agent_count": 2, "updated_at": "2026-05-28"},
                    {"name": "beta", "agent_count": 0, "updated_at": "2026-05-27"},
                ]
            },
        )

    _route(handler, monkeypatch)
    result = runner.invoke(app, ["project", "list"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "alpha" in result.stdout
    assert "beta" in result.stdout


@pytest.mark.unit
def test_project_list_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--json` emits a parseable array and suppresses the human chatter."""
    _setup_env(tmp_path, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"projects": [{"name": "alpha", "agent_count": 1}]},
        )

    _route(handler, monkeypatch)
    result = runner.invoke(app, ["project", "list", "--json"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    # Stdout is parseable as JSON…
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert data[0]["name"] == "alpha"
    # …and the pre-call remote-context echo is suppressed on stderr
    # (it would normally start with "→ dev https://…"). Whatever else
    # may land there (warnings), the dim echo line is gone.
    assert "→ dev" not in result.stderr
    assert "→ list projects" not in result.stderr


@pytest.mark.unit
def test_project_list_output_flag_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`-o json` is the canonical alias (parity with `runs show`)."""
    _setup_env(tmp_path, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"projects": []})

    _route(handler, monkeypatch)
    result = runner.invoke(app, ["project", "list", "-o", "json"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == []


# ---------------------------------------------------------------------------
# `mdk project show <name>` — read with --json parity
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_project_show_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_env(tmp_path, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/projects/alpha"
        return httpx.Response(
            200,
            json={"name": "alpha", "agent_count": 2, "tags": ["pricing"]},
        )

    _route(handler, monkeypatch)
    result = runner.invoke(app, ["project", "show", "alpha", "--json"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["name"] == "alpha"
    assert payload["tags"] == ["pricing"]


# ---------------------------------------------------------------------------
# `mdk project add-agent` — write with next-step hint
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_project_add_agent_from_catalog_next_step_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    seen_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/projects/myproj/agents"
        assert request.method == "POST"
        seen_body.update(json.loads(request.content))
        return httpx.Response(201, json={"agent_id": "ag-1"})

    _route(handler, monkeypatch)
    result = runner.invoke(
        app,
        ["project", "add-agent", "myproj", "billing-faq", "--from-catalog", "faq-bot"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Body went through correctly.
    assert seen_body["name"] == "billing-faq"
    assert seen_body["source"] == "catalog"
    assert seen_body["catalog_slug"] == "faq-bot"
    # Next-step hint is on stderr (parity with `mdk init` / `mdk add`).
    assert "Next steps" in result.stderr
    assert "mdk project show myproj" in result.stderr
    assert "mdk run billing-faq" in result.stderr


@pytest.mark.unit
def test_project_add_agent_from_llm_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_env(tmp_path, monkeypatch)
    seen_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_body.update(json.loads(request.content))
        return httpx.Response(201, json={"agent_id": "ag-2"})

    _route(handler, monkeypatch)
    result = runner.invoke(
        app,
        [
            "project",
            "add-agent",
            "myproj",
            "triager",
            "--from-llm",
            "ticket triage by priority",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert seen_body["source"] == "llm"
    assert seen_body["description"] == "ticket triage by priority"


@pytest.mark.unit
def test_project_add_agent_mutex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--from-catalog` and `--from-llm` are mutually exclusive."""
    _setup_env(tmp_path, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach the runtime")

    _route(handler, monkeypatch)
    result = runner.invoke(
        app,
        [
            "project",
            "add-agent",
            "myproj",
            "x",
            "--from-catalog",
            "a",
            "--from-llm",
            "b",
        ],
    )
    assert result.exit_code != 0
    # Either both or neither — the error message names both flags.
    assert "from-catalog" in result.stderr
    assert "from-llm" in result.stderr


# ---------------------------------------------------------------------------
# --target plumbing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_target_routes_to_non_active(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--target prod` overrides the active (`dev`) target."""
    _setup_env(tmp_path, monkeypatch)
    seen_hosts: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.add(request.url.host)
        return httpx.Response(200, json={"projects": []})

    _route(handler, monkeypatch)
    result = runner.invoke(app, ["project", "list", "--target", "prod", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert seen_hosts == {"prod.example.com"}


@pytest.mark.unit
def test_target_falls_back_to_active_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `--target` → use the active config target (`dev`)."""
    _setup_env(tmp_path, monkeypatch)
    seen_hosts: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.add(request.url.host)
        return httpx.Response(200, json={"projects": []})

    _route(handler, monkeypatch)
    result = runner.invoke(app, ["project", "list", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert seen_hosts == {"dev.example.com"}


@pytest.mark.unit
def test_target_global_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`MDK_TARGET` env var supplies the default when no `--target` flag."""
    _setup_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MDK_TARGET", "prod")
    seen_hosts: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.add(request.url.host)
        return httpx.Response(200, json={"projects": []})

    _route(handler, monkeypatch)
    result = runner.invoke(app, ["project", "list", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert seen_hosts == {"prod.example.com"}


# ---------------------------------------------------------------------------
# echo_remote_context — present by default, suppressed under --json
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_echo_remote_context_present_in_table_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"projects": []})

    _route(handler, monkeypatch)
    result = runner.invoke(app, ["project", "list"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    # The remote-context echo lives on stderr in human-readable mode.
    assert "dev" in result.stderr  # target name surfaces
    assert "dev.example.com" in result.stderr  # url surfaces
