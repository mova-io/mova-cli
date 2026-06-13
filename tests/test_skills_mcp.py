"""Tests for the MCP skill backend (Skills PR 3 — ADR 002 PR 3 of N).

The backend talks to subprocesses over JSON-RPC stdio. Tests run
without spawning real subprocesses by substituting a ``_FakeProcess``
that exposes the same ``stdin/stdout/stderr/wait/returncode/terminate/
kill`` surface as ``asyncio.subprocess.Process``. Each test scripts
the bytes the "server" emits on stdout and asserts on the bytes the
backend writes to stdin.

Coverage map:

* JSON-RPC line framing (helpers ``_write_message`` / ``_read_response``)
* Subprocess spawn + handshake (initialize → notifications/initialized)
* tools/list cache + unknown-tool error
* Happy path: tools/call → structuredContent
* Happy path: tools/call → content[0].text JSON parsing fallback
* Server-side ``isError`` → backend_error with server's message
* JSON-RPC error envelope → backend_error
* Subprocess EOF mid-call → backend_error with stderr tail
* Subprocess fails to start → backend_error
* Bad entry (empty / unparseable shlex) → backend_error
* Non-dict tool result → validation_failed
* Subprocess reuse across calls (no respawn)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from movate.core.skill_backend import SkillError, SkillErrorType, SkillExecutionContext
from movate.core.skill_backend.mcp import (
    MCPSkillBackend,
    _extract_text,
)
from movate.core.skill_loader import load_skill

# ---------------------------------------------------------------------------
# Helpers — fake subprocess, fake skill bundle
# ---------------------------------------------------------------------------


class _FakeStream:
    """Stand-in for asyncio.StreamReader / StreamWriter.

    Reader path: scripted lines that ``readline()`` yields in order.
    Once exhausted, returns ``b""`` (EOF — what a closed pipe looks like).

    Writer path: captures every ``write()`` so tests can assert on
    what the backend sent. ``drain()`` is a no-op.
    """

    def __init__(self, scripted_lines: list[bytes] | None = None) -> None:
        self._lines = list(scripted_lines or [])
        self.written: list[bytes] = []

    # StreamReader-ish
    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)

    async def read(self, n: int = -1) -> bytes:
        """Used by ``_read_stderr_tail``. Return whatever's left, bounded by n."""
        if not self._lines:
            return b""
        joined = b"".join(self._lines)
        self._lines = []
        if n < 0 or n >= len(joined):
            return joined
        return joined[:n]

    # StreamWriter-ish
    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass


class _FakeProcess:
    """Minimal stand-in for asyncio.subprocess.Process.

    Tests pre-script the server's stdout (and optionally stderr) by
    constructing this with the bytes the "server" should emit. The
    backend's writes to stdin go into ``stdin.written``.
    """

    def __init__(
        self,
        *,
        stdout_lines: list[bytes] | None = None,
        stderr_lines: list[bytes] | None = None,
        returncode: int | None = None,
    ) -> None:
        self.stdin = _FakeStream()
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream(stderr_lines)
        self._returncode = returncode
        self._terminated = False
        self._killed = False

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self._terminated = True
        self._returncode = -15  # SIGTERM

    def kill(self) -> None:
        self._killed = True
        self._returncode = -9

    async def wait(self) -> int:
        if self._returncode is None:
            self._returncode = 0
        return self._returncode


def _handshake_lines() -> list[bytes]:
    """Stdout bytes for a successful MCP handshake: initialize result
    for id=1. The notifications/initialized step is fire-and-forget;
    no reply line needed."""
    return [
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "serverInfo": {"name": "test-server", "version": "0.0.1"},
                },
            }
        ).encode("utf-8")
        + b"\n",
    ]


def _tools_list_line(*tools: str, request_id: int = 2) -> bytes:
    """A tools/list response line declaring the named tools."""
    return (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": [{"name": t, "description": f"the {t} tool"} for t in tools]},
            }
        ).encode("utf-8")
        + b"\n"
    )


def _tools_call_line(
    *,
    structured: dict[str, Any] | None = None,
    text: str | None = None,
    is_error: bool = False,
    request_id: int = 3,
) -> bytes:
    """A tools/call response line. Either ``structured`` (modern path)
    or ``text`` (legacy content-block path), or both with structured
    winning."""
    result: dict[str, Any] = {"content": []}
    if text is not None:
        result["content"] = [{"type": "text", "text": text}]
    if structured is not None:
        result["structuredContent"] = structured
    if is_error:
        result["isError"] = True
    return (
        json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}).encode("utf-8") + b"\n"
    )


