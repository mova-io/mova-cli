"""MCP server discovery: turn ``mcp_servers:`` declarations into skills (ADR 101 D2).

At agent-load time, for each declared :class:`~movate.core.models.MCPServerRef`
this module connects to the server, lists its tools, filters them per the ref's
allow/deny list, and mints an in-memory :class:`~movate.core.tool_registry.models.ToolDescriptor`
per surviving tool — fed through the existing
:func:`~movate.core.tool_registry.bridge.tool_descriptor_to_skill_bundle` so each
becomes a :class:`~movate.core.skill_loader.SkillBundle` indistinguishable from a
``skills/``-sourced or registry-sourced skill. The executor then dispatches them
through the unchanged ``kind: mcp`` backend.

Design notes:

* **One MCP client.** Discovery reuses :class:`MCPSkillBackend` (spawn,
  handshake, connection pooling, timeouts) via its :meth:`discover_tools`
  method — there is no second JSON-RPC implementation.
* **Single-tool dispatch per discovered tool.** Each tool becomes its own
  bundle with a concrete ``implementation.tool`` (the *verbatim* MCP tool name)
  and the tool's own ``inputSchema``. The executor builds one tool-spec per
  bundle (``executor.py``), so this is the complete, correct shape — no
  ``__tool__`` injection needed.
* **Name sanitization.** MCP tool names commonly contain underscores /
  uppercase, which ``SkillSpec.name`` and ``ToolDescriptor.name`` forbid. We
  sanitize the *identifier* (``<server>.<sanitized-tool>``) while preserving the
  *verbatim* wire name in the backend config so ``tools/call`` is byte-exact.
* **Sync bridge.** ``load_agent`` is synchronous but may be called from inside a
  running event loop (Temporal activities, the runtime). :func:`discover_sync`
  bridges both cases.
* **Governance by construction.** The allow/deny filter runs *before* minting,
  so a tool the author didn't authorize never becomes a bundle — the same
  guarantee the tool registry's allowlist gives, realized here at the filter.

Credentials (``credentials_ref``) resolve at *dispatch* time via the existing
backend env/header injection (ADR 101 D3), not here — discovery relies on
ambient env for any auth a server needs to *list* its tools. OAuth / interactive
auth is deferred to the Phase 3 hardening ADR.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from movate.core.tool_registry.bridge import tool_descriptor_to_skill_bundle
from movate.core.tool_registry.models import (
    ToolBackendConfig,
    ToolDescriptor,
    ToolGovernance,
    ToolScope,
)

if TYPE_CHECKING:
    from movate.core.models import MCPServerRef
    from movate.core.skill_loader import SkillBundle

_log = logging.getLogger(__name__)

# Overall per-server wall-clock budget for connect + handshake + tools/list.
# A hung server can't stall agent load past this; a timeout is treated as
# "unreachable" and governed by the ref's ``required`` flag.
DISCOVERY_TIMEOUT_S = 20.0

# Placeholder semver for minted descriptors — MCP servers don't report a tool
# version. ``ToolDescriptor.version`` requires MAJOR.MINOR.PATCH.
_MINTED_VERSION = "0.0.0"

# A ToolDescriptor name segment: lowercase, letter-start, alphanumeric + hyphens.
_SEGMENT_RE = re.compile(r"^[a-z][a-z0-9-]*$")


class MCPDiscoveryError(Exception):
    """A ``required: true`` MCP server failed to discover, or a name collided."""


@dataclass
class MCPDiscoveryResult:
    """Outcome of discovering one agent's declared MCP servers."""

    bundles: list[SkillBundle] = field(default_factory=list)
    """One SkillBundle per discovered, filtered tool (across all servers)."""

    warnings: list[str] = field(default_factory=list)
    """Human-readable warnings for fail-soft servers that didn't discover."""

    fingerprints: dict[str, str] = field(default_factory=dict)
    """server name → sha256 of its discovered toolset, for drift detection."""


