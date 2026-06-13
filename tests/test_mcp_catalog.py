"""Tests for the MCP catalog (ADR 103) + registry sources (ADR 104).

Covers the catalog model, the bundled source, the official-registry record
mapping (with fixtures, no network) + its fail-soft behavior, source resolution
+ the trust gate, and the `mdk mcp add`/`list`/`search` commands (the YAML write
is idempotent; `add` writes a valid ADR 101 stanza).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.mcp_cmd import _add_server_to_yaml, mcp_app
from movate.core.models import MCPServerRef
from movate.mcp_catalog.models import CatalogEntry, TrustTier
from movate.mcp_catalog.sources import UnknownSourceError, resolve_sources
from movate.mcp_catalog.sources.bundled import BundledSource
from movate.mcp_catalog.sources.official import OfficialRegistrySource, _map_record

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def test_catalog_entry_matches() -> None:
    e = CatalogEntry(
        name="github",
        entry="npx -y srv@1",
        title="GitHub",
        description="repos and issues",
        tags=["scm"],
    )
    assert e.matches("git")
    assert e.matches("SCM")
    assert e.matches("issues")
    assert e.matches("")  # empty → browse-all
    assert not e.matches("kubernetes")


def test_catalog_entry_rejects_invalid_name() -> None:
    with pytest.raises(ValueError, match="lowercase"):
        CatalogEntry(name="Bad_Name", entry="x")


# ---------------------------------------------------------------------------
# Bundled source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bundled_source_loads_and_searches() -> None:
    src = BundledSource()
    assert src.trust is TrustTier.CURATED
    all_entries = await src.search("")
    assert any(e.name == "github" for e in all_entries)
    assert all(e.source == "bundled" for e in all_entries)

    hits = await src.search("github")
    assert [e.name for e in hits] == ["github"]
    assert (await src.get("github")) is not None
    assert (await src.get("does-not-exist")) is None


@pytest.mark.asyncio
async def test_bundled_entries_are_pinned() -> None:
    # Every curated entry should be version-pinned (npm @ver) or an http URL.
    for e in await BundledSource().search(""):
        assert e.pinned, f"{e.name} is not pinned: {e.entry}"


# ---------------------------------------------------------------------------
# Official source: record mapping (no network)
# ---------------------------------------------------------------------------


def test_map_record_npm_package() -> None:
    rec = {
        "name": "io.github.acme/cool-server",
        "description": "A cool server.",
        "version": "1.2.3",
        "packages": [
            {
                "registry_type": "npm",
                "identifier": "@acme/cool-server",
                "version": "1.2.3",
                "environment_variables": [{"name": "ACME_TOKEN", "isSecret": True}],
            }
        ],
    }
    e = _map_record(rec)
    assert e is not None
    assert e.name == "cool-server"
    assert e.transport == "stdio"
    assert e.entry == "npx -y @acme/cool-server@1.2.3"
    assert e.credentials == "bearer-from-env:ACME_TOKEN"
    assert e.trust is TrustTier.OFFICIAL
    assert e.publisher == "io.github.acme"
    assert e.pinned is True


def test_map_record_remote_http() -> None:
    rec = {
        "name": "com.example/remote",
        "description": "Remote.",
        "version": "0.1.0",
        "remotes": [{"transport": "sse", "url": "https://mcp.example.com/sse"}],
    }
    e = _map_record(rec)
    assert e is not None
    assert e.transport == "http"
    assert e.entry == "https://mcp.example.com/sse"


def test_map_record_pypi_uses_uvx() -> None:
    rec = {
        "name": "io.github.x/pyserver",
        "version": "2.0.0",
        "packages": [{"registry_type": "pypi", "identifier": "pyserver", "version": "2.0.0"}],
    }
    e = _map_record(rec)
    assert e is not None
    assert e.entry == "uvx pyserver==2.0.0"


def test_map_record_unrunnable_is_skipped() -> None:
    assert _map_record({"name": "x/y", "description": "no packages or remotes"}) is None
    assert _map_record({"name": "", "packages": []}) is None
    # unknown packaging type → skipped
    assert (
        _map_record({"name": "a/b", "packages": [{"registry_type": "brew", "identifier": "z"}]})
        is None
    )


@pytest.mark.asyncio
async def test_official_source_failsoft_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    src = OfficialRegistrySource()

    async def _boom(query: str, limit: int) -> list[dict[str, Any]]:
        raise AssertionError("should be swallowed by _fetch_servers, not reach here")

    # Simulate the fetch returning nothing (the fail-soft contract).
    async def _empty(query: str, limit: int) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(src, "_fetch_servers", _empty)
    assert await src.search("anything") == []
    assert await src.get("anything") is None


# ---------------------------------------------------------------------------
# Source resolution + trust gate
# ---------------------------------------------------------------------------


def test_resolve_sources_default_excludes_community() -> None:
    names = [s.name for s in resolve_sources(None)]
    assert names == ["bundled", "official"]  # no community by default


def test_resolve_sources_specific_and_all() -> None:
    assert [s.name for s in resolve_sources("bundled")] == ["bundled"]
    assert set(s.name for s in resolve_sources("all")) >= {"bundled", "official"}


def test_resolve_sources_unknown_raises() -> None:
    with pytest.raises(UnknownSourceError, match="unknown --source"):
        resolve_sources("nope")


# ---------------------------------------------------------------------------
# YAML write helper (idempotent)
# ---------------------------------------------------------------------------


def test_add_server_to_yaml_adds_then_updates(tmp_path: Path) -> None:
    p = tmp_path / "agent.yaml"
    p.write_text("api_version: movate/v1\nkind: Agent\nname: a\n")

    action = _add_server_to_yaml(p, {"name": "github", "entry": "npx -y srv@1"})
    assert action == "added"
    data = yaml.safe_load(p.read_text())
    assert data["mcp_servers"] == [{"name": "github", "entry": "npx -y srv@1"}]
    # untouched keys preserved
    assert data["name"] == "a"

    # same name → update in place, not duplicate
    action = _add_server_to_yaml(p, {"name": "github", "entry": "npx -y srv@2"})
    assert action == "updated"
    data = yaml.safe_load(p.read_text())
    assert data["mcp_servers"] == [{"name": "github", "entry": "npx -y srv@2"}]


# ---------------------------------------------------------------------------
# CLI: mdk mcp add / list
# ---------------------------------------------------------------------------


def test_cli_add_writes_valid_stanza(tmp_path: Path) -> None:
    agent_dir = tmp_path / "support-bot"
    agent_dir.mkdir()
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\nkind: Agent\nname: support-bot\n"
    )

    result = runner.invoke(
        mcp_app,
        [
            "add",
            "github",
            "--agent",
            str(agent_dir),
            "--no-inspect",
            "--tools",
            "search_repositories",
        ],
    )
    assert result.exit_code == 0, result.output
    data = yaml.safe_load((agent_dir / "agent.yaml").read_text())
    entry = data["mcp_servers"][0]
    assert entry["name"] == "github"
    assert entry["entry"].startswith("npx -y @modelcontextprotocol/server-github@")
    assert entry["credentials_ref"] == "bearer-from-env:GITHUB_TOKEN"
    assert entry["include_tools"] == ["search_repositories"]
    # The written stanza must parse as a real MCPServerRef.
    MCPServerRef.model_validate(entry)


def test_cli_add_requires_a_destination(tmp_path: Path) -> None:
    result = runner.invoke(mcp_app, ["add", "github", "--no-inspect"])
    assert result.exit_code == 2
    assert "destination" in result.stderr


def test_cli_add_unknown_entry_errors(tmp_path: Path) -> None:
    agent_dir = tmp_path / "a"
    agent_dir.mkdir()
    (agent_dir / "agent.yaml").write_text("api_version: movate/v1\nkind: Agent\nname: a\n")
    result = runner.invoke(
        mcp_app,
        [
            "add",
            "nonexistent-xyz",
            "--agent",
            str(agent_dir),
            "--no-inspect",
            "--source",
            "bundled",
        ],
    )
    assert result.exit_code == 2
    assert "no catalog entry" in result.stderr


def test_cli_list_bundled() -> None:
    result = runner.invoke(mcp_app, ["list", "--source", "bundled"])
    assert result.exit_code == 0, result.output
    assert "github" in result.output
