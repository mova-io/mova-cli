"""Edit-instructions catalog action — rewrite an agent's ``prompt.md`` (ADR 025 D1).

Resolves the prompt file via the agent.yaml ``prompt:`` reference (the same
field :func:`movate.core.loader.load_agent` reads) so the action edits exactly
the file the runtime renders — never a guessed path.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from movate.authoring._diff import unified
from movate.authoring._yaml_edit import load_agent_yaml
from movate.authoring.base import AuthoringActionError, AuthoringContext, BaseAuthoringAction
from movate.authoring.models import ActionPlan, ActionResult, SideEffect


def _prompt_path(ctx: AuthoringContext, agent: str) -> Path:
    """Resolve the agent's prompt file from its agent.yaml ``prompt:`` ref."""
    agent_dir = ctx.agent_dir(agent)
    data = load_agent_yaml(agent_dir / "agent.yaml")
    prompt_ref = str(data.get("prompt", "./prompt.md"))
    return (agent_dir / prompt_ref).resolve()


class EditInstructionsArgs(BaseModel):
    """Args for :class:`EditInstructionsAction`."""

    model_config = ConfigDict(extra="forbid")

    agent: str = Field(..., description="Agent whose prompt.md is rewritten.")
    body: str = Field(..., description="The new full prompt/instructions body.")


class EditInstructionsAction(BaseAuthoringAction):
    """Replace the agent's ``prompt.md`` body.

    The most common evolution ("make the tone more formal", "add a step").
    Reversible via checkpoint; purely a filesystem edit.
    """

    name = "edit-instructions"
    description = (
        "Rewrite the agent's instructions (prompt.md). Use to change tone, add "
        "steps, or refine behavior. Replaces the full prompt body. Reversible."
    )
    side_effects: tuple[SideEffect, ...] = (SideEffect.FILESYSTEM,)
    reversible = True
    args_model: type[BaseModel] = EditInstructionsArgs

    def plan(self, ctx: AuthoringContext, args: EditInstructionsArgs) -> ActionPlan:
        path = _prompt_path(ctx, args.agent)
        rel = str(path.relative_to(ctx.project)) if path.is_relative_to(ctx.project) else str(path)
        old = path.read_text(encoding="utf-8") if path.is_file() else ""
        diff = unified(old, args.body, rel_path=rel)
        return ActionPlan(
            action=self.name,
            summary=f"edit instructions (prompt.md) for agent {args.agent!r}",
            diff=diff,
            side_effects=list(self.side_effects),
            reversible=True,
            requires_confirmation=False,
            details={"path": rel},
        )

    def apply(self, ctx: AuthoringContext, args: EditInstructionsArgs) -> ActionResult:
        path = _prompt_path(ctx, args.agent)
        if not path.parent.is_dir():
            raise AuthoringActionError(f"agent not found: {args.agent}")
        path.write_text(args.body, encoding="utf-8")
        rel = str(path.relative_to(ctx.project)) if path.is_relative_to(ctx.project) else str(path)
        return ActionResult(
            action=self.name,
            summary=f"rewrote instructions for {args.agent!r}",
            changed_paths=[rel],
        )
