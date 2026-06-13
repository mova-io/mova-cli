"""Integration: ``load_agent`` wires ADR 101 MCP discovery into the skill list.

Proves the end-to-end path — agent.yaml / project.yaml ``mcp_servers:`` →
discovery → ``AgentBundle.skills`` — using a stubbed MCP backend so no real
subprocess/network is touched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import movate.core.skill_backend.mcp as mcp_mod
from movate.core.config import AgentDefaults
from movate.core.loader import AgentLoadError, load_agent

_NO_DEFAULTS = AgentDefaults()


class _FakeBackend:
    def __init__(self, tools_by_server: dict[str, list[dict[str, Any]]]) -> None:
        self._tools = tools_by_server

    async def discover_tools(self, entry: str, server_name: str) -> list[dict[str, Any]]:
        return self._tools.get(server_name, [])

    async def aclose(self) -> None:
        pass


def _install(monkeypatch: pytest.MonkeyPatch, tools: dict[str, list[dict[str, Any]]]) -> None:
    monkeypatch.setattr(mcp_mod, "MCPSkillBackend", lambda: _FakeBackend(tools))


def _tool(name: str) -> dict[str, Any]:
    return {"name": name, "description": f"{name} tool", "inputSchema": {"type": "object"}}


def _write_agent(agent_dir: Path, *, extra: str = "") -> Path:
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: mcp-agent\n"
        "version: 0.1.0\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        "schema:\n"
        "  input:\n"
        "    message: string\n"
        "  output:\n"
        "    response: string\n"
        f"{extra}"
    )
    (agent_dir / "prompt.md").write_text("p\n\n{{ input.message }}")
    return agent_dir


def test_agent_mcp_servers_discovered_into_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, {"github": [_tool("search_repositories"), _tool("get_file")]})
    agent_dir = _write_agent(
        tmp_path / "a",
        extra=("mcp_servers:\n  - name: github\n    entry: npx -y srv\n"),
    )

    bundle = load_agent(agent_dir, defaults=_NO_DEFAULTS)
    names = sorted(s.spec.name for s in bundle.skills)
    assert names == ["github-get-file", "github-search-repositories"]
    # Verbatim wire name preserved for dispatch.
    tools = {s.spec.name: s.spec.implementation.tool for s in bundle.skills}
    assert tools["github-search-repositories"] == "search_repositories"


def test_no_mcp_servers_means_no_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # If discovery were invoked it would explode (backend factory raises).
    def _boom() -> object:
        raise AssertionError("discovery must not run when no servers declared")

    monkeypatch.setattr(mcp_mod, "MCPSkillBackend", _boom)
    agent_dir = _write_agent(tmp_path / "a")
    bundle = load_agent(agent_dir, defaults=_NO_DEFAULTS)
    assert bundle.skills == []


def test_project_mcp_servers_merged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"shared": [_tool("lookup")], "github": [_tool("search")]})
    # project.yaml at the project root declares a shared server.
    (tmp_path / "project.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Project\n"
        "name: proj\n"
        "mcp_servers:\n  - name: shared\n    entry: npx shared\n"
    )
    # agent under <root>/agents/<name> declares its own server.
    agent_dir = _write_agent(
        tmp_path / "agents" / "a",
        extra=("mcp_servers:\n  - name: github\n    entry: npx gh\n"),
    )

    bundle = load_agent(agent_dir)
    names = sorted(s.spec.name for s in bundle.skills)
    assert names == ["github-search", "shared-lookup"]


def test_required_server_failure_fails_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Raising:
        async def discover_tools(self, entry: str, server_name: str) -> list[dict[str, Any]]:
            raise RuntimeError("unreachable")

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(mcp_mod, "MCPSkillBackend", _Raising)
    agent_dir = _write_agent(
        tmp_path / "a",
        extra=("mcp_servers:\n  - name: github\n    entry: npx gh\n    required: true\n"),
    )

    with pytest.raises(AgentLoadError, match="mcp server discovery failed"):
        load_agent(agent_dir, defaults=_NO_DEFAULTS)