def _write_mcp_skill(
    parent: Path,
    *,
    name: str = "github-issue",
    entry: str = "./mcp-servers/github",
    tool: str = "get_issue",
    input_schema: str = "{repo: string, issue_number: integer}",
    output_schema: str = "{title: string, body: string}",
) -> Path:
    """Synth a mcp-kind skill on disk."""
    skill_dir = parent / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        f"name: {name}\n"
        "version: 0.1.0\n"
        "schema:\n"
        f"  input: {input_schema}\n"
        f"  output: {output_schema}\n"
        "implementation:\n"
        "  kind: mcp\n"
        f"  entry: {entry}\n"
        f"  tool: {tool}\n"
    )
    return skill_dir


def _ctx() -> SkillExecutionContext:
    return SkillExecutionContext(call_ms_budget=30_000)


def _install_fake_spawn(
    monkeypatch: pytest.MonkeyPatch, fake: _FakeProcess
) -> list[tuple[Any, ...]]:
    """Patch ``asyncio.create_subprocess_exec`` to yield ``fake`` and
    record the argv it was called with. Returns the list of recorded
    invocations (tests can assert on the command argv)."""
    invocations: list[tuple[Any, ...]] = []

    async def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProcess:
        invocations.append(args)
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    return invocations


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_extract_text_pulls_first_text_block() -> None:
    content = [
        {"type": "image", "data": "..."},
        {"type": "text", "text": "hello"},
        {"type": "text", "text": "ignored second"},
    ]
    assert _extract_text(content) == "hello"


def test_extract_text_returns_none_when_no_text_block() -> None:
    assert _extract_text([{"type": "image", "data": "..."}]) is None
    assert _extract_text([]) is None
    assert _extract_text(None) is None
    assert _extract_text("not a list") is None


# ---------------------------------------------------------------------------
# Spawn + handshake
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_runs_handshake_and_caches_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First call spawns + initializes; second call reuses the process."""
    fake = _FakeProcess(
        stdout_lines=[
            *_handshake_lines(),
            _tools_list_line("get_issue", request_id=2),
            _tools_call_line(structured={"title": "T", "body": "B"}, request_id=3),
            # Second invocation: tools/call again (no re-handshake / re-list)
            _tools_call_line(structured={"title": "T2", "body": "B2"}, request_id=4),
        ]
    )
    invocations = _install_fake_spawn(monkeypatch, fake)
    skill_dir = _write_mcp_skill(tmp_path, entry="./test-server")
    bundle = load_skill(skill_dir)
    backend = MCPSkillBackend()
    try:
        result1 = await backend.execute(bundle, {"repo": "x", "issue_number": 1}, _ctx())
        result2 = await backend.execute(bundle, {"repo": "y", "issue_number": 2}, _ctx())
    finally:
        await backend.aclose()
    assert result1 == {"title": "T", "body": "B"}
    assert result2 == {"title": "T2", "body": "B2"}
    # create_subprocess_exec was called exactly once → process reused.
    assert len(invocations) == 1


@pytest.mark.asyncio
async def test_spawn_failure_surfaces_backend_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binary that doesn't exist → FileNotFoundError → backend_error."""

    async def boom(*args: Any, **kwargs: Any) -> _FakeProcess:
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    skill_dir = _write_mcp_skill(tmp_path, entry="./missing-binary")
    bundle = load_skill(skill_dir)
    backend = MCPSkillBackend()
    try:
        with pytest.raises(SkillError) as info:
            await backend.execute(bundle, {"repo": "x", "issue_number": 1}, _ctx())
    finally:
        await backend.aclose()
    assert info.value.type == SkillErrorType.BACKEND_ERROR
    assert "missing-binary" in info.value.message


