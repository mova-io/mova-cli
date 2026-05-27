"""The mdk authoring MCP **server** — expose the catalog as MCP tools (ADR 025 S3/D5).

This is the *inverse* of the MCP skill backend (:mod:`movate.core.skill_backend.mcp`,
where mdk is an MCP *client*). Here mdk is an MCP *server*: it speaks JSON-RPC
2.0 over stdio and exposes the authoring action catalog
(:mod:`movate.authoring.catalog`) as MCP tools so a structured IDE/agent
(Claude Code, Cursor, …) can drive the *same* plan→preview→apply→verify spine
the thin ``mdk authoring`` CLI does — no behavioral drift (D5: "three surfaces,
one catalog").

Dependency decision (CLAUDE.md §8)
----------------------------------
No new dependency. The skill backend deliberately hand-rolls the JSON-RPC wire
protocol rather than pull in the heavy official ``mcp`` SDK; this server follows
the **same** decision (~the same ~150 LOC of newline-delimited JSON-RPC). MCP's
``initialize`` → ``tools/list`` → ``tools/call`` subset is small and stdlib-only
(``json`` + ``sys``/file streams), so the base install stays lean and there is no
opt-in extra to install. The protocol version negotiated matches the client
backend's (:data:`MCP_PROTOCOL_VERSION`).

The tool manifest
-----------------
The catalog is self-describing (:func:`movate.authoring.catalog.describe_catalog`),
so the tool list is **generated, never hand-written** (the lesson this codebase
keeps relearning). For each catalog action ``<name>`` the server exposes:

* ``plan_<name>`` — dry-run the action: return the :class:`~movate.authoring.models.ActionPlan`
  (diff + side effects + cost + the ``requires_confirmation`` gate). **No writes.**
* ``apply_<name>`` — route through :class:`~movate.authoring.driver.AuthoringDriver`
  (checkpoint → apply → verify), so the snapshot/verify/reversibility guarantees
  stay intact. The ``confirmed`` / ``fast_mode`` / ``verify`` knobs carry D2's
  safety; a confirmation-gated action refuses to apply without ``confirmed: true``.

Plus two catalog-wide tools:

* ``validate`` — load+validate an agent (the structural sensor, no mutation).
* ``run`` — hermetic mock smoke of an agent (no keys, no network).

Boundaries (ADR 025 D8): the tools compose **only** catalog actions — no raw
filesystem writes, no shell, no ``az``, no credentials. This is a local
control-plane authoring tool; nothing here ships in the runtime/execution plane.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from movate.authoring.base import AuthoringActionError, AuthoringContext
from movate.authoring.catalog import UnknownActionError, get_action, list_actions
from movate.authoring.driver import AuthoringDriver, ConfirmationRequiredError
from movate.authoring.verify import AgentLoadError, mock_run, validate_agent

if TYPE_CHECKING:
    from collections.abc import Iterable

# MCP protocol version we advertise. Locked to the same value the skill-backend
# client negotiates (:mod:`movate.core.skill_backend.mcp`) — the 2024-11-05
# baseline most MCP hosts support. Bumped deliberately, not opportunistically.
MCP_PROTOCOL_VERSION = "2024-11-05"

# JSON-RPC + tool-name constants. ``plan_``/``apply_`` prefix every catalog
# action; ``validate``/``run`` are the two catalog-wide tools.
_PLAN_PREFIX = "plan_"
_APPLY_PREFIX = "apply_"
_VALIDATE_TOOL = "validate"
_RUN_TOOL = "run"

# JSON-RPC 2.0 error codes (the standard ones we use).
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603


class _RpcError(Exception):
    """A JSON-RPC level error (bad method/params) → an ``error`` response frame."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class AuthoringMCPServer:
    """Maps the authoring catalog onto MCP's ``initialize``/``tools/*`` methods.

    Stateless beyond the :class:`AuthoringContext` it drives the catalog through;
    construct one per ``mdk mcp serve`` invocation. The dispatch is pure — it
    takes a decoded JSON-RPC request dict and returns a response dict (or ``None``
    for a notification) — so it is fully testable without spawning a subprocess
    or touching stdio.
    """

    ctx: AuthoringContext

    # -- self-describing tool manifest (generated from the catalog) -----------

    def tool_manifest(self) -> list[dict[str, Any]]:
        """The MCP ``tools/list`` payload — generated from the catalog (never hand-written).

        For each action: a ``plan_<name>`` (dry-run) + ``apply_<name>`` (driven),
        with the action's own ``args_model`` JSON schema as the tool input schema.
        Plus the catalog-wide ``validate`` and ``run`` tools.
        """
        tools: list[dict[str, Any]] = []
        for action in list_actions():
            args_schema = action.args_model.model_json_schema()
            tools.append(
                {
                    "name": f"{_PLAN_PREFIX}{action.name}",
                    "description": (
                        f"Dry-run the {action.name!r} authoring action: returns the plan "
                        f"(unified diff, side effects, cost estimate, and the "
                        f"requires_confirmation gate). Makes NO writes. {action.description}"
                    ),
                    "inputSchema": args_schema,
                }
            )
            tools.append(
                {
                    "name": f"{_APPLY_PREFIX}{action.name}",
                    "description": (
                        f"Apply the {action.name!r} authoring action through the safe "
                        f"plan→checkpoint→apply→verify driver. Pass confirmed=true for a "
                        f"cost/networked/destructive action (it refuses otherwise), or "
                        f"fast_mode=true to auto-apply an additive+reversible+free one. "
                        f"{action.description}"
                    ),
                    "inputSchema": _apply_input_schema(args_schema),
                }
            )
        tools.append(
            {
                "name": _VALIDATE_TOOL,
                "description": (
                    "Validate an agent directory (load_agent — the structural sensor "
                    "`mdk validate` uses). No mutation. Returns ok + any friendly error."
                ),
                "inputSchema": _AGENT_ONLY_SCHEMA,
            }
        )
        tools.append(
            {
                "name": _RUN_TOOL,
                "description": (
                    "Hermetic mock smoke of an agent (deterministic mock provider + "
                    "in-memory storage; no API keys, no network). Returns ok."
                ),
                "inputSchema": _AGENT_ONLY_SCHEMA,
            }
        )
        return tools

    # -- JSON-RPC dispatch ----------------------------------------------------

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Dispatch one decoded JSON-RPC request → a response dict (or None).

        Returns ``None`` for a notification (no ``id`` — e.g.
        ``notifications/initialized``), which the transport must not reply to.
        Maps an :class:`_RpcError` to a JSON-RPC ``error`` frame; any tool-level
        failure is returned as a normal result with ``isError: true`` (MCP's
        convention) so the calling agent sees it in the tool-result channel.
        """
        method = message.get("method")
        msg_id = message.get("id")
        if msg_id is None:
            # A notification — handle for side effects, never reply.
            return None
        try:
            result = self._dispatch(method, message.get("params") or {})
        except _RpcError as exc:
            return _error_frame(msg_id, exc.code, exc.message)
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _dispatch(self, method: Any, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return self._initialize()
        if method == "tools/list":
            return {"tools": self.tool_manifest()}
        if method == "tools/call":
            return self._tools_call(params)
        if method == "ping":
            return {}
        raise _RpcError(_METHOD_NOT_FOUND, f"unknown method: {method!r}")

    def _initialize(self) -> dict[str, Any]:
        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "mdk-authoring", "version": _mdk_version()},
        }

    def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise _RpcError(_INVALID_PARAMS, "tools/call requires a string 'name'")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise _RpcError(_INVALID_PARAMS, "tools/call 'arguments' must be an object")
        try:
            payload = self._invoke_tool(name, arguments)
        except (AuthoringActionError, AgentLoadError, ValueError) as exc:
            # A tool-level failure (bad args, action error, validate failure) is
            # surfaced as an MCP result with isError, NOT a JSON-RPC error — the
            # calling agent reads it in the tool-result channel and can re-plan.
            return _tool_error(str(exc))
        except ConfirmationRequiredError as exc:
            return _tool_error(str(exc))
        return _tool_result(payload)

    def _invoke_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Route a tool name to a catalog plan/apply or the validate/run helpers."""
        driver = AuthoringDriver(self.ctx)
        if name == _VALIDATE_TOOL:
            return self._validate(arguments)
        if name == _RUN_TOOL:
            return self._run(arguments)
        if name.startswith(_PLAN_PREFIX):
            action_name = name[len(_PLAN_PREFIX) :]
            _assert_known_action(action_name)
            plan = driver.plan(action_name, arguments)
            return plan.model_dump(mode="json")
        if name.startswith(_APPLY_PREFIX):
            action_name = name[len(_APPLY_PREFIX) :]
            _assert_known_action(action_name)
            confirmed = bool(arguments.pop("confirmed", False))
            fast_mode = bool(arguments.pop("fast_mode", False))
            verify = bool(arguments.pop("verify", True))
            outcome = driver.apply(
                action_name,
                arguments,
                confirmed=confirmed,
                fast_mode=fast_mode,
                verify=verify,
            )
            return {
                "plan": outcome.plan.model_dump(mode="json"),
                "result": outcome.result.model_dump(mode="json") if outcome.result else None,
                "verify": outcome.verify.model_dump(mode="json") if outcome.verify else None,
                "applied": outcome.log_entry is not None,
            }
        raise _RpcError(_METHOD_NOT_FOUND, f"no such tool: {name!r}")

    def _validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        agent = _require_agent(arguments)
        validate_agent(self.ctx.agent_dir(agent))
        return {"agent": agent, "ok": True}

    def _run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        agent = _require_agent(arguments)
        bundle = validate_agent(self.ctx.agent_dir(agent))
        return {"agent": agent, "ok": bool(mock_run(bundle))}


