"""MCP catalog — discover external MCP servers and pin them into agents (ADR 103/104).

A curated, bundled catalog plus pluggable external registry *sources* (the
``MCPRegistrySource`` Protocol), all feeding the ``mdk mcp list``/``search``/
``add`` authoring commands. ``mdk mcp add`` resolves a catalog entry and writes
the ADR 101 ``mcp_servers:`` stanza an agent/project already knows how to
discover and run — it never auto-installs or auto-runs a server.

Trust tiers gate where an entry may come from:

* ``CURATED`` — the bundled, in-repo-reviewed catalog (the default, offline).
* ``OFFICIAL`` — canonical registries (the official MCP registry, GitHub's).
* ``COMMUNITY`` — community directories (mcp.so, Glama); opt-in only, pinned +
  warned, never default.
"""

from __future__ import annotations

from movate.mcp_catalog.models import CatalogEntry, TrustTier

__all__ = ["CatalogEntry", "TrustTier"]
