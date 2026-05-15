"""``mdk docs`` — auto-generated documentation from project state.

For now, exposes :func:`generate_runbook` — an ops-friendly markdown
runbook compiled from ``movate.yaml`` + the agents directory + the
discovered env vars. Future siblings (``mdk docs api``,
``mdk docs changelog``) plug in here.

The generator is a pure function: given a :class:`RunbookContext`,
returns a markdown string. The CLI is a thin wrapper that builds
the context, calls the generator, and writes the output. Easy to
test; easy to wire into future ``mdk docs --bundle`` flows.
"""

from __future__ import annotations

from movate.docs.runbook import (
    AgentEntry,
    RunbookContext,
    build_context,
    generate_runbook,
)

__all__ = [
    "AgentEntry",
    "RunbookContext",
    "build_context",
    "generate_runbook",
]
