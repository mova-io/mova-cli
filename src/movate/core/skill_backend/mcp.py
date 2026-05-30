"""MCP skill backend — talk to a Model Context Protocol server over stdio.

Third backend per ADR 002. Lets skills invoke tools exposed by an MCP
server (Anthropic's Model Context Protocol). The server can be any
process that speaks MCP — internal tool servers, npx-installed
community servers, customer-hosted bridges to legacy systems.

Why hand-rolled instead of the official ``mcp`` Python SDK?

MCP's wire protocol is JSON-RPC 2.0 over stdio. The subset we need
(initialize → notifications/initialized → tools/call) is ~150 LOC
to implement cleanly. The official SDK pulls in a transitive
dependency tree that's heavy for the slice we use, and would add
another ``[mcp]`` optional-extra to install. A focused implementation
keeps the dep footprint small and gives us tight control over the
error → :class:`SkillError` mapping.

Lifecycle:

* First invocation of a skill referencing a particular ``entry``
  command spawns the subprocess + performs the MCP handshake.
* Subsequent calls reuse the running subprocess (one connection pool
  per unique server command).
* :meth:`MCPSkillBackend.aclose` terminates every subprocess
  gracefully on executor shutdown.

Failure → :class:`SkillError` mapping:

* Subprocess fails to start (binary missing, exits early) → ``backend_error``
* MCP handshake fails (bad protocol version, malformed JSON-RPC) → ``backend_error``
* Tool name doesn't appear in the server's ``tools/list`` → ``backend_error``
* Server returns an error response → ``backend_error`` with the server's message
* Subprocess dies mid-call → ``backend_error``
* Wall-clock timeout (server hangs) → :data:`SkillErrorType.TIMEOUT`
* JSON-RPC response can't be parsed → ``backend_error``
* Tool returns content that isn't a JSON object → ``validation_failed``

Tracing (ADR 024):

When ``ctx.tracer`` is set the backend opens an ``mcp.call`` child span
under ``ctx.parent_span`` and closes it on success/error. The span carries
the ``entry`` command, ``tool`` name, and — on error — the bounded stderr
ring-buffer so operators can see why a server misbehaved.

Scope today (v0.6): stdio transport, single tool per skill (skill
yaml declares ``tool:`` for the specific tool to call). HTTP/SSE
transport and multi-tool batching land in a follow-up if real
customer demand surfaces.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from movate.core.models import SkillImplementationKind
from movate.core.skill_backend.base import (
    SkillError,
    SkillErrorType,
    SkillExecutionContext,
    register_backend,
)

if TYPE_CHECKING:
    from movate.core.skill_loader import SkillBundle


# MCP protocol version we negotiate. Locking to a known version makes
# debugging easier; we'll bump deliberately when MCP servers in the
# wild move forward. Compatible with the 2024-11-05 spec which is the
# baseline most servers support today.
_MCP_PROTOCOL_VERSION = "2024-11-05"

# Initial JSON-RPC request id. Each call increments; ids must be unique
# within a single connection so the server can correlate request to
# response.
_INITIAL_ID = 1

# Maximum lines retained in the per-session stderr ring buffer.
# Enough to diagnose most server-startup failures without blowing up
# memory on a long-running session. Kept intentionally small — the
# goal is crash diagnostics, not a full log stream.
_STDERR_RING_MAX = 50


@dataclass
class _Session:
    """One running MCP server + the I/O streams we read/write through.

    Held in :attr:`MCPSkillBackend._sessions` keyed by the unique
    ``entry`` command. Reusing the subprocess across skill calls is
    the whole point of caching — subprocess spawn + MCP handshake
    typically takes 200-500ms; amortizing across N calls matters.

    ``stderr_ring`` is a fixed-capacity ring buffer (deque with maxlen)
    that a background reader task populates continuously. On error the
    buffer is flushed into the span's ``mcp.stderr_log`` attribute;
    on success it is silently discarded. Bounded at ``_STDERR_RING_MAX``
    lines so a chatty server never causes unbounded memory growth.
    """

    process: asyncio.subprocess.Process
    next_id: int = _INITIAL_ID
    initialized: bool = False
    # Tools the server reported via tools/list — populated lazily on
    # first call so a healthy skill doesn't pay the round-trip cost
    # if it never gets invoked.
    tools_known: set[str] | None = None
    # Bounded ring buffer for continuous stderr capture.
    stderr_ring: deque[str] = field(default_factory=lambda: deque(maxlen=_STDERR_RING_MAX))
    # Background task that drains the server's stderr into ``stderr_ring``.
    _stderr_task: asyncio.Task[None] | None = field(default=None, repr=False)


class MCPSkillBackend:
    """Dispatches ``kind: mcp`` skills via subprocess + JSON-RPC stdio.

    Per-instance lifecycle: one ``_Session`` per unique ``entry``
    command. The executor's tool-use loop hits ``execute()``; this
    backend spawns + handshakes lazily, then reuses the subprocess for
    every subsequent call to the same server.
    """

    kind = SkillImplementationKind.MCP

    def __init__(self) -> None:
        # name → running session. Key is the unique ``entry`` string
        # (e.g. ``./mcp-servers/github``); we deduplicate by command
        # so two skills pointing at the same server share one process.
        self._sessions: dict[str, _Session] = {}
        # Guards concurrent first-call spawns so two simultaneous
        # tool calls to a new server don't race on subprocess startup.
        self._spawn_lock = asyncio.Lock()

    async def execute(
        self,
        skill: SkillBundle,
        input: dict[str, Any],
        ctx: SkillExecutionContext,
    ) -> dict[str, Any]:
        impl = skill.spec.implementation
        if not impl.tool:  # pragma: no cover — model validator catches this
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=f"mcp skill {skill.spec.name!r}: implementation.tool is empty",
            )

        # Spawn the subprocess + handshake on first use. Concurrent
        # calls for a brand-new server bottleneck on the same lock so
        # we don't end up with two processes for one ``entry``.
        async with self._spawn_lock:
            session = await self._ensure_session(impl.entry, skill.spec.name)

        # Validate the requested tool against tools/list — populated
        # on first call. Catches typos at first invocation rather than
        # at the server's "unknown tool" response (which is per-server
        # and inconsistent).
        if session.tools_known is None:
            session.tools_known = await self._list_tools(session, skill.spec.name)
        if impl.tool not in session.tools_known:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"mcp skill {skill.spec.name!r}: server has no tool "
                    f"{impl.tool!r}; available: {sorted(session.tools_known)}"
                ),
            )

        # ADR 024 — open an ``mcp.call`` child span under the executor's
        # ``skill.<name>`` span (ctx.parent_span). No-op when tracer is None.
        _span = None
        _t0 = 0.0
        if ctx.tracer is not None:
            _t0 = time.monotonic()
            _span = ctx.tracer.start_span(
                "mcp.call",
                {
                    "skill": skill.spec.name,
                    "entry": impl.entry,
                    "tool": impl.tool,
                },
                parent=ctx.parent_span,
            )

        try:
            # Make the tools/call request. Schema validation happens one
            # layer up in dispatch_skill, both directions; here we just
            # produce a dict from whatever the server returned.
            result = await self._call_tool(
                session,
                tool=impl.tool,
                arguments=input,
                skill_name=skill.spec.name,
            )
            if _span is not None and ctx.tracer is not None:
                lat = (time.monotonic() - _t0) * 1000
                ctx.tracer.set_attribute(_span, "latency_ms", round(lat, 1))
                ctx.tracer.end_span(_span, status="ok")
            return result
        except Exception:
            if _span is not None and ctx.tracer is not None:
                lat = (time.monotonic() - _t0) * 1000
                ctx.tracer.set_attribute(_span, "latency_ms", round(lat, 1))
                # Flush the stderr ring buffer into the span on error so
                # operators can see what the MCP server printed before dying.
                stderr_log = "\n".join(session.stderr_ring)
                if stderr_log:
                    ctx.tracer.set_attribute(_span, "mcp.stderr_log", stderr_log)
                ctx.tracer.end_span(_span, status="error")
            raise

    async def aclose(self) -> None:
        """Terminate every cached subprocess. Safe to call multiple
        times; missing processes are silently skipped."""
        for entry, session in list(self._sessions.items()):
            # Cancel the background stderr-drain task before terminating
            # the process so we don't leave a dangling task behind.
            if session._stderr_task is not None and not session._stderr_task.done():
                session._stderr_task.cancel()
                await asyncio.gather(session._stderr_task, return_exceptions=True)
            await _terminate_process(session.process)
            del self._sessions[entry]

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def _ensure_session(self, entry: str, skill_name: str) -> _Session:
        """Get or create a running session for the given ``entry`` command."""
        existing = self._sessions.get(entry)
        if existing is not None and existing.process.returncode is None:
            return existing
        # Process died (or never started) — spawn fresh.
        session = await self._spawn(entry, skill_name)
        self._sessions[entry] = session
        return session

    async def _spawn(self, entry: str, skill_name: str) -> _Session:
        """Spawn the MCP server subprocess and perform the handshake.

        Uses ``shlex.split`` to tokenize the command so operators can
        write ``npx -y @some/mcp-pkg --flag`` naturally. The subprocess
        inherits the parent env so secrets like ``GITHUB_TOKEN`` flow
        through without bespoke wiring — same convention as HTTP skills'
        ``bearer-from-env:`` shape.
        """
        try:
            argv = shlex.split(entry)
        except ValueError as exc:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"mcp skill {skill_name!r}: failed to parse entry "
                    f"{entry!r} as a shell command: {exc}"
                ),
            ) from exc
        if not argv:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=f"mcp skill {skill_name!r}: entry parsed to empty command",
            )

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"mcp skill {skill_name!r}: couldn't start MCP server "
                    f"{argv[0]!r}: {type(exc).__name__}: {exc}"
                ),
            ) from exc

        session = _Session(process=process)

        # Start draining stderr into the ring buffer in the background.
        # This keeps stderr from blocking the subprocess and gives us
        # structured context on error. The task is cancelled in aclose().
        session._stderr_task = asyncio.create_task(
            _drain_stderr(session), name=f"mcp-stderr-{id(session)}"
        )

        # Step 1: handshake. Send initialize, expect a result.
        try:
            handshake_result = await self._rpc_call(
                session,
                method="initialize",
                params={
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "movate", "version": "0.6.0"},
                },
                skill_name=skill_name,
            )
        except SkillError:
            await _terminate_process(process)
            raise
        # Light validation — the server should echo a protocolVersion.
        # We don't strict-check the version because servers may
        # respond with a downgraded version they support; what matters
        # is the handshake succeeded and we got a coherent result.
        if not isinstance(handshake_result, dict) or "protocolVersion" not in handshake_result:
            await _terminate_process(process)
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"mcp skill {skill_name!r}: server's initialize response "
                    f"missing protocolVersion: {handshake_result!r}"
                ),
            )

        # Step 2: notifications/initialized (notification, no response).
        await self._send_notification(
            session,
            method="notifications/initialized",
            skill_name=skill_name,
        )
        session.initialized = True
        return session

    async def _list_tools(self, session: _Session, skill_name: str) -> set[str]:
        """Query the server for its tool catalog. Cached on the session."""
        result = await self._rpc_call(
            session,
            method="tools/list",
            params={},
            skill_name=skill_name,
        )
        if not isinstance(result, dict):
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"mcp skill {skill_name!r}: tools/list response wasn't "
                    f"a JSON object: {result!r}"
                ),
            )
        tools = result.get("tools", [])
        names: set[str] = set()
        for entry in tools:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                names.add(entry["name"])
        return names

    async def _call_tool(
        self,
        session: _Session,
        *,
        tool: str,
        arguments: dict[str, Any],
        skill_name: str,
    ) -> dict[str, Any]:
        """Make a tools/call RPC and parse the response into a dict.

        MCP tool responses carry ``content`` (a list of content blocks)
        and optionally ``structuredContent`` (the modern field for
        machine-readable results). We prefer ``structuredContent``
        when present; fall back to parsing ``content[0].text`` as JSON
        for servers that haven't adopted the structured form yet.
        """
        result = await self._rpc_call(
            session,
            method="tools/call",
            params={"name": tool, "arguments": arguments},
            skill_name=skill_name,
        )
        if not isinstance(result, dict):
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"mcp skill {skill_name!r}: tools/call response wasn't "
                    f"a JSON object: {result!r}"
                ),
            )

        # MCP's "isError: true" carries server-side tool errors as a
        # normal result with an error flag. Surface as backend_error so
        # the LLM sees the consistent error vocabulary.
        if result.get("isError"):
            err_text = _extract_text(result.get("content"))
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"mcp skill {skill_name!r}: server reported tool error: "
                    f"{err_text or '<no error text>'}"
                ),
            )

        # Preferred: structuredContent (modern MCP servers).
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured

        # Fallback: parse content[0].text as JSON.
        text = _extract_text(result.get("content"))
        if text is None:
            raise SkillError(
                type=SkillErrorType.VALIDATION_FAILED,
                message=(
                    f"mcp skill {skill_name!r}: server returned neither "
                    "structuredContent nor any text content; can't extract a "
                    "result dict"
                ),
            )
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"mcp skill {skill_name!r}: server's content text wasn't valid JSON: {exc}"
                ),
            ) from exc
        if not isinstance(parsed, dict):
            raise SkillError(
                type=SkillErrorType.VALIDATION_FAILED,
                message=(
                    f"mcp skill {skill_name!r}: server returned content of "
                    f"type {type(parsed).__name__}, expected a JSON object"
                ),
            )
        return parsed

    # ------------------------------------------------------------------
    # JSON-RPC wire protocol
    # ------------------------------------------------------------------

    async def _rpc_call(
        self,
        session: _Session,
        *,
        method: str,
        params: dict[str, Any],
        skill_name: str,
    ) -> Any:
        """Send a JSON-RPC request, wait for the matching response.

        IDs are monotonically increasing per session — the server may
        interleave responses with notifications, so we filter by
        ``id`` to find the matching reply.
        """
        request_id = session.next_id
        session.next_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        await _write_message(session, request, skill_name=skill_name)
        return await _read_response(
            session,
            expected_id=request_id,
            method=method,
            skill_name=skill_name,
        )

    async def _send_notification(
        self,
        session: _Session,
        *,
        method: str,
        skill_name: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await _write_message(session, message, skill_name=skill_name)


# ---------------------------------------------------------------------------
# Module-level helpers (testable independently)
# ---------------------------------------------------------------------------


async def _drain_stderr(session: _Session) -> None:
    """Background coroutine: continuously drain the server's stderr into
    ``session.stderr_ring``.

    Runs as a task until the server process exits or the task is
    cancelled (by ``aclose``). Each line is decoded and appended to the
    ring buffer; the deque's ``maxlen`` keeps memory bounded regardless
    of how chatty the server is.

    Intentionally swallows all exceptions — this is a best-effort
    diagnostic aid. A failure here (e.g. broken pipe) must never
    propagate to the skill's result path.
    """
    if session.process.stderr is None:
        return
    try:
        while True:
            line = await session.process.stderr.readline()
            if not line:
                break
            session.stderr_ring.append(line.decode("utf-8", errors="replace").rstrip())
    except Exception:
        pass


async def _write_message(
    session: _Session,
    message: dict[str, Any],
    *,
    skill_name: str,
) -> None:
    """Serialize + send one JSON-RPC message as a newline-delimited line.

    MCP servers expect line-delimited JSON on stdin; that's the
    stable convention even though the spec mentions header-based
    framing too. The newline-delimited form is universally supported.
    """
    if session.process.stdin is None:  # pragma: no cover — only true if we mocked weirdly
        raise SkillError(
            type=SkillErrorType.BACKEND_ERROR,
            message=f"mcp skill {skill_name!r}: server stdin is unavailable",
        )
    payload = (json.dumps(message) + "\n").encode("utf-8")
    try:
        session.process.stdin.write(payload)
        await session.process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError) as exc:
        raise SkillError(
            type=SkillErrorType.BACKEND_ERROR,
            message=(
                f"mcp skill {skill_name!r}: server stdin closed (server probably exited): {exc}"
            ),
        ) from exc


async def _read_response(
    session: _Session,
    *,
    expected_id: int,
    method: str,
    skill_name: str,
) -> Any:
    """Read lines from the server's stdout until we get the response
    for ``expected_id``.

    Notifications (no ``id``) and out-of-order responses are silently
    skipped — servers may emit informational notifications between
    requests; we discard them since we don't act on any today.
    """
    if session.process.stdout is None:  # pragma: no cover
        raise SkillError(
            type=SkillErrorType.BACKEND_ERROR,
            message=f"mcp skill {skill_name!r}: server stdout is unavailable",
        )
    while True:
        line = await session.process.stdout.readline()
        if not line:
            # EOF — server died. Report the stderr tail if we have it
            # so operators can see why.
            tail = await _read_stderr_tail(session)
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"mcp skill {skill_name!r}: server closed stdout before "
                    f"replying to {method} (exit {session.process.returncode}); "
                    f"stderr: {tail!r}"
                ),
            )
        try:
            message = json.loads(line)
        except ValueError as exc:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"mcp skill {skill_name!r}: server emitted a non-JSON "
                    f"line on stdout: {line!r} ({exc})"
                ),
            ) from exc
        if not isinstance(message, dict):
            # Not a well-formed JSON-RPC frame; skip.
            continue
        if "id" not in message:
            # Notification — server-side log or progress event. Ignored.
            continue
        if message["id"] != expected_id:
            # Response to a different request (shouldn't happen in our
            # single-flight client, but tolerate it).
            continue
        if "error" in message:
            err = message["error"]
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"mcp skill {skill_name!r}: server returned JSON-RPC error on {method}: {err}"
                ),
            )
        return message.get("result")


async def _read_stderr_tail(session: _Session) -> str:
    """Return a diagnostic tail of the server's stderr.

    If a background drain task is running, yield to the event loop once
    so it has a chance to populate the ring buffer before we check it.
    Then return whatever's in the ring (no I/O needed — already decoded).

    When no drain task is active (e.g. immediate spawn failure before
    the task was created), falls back to a short blocking read of the
    raw stderr stream instead.

    Non-blocking in all paths: the fallback read uses a 0.1 s timeout.
    """
    if session._stderr_task is not None and not session._stderr_task.done():
        # Yield once so the drain task can read at least one iteration.
        await asyncio.sleep(0)
        return "\n".join(session.stderr_ring)

    # No drain task (pre-task spawn failure) — try raw stream.
    if session.process.stderr is None:  # pragma: no cover
        return ""
    try:
        # Fallback: short blocking read for cases where the drain task
        # was never started (e.g. spawn raised before _Session was created).
        data = await asyncio.wait_for(session.process.stderr.read(2048), timeout=0.1)
    except TimeoutError:
        return ""
    return data.decode("utf-8", errors="replace").strip()


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Send SIGTERM, wait briefly, escalate to SIGKILL if needed.

    Two-step shutdown is the standard pattern for cooperative MCP
    servers — most respond to SIGTERM within milliseconds; the
    SIGKILL fallback handles a hung/stuck process without leaking it
    into the system process table.
    """
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()


def _extract_text(content: Any) -> str | None:
    """Pull the first text block out of an MCP ``content`` array.

    MCP servers return content as a list of typed blocks:
    ``[{"type": "text", "text": "..."}, ...]``. We take the first
    ``text`` block. Returns ``None`` if no text content exists.
    """
    if not isinstance(content, list):
        return None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                return text
    return None


# Auto-register on import. The executor + skills_cmd import this
# module for its side-effect of registering with the dispatch table.
register_backend(MCPSkillBackend())
