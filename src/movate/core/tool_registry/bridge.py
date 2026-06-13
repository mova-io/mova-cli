"""Bridge: convert a resolved ToolDescriptor to a SkillBundle.

This is the load-time step that turns a registry tool into the same
``SkillBundle`` shape the executor already consumes. The runtime
dispatches it through the unchanged ``SkillBackend`` dispatch
(``dispatch_skill``), so the executor never knows whether a skill came
from a per-agent ``skills/`` directory or from the shared tool registry.

ADR 052 D1: "Resolution is a build-time / load-time step that turns a
``name@version`` reference into a concrete ``SkillSpec``; from there the
runtime is unchanged."
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from movate.core.models import (
    SchemaPaths,
    SkillCost,
    SkillImplementation,
    SkillImplementationKind,
    SkillSpec,
)
from movate.core.skill_loader import SkillBundle
from movate.core.tool_registry.models import ToolDescriptor


def _backend_kind_to_skill_kind(kind: str) -> SkillImplementationKind:
    """Map a tool backend kind to a SkillImplementationKind."""
    mapping = {
        "mcp": SkillImplementationKind.MCP,
        "exec": SkillImplementationKind.EXEC,
        "http": SkillImplementationKind.HTTP,
    }
    return mapping.get(kind, SkillImplementationKind.PYTHON)


def tool_descriptor_to_skill_bundle(
    descriptor: ToolDescriptor,
) -> SkillBundle:
    """Convert a ``ToolDescriptor`` to a ``SkillBundle``.

    The resulting SkillBundle has the same shape as one loaded from a
    ``skills/<name>/skill.yaml`` directory, so the executor's tool-use
    loop dispatches it identically.
    """
    kind = _backend_kind_to_skill_kind(descriptor.backend.kind)
    config = descriptor.backend.config

    # Build the SkillImplementation from the backend config.
    impl_kwargs: dict[str, Any] = {
        "kind": kind,
        "entry": config.get("entry", ""),
    }

    # Copy kind-specific fields.
    if kind == SkillImplementationKind.MCP:
        impl_kwargs["tool"] = config.get("tool", "")
        # ADR 101 D3: an optional ``bearer-from-env:VAR`` auth spec for an
        # HTTP MCP server (ignored by the stdio transport). Reuses the same
        # ``auth`` field + resolver the HTTP backend uses.
        if config.get("auth"):
            impl_kwargs["auth"] = config["auth"]
    elif kind == SkillImplementationKind.HTTP:
        impl_kwargs["method"] = config.get("method", "POST")
        impl_kwargs["auth"] = config.get("auth")
        impl_kwargs["headers"] = config.get("headers", {})
        impl_kwargs["timeout_seconds"] = config.get("timeout_seconds")

    # Use a permissive SkillSpec name: tool registry names are dotted
    # (e.g. "jira.create-issue") but SkillSpec requires lowercase
    # alphanumeric with hyphens. Convert dots to hyphens.
    skill_name = descriptor.name.replace(".", "-")

    # Build the input/output schemas. Use permissive defaults.
    input_schema = descriptor.input_schema or {"type": "object"}
    output_schema = descriptor.output_schema or {"type": "object"}

    spec = SkillSpec(
        api_version="movate/v1",
        kind="Skill",
        name=skill_name,
        version=descriptor.version,
        description=descriptor.description,
        owner=descriptor.owner or "",
        schema=SchemaPaths(input=input_schema, output=output_schema),
        implementation=SkillImplementation(**impl_kwargs),
        cost=SkillCost(),
        tags=descriptor.tags,
    )

    return SkillBundle(
        spec=spec,
        skill_dir=Path("."),  # No physical directory for registry tools.
        input_schema=input_schema,
        output_schema=output_schema,
        input_validator=Draft202012Validator(input_schema),
        output_validator=Draft202012Validator(output_schema),
    )