@pytest.mark.asyncio
async def test_handshake_missing_protocol_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Server's initialize response is missing protocolVersion → fail loud."""
    fake = _FakeProcess(
        stdout_lines=[
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}).encode("utf-8")
            + b"\n",
        ]
    )
    _install_fake_spawn(monkeypatch, fake)
    skill_dir = _write_mcp_skill(tmp_path)
    bundle = load_skill(skill_dir)
    backend = MCPSkillBackend()
    try:
        with pytest.raises(SkillError, match="protocolVersion"):
            await backend.execute(bundle, {"repo": "x", "issue_number": 1}, _ctx())
    finally:
        await backend.aclose()


# ---------------------------------------------------------------------------
# Tool resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_tool_lists_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skill points at a tool the server doesn't expose. Error message
    surfaces the available list so the operator knows what to fix."""
    fake = _FakeProcess(
        stdout_lines=[
            *_handshake_lines(),
            _tools_list_line("list_issues", "search_repos", request_id=2),
        ]
    )
    _install_fake_spawn(monkeypatch, fake)
    skill_dir = _write_mcp_skill(tmp_path, tool="nonexistent")
    bundle = load_skill(skill_dir)
    backend = MCPSkillBackend()
    try:
        with pytest.raises(SkillError) as info:
            await backend.execute(bundle, {"repo": "x", "issue_number": 1}, _ctx())
    finally:
        await backend.aclose()
    assert info.value.type == SkillErrorType.BACKEND_ERROR
    assert "nonexistent" in info.value.message
    assert "list_issues" in info.value.message
    assert "search_repos" in info.value.message


# ---------------------------------------------------------------------------
# Happy path: structured vs text content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_call_returns_structured_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Modern servers return structuredContent — used directly as the
    result dict, no parsing needed."""
    fake = _FakeProcess(
        stdout_lines=[
            *_handshake_lines(),
            _tools_list_line("get_issue", request_id=2),
            _tools_call_line(structured={"title": "Bug X", "body": "Repro Y"}, request_id=3),
        ]
    )
    _install_fake_spawn(monkeypatch, fake)
    skill_dir = _write_mcp_skill(tmp_path)
    bundle = load_skill(skill_dir)
    backend = MCPSkillBackend()
    try:
        result = await backend.execute(bundle, {"repo": "owner/r", "issue_number": 42}, _ctx())
    finally:
        await backend.aclose()
    assert result == {"title": "Bug X", "body": "Repro Y"}


@pytest.mark.asyncio
async def test_tools_call_falls_back_to_text_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Older servers return content[0].text as JSON string — we parse
    it as a fallback when structuredContent is absent."""
    fake = _FakeProcess(
        stdout_lines=[
            *_handshake_lines(),
            _tools_list_line("get_issue", request_id=2),
            _tools_call_line(
                text=json.dumps({"title": "Legacy", "body": "Older server"}),
                request_id=3,
            ),
        ]
    )
    _install_fake_spawn(monkeypatch, fake)
    skill_dir = _write_mcp_skill(tmp_path)
    bundle = load_skill(skill_dir)
    backend = MCPSkillBackend()
    try:
        result = await backend.execute(bundle, {"repo": "x", "issue_number": 1}, _ctx())
    finally:
        await backend.aclose()
    assert result == {"title": "Legacy", "body": "Older server"}


@pytest.mark.asyncio
async def test_tools_call_plain_text_is_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A text content block that isn't JSON is wrapped as {"content": text}.

    Most MCP tools (echo, fetch, search, summaries) return prose, not a JSON
    object — wrapping keeps the value usable in the tool-use loop instead of
    erroring (a skill needing a typed object still enforces it via its output
    schema at dispatch_skill)."""
    fake = _FakeProcess(
        stdout_lines=[
            *_handshake_lines(),
            _tools_list_line("get_issue", request_id=2),
            _tools_call_line(text="The sum of 5 and 7 is 12.", request_id=3),
        ]
    )
    _install_fake_spawn(monkeypatch, fake)
    skill_dir = _write_mcp_skill(tmp_path)
    bundle = load_skill(skill_dir)
    backend = MCPSkillBackend()
    try:
        result = await backend.execute(bundle, {"repo": "x", "issue_number": 1}, _ctx())
    finally:
        await backend.aclose()
    assert result == {"content": "The sum of 5 and 7 is 12."}


