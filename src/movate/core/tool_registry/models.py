"""ToolDescriptor — the unit of the tool registry (ADR 052 D1).

A typed, versioned, governed pointer to a capability. Agents depend on
tools the way a package manifest depends on libraries: ``name@version``
references are late-bound at load time by the ``ToolResolver``.

The descriptor hides the implementation behind the normalized
JSON-in / JSON-out contract that already governs every skill.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

# Tool name regex: dotted segments of lowercase alphanumeric + hyphens.
# e.g. "servicenow.incident.create", "jira.create-issue"
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*)*$")


class ToolScope(StrEnum):
    """Registry tier — mirrors ADR 041's catalog source model."""

    MOVATE = "movate"
    """Movate-curated tools (the connector pack). Cached in customer storage."""

    TENANT = "tenant"
    """Customer's own org-wide tools. Customer storage, never synced upward."""

    PROJECT = "project"
    """A single project's tools. Customer storage, project_id scoped."""


class ToolBackendConfig(BaseModel):
    """How the runtime reaches the tool (ADR 052 D2).

    ``kind`` selects one of the three reach mechanisms: mcp, exec, http.
    ``config`` carries kind-specific parameters (e.g. ``entry`` for exec,
    ``entry`` + ``tool`` for mcp, ``entry`` + ``method`` for http).
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["mcp", "exec", "http"] = Field(
        ...,
        description=(
            "Which SkillBackend implementation reaches this tool. "
            "``mcp`` = MCP server (existing MCPSkillBackend), "
            "``exec`` = raw-script escape hatch (new ExecSkillBackend), "
            "``http`` = REST endpoint (existing HttpSkillBackend)."
        ),
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Kind-specific configuration. For ``exec``: ``{entry: 'cmd', "
            "timeout_s: 30, network: false}``. For ``mcp``: ``{entry: "
            "'npx -y @pkg', tool: 'name'}``. For ``http``: ``{entry: "
            "'https://...', method: 'POST'}``."
        ),
    )


class ToolGovernance(BaseModel):
    """Governance metadata on a tool descriptor (ADR 052 D6).

    Enforced at resolve and at run time, reusing existing seams.
    """

    model_config = ConfigDict(extra="forbid")

    mutating: bool = Field(
        default=False,
        description=(
            "When true, the tool writes to a system of record. "
            "Routes through the HUMAN/HITL node for approval."
        ),
    )
    default_grant: bool = Field(
        default=True,
        description=(
            "When true, the tool is available to all agents by default. "
            "When false, agents must be on the tool's allowlist."
        ),
    )


class ToolDescriptor(BaseModel):
    """A typed, versioned, governed pointer to a capability (ADR 052 D1).

    Agents depend on tools by ``name@version``; the ``ToolResolver``
    materializes the concrete ``SkillSpec`` at load time.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description=(
            "Dotted tool name, e.g. ``servicenow.incident.create``. "
            "Lowercase, dot-separated segments."
        ),
    )
    version: str = Field(
        ...,
        description="Semver version string (MAJOR.MINOR.PATCH).",
    )
    scope: ToolScope = Field(
        default=ToolScope.TENANT,
        description="Registry tier: movate, tenant, or project.",
    )
    description: str = Field(
        default="",
        description="Human-readable description of what the tool does.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Searchable tags for discovery.",
    )
    input_schema: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema for the tool's input.",
    )
    output_schema: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema for the tool's output.",
    )
    backend: ToolBackendConfig = Field(
        ...,
        description="How the runtime reaches this tool (D2).",
    )
    credentials_ref: str | None = Field(
        default=None,
        description=(
            "Per-tenant credential reference resolved via ADR 018. "
            "e.g. ``'servicenow'``. Never inlined."
        ),
    )
    governance: ToolGovernance = Field(
        default_factory=ToolGovernance,
        description="Governance metadata (mutating flag, default grant).",
    )
    owner: str | None = Field(
        default=None,
        description="Owner email or team name.",
    )
    created_at: datetime | None = Field(
        default=None,
        description="When this descriptor was first published.",
    )
    updated_at: datetime | None = Field(
        default=None,
        description="When this descriptor was last updated.",
    )
    tenant_id: str = Field(
        default="local",
        description="Owning tenant. Set by the publish path.",
    )
    project_id: str | None = Field(
        default=None,
        description="Owning project (for project-scoped tools).",
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _TOOL_NAME_RE.match(v):
            raise ValueError(
                f"tool name {v!r} must be lowercase, dot-separated segments "
                f"of alphanumeric + hyphens (e.g. 'jira.create-issue')"
            )
        return v

    @field_validator("version")
    @classmethod
    def _validate_semver(cls, v: str) -> str:
        if not SEMVER_RE.match(v):
            raise ValueError(f"tool version {v!r} must be semver (MAJOR.MINOR.PATCH)")
        return v

    def stamp_now(self) -> ToolDescriptor:
        """Return a copy with created_at/updated_at stamped to now (UTC)."""
        now = datetime.now(UTC)
        return self.model_copy(
            update={
                "created_at": self.created_at or now,
                "updated_at": now,
            }
        )
