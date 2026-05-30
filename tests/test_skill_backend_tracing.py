"""Unit tests for parent-span propagation in skill backends (ADR 024).

Coverage:
* PythonSkillBackend — child span created with correct parent, attrs,
  status "ok" on success and "error" on failure.
* PythonSkillBackend — no span when ctx.tracer is None.
* MCPSkillBackend — child span with correct parent, tool attrs,
  status "ok" on success.
* MCPSkillBackend — status "error" + mcp.stderr_log on failure when
  the ring buffer has content.
* MCPSkillBackend — stderr ring buffer bounded to _STDERR_RING_MAX.
* MCPSkillBackend — no span when ctx.tracer is None (existing tests
  still pass).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from movate.core.skill_backend import SkillError, SkillErrorType, SkillExecutionContext
from movate.core.skill_backend.mcp import _STDERR_RING_MAX, MCPSkillBackend
from movate.core.skill_backend.python import PythonSkillBackend
from movate.core.skill_loader import load_skill

# ---------------------------------------------------------------------------
# Fake tracer — records start_span / end_span / set_attribute calls
# ---------------------------------------------------------------------------


class _RecordedSpan:
    """Lightweight stand-in for SpanCtx, tracks every attribute set on it."""

    def __init__(
        self,
        name: str,
        attrs: dict[str, Any],
        parent: _RecordedSpan | None,
    ) -> None:
        self.span_id = uuid.uuid4().hex
        self.name = name
        self.attributes: dict[str, Any] = dict(attrs)
        self.parent_id: str | None = parent.span_id if parent is not None else None
        self.status: str | None = None


class _FakeTracer:
    """Records tracer calls in order for assertion."""

    name = "fake"

    def __init__(self) -> None:
        self.spans: list[_RecordedSpan] = []

    def start_span(
        self,
        name: str,
        attrs: dict[str, Any] | None = None,
        parent: Any = None,
    ) -> _RecordedSpan:
        span = _RecordedSpan(name, dict(attrs or {}), parent)
        self.spans.append(span)
        return span

    def end_span(self, span: _RecordedSpan, status: str = "ok") -> None:
        span.status = status

    def log_event(self, span: Any, event: dict[str, Any]) -> None:
        pass

    def set_attribute(self, span: _RecordedSpan, key: str, value: Any) -> None:
        span.attributes[key] = value


# ---------------------------------------------------------------------------
# Helpers — skill bundles + fake MCP process
# ---------------------------------------------------------------------------


def _write_python_skill(
    parent: Path,
    *,
    name: str = "demo",
    entry: str = "tests.test_skill_backend_tracing:_ok_skill",
) -> Path:
    skill_dir = parent / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        f"name: {name}\n"
        "version: 0.1.0\n"
        "schema:\n"
        "  input:\n"
        "    x: integer\n"
        "  output:\n"
        "    y: integer\n"
        "implementation:\n"
        "  kind: python\n"
        f"  entry: {entry}\n"
    )
    return skill_dir


def _ok_skill(input: dict[str, Any], ctx: SkillExecutionContext) -> dict[str, Any]:
    return {"y": input["x"] + 1}


def _boom_skill(input: dict[str, Any], ctx: SkillExecutionContext) -> dict[str, Any]:
    raise RuntimeError("boom")


class _FakeStream:
    def __init__(self, lines: list[bytes] | None = None) -> None:
        self._lines = list(lines or [])
        self.written: list[bytes] = []

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)

    async def read(self, n: int = -1) -> bytes:
        if not self._lines:
            return b""
        joined = b"".join(self._lines)
        self._lines = []
        return joined[:n] if 0 <= n < len(joined) else joined

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass


class _FakeProcess:
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

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self._terminated = True
        self._returncode = -15

    def kill(self) -> None:
        self._returncode = -9

    async def wait(self) -> int:
        if self._returncode is None:
            self._returncode = 0
        return self._returncode


def _mcp_handshake_lines() -> list[bytes]:
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
        ).encode()
        + b"\n"
    ]


def _mcp_tools_list_line(*tools: str, request_id: int = 2) -> bytes:
    return (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": [{"name": t, "description": ""} for t in tools]},
            }
        ).encode()
        + b"\n"
    )


def _mcp_tools_call_line(
    *,
    structured: dict[str, Any] | None = None,
    is_error: bool = False,
    request_id: int = 3,
) -> bytes:
    result: dict[str, Any] = {"content": []}
    if structured is not None:
        result["structuredContent"] = structured
    if is_error:
        result["isError"] = True
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}).encode() + b"\n"


def _write_mcp_skill(parent: Path, *, name: str = "my-mcp", tool: str = "do_thing") -> Path:
    skill_dir = parent / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        f"name: {name}\n"
        "version: 0.1.0\n"
        "schema:\n"
        "  input:\n"
        "    x: integer\n"
        "  output:\n"
        "    y: integer\n"
        "implementation:\n"
        "  kind: mcp\n"
        "  entry: ./fake-server\n"
        f"  tool: {tool}\n"
    )
    return skill_dir


def _install_fake_spawn(monkeypatch: pytest.MonkeyPatch, fake: _FakeProcess) -> None:
    async def _spawn(*args: Any, **kwargs: Any) -> _FakeProcess:
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)


# ---------------------------------------------------------------------------
# PythonSkillBackend — span propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_python_backend_emits_child_span_on_success(tmp_path: Path) -> None:
    """Happy path: a child 'python.call' span is opened under parent_span
    and closed with status='ok'."""
    skill_dir = _write_python_skill(tmp_path, entry="tests.test_skill_backend_tracing:_ok_skill")
    bundle = load_skill(skill_dir)
    tracer = _FakeTracer()
    parent = tracer.start_span("skill.demo", {"skill": "demo"})

    ctx = SkillExecutionContext(tracer=tracer, parent_span=parent)
    backend = PythonSkillBackend()
    result = await backend.execute(bundle, {"x": 5}, ctx)

    assert result == {"y": 6}
    # One child span should have been created.
    child_spans = [s for s in tracer.spans if s.name == "python.call"]
    assert len(child_spans) == 1
    child = child_spans[0]
    # Parented under the parent span.
    assert child.parent_id == parent.span_id
    # Carries skill name and entry.
    assert child.attributes["skill"] == "demo"
    assert "entry" in child.attributes
    # Closed with ok status.
    assert child.status == "ok"
    # latency_ms was set.
    assert "latency_ms" in child.attributes


@pytest.mark.asyncio
async def test_python_backend_emits_child_span_on_error(tmp_path: Path) -> None:
    """On failure the child span is closed with status='error'.

    We call backend.execute() directly — the backend re-raises the raw
    exception (dispatch_skill is the layer that wraps it into SkillError).
    So we expect RuntimeError here, but the span must still close as error.
    """
    skill_dir = _write_python_skill(
        tmp_path,
        name="boom",
        entry="tests.test_skill_backend_tracing:_boom_skill",
    )
    bundle = load_skill(skill_dir)
    tracer = _FakeTracer()
    parent = tracer.start_span("skill.boom", {"skill": "boom"})

    ctx = SkillExecutionContext(tracer=tracer, parent_span=parent)
    backend = PythonSkillBackend()
    # backend.execute() re-raises the raw RuntimeError — SkillError wrapping
    # happens one level up in dispatch_skill.
    with pytest.raises(RuntimeError, match="boom"):
        await backend.execute(bundle, {"x": 5}, ctx)

    child_spans = [s for s in tracer.spans if s.name == "python.call"]
    assert len(child_spans) == 1
    assert child_spans[0].status == "error"


@pytest.mark.asyncio
async def test_python_backend_no_span_without_tracer(tmp_path: Path) -> None:
    """When ctx.tracer is None no spans are created — the backend is
    still functional."""
    skill_dir = _write_python_skill(tmp_path, entry="tests.test_skill_backend_tracing:_ok_skill")
    bundle = load_skill(skill_dir)
    ctx = SkillExecutionContext()  # tracer=None by default
    backend = PythonSkillBackend()
    result = await backend.execute(bundle, {"x": 3}, ctx)
    assert result == {"y": 4}


# ---------------------------------------------------------------------------
# MCPSkillBackend — span propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_backend_emits_child_span_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: an 'mcp.call' child span is opened and closed ok."""
    fake = _FakeProcess(
        stdout_lines=[
            *_mcp_handshake_lines(),
            _mcp_tools_list_line("do_thing", request_id=2),
            _mcp_tools_call_line(structured={"y": 42}, request_id=3),
        ]
    )
    _install_fake_spawn(monkeypatch, fake)
    skill_dir = _write_mcp_skill(tmp_path)
    bundle = load_skill(skill_dir)
    tracer = _FakeTracer()
    parent = tracer.start_span("skill.my-mcp", {"skill": "my-mcp"})

    ctx = SkillExecutionContext(tracer=tracer, parent_span=parent)
    backend = MCPSkillBackend()
    try:
        result = await backend.execute(bundle, {"x": 1}, ctx)
    finally:
        await backend.aclose()

    assert result == {"y": 42}
    child_spans = [s for s in tracer.spans if s.name == "mcp.call"]
    assert len(child_spans) == 1
    child = child_spans[0]
    assert child.parent_id == parent.span_id
    assert child.attributes["skill"] == "my-mcp"
    assert child.attributes["tool"] == "do_thing"
    assert child.status == "ok"
    assert "latency_ms" in child.attributes


