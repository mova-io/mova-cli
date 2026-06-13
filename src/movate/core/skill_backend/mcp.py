"""MCP skill backend — talk to a Model Context Protocol server over stdio or HTTP/SSE.

Third backend per ADR 002. Lets skills invoke tools exposed by an MCP
server (Anthropic's Model Context Protocol). The server can be any
process that speaks MCP — internal tool servers, npx-installed
community servers, customer-hosted bridges to legacy systems — **or** a
hosted MCP server reachable over HTTP/SSE (GitHub, Slack, Jira community
servers, customer-hosted SaaS bridges).

Two transport modes (selected automatically from ``entry``):

* **stdio** (default) — ``entry`` is a shell command; the backend spawns
  a subprocess, connects via newline-delimited JSON-RPC over stdin/stdout.
* **HTTP/SSE** — ``entry`` starts with ``http://`` or ``https://``; the
  backend POSTs JSON-RPC requests to the URL and reads SSE responses.
  Connection pooling per URL mirrors the subprocess pool per command.

Two tool modes:

* **Single-tool** (``tool:`` specified in skill.yaml) — backward-compatible;
  the skill invokes exactly one named tool on the server.
* **Multi-tool** (``tool:`` omitted) — the backend calls ``tools/list``,
  registers every tool as a namespaced callable
  (``<skill-name>.<tool-name>``), and the executor sees multiple tools
  from one skill declaration.

Why hand-rolled instead of the official ``mcp`` Python SDK?

MCP's wire protocol is JSON-RPC 2.0 over stdio. The subset we need
(initialize → notifications/initialized → tools/call) is ~150 LOC
to implement cleanly. The official SDK pulls in a transitive
dependency tree that's heavy for the slice we use, and would add
another ``[mcp]`` optional-extra to install. A focused implementation
keeps the dep footprint small and gives us tight control over the
error → :class:`SkillError` mapping. The HTTP/SSE transport reuses
the existing ``httpx`` dep (already in ``pyproject.toml``).

Lifecycle:

* First invocation of a skill referencing a particular ``entry``
  command spawns the subprocess (or opens an HTTP client) + performs
  the MCP handshake.
* Subsequent calls reuse the running session (one connection pool
  per unique ``entry``).
* :meth:`MCPSkillBackend.aclose` terminates every subprocess and
  closes every HTTP client gracefully on executor shutdown.

Failure → :class:`SkillError` mapping:

* Subprocess fails to start (binary missing, exits early) → ``backend_error``
* HTTP connection fails (DNS, TLS, timeout) → ``backend_error``
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
ring-buffer (stdio) or HTTP status (HTTP/SSE) so operators can see why a
server misbehaved.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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
    import httpx

    from movate.core.skill_loader import SkillBundle

_log = logging.getLogger(__name__)

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


def _is_http_entry(entry: str) -> bool:
    """Return True when *entry* looks like an HTTP/SSE URL."""
    lower = entry.lower()
    return lower.startswith("http://") or lower.startswith("https://")


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


@dataclass
class _HttpSession:
    """One HTTP/SSE connection to a remote MCP server.

    Held in :attr:`MCPSkillBackend._http_sessions` keyed by the URL.
    Mirrors ``_Session`` for the stdio path but uses an
    ``httpx.AsyncClient`` + plain HTTP POST for JSON-RPC instead of
    subprocess I/O.
    """

    client: httpx.AsyncClient
    url: str
    # Same as ``url`` but with any secret query-param value redacted — used in
    # log/error messages so an ``?api_key=`` credential never leaks.
    display_url: str = ""
    next_id: int = _INITIAL_ID
    initialized: bool = False
    # MCP Streamable HTTP session id (ADR 101). The server issues an
    # ``Mcp-Session-Id`` on the ``initialize`` response; every subsequent
    # request MUST echo it or the server rejects with "No valid session ID".
    session_id: str | None = None
    # Tools the server reported via tools/list — populated lazily.
    tools_known: set[str] | None = None
    # Full tools/list response: list of tool descriptors (name, description,
    # inputSchema). Stored for multi-tool discovery.
    tools_descriptors: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Default the masked display URL to the real URL when no secret is in it.
        if not self.display_url:
            self.display_url = self.url


class MCPSkillBackend:
    """Dispatches ``kind: mcp`` skills via subprocess stdio or HTTP/SSE.

    Per-instance lifecycle: one ``_Session`` (stdio) or ``_HttpSession``
    (HTTP) per unique ``entry``. The executor's tool-use loop hits
    ``execute()``; this backend spawns + handshakes lazily, then reuses
    the connection for every subsequent call to the same server.
    """

    kind = SkillImplementationKind.MCP

    def __init__(self) -> None:
        # name → running session. Key is the unique ``entry`` string
        # (e.g. ``./mcp-servers/github``); we deduplicate by command
        # so two skills pointing at the same server share one process.
        self._sessions: dict[str, _Session] = {}
        # URL → HTTP session. Same deduplication for HTTP/SSE servers.
        self._http_sessions: dict[str, _HttpSession] = {}
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
        is_http = _is_http_entry(impl.entry)

        # Resolve the tool name.  In single-tool mode (tool: specified),
        # use it directly.  In multi-tool mode (tool: omitted), the
        # caller passes ``__tool__`` in the input dict — the executor
        # injects it when dispatching a namespaced tool call like
        # ``github.create_issue``.  If neither is present, error early.
        tool_name = impl.tool or input.pop("__tool__", None)
        if not tool_name:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"mcp skill {skill.spec.name!r}: no tool specified "
                    "(set implementation.tool in skill.yaml, or pass "
                    "__tool__ in the input for multi-tool mode)"
                ),
            )

        if is_http:
            return await self._execute_http(skill, tool_name, input, ctx)
        return await self._execute_stdio(skill, tool_name, input, ctx)

    async def _execute_stdio(
        self,
        skill: SkillBundle,
        tool: str,
        input: dict[str, Any],
        ctx: SkillExecutionContext,
    ) -> dict[str, Any]:
        """Execute a tool call via the stdio transport."""
        impl = skill.spec.implementation

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
        if tool not in session.tools_known:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"mcp skill {skill.spec.name!r}: server has no tool "
                    f"{tool!r}; available: {sorted(session.tools_known)}"
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
                    "tool": tool,
                },
                parent=ctx.parent_span,
            )

        try:
            result = await self._call_tool(
                session,
                tool=tool,
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

    async def _execute_http(
        self,
        skill: SkillBundle,
        tool: str,
        input: dict[str, Any],
        ctx: SkillExecutionContext,
    ) -> dict[str, Any]:
        """Execute a tool call via the HTTP/SSE transport."""
        impl = skill.spec.implementation

        async with self._spawn_lock:
            http_session = await self._ensure_http_session(impl.entry, skill.spec.name, impl.auth)

        if http_session.tools_known is None:
            tools_known, descriptors = await self._http_list_tools(http_session, skill.spec.name)
            http_session.tools_known = tools_known
            http_session.tools_descriptors = descriptors
        if tool not in http_session.tools_known:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"mcp skill {skill.spec.name!r}: server has no tool "
                    f"{tool!r}; available: {sorted(http_session.tools_known)}"
                ),
            )

        _span = None
        _t0 = 0.0
        if ctx.tracer is not None:
            _t0 = time.monotonic()
            _span = ctx.tracer.start_span(
                "mcp.call",
                {"skill": skill.spec.name, "entry": impl.entry, "tool": tool},
                parent=ctx.parent_span,
            )

        try:
            result = await self._http_call_tool(
                http_session,
                tool=tool,
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
                ctx.tracer.end_span(_span, status="error")
            raise

    async def aclose(self) -> None:
        """Terminate every cached subprocess and close HTTP clients.
        Safe to call multiple times; missing processes are silently skipped."""
        for entry, session in list(self._sessions.items()):
            # Cancel the background stderr-drain task before terminating
            # the process so we don't leave a dangling task behind.
            if session._stderr_task is not None and not session._stderr_task.done():
                session._stderr_task.cancel()
                await asyncio.gather(session._stderr_task, return_exceptions=True)
            await _terminate_process(session.process)
            del self._sessions[entry]
        for url, http_session in list(self._http_sessions.items()):
            await http_session.client.aclose()
            del self._http_sessions[url]

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

        # structuredContent → JSON-object text → wrapped text (see helper).
        return _result_to_dict(result, skill_name)

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

    # ------------------------------------------------------------------
    # Multi-tool discovery (both transports)
    # ------------------------------------------------------------------

    async def discover_tools(
        self,
        entry: str,
        skill_name: str,
        auth: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the full tools/list descriptors for *entry*.

        Used by ``mdk skills add-mcp`` and ADR 101 load-time discovery to list
        available tools. Returns a list of ``{"name": ..., "description": ...,
        "inputSchema": ...}`` dicts. ``auth`` (a ``bearer-from-env:VAR`` spec)
        authenticates the HTTP transport so a token-gated server can be listed;
        it is ignored for stdio (those servers read their own inherited env).
        """
        if _is_http_entry(entry):
            async with self._spawn_lock:
                http_session = await self._ensure_http_session(entry, skill_name, auth)
            if http_session.tools_known is None:
                tools_known, descriptors = await self._http_list_tools(http_session, skill_name)
                http_session.tools_known = tools_known
                http_session.tools_descriptors = descriptors
            return http_session.tools_descriptors
        else:
            async with self._spawn_lock:
                session = await self._ensure_session(entry, skill_name)
            if session.tools_known is None:
                session.tools_known = await self._list_tools(session, skill_name)
            # Re-fetch full descriptors (the _list_tools path only stored
            # names). This is a one-off discovery call so the extra
            # round-trip is acceptable.
            result = await self._rpc_call(
                session,
                method="tools/list",
                params={},
                skill_name=skill_name,
            )
            if isinstance(result, dict):
                return [
                    t
                    for t in result.get("tools", [])
                    if isinstance(t, dict) and isinstance(t.get("name"), str)
                ]
            return []

    # ------------------------------------------------------------------
    # HTTP/SSE session management
    # ------------------------------------------------------------------

    async def _ensure_http_session(
        self, url: str, skill_name: str, auth: str | None = None
    ) -> _HttpSession:
        """Get or create an HTTP/SSE session for *url*.

        ``auth`` is an optional ``bearer-from-env:VAR`` spec (ADR 101 D3): when
        present, the resolved token is set as a default ``Authorization`` header
        on the client. Sessions are keyed by URL; the first session's auth wins
        if the same URL is reached with differing specs (one MCP server = one
        auth in practice).
        """
        existing = self._http_sessions.get(url)
        if existing is not None:
            return existing
        session = await self._http_spawn(url, skill_name, auth)
        self._http_sessions[url] = session
        return session

    async def _http_spawn(self, url: str, skill_name: str, auth: str | None = None) -> _HttpSession:
        """Open an HTTP client and perform the MCP handshake."""
        import httpx as _httpx  # noqa: PLC0415

        # Resolve the credential spec into request headers and/or a URL
        # query-param, plus a masked display URL for logs/errors (ADR 101 D3).
        effective_url, display_url, headers = _apply_http_auth(url, auth, skill_name)

        client = _httpx.AsyncClient(
            timeout=_httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers=headers,
        )
        session = _HttpSession(client=client, url=effective_url, display_url=display_url)

        try:
            handshake_result = await self._http_rpc_call(
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
            await client.aclose()
            raise

        if not isinstance(handshake_result, dict) or "protocolVersion" not in handshake_result:
            await client.aclose()
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"mcp skill {skill_name!r}: HTTP server's initialize "
                    f"response missing protocolVersion: {handshake_result!r}"
                ),
            )

        # Send initialized notification (fire-and-forget).
        await self._http_rpc_notify(
            session,
            method="notifications/initialized",
            skill_name=skill_name,
        )
        session.initialized = True
        return session

    async def _http_list_tools(
        self, session: _HttpSession, skill_name: str
    ) -> tuple[set[str], list[dict[str, Any]]]:
        """Query the HTTP server's tool catalog."""
        result = await self._http_rpc_call(
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
        descriptors: list[dict[str, Any]] = []
        for entry in tools:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                names.add(entry["name"])
                descriptors.append(entry)
        return names, descriptors

    async def _http_call_tool(
        self,
        session: _HttpSession,
        *,
        tool: str,
        arguments: dict[str, Any],
        skill_name: str,
    ) -> dict[str, Any]:
        """Make a tools/call RPC over HTTP and parse the response."""
        result = await self._http_rpc_call(
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

        if result.get("isError"):
            err_text = _extract_text(result.get("content"))
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"mcp skill {skill_name!r}: server reported tool error: "
                    f"{err_text or '<no error text>'}"
                ),
            )

        return _result_to_dict(result, skill_name)

    # ------------------------------------------------------------------
    # HTTP/SSE JSON-RPC wire protocol
    # ------------------------------------------------------------------

    async def _http_rpc_call(
        self,
        session: _HttpSession,
        *,
        method: str,
        params: dict[str, Any],
        skill_name: str,
    ) -> Any:
        """POST a JSON-RPC request to the server, parse the response.

        The MCP HTTP/SSE transport sends JSON-RPC as a POST body and
        receives a JSON-RPC response (possibly SSE-wrapped). We handle
        both plain JSON and SSE (``text/event-stream``) responses for
        maximum compatibility with hosted servers.
        """
        request_id = session.next_id
        session.next_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        headers = {"Accept": "application/json, text/event-stream"}
        if session.session_id:
            headers["Mcp-Session-Id"] = session.session_id
        try:
            resp = await session.client.post(session.url, json=request, headers=headers)
        except Exception as exc:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"mcp skill {skill_name!r}: HTTP request to "
                    f"{session.display_url} failed: {type(exc).__name__}: {exc}"
                ),
            ) from exc

        # Capture the Streamable-HTTP session id (issued on ``initialize``) so
        # later requests echo it. Header lookup is case-insensitive in httpx.
        sid = resp.headers.get("mcp-session-id")
        if sid:
            session.session_id = sid

        if resp.is_error:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"mcp skill {skill_name!r}: HTTP {resp.status_code} "
                    f"from {session.display_url}: {resp.text[:500]}"
                ),
            )

        # Parse response — may be plain JSON or SSE.
        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return _parse_sse_response(resp.text, request_id, method, skill_name)
        # Plain JSON response.
        try:
            message = resp.json()
        except ValueError as exc:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"mcp skill {skill_name!r}: HTTP response from "
                    f"{session.display_url} wasn't valid JSON: {exc}"
                ),
            ) from exc

        if not isinstance(message, dict):
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"mcp skill {skill_name!r}: HTTP response wasn't a JSON object: {message!r}"
                ),
            )

        if "error" in message:
            err = message["error"]
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"mcp skill {skill_name!r}: server returned JSON-RPC error on {method}: {err}"
                ),
            )
        return message.get("result")

    async def _http_rpc_notify(
        self,
        session: _HttpSession,
        *,
        method: str,
        skill_name: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """POST a JSON-RPC notification (no id, no response expected)."""
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        headers = {"Accept": "application/json, text/event-stream"}
        if session.session_id:
            headers["Mcp-Session-Id"] = session.session_id
        import contextlib  # noqa: PLC0415

        with contextlib.suppress(Exception):
            await session.client.post(session.url, json=message, headers=headers)


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


def _apply_http_auth(
    url: str, auth: str | None, skill_name: str
) -> tuple[str, str, dict[str, str]]:
    """Resolve a credential spec for the HTTP transport (ADR 101 D3).

    Returns ``(effective_url, display_url, headers)``:

    * ``bearer-from-env:VAR`` → ``Authorization: Bearer <VAR>`` header; URL
      unchanged. (Shares the ``kind: http`` resolver.)
    * ``apikey-query:PARAM=VAR`` → append ``?PARAM=<VAR>`` (or ``&``) to the
      URL — for hosted servers that authenticate by query param (e.g. Smithery's
      ``?api_key=``), where a Bearer token is rejected. ``display_url`` masks the
      value so it never appears in logs/errors.
    * ``None`` → no auth.

    A missing/empty env var, or an unrecognized spec, raises ``SkillError`` so
    the failure is loud and the operator knows which var to set.
    """
    if not auth:
        return url, url, {}
    if auth.startswith("bearer-from-env:"):
        from movate.core.skill_backend.http import _build_auth_header  # noqa: PLC0415

        return url, url, {"Authorization": _build_auth_header(auth, skill_name)}
    if auth.startswith("apikey-query:"):
        spec = auth.removeprefix("apikey-query:")
        if "=" not in spec:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"mcp skill {skill_name!r}: apikey-query spec {auth!r} must be "
                    f"'apikey-query:<param>=<ENV_VAR>'"
                ),
            )
        param, var = (s.strip() for s in spec.split("=", 1))
        value = os.environ.get(var) if (param and var) else None
        if not value:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"mcp skill {skill_name!r}: env var {var!r} (for {param} query "
                    f"auth) is unset or empty; set it or change the credential spec"
                ),
            )
        sep = "&" if "?" in url else "?"
        effective = f"{url}{sep}{param}={value}"
        display = f"{url}{sep}{param}=***"
        return effective, display, {}
    raise SkillError(
        type=SkillErrorType.BACKEND_ERROR,
        message=(
            f"mcp skill {skill_name!r}: unsupported credential spec {auth!r} "
            f"(expected 'bearer-from-env:VAR' or 'apikey-query:param=VAR')"
        ),
    )


