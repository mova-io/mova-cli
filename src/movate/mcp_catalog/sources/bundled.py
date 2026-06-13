"""Bundled catalog source (ADR 103 D1 / 104 D1) — the CURATED, offline default.

Reads the in-repo ``catalog.yaml`` once and serves it. No network, highest
trust: an entry is here only because it was reviewed in-repo.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml

from movate.mcp_catalog.models import CatalogEntry, TrustTier

_CATALOG_PATH = Path(__file__).resolve().parent.parent / "catalog.yaml"


@functools.lru_cache(maxsize=1)
def _load_entries() -> tuple[CatalogEntry, ...]:
    """Parse the bundled catalog.yaml into validated entries (cached)."""
    raw: Any = yaml.safe_load(_CATALOG_PATH.read_text()) or {}
    rows = raw.get("servers") or []
    entries: list[CatalogEntry] = []
    for row in rows:
        # provenance is fixed for the bundled source — set it here, not in YAML.
        entries.append(
            CatalogEntry(
                **row,
                source="bundled",
                trust=TrustTier.CURATED,
                pinned=_looks_pinned(row.get("entry", "")),
            )
        )
    return tuple(entries)


def _looks_pinned(entry: str) -> bool:
    """Heuristic: a stdio command pinning ``@<version>`` or any http URL counts."""
    if entry.lower().startswith(("http://", "https://")):
        return True
    # npm-style pin: a token containing ``@<something>`` after the package name.
    return "@" in entry.rsplit("/", maxsplit=1)[-1] or "@" in entry.rsplit(" ", 1)[-1]


class BundledSource:
    """The curated, in-repo catalog (ADR 103 D1)."""

    name = "bundled"
    trust = TrustTier.CURATED

    async def search(self, query: str, *, limit: int = 25) -> list[CatalogEntry]:
        hits = [e for e in _load_entries() if e.matches(query)]
        return hits[:limit]

    async def get(self, ref: str) -> CatalogEntry | None:
        for e in _load_entries():
            if e.name == ref:
                return e
        return None
