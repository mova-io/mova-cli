"""The mdk authoring action catalog + plan→apply→verify spine (ADR 025 PR1).

This package is the LLM-agnostic foundation of the authoring copilot: a typed,
self-describing, validated, reversible **catalog** of operations that evolve an
agent/project (add-context, edit-instructions, set-model, ingest-kb, …), plus a
**driver** that applies them safely (plan → confirm → checkpoint → apply →
verify → undo). It is usable programmatically and from the thin
``mdk authoring`` CLI today; PR3 (conversational ``mdk dev``) and PR4 (the MCP
server) will consume this same catalog with no behavioral drift.

There is **no LLM here** — the planner that maps natural language to catalog
actions arrives in PR3.

Boundaries (ADR 025 D8): actions compose only existing shipped primitives — no
raw filesystem writes outside the catalog, no shell, no arbitrary code. Every
agent.yaml mutation routes through the single canonical round-trip; every
mutation flows through ``validate`` + the registry + content-addressed
versioning. No auto-deploy, no credential / ``az`` access.

Public surface:

* :class:`AuthoringAction` / :class:`AuthoringContext` — the action protocol +
  the project + injected-deps it runs against.
* :mod:`movate.authoring.catalog` — ``get_action`` / ``list_actions`` /
  ``describe_catalog`` (the self-describing registry).
* :class:`AuthoringDriver` — the plan→apply→verify→undo spine.
* The value objects: :class:`ActionPlan`, :class:`ActionResult`,
  :class:`VerifyReport`, :class:`ActionLogEntry`, :class:`SideEffect`.
"""

from __future__ import annotations

from movate.authoring.base import (
    AuthoringAction,
    AuthoringActionError,
    AuthoringContext,
    self_description,
)
from movate.authoring.catalog import (
    UnknownActionError,
    action_names,
    describe_catalog,
    get_action,
    list_actions,
    register,
)
from movate.authoring.driver import (
    ApplyOutcome,
    AuthoringDriver,
    ConfirmationRequiredError,
)
from movate.authoring.models import (
    ActionLogEntry,
    ActionPlan,
    ActionResult,
    SideEffect,
    VerifyReport,
)

__all__ = [
    "ActionLogEntry",
    "ActionPlan",
    "ActionResult",
    "ApplyOutcome",
    "AuthoringAction",
    "AuthoringActionError",
    "AuthoringContext",
    "AuthoringDriver",
    "ConfirmationRequiredError",
    "SideEffect",
    "UnknownActionError",
    "VerifyReport",
    "action_names",
    "describe_catalog",
    "get_action",
    "list_actions",
    "register",
    "self_description",
]