@pytest.mark.asyncio
async def test_tools_call_text_json_array_is_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Content text that parses as a JSON non-object (array) is wrapped too —
    {"content": [...]} — rather than erroring; the object contract is enforced
    (if needed) by the skill's output schema at dispatch."""
    fake = _FakeProcess(
        stdout_lines=[
            *_handshake_lines(),
            _tools_list_line("get_issue", request_id=2),
            _tools_call_line(text=json.dumps([1, 2, 3]), request_id=3),
        ]
    )
    _install_fake_spawn(monkeypatch, fake)
    skill_dir = _write_mcp_skill(tmp_path)
    bundle = load_skill(skill_dir)
    backend = MCPSkillBackend()
    try:
        result = await backend.execute(bundle, {"repo": "x", "issue_number": 1}, _ctx())
    finally:
        await backend.aclose()
    assert result == {"content": [1, 2, 3]}


# ---------------------------------------------------------------------------
# Server-side errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_is_error_true_surfaces_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP tools can report their own errors via ``isError: true`` +
    a text content block. The LLM should see this as backend_error
    with the server's error message visible for recovery."""
    fake = _FakeProcess(
        stdout_lines=[
            *_handshake_lines(),
            _tools_list_line("get_issue", request_id=2),
            _tools_call_line(
                text="issue 42 not found in repo owner/r",
                is_error=True,
                request_id=3,
            ),
        ]
    )
    _install_fake_spawn(monkeypatch, fake)
    skill_dir = _write_mcp_skill(tmp_path)
    bundle = load_skill(skill_dir)
    backend = MCPSkillBackend()
    try:
        with pytest.raises(SkillError) as info:
            await backend.execute(bundle, {"repo": "owner/r", "issue_number": 42}, _ctx())
    finally:
        await backend.aclose()
    assert info.value.type == SkillErrorType.BACKEND_ERROR
    assert "not found" in info.value.message


@pytest.mark.asyncio
async def test_jsonrpc_error_envelope_surfaces_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Server returns a JSON-RPC error envelope (``error`` key instead
    of ``result``). Surface as backend_error with the server's payload."""
    fake = _FakeProcess(
        stdout_lines=[
            *_handshake_lines(),
            _tools_list_line("get_issue", request_id=2),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "error": {"code": -32601, "message": "method not found"},
                }
            ).encode("utf-8")
            + b"\n",
        ]
    )
    _install_fake_spawn(monkeypatch, fake)
    skill_dir = _write_mcp_skill(tmp_path)
    bundle = load_skill(skill_dir)
    backend = MCPSkillBackend()
    try:
        with pytest.raises(SkillError) as info:
            await backend.execute(bundle, {"repo": "x", "issue_number": 1}, _ctx())
    finally:
        await backend.aclose()
    assert info.value.type == SkillErrorType.BACKEND_ERROR
    assert "method not found" in info.value.message


# ---------------------------------------------------------------------------
# Subprocess EOF / death mid-call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_eof_mid_call_includes_stderr_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Server closes stdout before responding (most common: it crashed).
    The error message includes the stderr tail so operators see WHY."""
    fake = _FakeProcess(
        stdout_lines=[
            *_handshake_lines(),
            _tools_list_line("get_issue", request_id=2),
            # No tools/call response — stdout returns b"" (EOF).
        ],
        stderr_lines=[b"FATAL: out of memory\n"],
        returncode=137,  # OOM-killed
    )
    _install_fake_spawn(monkeypatch, fake)
    skill_dir = _write_mcp_skill(tmp_path)
    bundle = load_skill(skill_dir)
    backend = MCPSkillBackend()
    try:
        with pytest.raises(SkillError) as info:
            await backend.execute(bundle, {"repo": "x", "issue_number": 1}, _ctx())
    finally:
        await backend.aclose()
    assert info.value.type == SkillErrorType.BACKEND_ERROR
    assert "closed stdout" in info.value.message
    assert "out of memory" in info.value.message


# ---------------------------------------------------------------------------
# JSON-RPC notifications + writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backend_sends_initialized_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After initialize succeeds, the backend sends
    notifications/initialized — required by the MCP protocol before
    making tool calls."""
    fake = _FakeProcess(
        stdout_lines=[
            *_handshake_lines(),
            _tools_list_line("get_issue", request_id=2),
            _tools_call_line(structured={"title": "T", "body": "B"}, request_id=3),
        ]
    )
    _install_fake_spawn(monkeypatch, fake)
    skill_dir = _write_mcp_skill(tmp_path)
    bundle = load_skill(skill_dir)
    backend = MCPSkillBackend()
    try:
        await backend.execute(bundle, {"repo": "x", "issue_number": 1}, _ctx())
    finally:
        await backend.aclose()
    # Inspect what got written to stdin.
    writes = [json.loads(w) for w in fake.stdin.written]
    # 1: initialize, 2: notifications/initialized, 3: tools/list, 4: tools/call
    methods = [m.get("method") for m in writes]
    assert methods == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
    # The notification has no id (it's fire-and-forget).
    assert "id" not in writes[1]


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_terminates_running_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """aclose() must terminate every cached subprocess so the executor
    doesn't leak processes when it shuts down."""
    fake = _FakeProcess(
        stdout_lines=[
            *_handshake_lines(),
            _tools_list_line("get_issue", request_id=2),
            _tools_call_line(structured={"title": "T", "body": "B"}, request_id=3),
        ]
    )
    _install_fake_spawn(monkeypatch, fake)
    skill_dir = _write_mcp_skill(tmp_path)
    bundle = load_skill(skill_dir)
    backend = MCPSkillBackend()
    await backend.execute(bundle, {"repo": "x", "issue_number": 1}, _ctx())
    # Process still running.
    assert fake._returncode is None
    await backend.aclose()
    assert fake._terminated is True
    # Backend cache cleared.
    assert backend._sessions == {}


