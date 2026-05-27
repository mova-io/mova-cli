"""Context catalog actions — add / edit / remove a shared context (ADR 025 D1).

Each reuses the shipped context primitives from
:mod:`movate.cli.contexts_cmd` — :func:`attach_context_to_agent` /
:func:`detach_context_from_agent` for the comment-preserving agent.yaml wiring,
and the same ``contexts/<name>.md`` write ``mdk contexts create`` performs.
No parallel writer is introduced.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from movate.authoring._diff import unified
from movate.authoring.base import AuthoringActionError, AuthoringContext, BaseAuthoringAction
from movate.authoring.models import ActionPlan, ActionResult, SideEffect
from movate.cli.contexts_cmd import (
    _CONTEXT_TEMPLATE,
    attach_context_to_agent,
    detach_context_from_agent,
)

if TYPE_CHECKING:
    from pathlib import Path


def _context_path(ctx: AuthoringContext, agent: str, name: str) -> Path:
    """Resolve the agent-local context file ``agents/<agent>/contexts/<name>.md``."""
    return ctx.agent_dir(agent) / "contexts" / f"{name}.md"


class AddContextArgs(BaseModel):
    """Args for :class:`AddContextAction`."""

    model_config = ConfigDict(extra="forbid")

    agent: str = Field(..., description="Agent whose prompt gains the context.")
    name: str = Field(..., description="Context name (stem, no extension).")
    body: str = Field(
        default="",
        description="Markdown body. Empty uses the standard starter template.",
    )


class AddContextAction(BaseAuthoringAction):
    """Create an agent-local context file + wire it into agent.yaml.

    Composes the same two primitives ``mdk contexts create --agent`` does: write
    ``agents/<agent>/contexts/<name>.md`` then
    :func:`attach_context_to_agent`. Additive + reversible + free → may
    auto-apply in fast mode.
    """

    name = "add-context"
    description = (
        "Create a new shared context (a Markdown file injected into the agent's "
        "system prompt) and wire it into the agent's agent.yaml `contexts:` list. "
        "Use to add policy/tone/domain background. Additive and reversible."
    )
    side_effects: tuple[SideEffect, ...] = (SideEffect.FILESYSTEM,)
    reversible = True
    args_model: type[BaseModel] = AddContextArgs

    def plan(self, ctx: AuthoringContext, args: AddContextArgs) -> ActionPlan:
        dest = _context_path(ctx, args.agent, args.name)
        rel = str(dest.relative_to(ctx.project)) if dest.is_relative_to(ctx.project) else str(dest)
        body = args.body or _CONTEXT_TEMPLATE.format(name=args.name)
        diff = unified("", body, rel_path=rel)
        already = dest.is_file()
        return ActionPlan(
            action=self.name,
            summary=(
                f"add context {args.name!r} to agent {args.agent!r} "
                + ("(file exists — would overwrite)" if already else "(new file + wire-in)")
            ),
            diff=diff,
            side_effects=list(self.side_effects),
            reversible=True,
            requires_confirmation=already,  # overwriting an existing file is a confirm-gated edit
            details={"path": rel, "exists": already},
        )

    def apply(self, ctx: AuthoringContext, args: AddContextArgs) -> ActionResult:
        agent_yaml = ctx.agent_yaml(args.agent)
        if not agent_yaml.is_file():
            raise AuthoringActionError(f"agent not found: {args.agent}")
        dest = _context_path(ctx, args.agent, args.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        body = args.body or _CONTEXT_TEMPLATE.format(name=args.name)
        dest.write_text(body, encoding="utf-8")
        attach_context_to_agent(agent_yaml, args.name)
        rel = str(dest.relative_to(ctx.project)) if dest.is_relative_to(ctx.project) else str(dest)
        return ActionResult(
            action=self.name,
            summary=f"added context {args.name!r} to {args.agent!r}",
            changed_paths=[rel, str(agent_yaml.relative_to(ctx.project))],
        )


class EditContextArgs(BaseModel):
    """Args for :class:`EditContextAction`."""

    model_config = ConfigDict(extra="forbid")

    agent: str = Field(..., description="Agent that owns the context.")
    name: str = Field(..., description="Context name (stem) to edit.")
    body: str = Field(..., description="The new full Markdown body.")


class EditContextAction(BaseAuthoringAction):
    """Replace an existing agent-local context's body (the prompt.md-style edit).

    Edits the file in place; does not touch agent.yaml (the wiring is unchanged).
    Reversible via checkpoint.
    """

    name = "edit-context"
    description = (
        "Replace the body of an existing context file. Use to refine the policy/"
        "background text injected into the agent's prompt. Reversible."
    )
    side_effects: tuple[SideEffect, ...] = (SideEffect.FILESYSTEM,)
    reversible = True
    args_model: type[BaseModel] = EditContextArgs

    def plan(self, ctx: AuthoringContext, args: EditContextArgs) -> ActionPlan:
        dest = _context_path(ctx, args.agent, args.name)
        rel = str(dest.relative_to(ctx.project)) if dest.is_relative_to(ctx.project) else str(dest)
        old = dest.read_text(encoding="utf-8") if dest.is_file() else ""
        diff = unified(old, args.body, rel_path=rel)
        return ActionPlan(
            action=self.name,
            summary=f"edit context {args.name!r} on agent {args.agent!r}",
            diff=diff,
            side_effects=list(self.side_effects),
            reversible=True,
            requires_confirmation=False,
            details={"path": rel, "exists": dest.is_file()},
        )

    def apply(self, ctx: AuthoringContext, args: EditContextArgs) -> ActionResult:
        dest = _context_path(ctx, args.agent, args.name)
        if not dest.is_file():
            raise AuthoringActionError(
                f"context {args.name!r} not found for agent {args.agent!r}; add it first"
            )
        dest.write_text(args.body, encoding="utf-8")
        rel = str(dest.relative_to(ctx.project)) if dest.is_relative_to(ctx.project) else str(dest)
        return ActionResult(
            action=self.name,
            summary=f"edited context {args.name!r} on {args.agent!r}",
            changed_paths=[rel],
        )


class RemoveContextArgs(BaseModel):
    """Args for :class:`RemoveContextAction`."""

    model_config = ConfigDict(extra="forbid")

    agent: str = Field(..., description="Agent to detach the context from.")
    name: str = Field(..., description="Context name (stem) to remove.")


class RemoveContextAction(BaseAuthoringAction):
    """Detach a context from agent.yaml (destructive → confirm-gated).

    Reuses :func:`detach_context_from_agent`. The context FILE is left on
    disk (matching ``mdk contexts detach``), so this is reversible — but it
    removes wiring, so it requires confirmation per D2.
    """

    name = "remove-context"
    description = (
        "Remove a context from the agent's agent.yaml `contexts:` list (the file "
        "stays on disk). Destructive to the agent's prompt composition; requires "
        "confirmation."
    )
    side_effects: tuple[SideEffect, ...] = (SideEffect.FILESYSTEM,)
    reversible = True
    args_model: type[BaseModel] = RemoveContextArgs

    def plan(self, ctx: AuthoringContext, args: RemoveContextArgs) -> ActionPlan:
        agent_yaml = ctx.agent_yaml(args.agent)
        rel = (
            str(agent_yaml.relative_to(ctx.project))
            if agent_yaml.is_relative_to(ctx.project)
            else str(agent_yaml)
        )
        return ActionPlan(
            action=self.name,
            summary=f"detach context {args.name!r} from agent {args.agent!r}",
            diff="",
            side_effects=list(self.side_effects),
            reversible=True,
            requires_confirmation=True,  # destructive (removal) — always confirm
            details={"agent_yaml": rel},
        )

    def apply(self, ctx: AuthoringContext, args: RemoveContextArgs) -> ActionResult:
        agent_yaml = ctx.agent_yaml(args.agent)
        if not agent_yaml.is_file():
            raise AuthoringActionError(f"agent not found: {args.agent}")
        removed = detach_context_from_agent(agent_yaml, args.name)
        return ActionResult(
            action=self.name,
            summary=(
                f"detached {args.name!r} from {args.agent!r}"
                if removed
                else f"{args.name!r} was not attached to {args.agent!r}"
            ),
            changed_paths=[str(agent_yaml.relative_to(ctx.project))] if removed else [],
        )
