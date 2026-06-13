"""MCP registry sources (ADR 104).

One ``MCPRegistrySource`` Protocol, one adapter per registry. The bundled
catalog is just the ``CURATED`` source; external registries are additive
adapters gated by trust tier. ``resolve_sources`` turns a ``--source`` selector
into the concrete source list the ``mdk mcp`` commands query.
"""

from __future__ import annotations

from movate.mcp_catalog.sources.base import (
    DEFAULT_SOURCES,
    MCPRegistrySource,
    UnknownSourceError,
    available_sources,
    resolve_sources,
)

__all__ = [
    "DEFAULT_SOURCES",
    "MCPRegistrySource",
    "UnknownSourceError",
    "available_sources",
    "resolve_sources",
]
