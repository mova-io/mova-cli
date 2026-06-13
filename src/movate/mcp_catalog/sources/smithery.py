"""Smithery registry source (ADR 104 D4).

Smithery (``registry.smithery.ai``) is the cleanest runnable community registry:
every server has a stable ``qualifiedName`` and is reachable over MCP Streamable
HTTP at the deterministic gateway URL ``https://server.smithery.ai/<qualifiedName>/mcp``,
authenticated with the operator's Smithery API key as an ``?api_key=`` query
param. So unlike browse-only directories (Glama, mcp.so — whose APIs expose no
runnable launch spec), Smithery maps directly to a runnable ADR 101
``mcp_servers:`` entry.

Trust tier is ``COMMUNITY`` (opt-in only): Smithery hosts arbitrary
community-authored servers. ``mdk mcp add`` from here writes
``credentials_ref: apikey-query:api_key=SMITHERY_API_KEY`` so the key stays in
the env, never in the stored URL. The registry listing itself is unauthenticated
(discovery needs no key); only *connecting* to a server does.
"""

from __future__ import annotations

import logging
from typing import Any

from movate.mcp_catalog.models import CatalogEntry, TrustTier

_log = logging.getLogger(__name__)

_REGISTRY_URL = "https://registry.smithery.ai/servers"
_GATEWAY = "https://server.smithery.ai"
_TIMEOUT_S = 8.0
# The credential spec mdk writes for a Smithery entry — query-param auth,
# key resolved from this env var at connect time (see _apply_http_auth).
_CREDENTIALS = "apikey-query:api_key=SMITHERY_API_KEY"


def _map(rec: dict[str, Any]) -> CatalogEntry | None:
    """Map a Smithery registry record → CatalogEntry (or None if unusable)."""
    qname = rec.get("qualifiedName")
    if not isinstance(qname, str) or not qname:
        return None
    # Slug: last path segment of the qualifiedName, hyphen-normalized.
    import re  # noqa: PLC0415

    slug = re.sub(r"[^a-z0-9-]+", "-", qname.rsplit("/", 1)[-1].lower()).strip("-")
    if not slug or not slug[0].isalpha():
        return None
    return CatalogEntry(
        name=slug,
        title=str(rec.get("displayName") or qname),
        description=str(rec.get("description") or ""),
        transport="http",
        entry=f"{_GATEWAY}/{qname}/mcp",
        credentials=_CREDENTIALS,
        homepage=str(rec.get("homepage") or f"https://smithery.ai/server/{qname}"),
        tags=[],
        source="smithery",
        trust=TrustTier.COMMUNITY,
        publisher=qname.rsplit("/", 1)[0] if "/" in qname else None,
        pinned=True,  # hosted gateway endpoint — the URL is the version
    )


class SmitherySource:
    """The Smithery hosted-MCP registry (ADR 104 D4)."""

    name = "smithery"
    trust = TrustTier.COMMUNITY

    async def search(self, query: str, *, limit: int = 25) -> list[CatalogEntry]:
        records = await self._fetch(query, limit)
        out: list[CatalogEntry] = []
        for rec in records:
            mapped = _map(rec)
            if mapped is not None:
                out.append(mapped)
        return out[:limit]

    async def get(self, ref: str) -> CatalogEntry | None:
        for e in await self.search(ref, limit=50):
            if e.name == ref:
                return e
        hits = await self.search(ref, limit=1)
        return hits[0] if hits else None

    async def _fetch(self, query: str, limit: int) -> list[dict[str, Any]]:
        """GET the Smithery registry listing. Fail-soft → [] on any error."""
        import httpx  # noqa: PLC0415

        params: dict[str, Any] = {"pageSize": limit}
        if query:
            params["q"] = query
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
                resp = await client.get(_REGISTRY_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # network / HTTP / JSON — fail-soft
            _log.warning("smithery registry unavailable: %s", exc)
            return []
        rows = data.get("servers") if isinstance(data, dict) else data
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