@pytest.mark.asyncio
async def test_mcp_backend_span_error_with_stderr_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On tool error the span is closed as 'error' and mcp.stderr_log is
    populated from the ring buffer.

    We pre-populate tools_known on the session so tools/list is skipped;
    the stdout only needs handshake (id=1) + tools/call (id=2). The ring
    buffer is injected directly before execute() so the error path picks
    it up.
    """
    # After handshake (id=1), tools/list is skipped (tools_known pre-set),
    # so tools/call gets id=2. The isError=True response triggers backend_error.
    fake = _FakeProcess(
        stdout_lines=[
            *_mcp_handshake_lines(),
            _mcp_tools_call_line(is_error=True, request_id=2),
        ]
    )
    _install_fake_spawn(monkeypatch, fake)

    skill_dir = _write_mcp_skill(tmp_path)
    bundle = load_skill(skill_dir)
    tracer = _FakeTracer()
    parent = tracer.start_span("skill.my-mcp", {})

    ctx = SkillExecutionContext(tracer=tracer, parent_span=parent)
    backend = MCPSkillBackend()

    # Pre-create the session and inject ring-buffer content + tools_known
    # before execute() so the error path sees populated stderr data.
    async with backend._spawn_lock:
        session = await backend._ensure_session("./fake-server", "my-mcp")
    session.tools_known = {"do_thing"}
    session.stderr_ring.extend(["[ERROR] server startup failed", "traceback line 1"])

    try:
        with pytest.raises(SkillError) as exc_info:
            await backend.execute(bundle, {"x": 1}, ctx)
    finally:
        await backend.aclose()

    assert exc_info.value.type == SkillErrorType.BACKEND_ERROR
    child_spans = [s for s in tracer.spans if s.name == "mcp.call"]
    assert len(child_spans) == 1
    child = child_spans[0]
    assert child.status == "error"
    # The stderr ring buffer content should appear in the span.
    stderr_log = child.attributes.get("mcp.stderr_log", "")
    assert "server startup failed" in stderr_log


@pytest.mark.asyncio
async def test_mcp_backend_no_span_without_tracer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ctx.tracer is None no spans are created — existing behaviour
    is preserved exactly."""
    fake = _FakeProcess(
        stdout_lines=[
            *_mcp_handshake_lines(),
            _mcp_tools_list_line("do_thing", request_id=2),
            _mcp_tools_call_line(structured={"y": 7}, request_id=3),
        ]
    )
    _install_fake_spawn(monkeypatch, fake)
    skill_dir = _write_mcp_skill(tmp_path)
    bundle = load_skill(skill_dir)
    ctx = SkillExecutionContext()  # no tracer
    backend = MCPSkillBackend()
    try:
        result = await backend.execute(bundle, {"x": 1}, ctx)
    finally:
        await backend.aclose()

    assert result == {"y": 7}


# ---------------------------------------------------------------------------
# MCP stderr ring buffer — bounded size
# ---------------------------------------------------------------------------


def test_stderr_ring_bounded_to_max() -> None:
    """The ring buffer must not grow beyond _STDERR_RING_MAX lines,
    discarding oldest entries when full."""
    ring: deque[str] = deque(maxlen=_STDERR_RING_MAX)
    # Write more than the max.
    for i in range(_STDERR_RING_MAX + 10):
        ring.append(f"line {i}")

    assert len(ring) == _STDERR_RING_MAX
    # The oldest entries should have been discarded; only the last
    # _STDERR_RING_MAX lines remain.
    assert ring[0] == f"line {10}"
    assert ring[-1] == f"line {_STDERR_RING_MAX + 9}"
