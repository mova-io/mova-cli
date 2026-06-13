"""GitHub MCP Registry source (ADR 104 D3).

GitHub-published MCP servers are surfaced through the canonical registry under
the ``io.github.*`` namespace, so this adapter composes
:class:`OfficialRegistrySource` and filters to that namespace rather than
inventing a second HTTP client. Trust tier is ``OFFICIAL`` (GitHub-curated
publishers). If GitHub later exposes a dedicated registry endpoint, only the
fetch needs swapping — the mapping + filter stay.
"""

from __future__ import annotations

from movate.mcp_catalog.models import CatalogEntry, TrustTier
from movate.mcp_catalog.sources.official import OfficialRegistrySource

_GITHUB_NAMESPACE = "io.github."


class GitHubRegistrySource:
    """GitHub-hosted MCP servers (the ``io.github.*`` slice of the registry)."""

    name = "github"
    trust = TrustTier.OFFICIAL

    def __init__(self) -> None:
        self._official = OfficialRegistrySource()

    async def search(self, query: str, *, limit: int = 25) -> list[CatalogEntry]:
        # Over-fetch then filter to the GitHub namespace; re-stamp provenance.
        hits = await self._official.search(query, limit=limit * 3)
        out = [
            e.model_copy(update={"source": "github"})
            for e in hits
            if (e.publisher or "").startswith(_GITHUB_NAMESPACE)
        ]
        return out[:limit]

    async def get(self, ref: str) -> CatalogEntry | None:
        hits = await self.search(ref, limit=50)
        for h in hits:
            if h.name == ref:
                return h
        return hits[0] if hits else None
