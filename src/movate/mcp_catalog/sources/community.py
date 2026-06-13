"""Community directory sources (ADR 104 D4): mcp.so, Glama.

These are broad community catalogs. They are **COMMUNITY** trust — opt-in only
(never in the default source set), and every imported entry is pinned-or-warned
by ``mdk mcp add``. Importing still only writes a *declaration*; nothing runs.

Honest status: the exact upstream API shapes for these directories are validated
against the live services during end-to-end testing, not from here. So:

* The base URL of each is **configurable** via an env var (so it can be pointed
  at the real endpoint / a mirror / a fixture without a code change).
* The record mapping (:func:`_map_directory_record`) is defensive and
  fixture-tested — it reads the fields we recognize and skips anything it can't
  turn into a runnable entry.
* Every fetch is **fail-soft**: a wrong/unreachable endpoint yields zero results
  and a warning, never a crash. A community source contributing nothing degrades
  discovery gracefully.

This keeps the seam real and safe today; the live wiring is a config/mapping
tune-up, not a redesign.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from movate.mcp_catalog.models import CatalogEntry, TrustTier

_log = logging.getLogger(__name__)
_TIMEOUT_S = 8.0
_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _slug(value: str) -> str | None:
    """Reduce a directory's id/name to a valid mcp_servers name, or None."""
    tail = value.rsplit("/", maxsplit=1)[-1]
    slug = _SLUG_RE.sub("-", tail.lower()).strip("-")
    if not slug or not slug[0].isalpha():
        return None
    return slug[:64]


def _map_directory_record(rec: dict[str, Any], *, source: str) -> CatalogEntry | None:
    """Map a generic community-directory record → CatalogEntry, or None.

    Defensive across the field names community directories tend to use
    (``name``/``slug``/``qualifiedName``, ``description``, ``repository``/
    ``url``/``homepage``, an install ``command`` or npm ``package``, or a remote
    ``url`` for HTTP transport). Returns None when no runnable entry is derivable.
    """
    raw_name = rec.get("name") or rec.get("slug") or rec.get("qualifiedName") or rec.get("id")
    if not isinstance(raw_name, str) or not raw_name:
        return None
    slug = _slug(raw_name)
    if slug is None:
        return None

    description = str(rec.get("description") or rec.get("summary") or "")
    homepage = str(rec.get("homepage") or rec.get("repository") or rec.get("url") or "")
    if isinstance(rec.get("repository"), dict):
        homepage = str(rec["repository"].get("url") or homepage)

    transport: str
    entry: str

    # 1) explicit launch command, 2) npm package, 3) remote http url.
    command = rec.get("command") or rec.get("install")
    package = rec.get("package") or rec.get("npm")
    remote_url = rec.get("serverUrl") or rec.get("remoteUrl")
    if isinstance(command, str) and command.strip():
        transport, entry = "stdio", command.strip()
    elif isinstance(package, str) and package.strip():
        ver = rec.get("version")
        transport = "stdio"
        entry = f"npx -y {package.strip()}" + (f"@{ver}" if ver else "")
    elif isinstance(remote_url, str) and remote_url.lower().startswith(("http://", "https://")):
        transport, entry = "http", remote_url
    else:
        return None

    pinned = transport == "http" or "@" in entry.rsplit("/", maxsplit=1)[-1]
    return CatalogEntry(
        name=slug,
        title=str(rec.get("title") or raw_name.split("/")[-1]),
        description=description,
        transport=transport,  # type: ignore[arg-type]
        entry=entry,
        homepage=homepage,
        tags=[t for t in (rec.get("tags") or []) if isinstance(t, str)],
        source=source,
        trust=TrustTier.COMMUNITY,
        publisher=str(rec.get("author") or rec.get("owner") or "") or None,
        pinned=pinned,
    )


class _CommunitySource:
    """Shared implementation for a configurable community directory."""

    name: str
    trust = TrustTier.COMMUNITY
    _base_url_env: str
    _default_base_url: str
    _search_path: str = "/servers"

    def _base_url(self) -> str:
        return os.environ.get(self._base_url_env, self._default_base_url).rstrip("/")

    async def search(self, query: str, *, limit: int = 25) -> list[CatalogEntry]:
        records = await self._fetch(query, limit)
        out: list[CatalogEntry] = []
        for rec in records:
            mapped = _map_directory_record(rec, source=self.name)
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
        """GET the directory's listing. Fail-soft → [] on any error."""
        import httpx  # noqa: PLC0415

        params: dict[str, Any] = {"limit": limit}
        if query:
            params["q"] = query
        url = f"{self._base_url()}{self._search_path}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # network / HTTP / JSON — fail-soft
            _log.warning("community source %s unavailable (%s): %s", self.name, url, exc)
            return []
        for key in ("servers", "data", "results", "items"):
            if isinstance(data, dict) and isinstance(data.get(key), list):
                return [r for r in data[key] if isinstance(r, dict)]
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


class GlamaSource(_CommunitySource):
    """Glama MCP directory (ADR 104 D4). Opt-in; endpoint configurable for E2E."""

    name = "glama"
    _base_url_env = "MOVATE_GLAMA_URL"
    _default_base_url = "https://glama.ai/api/mcp/v1"


class McpSoSource(_CommunitySource):
    """mcp.so community directory (ADR 104 D4). Opt-in; endpoint configurable for E2E."""

    name = "mcp.so"
    _base_url_env = "MOVATE_MCP_SO_URL"
    _default_base_url = "https://mcp.so/api"
