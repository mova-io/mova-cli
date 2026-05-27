"""The ``AuthoringAction`` protocol + the shared ``AuthoringContext`` (ADR 025 D1).

An :class:`AuthoringAction` is the core seam of the authoring copilot: a typed,
self-describing, validated, reversible operation that mutates an agent/project
by composing **existing shipped primitives** — never raw filesystem writes,
shell, or arbitrary code (D8). Each action declares its ``name``, an LLM-facing
``description``, an ``args_model`` (pydantic input schema), its ``side_effects``
and whether it is ``reversible``, and implements two methods:

* :meth:`AuthoringAction.plan` — a dry-run :class:`~movate.authoring.models.ActionPlan`
  (diff + cost/side-effect estimate). **No writes.**
* :meth:`AuthoringAction.apply` — executes via the existing primitive and
  returns an :class:`~movate.authoring.models.ActionResult`.

The protocol is intentionally minimal so PR3 (the LLM planner) and PR4 (the MCP
server) can drive the *same* catalog the thin CLI does, with no behavioral drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from movate.authoring.models import ActionPlan, ActionResult, SideEffect

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

    from movate.storage.base import StorageProvider


class AuthoringActionError(Exception):
    """Raised by an action's ``plan``/``apply`` when it cannot proceed.

    Carries an operator-facing message. The driver maps this to a structured
    failure rather than a raw traceback so a caller (PR3) can react.
    """


@dataclass
class AuthoringContext:
    """The project + injected dependencies an action operates against.

    The library takes its collaborators by injection so the whole catalog is
    hermetic and testable: the verify loop's storage / mock-run, and the
    networked KB-ingest path, never reach for ``~/.movate`` or real keys
    unless a caller (the thin CLI, PR3) explicitly wires them.

    Attributes
    ----------
    project:
        The project root (or a single-agent dir). Actions resolve
        ``agents/<name>/`` and project-level ``contexts/`` / ``skills/``
        beneath it, mirroring the rest of mdk.
    storage:
        Optional :class:`StorageProvider` for actions that persist (KB
        ingest). When ``None`` an action that needs it raises rather than
        silently building a ``~/.movate`` SQLite store.
    api_key:
        Optional provider/embedding API key for cost-bearing actions. The
        catalog never reads it from the environment itself — the caller
        injects it after the confirmation gate.
    complete_fn:
        Optional LLM completion callable, only used by actions that *can*
        optionally enrich (e.g. KB graph extraction). Never required.
    """

    project: Path
    storage: StorageProvider | None = None
    api_key: str | None = None
    complete_fn: Callable[..., Any] | None = field(default=None)

    def agent_dir(self, agent: str) -> Path:
        """Resolve ``<project>/agents/<agent>/`` (does not assert existence)."""
        return (self.project / "agents" / agent).resolve()

    def agent_yaml(self, agent: str) -> Path:
        """Resolve ``<project>/agents/<agent>/agent.yaml``."""
        return self.agent_dir(agent) / "agent.yaml"


@runtime_checkable
class AuthoringAction(Protocol):
    """The typed, reversible, validated authoring operation seam (D1).

    Implementations are plain classes registered into the catalog
    (:mod:`movate.authoring.catalog`). They must be free of any LLM
    dependency — the planner that maps natural language to actions arrives
    in PR3 and consumes this protocol from the outside.
    """

    #: Stable identifier, used by the CLI / MCP tool name / planner.
    name: str
    #: LLM-facing description — what the action does + when to use it.
    description: str
    #: The side effects applying this action has (drives the confirm gate).
    side_effects: tuple[SideEffect, ...]
    #: Whether an apply can be cleanly reverted via a checkpoint.
    reversible: bool
    #: The pydantic model the action's ``args`` dict is validated against.
    args_model: type[BaseModel]

    # ``args`` is typed ``Any`` (not the concrete per-action model) so a class
    # whose ``plan``/``apply`` narrows to its own ``*Args`` type still satisfies
    # the Protocol (a narrower parameter type would otherwise violate LSP). The
    # driver validates the dict against ``args_model`` before calling, so the
    # concrete type is sound at the call site.
    def plan(self, ctx: AuthoringContext, args: Any) -> ActionPlan:
        """Produce a dry-run plan (diff + estimate). MUST NOT write to disk."""
        ...

    def apply(self, ctx: AuthoringContext, args: Any) -> ActionResult:
        """Execute the action via the shipped primitive. May write to disk."""
        ...


class BaseAuthoringAction:
    """Optional convenience base for catalog actions.

    Declares the catalog attributes with their *broad* Protocol types so a
    concrete action's class-level overrides (e.g. ``args_model = AddContextArgs``)
    are checked covariantly against ``type[BaseModel]`` rather than inferred as
    an invariant concrete type. Inheriting is not required to satisfy
    :class:`AuthoringAction` (it's structural) — it just keeps the type checker
    happy without per-attribute annotations on every action.
    """

    name: str
    description: str
    side_effects: tuple[SideEffect, ...] = ()
    reversible: bool = True
    args_model: type[BaseModel]


def self_description(action: AuthoringAction) -> dict[str, Any]:
    """Serialize an action's declared metadata + its arg JSON schema.

    Used by ``mdk authoring list`` and (PR3/PR4) the planner system prompt /
    MCP tool manifest — the catalog is self-describing, so there is never a
    hand-maintained tool list to drift from the code.
    """
    return {
        "name": action.name,
        "description": action.description,
        "side_effects": [s.value for s in action.side_effects],
        "reversible": action.reversible,
        "args_schema": action.args_model.model_json_schema(),
    }
