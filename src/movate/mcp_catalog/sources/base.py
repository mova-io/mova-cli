"""The ``MCPRegistrySource`` adapter seam + source resolution (ADR 104 D1).

A source is a read-only metadata provider: ``search`` (browse) and ``get``
(resolve one). Every source returns :class:`CatalogEntry` objects, so the
``mdk mcp`` commands never special-case a registry. ``resolve_sources`` maps a
``--source`` selector to concrete sources, enforcing the trust gate: bundled +
official are the default; community sources are reachable only when named
explicitly (or via ``all``).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from movate.mcp_catalog.models import CatalogEntry, TrustTier


@runtime_checkable
class MCPRegistrySource(Protocol):
    """Read-only source of MCP server catalog entries (ADR 104 D1)."""

    name: str
    trust: TrustTier

    async def search(self, query: str, *, limit: int = 25) -> list[CatalogEntry]:
        """Return entries matching *query* (empty query → browse all)."""
        ...

    async def get(self, ref: str) -> CatalogEntry | None:
        """Resolve one entry by name/ref, or None if absent."""
        ...


class UnknownSourceError(Exception):
    """Raised when a ``--source`` selector names no registered source."""


# Sources consulted when the author doesn't pass ``--source``. Bundled first
# (offline, highest trust), then the official registry. COMMUNITY sources are
# deliberately excluded from the default set (ADR 104 D4).
DEFAULT_SOURCES: tuple[str, ...] = ("bundled", "official")


def _registry() -> dict[str, MCPRegistrySource]:
    """Build the name → source map.

    Lazy construction (per call) keeps import cost off non-catalog code paths
    and lets each source own its own (possibly httpx-bearing) setup.
    """
    from movate.mcp_catalog.sources.bundled import BundledSource  # noqa: PLC0415
    from movate.mcp_catalog.sources.github import GitHubRegistrySource  # noqa: PLC0415
    from movate.mcp_catalog.sources.official import OfficialRegistrySource  # noqa: PLC0415
    from movate.mcp_catalog.sources.smithery import SmitherySource  # noqa: PLC0415

    # Glama + mcp.so were prototyped but their public APIs expose no runnable
    # launch spec (only descriptions/repos), so they can't produce an `mdk mcp
    # add` entry — verified live during E2E. Smithery is the runnable community
    # registry (stable qualifiedName → Streamable-HTTP gateway URL), so it
    # replaces them as the COMMUNITY source.
    sources: list[MCPRegistrySource] = [
        BundledSource(),
        OfficialRegistrySource(),
        GitHubRegistrySource(),
        SmitherySource(),
    ]
    return {s.name: s for s in sources}


def available_sources() -> list[tuple[str, TrustTier]]:
    """List registered sources as (name, trust) — for help text / errors."""
    return [(s.name, s.trust) for s in _registry().values()]


def resolve_sources(selector: str | None) -> list[MCPRegistrySource]:
    """Resolve a ``--source`` selector into concrete sources (ADR 104 D4).

    * ``None`` → :data:`DEFAULT_SOURCES` (bundled + official; no community).
    * ``"all"`` → every registered source, **including** community ones (the
      caller is responsible for surfacing each entry's trust tier).
    * a specific name → just that source (the explicit opt-in path that makes a
      community source reachable).

    Raises :class:`UnknownSourceError` for an unrecognized name.
    """
    reg = _registry()
    if selector is None:
        return [reg[n] for n in DEFAULT_SOURCES if n in reg]
    if selector == "all":
        return list(reg.values())
    if selector not in reg:
        known = ", ".join([*sorted(reg), "all"])
        raise UnknownSourceError(f"unknown --source {selector!r}; available: {known}")
    return [reg[selector]]