# ---------------------------------------------------------------------------
# Model-level validation (the field validator)
# ---------------------------------------------------------------------------


def test_skill_spec_mcp_omitted_tool_loads_ok(tmp_path: Path) -> None:
    """A kind=mcp skill without a ``tool:`` field now loads successfully
    in multi-tool mode. Previously this was an error; now tool: is
    optional — omitting it means 'import all tools from the server'."""
    skill_dir = tmp_path / "no-tool"
    skill_dir.mkdir()
    (skill_dir / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        "name: no-tool\n"
        "version: 0.1.0\n"
        "schema:\n"
        "  input: {x: string}\n"
        "  output: {y: string}\n"
        "implementation:\n"
        "  kind: mcp\n"
        "  entry: ./server\n"
        # no `tool:` — multi-tool mode
    )
    bundle = load_skill(skill_dir)
    assert bundle.spec.implementation.tool is None


def test_skill_spec_mcp_requires_entry(tmp_path: Path) -> None:
    skill_dir = tmp_path / "no-entry"
    skill_dir.mkdir()
    (skill_dir / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        "name: no-entry\n"
        "version: 0.1.0\n"
        "schema:\n"
        "  input: {x: string}\n"
        "  output: {y: string}\n"
        "implementation:\n"
        "  kind: mcp\n"
        "  entry: ''\n"
        "  tool: foo\n"
    )
    from movate.core.skill_loader import SkillLoadError  # noqa: PLC0415

    with pytest.raises(SkillLoadError, match="entry must be the server"):
        load_skill(skill_dir)


# ---------------------------------------------------------------------------
# Multi-tool mode (tool: omitted)
# ---------------------------------------------------------------------------


def _write_mcp_skill_multi(
    parent: Path,
    *,
    name: str = "github-multi",
    entry: str = "./mcp-servers/github",
    input_schema: str = "{__tool__: string}",
    output_schema: str = "{}",
) -> Path:
    """Synth a multi-tool mcp-kind skill on disk (no tool: field)."""
    skill_dir = parent / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        f"name: {name}\n"
        "version: 0.1.0\n"
        "schema:\n"
        f"  input: {input_schema}\n"
        f"  output: {output_schema}\n"
        "implementation:\n"
        "  kind: mcp\n"
        f"  entry: {entry}\n"
        # no tool: — multi-tool mode
    )
    return skill_dir


def test_skill_spec_mcp_allows_omitted_tool(tmp_path: Path) -> None:
    """A kind=mcp skill without a ``tool:`` field loads successfully
    for multi-tool mode."""
    skill_dir = _write_mcp_skill_multi(tmp_path)
    bundle = load_skill(skill_dir)
    assert bundle.spec.implementation.tool is None


