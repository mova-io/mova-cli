"""Official MCP Registry source (ADR 104 D2) — the default live source.

Reads the canonical registry's HTTP API (``registry.modelcontextprotocol.io``)
and maps its ``server.json``-shaped records to :class:`CatalogEntry`. Because the
official registry is a *metaregistry*, this one adapter surfaces much of what the
ecosystem lists.

Resilience (ADR 104 D6): network failures are **fail-soft** — ``search`` returns
``[]`` and ``get`` returns ``None`` rather than raising, so a registry outage
degrades to "fewer results," never a broken author session. The mapping is
intentionally defensive: the registry schema evolves, so we read fields we
recognize and skip records we can't turn into a runnable entry.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from movate.mcp_catalog.models import CatalogEntry, TrustTier

_log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://registry.modelcontextprotocol.io"
_TIMEOUT_S = 8.0
_NAME_SANITIZE_RE = re.compile(r"[^a-z0-9-]+")


def _base_url() -> str:
    return os.environ.get("MOVATE_MCP_REGISTRY_URL", _DEFAULT_BASE_URL).rstrip("/")


def _slug(registry_name: str) -> str | None:
    """Derive a short, valid mcp_servers name from a namespaced registry name.

    e.g. ``io.github.modelcontextprotocol/servers-github`` → ``servers-github``.
    Returns None if the result can't be a valid (letter-led) name.
    """
    tail = registry_name.rsplit("/", maxsplit=1)[-1]
    slug = _NAME_SANITIZE_RE.sub("-", tail.lower()).strip("-")
    if not slug or not slug[0].isalpha():
        return None
    return slug[:64]


def _map_record(rec: dict[str, Any]) -> CatalogEntry | None:
    """Map one registry server record → CatalogEntry, or None if unrunnable."""
    reg_name = rec.get("name")
    if not isinstance(reg_name, str) or not reg_name:
        return None
    slug = _slug(reg_name)
    if slug is None:
        return None

    description = str(rec.get("description") or "")
    version = rec.get("version")

    transport: str
    entry: str
    credentials: str | None = None

    remotes = rec.get("remotes")
    packages = rec.get("packages")

    if isinstance(remotes, list) and remotes:
        # HTTP/SSE transport — take the first remote with a URL.
        remote = next((r for r in remotes if isinstance(r, dict) and r.get("url")), None)
        if remote is None:
            return None
        transport = "http"
        entry = str(remote["url"])
        credentials = _credential_hint(remote.get("environment_variables"))
    elif isinstance(packages, list) and packages:
        pkg = next((p for p in packages if isinstance(p, dict) and p.get("identifier")), None)
        if pkg is None:
            return None
        reg_type = str(pkg.get("registry_type") or pkg.get("registryType") or "npm").lower()
        ident = str(pkg["identifier"])
        pkg_version = pkg.get("version") or version
        transport = "stdio"
        entry = _stdio_command(reg_type, ident, pkg_version)
        if entry == "":
            return None
        credentials = _credential_hint(pkg.get("environment_variables"))
    else:
        return None  # neither a remote nor a runnable package

    return CatalogEntry(
        name=slug,
        title=reg_name.split("/")[-1],
        description=description,
        transport=transport,  # type: ignore[arg-type]
        entry=entry,
        credentials=credentials,
        homepage=str(
            rec.get("repository", {}).get("url", "")
            if isinstance(rec.get("repository"), dict)
            else ""
        ),
        tags=[],
        source="official",
        trust=TrustTier.OFFICIAL,
        publisher=reg_name.rsplit("/", 1)[0] if "/" in reg_name else None,
        pinned=bool(version) or "@" in entry,
    )


def _stdio_command(registry_type: str, identifier: str, version: Any) -> str:
    """Build a pinned stdio launch command for a known package registry type."""
    ver = f"@{version}" if version else ""
    if registry_type in ("npm", "node"):
        return f"npx -y {identifier}{ver}"
    if registry_type in ("pypi", "python"):
        # uvx runs a pinned Python package without a global install.
        return f"uvx {identifier}{('==' + str(version)) if version else ''}"
    if registry_type in ("oci", "docker"):
        return f"docker run -i --rm {identifier}{(':' + str(version)) if version else ''}"
    return ""  # unknown packaging — skip rather than emit something unrunnable


def _credential_hint(env_vars: Any) -> str | None:
    """Map a record's environment_variables to a bearer-from-env hint.

    Picks the first secret/required env var as the auth hint so ``mdk mcp add``
    can tell the author which secret to set. Best-effort: the registry shape is
    advisory.
    """
    if not isinstance(env_vars, list):
        return None
    for ev in env_vars:
        if not isinstance(ev, dict):
            continue
        var = ev.get("name")
        if isinstance(var, str) and var and (ev.get("isSecret") or ev.get("isRequired")):
            return f"bearer-from-env:{var}"
    # fall back to the first named var if none flagged
    for ev in env_vars:
        if isinstance(ev, dict) and isinstance(ev.get("name"), str) and ev["name"]:
            return f"bearer-from-env:{ev['name']}"
    return None


class OfficialRegistrySource:
    """The canonical MCP registry (ADR 104 D2)."""

    name = "official"
    trust = TrustTier.OFFICIAL

    async def search(self, query: str, *, limit: int = 25) -> list[CatalogEntry]:
        records = await self._fetch_servers(query, limit)
        out: list[CatalogEntry] = []
        for rec in records:
            mapped = _map_record(rec)
            if mapped is not None:
                out.append(mapped)
        return out[:limit]

    async def get(self, ref: str) -> CatalogEntry | None:
        # The registry keys by namespaced name; resolve by searching and
        # matching our derived slug exactly (fall back to first hit).
        hits = await self.search(ref, limit=50)
        for h in hits:
            if h.name == ref:
                return h
        return hits[0] if hits else None

    async def _fetch_servers(self, query: str, limit: int) -> list[dict[str, Any]]:
        """GET the registry's servers list. Fail-soft → [] on any error."""
        import httpx  # noqa: PLC0415

        params: dict[str, Any] = {"limit": limit}
        if query:
            params["search"] = query
        url = f"{_base_url()}/v0/servers"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # network / HTTP / JSON — all fail-soft
            _log.warning("official MCP registry unavailable (%s): %s", url, exc)
            return []
        # The API returns {"servers": [...]} (records may nest under "server").
        rows = data.get("servers", data) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return []
        out: list[dict[str, Any]] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            inner = r.get("server")
            out.append(inner if isinstance(inner, dict) else r)
        return out