def _result_to_dict(result: dict[str, Any], skill_name: str) -> dict[str, Any]:
    """Turn a ``tools/call`` result into the JSON-object a skill returns.

    Shared by the stdio + HTTP transports so both behave identically. Order:

    1. ``structuredContent`` (modern servers) → used directly.
    2. text content that parses as a JSON **object** → used directly.
    3. **any other text** (prose, a JSON scalar/array) → wrapped as
       ``{"content": <value>}``.

    The wrapping in (3) is deliberate: most MCP tools return human-readable
    *text* (echo, fetch, search, summaries), not a JSON object. Erroring on
    those — as this did before — made the majority of real servers unusable
    from an agent's tool-use loop. Wrapping keeps the value available to the
    model; a skill that genuinely needs a typed object still enforces it via
    its output schema at ``dispatch_skill`` (a clear validation error, not a
    backend crash). ``isError`` is handled by the caller before this point.
    """
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured

    text = _extract_text(result.get("content"))
    if text is None:
        raise SkillError(
            type=SkillErrorType.VALIDATION_FAILED,
            message=(
                f"mcp skill {skill_name!r}: server returned neither "
                "structuredContent nor any text content; can't extract a result"
            ),
        )
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"content": text}  # plain prose — the common case
    if isinstance(parsed, dict):
        return parsed
    return {"content": parsed}  # JSON scalar/array — wrap so it's a dict


def _parse_sse_response(
    body: str,
    expected_id: int,
    method: str,
    skill_name: str,
) -> Any:
    """Parse a ``text/event-stream`` body for the JSON-RPC response.

    SSE frames look like::

        event: message
        data: {"jsonrpc":"2.0","id":1,"result":{...}}

    We look for the first ``data:`` line whose parsed JSON has an
    ``id`` matching ``expected_id``.
    """
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("data:"):
            continue
        payload = stripped[len("data:") :].strip()
        if not payload:
            continue
        try:
            message = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        if message.get("id") != expected_id:
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

    raise SkillError(
        type=SkillErrorType.BACKEND_ERROR,
        message=(
            f"mcp skill {skill_name!r}: SSE response from server "
            f"contained no matching JSON-RPC reply for id={expected_id}"
        ),
    )


# Auto-register on import. The executor + skills_cmd import this
# module for its side-effect of registering with the dispatch table.
register_backend(MCPSkillBackend())