@pytest.mark.asyncio
async def test_multi_tool_mode_passes_dunder_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In multi-tool mode, __tool__ in input selects the tool to call."""
    fake = _FakeProcess(
        stdout_lines=[
            *_handshake_lines(),
            _tools_list_line("create_issue", "list_repos", request_id=2),
            _tools_call_line(structured={"id": 123, "url": "https://..."}, request_id=3),
        ]
    )
    _install_fake_spawn(monkeypatch, fake)
    skill_dir = _write_mcp_skill_multi(tmp_path)
    bundle = load_skill(skill_dir)
    backend = MCPSkillBackend()
    try:
        result = await backend.execute(
            bundle,
            {"__tool__": "create_issue", "title": "Bug", "body": "Repro"},
            _ctx(),
        )
    finally:
        await backend.aclose()
    assert result == {"id": 123, "url": "https://..."}
    # Verify the tools/call request used the right tool name.
    writes = [json.loads(w) for w in fake.stdin.written]
    call_msg = next(m for m in writes if m.get("method") == "tools/call")
    assert call_msg["params"]["name"] == "create_issue"
    # __tool__ should NOT be forwarded as an argument to the server.
    assert "__tool__" not in call_msg["params"]["arguments"]


@pytest.mark.asyncio
async def test_multi_tool_mode_no_tool_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In multi-tool mode, if __tool__ is missing from input → error."""
    fake = _FakeProcess(stdout_lines=[*_handshake_lines()])
    _install_fake_spawn(monkeypatch, fake)
    skill_dir = _write_mcp_skill_multi(tmp_path)
    bundle = load_skill(skill_dir)
    backend = MCPSkillBackend()
    try:
        with pytest.raises(SkillError) as info:
            await backend.execute(bundle, {"title": "Bug"}, _ctx())
    finally:
        await backend.aclose()
    assert info.value.type == SkillErrorType.BACKEND_ERROR
    assert "no tool specified" in info.value.message


# ---------------------------------------------------------------------------
# HTTP/SSE transport
# ---------------------------------------------------------------------------


def test_is_http_entry() -> None:
    from movate.core.skill_backend.mcp import _is_http_entry  # noqa: PLC0415

    assert _is_http_entry("https://mcp.example.com/api") is True
    assert _is_http_entry("http://localhost:8080") is True
    assert _is_http_entry("HTTP://EXAMPLE.COM") is True
    assert _is_http_entry("./mcp-servers/github") is False
    assert _is_http_entry("npx -y @some/pkg") is False


def test_parse_sse_response_extracts_result() -> None:
    from movate.core.skill_backend.mcp import _parse_sse_response  # noqa: PLC0415

    body = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
    result = _parse_sse_response(body, expected_id=1, method="test", skill_name="s")
    assert result == {"ok": True}


def test_parse_sse_response_error_raises() -> None:
    from movate.core.skill_backend.mcp import _parse_sse_response  # noqa: PLC0415

    body = (
        'event: message\ndata: {"jsonrpc":"2.0","id":1,"error":{"code":-32600,"message":"bad"}}\n\n'
    )
    with pytest.raises(SkillError) as info:
        _parse_sse_response(body, expected_id=1, method="test", skill_name="s")
    assert info.value.type == SkillErrorType.BACKEND_ERROR
    assert "bad" in info.value.message


def test_parse_sse_response_no_match_raises() -> None:
    from movate.core.skill_backend.mcp import _parse_sse_response  # noqa: PLC0415

    body = "event: ping\ndata: {}\n\n"
    with pytest.raises(SkillError) as info:
        _parse_sse_response(body, expected_id=1, method="test", skill_name="s")
    assert "no matching" in info.value.message


# ---------------------------------------------------------------------------
# add-mcp CLI helper
# ---------------------------------------------------------------------------


def test_derive_skill_name_from_npm_package() -> None:
    from movate.cli.skills_cmd import _derive_skill_name  # noqa: PLC0415

    assert _derive_skill_name("npx -y @modelcontextprotocol/server-github") == "github"


def test_derive_skill_name_from_url() -> None:
    from movate.cli.skills_cmd import _derive_skill_name  # noqa: PLC0415

    assert _derive_skill_name("https://mcp.slack.com/api") == "slack"


def test_derive_skill_name_from_local_binary() -> None:
    from movate.cli.skills_cmd import _derive_skill_name  # noqa: PLC0415

    assert _derive_skill_name("./mcp-servers/jira-bridge") == "jira-bridge"