# ---------------------------------------------------------------------------
# stdio serve loop (the transport)
# ---------------------------------------------------------------------------


def serve_stdio(server: AuthoringMCPServer, stream_in: Any, stream_out: Any) -> None:
    """Run the newline-delimited JSON-RPC loop over the given text streams.

    Reads one JSON object per line from ``stream_in``, dispatches it via
    :meth:`AuthoringMCPServer.handle_message`, and writes the response (if any)
    as a single line to ``stream_out``. Notifications (no ``id``) produce no
    reply. A non-JSON line yields a parse-error frame (with no id) on stdout so
    a misbehaving client gets a coherent signal rather than a silent drop.

    The loop ends on EOF (the client closed stdin / the host shut us down).
    """
    for raw in stream_in:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            _write_line(stream_out, _error_frame(None, _INTERNAL_ERROR, "invalid JSON-RPC line"))
            continue
        if not isinstance(message, dict):
            _write_line(stream_out, _error_frame(None, _INTERNAL_ERROR, "frame was not an object"))
            continue
        response = server.handle_message(message)
        if response is not None:
            _write_line(stream_out, response)


def _write_line(stream_out: Any, frame: dict[str, Any]) -> None:
    stream_out.write(json.dumps(frame) + "\n")
    stream_out.flush()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

