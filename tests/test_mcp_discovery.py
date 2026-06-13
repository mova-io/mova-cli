"""Tests for ADR 101 MCP server discovery.

Discovery logic is isolated from the JSON-RPC plumbing (covered by
``test_skills_mcp.py``) by stubbing ``MCPSkillBackend.discover_tools`` — the
seam discovery depends on. Covers: name sanitization, include/exclude filters,
bundle minting (verbatim wire name preserved), fail-soft vs ``required``,
timeout, name-collision, the agent+project merge, and the sync bridge in both
the no-loop and running-loop cases.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

import movate.core.skill_backend.mcp as mcp_mod
from movate.core import mcp_discovery
from movate.core.mcp_discovery import (
    MCPDiscoveryError,
    _filter_tools,
    _is_mutating,
    _mint_bundle,
    _sanitize_segment,
    discover_mcp_skill_bundles,
    discover_sync,
)
from movate.core.models import MCPServerRef, merge_mcp_servers

# ---------------------------------------------------------------------------
# Fake backend installed at the import seam
# ---------------------------------------------------------------------------


class _FakeBackend:
    """Stands in for MCPSkillBackend; serves canned tools per server name."""

    def __init__(
        self,
        tools_by_server: dict[str, list[dict[str, Any]]] | None = None,
        *,
        raise_for: set[str] | None = None,
        hang_for: set[str] | None = None,
    ) -> None:
        self._tools = tools_by_server or {}
        self._raise_for = raise_for or set()
        self._hang_for = hang_for or set()
        self.closed = False

    async def discover_tools(
        self, entry: str, server_name: str, auth: str | None = None
    ) -> list[dict[str, Any]]:
        if server_name in self._raise_for:
            raise RuntimeError("connection refused")
        if server_name in self._hang_for:
            await asyncio.sleep(5.0)
        return self._tools.get(server_name, [])

    async def aclose(self) -> None:
        self.closed = True


def _install(monkeypatch: pytest.MonkeyPatch, backend: _FakeBackend) -> None:
    """Make ``MCPSkillBackend()`` (called inside discovery) return *backend*."""
    monkeypatch.setattr(mcp_mod, "MCPSkillBackend", lambda: backend)


def _tool(name: str, desc: str = "", schema: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": desc,
        "inputSchema": schema or {"type": "object", "properties": {}},
    }


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("search_repositories", "search-repositories"),
        ("getFileContents", "getfilecontents"),
        ("create.issue", "create-issue"),
        ("a__b--c", "a-b-c"),
        ("  trim_me_  ", "trim-me"),
        ("123starts-with-digit", None),  # segment must start with a letter
        ("___", None),  # reduces to empty
        ("", None),
    ],
)
def test_sanitize_segment(raw: str, expected: str | None) -> None:
    assert _sanitize_segment(raw) == expected


def test_filter_include_only() -> None:
    server = MCPServerRef(name="gh", entry="x", include_tools=["a", "c"])
    tools = [_tool("a"), _tool("b"), _tool("c")]
    assert [t["name"] for t in _filter_tools(tools, server)] == ["a", "c"]


def test_filter_exclude_only() -> None:
    server = MCPServerRef(name="gh", entry="x", exclude_tools=["b"])
    tools = [_tool("a"), _tool("b"), _tool("c")]
    assert [t["name"] for t in _filter_tools(tools, server)] == ["a", "c"]


def test_filter_none_passes_all() -> None:
    server = MCPServerRef(name="gh", entry="x")
    tools = [_tool("a"), _tool("b")]
    assert _filter_tools(tools, server) == tools


def test_mint_bundle_preserves_verbatim_tool_name() -> None:
    server = MCPServerRef(name="github", entry="npx srv", credentials_ref="kv://tok")
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    bundle = _mint_bundle(server, _tool("search_repositories", "Search repos", schema))

    # Identifier is sanitized + server-namespaced...
    assert bundle.spec.name == "github-search-repositories"
    # ...but the wire name sent to tools/call is byte-exact.
    assert bundle.spec.implementation.tool == "search_repositories"
    assert bundle.spec.implementation.entry == "npx srv"
    assert bundle.spec.implementation.kind.value == "mcp"
    assert bundle.spec.description == "Search repos"
    assert bundle.input_schema == schema


def test_merge_agent_overrides_project() -> None:
    proj = [MCPServerRef(name="gh", entry="proj"), MCPServerRef(name="jira", entry="j")]
    agent = [MCPServerRef(name="gh", entry="agent"), MCPServerRef(name="slack", entry="s")]
    merged = merge_mcp_servers(proj, agent)
    assert [m.name for m in merged] == ["gh", "jira", "slack"]
    assert merged[0].entry == "agent"  # agent wins on name collision


def test_mint_bundle_threads_credentials_to_auth() -> None:
    # ADR 101 D3: credentials_ref → implementation.auth so the HTTP transport
    # injects an Authorization header at dispatch.
    server = MCPServerRef(
        name="gh", entry="https://mcp.x/api", credentials_ref="bearer-from-env:GH"
    )
    bundle = _mint_bundle(server, _tool("search"))
    assert bundle.spec.implementation.auth == "bearer-from-env:GH"


def test_mint_bundle_no_credentials_leaves_auth_unset() -> None:
    bundle = _mint_bundle(MCPServerRef(name="gh", entry="npx x"), _tool("search"))
    assert bundle.spec.implementation.auth is None


@pytest.mark.parametrize(
    ("annotations", "expected"),
    [
        (None, False),
        ({}, False),
        ({"readOnlyHint": True}, False),
        ({"readOnlyHint": False}, True),
        ({"destructiveHint": True}, True),
        ({"destructiveHint": False}, False),
        ({"readOnlyHint": True, "destructiveHint": True}, True),
    ],
)
def test_is_mutating_maps_annotations(annotations: dict[str, Any] | None, expected: bool) -> None:
    tool: dict[str, Any] = {"name": "t", "inputSchema": {"type": "object"}}
    if annotations is not None:
        tool["annotations"] = annotations
    assert _is_mutating(tool) is expected


# ---------------------------------------------------------------------------
# Discovery flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBackend({"github": [_tool("search_repositories"), _tool("get_file")]})
    _install(monkeypatch, backend)

    res = await discover_mcp_skill_bundles([MCPServerRef(name="github", entry="x")])

    assert sorted(b.spec.name for b in res.bundles) == [
        "github-get-file",
        "github-search-repositories",
    ]
    assert res.warnings == []
    assert "github" in res.fingerprints
    assert backend.closed is True  # lifecycle closed even on success


@pytest.mark.asyncio
async def test_discover_applies_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBackend({"gh": [_tool("a"), _tool("b"), _tool("c")]})
    _install(monkeypatch, backend)

    res = await discover_mcp_skill_bundles(
        [MCPServerRef(name="gh", entry="x", include_tools=["b"])]
    )
    assert [b.spec.implementation.tool for b in res.bundles] == ["b"]


@pytest.mark.asyncio
async def test_failsoft_on_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBackend({"ok": [_tool("a")]}, raise_for={"down"})
    _install(monkeypatch, backend)

    res = await discover_mcp_skill_bundles(
        [
            MCPServerRef(name="down", entry="x"),  # required defaults False
            MCPServerRef(name="ok", entry="y"),
        ]
    )
    # down is skipped with a warning; ok still discovers.
    assert [b.spec.name for b in res.bundles] == ["ok-a"]
    assert any("down" in w for w in res.warnings)


@pytest.mark.asyncio
async def test_required_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBackend(raise_for={"down"})
    _install(monkeypatch, backend)

    with pytest.raises(MCPDiscoveryError, match="required"):
        await discover_mcp_skill_bundles([MCPServerRef(name="down", entry="x", required=True)])
    assert backend.closed is True  # still cleaned up


@pytest.mark.asyncio
async def test_timeout_is_failsoft(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_discovery, "DISCOVERY_TIMEOUT_S", 0.05)
    backend = _FakeBackend(hang_for={"slow"})
    _install(monkeypatch, backend)

    res = await discover_mcp_skill_bundles([MCPServerRef(name="slow", entry="x")])
    assert res.bundles == []
    assert any("timed out" in w for w in res.warnings)


@pytest.mark.asyncio
async def test_timeout_required_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_discovery, "DISCOVERY_TIMEOUT_S", 0.05)
    backend = _FakeBackend(hang_for={"slow"})
    _install(monkeypatch, backend)

    with pytest.raises(MCPDiscoveryError, match="timed out"):
        await discover_mcp_skill_bundles([MCPServerRef(name="slow", entry="x", required=True)])


@pytest.mark.asyncio
async def test_name_collision_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two servers whose sanitized identifiers collide.
    backend = _FakeBackend({"gh": [_tool("a")]})
    _install(monkeypatch, backend)

    with pytest.raises(MCPDiscoveryError, match="collides"):
        await discover_mcp_skill_bundles(
            [MCPServerRef(name="gh", entry="x")],
            existing_skill_names={"gh-a"},  # already taken by a real skill
        )


@pytest.mark.asyncio
async def test_unsanitizable_tool_skipped_softly(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBackend({"gh": [_tool("123bad"), _tool("good_one")]})
    _install(monkeypatch, backend)

    res = await discover_mcp_skill_bundles([MCPServerRef(name="gh", entry="x")])
    assert [b.spec.name for b in res.bundles] == ["gh-good-one"]
    assert any("123bad" in w for w in res.warnings)


# ---------------------------------------------------------------------------
# HTTP transport auth injection (ADR 101 D3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_spawn_injects_authorization_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["headers"] = kwargs.get("headers")

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    monkeypatch.setenv("GH_TOKEN", "s3cret")

    backend = mcp_mod.MCPSkillBackend()

    # Stub the JSON-RPC handshake so _http_spawn completes without a real server.
    async def _fake_rpc(session: Any, *, method: str, params: Any, skill_name: str) -> Any:
        return {"protocolVersion": "2024-11-05"}

    async def _fake_notify(session: Any, *, method: str, skill_name: str) -> None:
        return None

    monkeypatch.setattr(backend, "_http_rpc_call", _fake_rpc)
    monkeypatch.setattr(backend, "_http_rpc_notify", _fake_notify)

    await backend._ensure_http_session("https://mcp.x/api", "gh", "bearer-from-env:GH_TOKEN")
    assert captured["headers"] == {"Authorization": "Bearer s3cret"}


# ---------------------------------------------------------------------------
# Sync bridge
# ---------------------------------------------------------------------------


def test_discover_sync_no_running_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBackend({"gh": [_tool("a")]})
    _install(monkeypatch, backend)

    res = discover_sync([MCPServerRef(name="gh", entry="x")])
    assert [b.spec.name for b in res.bundles] == ["gh-a"]


def test_discover_sync_empty_is_cheap() -> None:
    res = discover_sync([])
    assert res.bundles == []


@pytest.mark.asyncio
async def test_discover_sync_from_running_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    # Called from inside pytest-asyncio's running loop — must offload to a
    # worker thread rather than blow up on asyncio.run().
    backend = _FakeBackend({"gh": [_tool("a")]})
    _install(monkeypatch, backend)

    res = await asyncio.to_thread(discover_sync, [MCPServerRef(name="gh", entry="x")])
    assert [b.spec.name for b in res.bundles] == ["gh-a"]


@pytest.mark.asyncio
async def test_discover_sync_inside_loop_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    # The hard case: discover_sync invoked WHILE a loop runs on this thread.
    backend = _FakeBackend({"gh": [_tool("a")]})
    _install(monkeypatch, backend)

    # Directly call the sync function from within the async test (running loop).
    res = discover_sync([MCPServerRef(name="gh", entry="x")])
    assert [b.spec.name for b in res.bundles] == ["gh-a"]
