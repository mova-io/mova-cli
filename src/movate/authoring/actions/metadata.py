"""Describe / rename agent catalog action — edit agent.yaml metadata (ADR 025 D1).

Edits the ``name:`` and/or ``description:`` fields of ``agent.yaml`` through the
canonical round-trip (:mod:`movate.authoring._yaml_edit`). Renaming only touches
the ``name:`` field (the logical agent name the runtime registers under), NOT
the on-disk directory — moving the directory would break every relative
reference (skills/contexts/workflow ``ref:``), so that stays an explicit,
out-of-band operation.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from movate.authoring._diff import diff_for_file
from movate.authoring._yaml_edit import load_agent_yaml, render_agent_yaml, write_agent_yaml
from movate.authoring.base import AuthoringContext, BaseAuthoringAction
from movate.authoring.models import ActionPlan, ActionResult, SideEffect


def _rel(ctx: AuthoringContext, path: Any) -> str:
    return str(path.relative_to(ctx.project)) if path.is_relative_to(ctx.project) else str(path)


class DescribeAgentArgs(BaseModel):
    """Args for :class:`DescribeAgentAction`."""

    model_config = ConfigDict(extra="forbid")

    agent: str = Field(..., description="Agent (directory name) to edit.")
    description: str | None = Field(default=None, description="New description text.")
    new_name: str | None = Field(
        default=None,
        description="New logical `name:` value (does NOT move the directory).",
    )

    @model_validator(mode="after")
    def _at_least_one(self) -> DescribeAgentArgs:
        if self.description is None and self.new_name is None:
            raise ValueError("provide at least one of `description` or `new_name`")
        return self


class DescribeAgentAction(BaseAuthoringAction):
    """Set the agent's ``description`` and/or rename its logical ``name``.

    A pure metadata edit. Renaming the logical name is confirm-gated (it
    changes how the agent is registered/referenced); a description-only edit
    is free + additive. Reversible via checkpoint.
    """

    name = "describe-agent"
    description = (
        "Edit an agent's description and/or rename its logical name in "
        "agent.yaml. Renaming the logical name requires confirmation; it does "
        "not move the agent directory. Reversible."
    )
    side_effects: tuple[SideEffect, ...] = (SideEffect.FILESYSTEM,)
    reversible = True
    args_model: type[BaseModel] = DescribeAgentArgs

    def _mutate(self, data: dict[str, Any], args: DescribeAgentArgs) -> dict[str, Any]:
        data = dict(data)
        if args.description is not None:
            data["description"] = args.description
        if args.new_name is not None:
            data["name"] = args.new_name
        return data

    def plan(self, ctx: AuthoringContext, args: DescribeAgentArgs) -> ActionPlan:
        agent_yaml = ctx.agent_yaml(args.agent)
        data = load_agent_yaml(agent_yaml)
        new_text = render_agent_yaml(self._mutate(data, args))
        rel = _rel(ctx, agent_yaml)
        bits = []
        if args.new_name is not None:
            bits.append(f"rename → {args.new_name!r}")
        if args.description is not None:
            bits.append("update description")
        return ActionPlan(
            action=self.name,
            summary=f"{', '.join(bits)} for agent {args.agent!r}",
            diff=diff_for_file(agent_yaml, new_text, rel_path=rel),
            side_effects=list(self.side_effects),
            reversible=True,
            requires_confirmation=args.new_name is not None,
            details={"agent_yaml": rel},
        )

    def apply(self, ctx: AuthoringContext, args: DescribeAgentArgs) -> ActionResult:
        agent_yaml = ctx.agent_yaml(args.agent)
        data = load_agent_yaml(agent_yaml)
        write_agent_yaml(agent_yaml, self._mutate(data, args))
        return ActionResult(
            action=self.name,
            summary=f"updated metadata for {args.agent!r}",
            changed_paths=[_rel(ctx, agent_yaml)],
        )
