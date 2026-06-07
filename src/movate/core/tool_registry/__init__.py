"""Tool Registry — a control-plane catalog of tool descriptors (ADR 052).

The registry is a *catalog of descriptors*. It never executes a tool.
A descriptor names a tool, its version, its I/O JSON-Schema, and *which
backend reaches it* — but the actual call still happens in the runtime,
through the existing ``SkillBackend`` dispatch.

Phase 1: ``ToolDescriptor`` + ``ToolResolver`` for tenant and project
scopes, the ``exec`` backend, storage methods, ``/api/v1/tools``
endpoints, and ``mdk tools`` CLI.
"""

from movate.core.tool_registry.models import (
    ToolBackendConfig,
    ToolDescriptor,
    ToolGovernance,
    ToolScope,
)
from movate.core.tool_registry.resolver import (
    ToolResolutionError,
    ToolResolver,
)

__all__ = [
    "ToolBackendConfig",
    "ToolDescriptor",
    "ToolGovernance",
    "ToolResolutionError",
    "ToolResolver",
    "ToolScope",
]