def _sanitize_segment(name: str) -> str | None:
    """Coerce a verbatim MCP tool name into a valid descriptor-name segment.

    Lowercases, replaces any run of non-``[a-z0-9]`` with a single hyphen, and
    strips leading/trailing hyphens. Returns ``None`` when the result can't be a
    valid segment (empty, or doesn't start with a letter) — the caller skips
    such a tool with a warning rather than minting an invalid descriptor.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not cleaned or not _SEGMENT_RE.match(cleaned):
        return None
    return cleaned


def _filter_tools(descriptors: list[dict[str, Any]], server: MCPServerRef) -> list[dict[str, Any]]:
    """Apply the server's include/exclude filter to raw tool descriptors.

    Filtering matches on the *verbatim* MCP tool name (``descriptor["name"]``),
    which is what an author reads from the server's docs / ``mdk mcp inspect``.
    """
    if server.include_tools is not None:
        allow = set(server.include_tools)
        return [d for d in descriptors if d.get("name") in allow]
    if server.exclude_tools is not None:
        deny = set(server.exclude_tools)
        return [d for d in descriptors if d.get("name") not in deny]
    return descriptors


def _mint_bundle(server: MCPServerRef, tool: dict[str, Any]) -> SkillBundle:
    """Build a SkillBundle for one discovered MCP tool via the registry bridge."""
    tool_name = tool["name"]  # verbatim wire name — preserved for tools/call
    segment = _sanitize_segment(tool_name)
    if segment is None:
        raise ValueError(
            f"mcp server {server.name!r}: tool name {tool_name!r} can't be "
            f"sanitized to a valid identifier (must reduce to a letter-led "
            f"alphanumeric+hyphen token)"
        )

    input_schema = tool.get("inputSchema") or {"type": "object"}
    if not isinstance(input_schema, dict):
        input_schema = {"type": "object"}

    backend_config: dict[str, Any] = {"entry": server.entry, "tool": tool_name}
    # An HTTP MCP server may be token-gated; carry the auth spec so the bridge
    # threads it into the dispatch path (ADR 101 D3). Ignored by stdio.
    if server.credentials_ref:
        backend_config["auth"] = server.credentials_ref

    descriptor = ToolDescriptor(
        name=f"{server.name}.{segment}",
        version=_MINTED_VERSION,
        scope=ToolScope.PROJECT,
        description=str(tool.get("description") or f"{tool_name} (via MCP {server.name})"),
        input_schema=input_schema,
        # MCP tools don't declare an output schema; leave permissive.
        output_schema={},
        backend=ToolBackendConfig(kind="mcp", config=backend_config),
        credentials_ref=server.credentials_ref,
        governance=ToolGovernance(default_grant=True, mutating=_is_mutating(tool)),
    )
    return tool_descriptor_to_skill_bundle(descriptor)


def _is_mutating(tool: dict[str, Any]) -> bool:
    """Read MCP tool annotations to flag write/destructive tools (ADR 101 D3).

    MCP servers may attach ``annotations`` with ``readOnlyHint`` /
    ``destructiveHint`` (both optional, hints not guarantees). We map them to
    ``ToolGovernance.mutating`` so a write-capable tool is *labelled* — surfaced
    by ``mdk mcp inspect`` and available to future policy/HITL gating. Absent
    annotations → ``False`` (unknown; the conservative default that matches the
    existing ToolGovernance default). Note: there is no HITL gate in the agent
    tool-use loop today, so this is metadata, not yet enforcement.
    """
    ann = tool.get("annotations")
    if not isinstance(ann, dict):
        return False
    if ann.get("destructiveHint") is True:
        return True
    return ann.get("readOnlyHint") is False


def _fingerprint(tools: list[dict[str, Any]]) -> str:
    """Stable hash of a server's discovered toolset (names + input schemas).

    Used for drift detection: a change here between author-time and a later
    load/validate means the server's surface moved under the agent's feet.
    """
    items = sorted(
        (str(t.get("name")), json.dumps(t.get("inputSchema") or {}, sort_keys=True)) for t in tools
    )
    return hashlib.sha256(json.dumps(items).encode("utf-8")).hexdigest()


async def discover_mcp_skill_bundles(
    servers: list[MCPServerRef],
    *,
    agent_name: str = "<unknown>",
    existing_skill_names: set[str] | None = None,
) -> MCPDiscoveryResult:
    """Discover tools for every declared MCP server (ADR 101 D2).

    Connects to each server, lists + filters its tools, and mints a SkillBundle
    per surviving tool. A server that fails to discover is fail-soft (a warning)
    unless its ``required`` flag is set, in which case
    :class:`MCPDiscoveryError` is raised. A discovered skill name that collides
    with an existing skill or another discovered tool is always a hard error
    (deterministic, not last-writer-wins).
    """
    # Local import avoids a module-load cycle (mcp.py imports core.models which
    # is imported widely) and keeps non-MCP agent loads from importing the
    # subprocess/httpx machinery at all.
    from movate.core.skill_backend.mcp import MCPSkillBackend  # noqa: PLC0415

    result = MCPDiscoveryResult()
    if not servers:
        return result

    seen: set[str] = set(existing_skill_names or set())
    backend = MCPSkillBackend()
    try:
        for server in servers:
            try:
                descriptors = await asyncio.wait_for(
                    backend.discover_tools(server.entry, server.name, server.credentials_ref),
                    timeout=DISCOVERY_TIMEOUT_S,
                )
            except (TimeoutError, asyncio.TimeoutError) as exc:  # noqa: UP041
                msg = (
                    f"mcp server {server.name!r}: tools/list timed out after "
                    f"{DISCOVERY_TIMEOUT_S:.0f}s ({server.entry!r})"
                )
                if server.required:
                    raise MCPDiscoveryError(f"required {msg}; failing agent load") from exc
                _log.warning("agent %s: %s — skipping (required=false)", agent_name, msg)
                result.warnings.append(msg)
                continue
            except Exception as exc:  # transport / protocol / spawn failure
                msg = f"mcp server {server.name!r}: discovery failed — {type(exc).__name__}: {exc}"
                if server.required:
                    raise MCPDiscoveryError(f"required {msg}; failing agent load") from exc
                _log.warning("agent %s: %s — skipping (required=false)", agent_name, msg)
                result.warnings.append(msg)
                continue

            filtered = _filter_tools(descriptors, server)
            result.fingerprints[server.name] = _fingerprint(filtered)

            for tool in filtered:
                try:
                    bundle = _mint_bundle(server, tool)
                except ValueError as exc:
                    # A single unmintable tool (e.g. unsanitizable name) is a
                    # soft skip — don't fail the whole server over one odd tool.
                    _log.warning("agent %s: %s — skipping tool", agent_name, exc)
                    result.warnings.append(str(exc))
                    continue

                if bundle.spec.name in seen:
                    raise MCPDiscoveryError(
                        f"mcp server {server.name!r}: discovered tool resolves to "
                        f"skill name {bundle.spec.name!r}, which collides with an "
                        f"existing skill or another discovered tool — rename via "
                        f"include_tools or resolve the duplicate"
                    )
                seen.add(bundle.spec.name)
                result.bundles.append(bundle)
    finally:
        await backend.aclose()

    return result


def discover_sync(
    servers: list[MCPServerRef],
    *,
    agent_name: str = "<unknown>",
    existing_skill_names: set[str] | None = None,
) -> MCPDiscoveryResult:
    """Synchronous wrapper over :func:`discover_mcp_skill_bundles`.

    ``load_agent`` is synchronous but is called from both plain sync contexts
    (the CLI) and from inside a running event loop (Temporal activities, the
    runtime). When no loop is running we use :func:`asyncio.run`; when one is,
    we run the coroutine to completion on a dedicated thread with its own loop
    so we never try to block the caller's running loop.
    """
    if not servers:
        return MCPDiscoveryResult()

    def _run() -> MCPDiscoveryResult:
        return asyncio.run(
            discover_mcp_skill_bundles(
                servers,
                agent_name=agent_name,
                existing_skill_names=existing_skill_names,
            )
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — safe to own one for the duration.
        return _run()

    # A loop is already running on this thread; offload to a worker thread that
    # owns its own event loop. The MCP backend's subprocesses/clients are
    # created and torn down entirely within that loop.
    import concurrent.futures  # noqa: PLC0415

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run).result()
