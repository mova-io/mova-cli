"""``mdk catalog`` — comprehensive polish layer over the per-endpoint PRs.

Same coverage shape as :mod:`tests.test_cli_project_polish` — the
two parity-layer subapps follow the same UX rules (``--json`` on
reads, next-step hints on writes, ``--target`` plumbing, suppressed
remote-context echo under ``--json``).

Mocks the runtime via ``httpx.MockTransport`` so the tests don't
need the per-endpoint PRs' server routes present.
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
    cfg_dir = home / ".movate"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(
        "active: dev\n"
        "targets:\n"
        "  dev:\n"
        "    url: https://dev.example.com\n"
        "    key_env: MDK_DEV_KEY\n"
    )


def _setup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path)
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / ".movate" / "config.yaml"))
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / "nocreds"))
    monkeypatch.setenv("MDK_DEV_KEY", "dev-key")


def _route(handler: object, monkeypatch: pytest.MonkeyPatch) -> None:
    real = httpx.AsyncClient
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport  # type: ignore[assignment]
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("httpx.AsyncClient", factory)


# ---------------------------------------------------------------------------
# `mdk catalog list` — read with --json parity + tag filter
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_catalog_list_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_env(tmp_path, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/catalog"
        return httpx.Response(
            200,
            json={
                "entries": [
                    {
                        "slug": "faq-bot",
                        "description": "FAQ over a docs site",
                        "tags": ["rag", "faq"],
                        "version": "1.2",
                    },
                    {
                        "slug": "triage-bot",
                        "description": "Ticket triage",
                        "tags": ["classifier"],
                        "version": "0.4",
                    },
                ]
            },
        )

    _route(handler, monkeypatch)
    result = runner.invoke(app, ["catalog", "list"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "faq-bot" in result.stdout
    assert "triage-bot" in result.stdout
    # Cross-reference hint surfaces in the human render so operators
    # discover `mdk project add-agent --from-catalog`.
    combined = result.stdout + result.stderr
    assert "mdk project add-agent" in combined
    assert "--from-catalog" in combined


@pytest.mark.unit
def test_catalog_list_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_env(tmp_path, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"entries": [{"slug": "faq-bot", "tags": ["rag"]}]},
        )

    _route(handler, monkeypatch)
    result = runner.invoke(app, ["catalog", "list", "--json"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert data[0]["slug"] == "faq-bot"
    # --json suppresses the remote-context echo line.
    assert "→ dev" not in result.stderr


@pytest.mark.unit
def test_catalog_list_tag_filter_query_param(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--tag rag` adds `?tag=rag` to the URL."""
    _setup_env(tmp_path, monkeypatch)
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={"entries": []})

    _route(handler, monkeypatch)
    result = runner.invoke(app, ["catalog", "list", "--tag", "rag", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert any("tag=rag" in u for u in seen_urls)


# ---------------------------------------------------------------------------
# `mdk catalog show <slug>` — read with --json parity
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_catalog_show_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_env(tmp_path, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/catalog/faq-bot"
        return httpx.Response(200, json={"slug": "faq-bot", "tags": ["rag"], "version": "1.2"})

    _route(handler, monkeypatch)
    result = runner.invoke(app, ["catalog", "show", "faq-bot", "--json"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["slug"] == "faq-bot"
    assert payload["version"] == "1.2"


# ---------------------------------------------------------------------------
# `mdk catalog publish` — write with next-step hint
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_catalog_publish_next_step_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_env(tmp_path, monkeypatch)
    seen_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/catalog"
        assert request.method == "POST"
        seen_body.update(json.loads(request.content))
        return httpx.Response(201, json={"slug": "faq-bot"})

    _route(handler, monkeypatch)
    result = runner.invoke(
        app,
        [
            "catalog",
            "publish",
            "my-faq-bot",
            "--slug",
            "faq-bot",
            "--description",
            "FAQ over docs",
            "--tag",
            "rag",
            "--tag",
            "faq",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Body assembled correctly (tags as a list).
    assert seen_body["agent"] == "my-faq-bot"
    assert seen_body["slug"] == "faq-bot"
    assert seen_body["tags"] == ["rag", "faq"]
    # Next-step hint surfaces both the show + project-add follow-ups.
    assert "Next steps" in result.stderr
    assert "mdk catalog show faq-bot" in result.stderr
    assert "mdk project add-agent" in result.stderr
    assert "--from-catalog faq-bot" in result.stderr


# ---------------------------------------------------------------------------
# --target plumbing parity (same shape as project tests)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_catalog_target_global_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`MDK_TARGET` env var supplies the default when no `--target` flag."""
    # Add a second target so we can verify routing landed on the right one.
    _write_config(tmp_path)
    cfg_path = tmp_path / ".movate" / "config.yaml"
    cfg_path.write_text(
        cfg_path.read_text()
        + "  prod:\n    url: https://prod.example.com\n    key_env: MDK_PROD_KEY\n"
    )
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / "nocreds"))
    monkeypatch.setenv("MDK_DEV_KEY", "dev-key")
    monkeypatch.setenv("MDK_PROD_KEY", "prod-key")
    monkeypatch.setenv("MDK_TARGET", "prod")
    seen_hosts: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.add(request.url.host)
        return httpx.Response(200, json={"entries": []})

    _route(handler, monkeypatch)
    result = runner.invoke(app, ["catalog", "list", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert seen_hosts == {"prod.example.com"}