# Tools that take only an agent name (validate / run).
_AGENT_ONLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "agent": {"type": "string", "description": "Agent name (under agents/<name>/)."}
    },
    "required": ["agent"],
    "additionalProperties": False,
}

# The plan-control knobs an apply_<action> tool accepts on top of the action's
# own args. Mirrors AuthoringDriver.apply's parameters (D2 safety gate).
_APPLY_CONTROL_PROPS: dict[str, Any] = {
    "confirmed": {
        "type": "boolean",
        "default": False,
        "description": (
            "Explicit yes for a cost/networked/destructive action. The driver "
            "refuses to apply a requires_confirmation plan without this."
        ),
    },
    "fast_mode": {
        "type": "boolean",
        "default": False,
        "description": "Auto-apply an additive+reversible+free action without confirmation.",
    },
    "verify": {
        "type": "boolean",
        "default": True,
        "description": "Run the validate + mock-run verify loop after apply (D3).",
    },
}


def _apply_input_schema(args_schema: dict[str, Any]) -> dict[str, Any]:
    """Compose an apply tool's input schema: the action's args + the control knobs.

    Copies the action's JSON schema and merges the ``confirmed``/``fast_mode``/
    ``verify`` driver knobs alongside its own properties. ``additionalProperties``
    is left False (matching the action's ``extra='forbid'`` models) but the
    control props are explicitly allowed.
    """
    merged = dict(args_schema)
    props = dict(merged.get("properties", {}))
    props.update(_APPLY_CONTROL_PROPS)
    merged["properties"] = props
    return merged


def _assert_known_action(action_name: str) -> None:
    """Raise an MCP method-not-found if the action isn't in the catalog."""
    try:
        get_action(action_name)
    except UnknownActionError as exc:
        raise _RpcError(_METHOD_NOT_FOUND, str(exc)) from None


def _require_agent(arguments: dict[str, Any]) -> str:
    agent = arguments.get("agent")
    if not isinstance(agent, str) or not agent:
        raise ValueError("an 'agent' name is required")
    return agent


def _tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a structured result in MCP's tools/call response shape.

    Carries both the modern ``structuredContent`` field and a ``content`` text
    block (the JSON serialized) so old and new MCP hosts can both read it —
    mirroring what the skill-backend client prefers when reading.
    """
    text = json.dumps(payload, indent=2)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
        "isError": False,
    }


def _tool_error(message: str) -> dict[str, Any]:
    """An MCP tool-level error result (isError: true) — not a JSON-RPC error."""
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _error_frame(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _mdk_version() -> str:
    from movate import __version__  # noqa: PLC0415

    return __version__


def build_server(project: Path) -> AuthoringMCPServer:
    """Construct a server rooted at ``project`` (a control-plane authoring root)."""
    return AuthoringMCPServer(ctx=AuthoringContext(project=project.resolve()))


def actions_for_manifest() -> Iterable[str]:
    """The catalog action names the manifest is generated from (introspection helper)."""
    return [a.name for a in list_actions()]


__all__ = [
    "MCP_PROTOCOL_VERSION",
    "AuthoringMCPServer",
    "build_server",
    "serve_stdio",
]
