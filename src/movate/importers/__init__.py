"""Importers — turn external descriptions into movate config (Sprint P).

Today: OpenAPI specs → one skill per operation. Future: maybe Postman
collections, gRPC reflect output, MCP server manifests. The pattern is
the same — a pure parsing function lifts the external format into a
:class:`OperationSpec` (or peer), and a pure generator lowers that into
the on-disk skill.yaml + impl shape.

Keeping the parser + generator pure means:

* Easy to unit-test (no filesystem mocks).
* Easy to reuse for ``mdk validate`` style preflight (operators can
  ask "would this generate cleanly?" before committing to a write).
* Easy to swap output formats later (skill.yaml today, agent.yaml
  for a future "import this whole tool registry as one agent" use
  case).
"""

from __future__ import annotations

from movate.importers.openapi import (
    OpenAPIParseError,
    OperationSpec,
    parse_openapi,
    skill_yaml_for,
)

__all__ = [
    "OpenAPIParseError",
    "OperationSpec",
    "parse_openapi",
    "skill_yaml_for",
]
