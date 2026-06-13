"""Catalog data model (ADR 103 D1).

A ``CatalogEntry`` is a discoverable MCP server: enough to (a) show an author
what it is and (b) write a correct, pinned ADR 101 ``mcp_servers:`` stanza. The
same model is produced by every source (the bundled catalog and external
registries), so the ``mdk mcp`` commands are source-agnostic.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Mirrors movate.core.models._MCP_SERVER_NAME_RE — a catalog entry's name is
# written verbatim as an mcp_servers `name:`, so it must be a valid one.
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


class TrustTier(StrEnum):
    """How much an entry's *source* is trusted (ADR 104 D5)."""

    CURATED = "curated"
    """Bundled, in-repo-reviewed (the connector pack lineage). Highest trust."""

    OFFICIAL = "official"
    """A canonical registry (official MCP registry, GitHub's). Trusted-by-default."""

    COMMUNITY = "community"
    """A community directory (mcp.so, Glama). Opt-in only; pin + warn."""


class CatalogEntry(BaseModel):
    """One discoverable MCP server (ADR 103 D1).

    ``entry`` is what gets written to ``mcp_servers:`` — a stdio command or an
    HTTP(S) URL, version-pinned where the ecosystem allows. ``credentials`` is
    the ADR 101 D3 ``bearer-from-env:VAR`` hint so ``mdk mcp add`` can tell the
    author exactly which secret to provision.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(..., min_length=1, max_length=64)
    title: str = Field(default="", description="Human-readable display name.")
    description: str = Field(default="", description="One-line summary.")
    transport: Literal["stdio", "http"] = Field(
        default="stdio", description="stdio command vs HTTP(S) URL."
    )
    entry: str = Field(
        ...,
        min_length=1,
        description="The mcp_servers entry: a stdio command or http(s):// URL (pinned).",
    )
    credentials: str | None = Field(
        default=None,
        description="ADR 101 D3 auth hint, e.g. 'bearer-from-env:GITHUB_TOKEN'. Advisory.",
    )
    homepage: str = Field(default="", description="Where to read more / verify the server.")
    tags: list[str] = Field(default_factory=list, description="Discovery facets.")
    tools_hint: list[str] = Field(
        default_factory=list,
        description="Advisory list of tools the server is expected to expose (not authoritative).",
    )

    # ---- provenance (set by the source, not authored in catalog.yaml) ----
    source: str = Field(default="bundled", description="Which source produced this entry.")
    trust: TrustTier = Field(default=TrustTier.CURATED, description="Trust tier of the source.")
    publisher: str | None = Field(default=None, description="Publisher / namespace, when known.")
    pinned: bool = Field(
        default=True,
        description="Whether ``entry`` pins a concrete version/digest (False → mdk mcp add warns).",
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(
                f"catalog entry name {v!r} must be lowercase, letter-start, "
                f"alphanumeric + hyphens (it is written verbatim as an "
                f"mcp_servers name)"
            )
        return v

    def matches(self, query: str) -> bool:
        """Case-insensitive substring match over name/title/description/tags."""
        q = query.lower().strip()
        if not q:
            return True
        haystack = " ".join([self.name, self.title, self.description, *self.tags]).lower()
        return q in haystack
