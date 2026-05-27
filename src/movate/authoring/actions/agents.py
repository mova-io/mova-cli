"""Add-agent + compose-workflow catalog actions (ADR 025 D1).

* **add-agent** composes the same primitive ``mdk add`` uses: resolve a role
  template via :func:`movate.templates.get_template_path`, copy it into
  ``agents/<name>/``, and substitute the agent-name placeholder.
* **compose-workflow** composes ``mdk compose``'s
  :func:`movate.cli.compose_cmd._scaffold_workflow_yaml` to build the
  ``workflow.yaml`` dict, then writes it via ``yaml.safe_dump`` (the same dump
  ``mdk compose`` does).

Neither invents a parallel writer.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, ConfigDict, Field

from movate.authoring.base import AuthoringActionError, AuthoringContext, BaseAuthoringAction
from movate.authoring.models import ActionPlan, ActionResult, SideEffect
from movate.cli.compose_cmd import _scaffold_workflow_yaml
from movate.templates import get_template_path, list_templates

if TYPE_CHECKING:
    from pathlib import Path

_AGENT_NAME_PLACEHOLDER = "__AGENT_NAME__"


class AddAgentArgs(BaseModel):
    """Args for :class:`AddAgentAction`."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="New agent name (directory under agents/).")
    template: str = Field(..., description="Role template to scaffold from (e.g. 'faq').")


class AddAgentAction(BaseAuthoringAction):
    """Add a role-templated agent to the project (the ``mdk add`` primitive).

    Additive + reversible + free. Refuses to overwrite an existing agent dir.
    """

    name = "add-agent"
    description = (
        "Add a new agent to the project from a role template (the `mdk add` "
        "scaffold). Additive and reversible. Use `add-agent` to grow a "
        "multi-agent project."
    )
    side_effects: tuple[SideEffect, ...] = (SideEffect.FILESYSTEM,)
    reversible = True
    args_model: type[BaseModel] = AddAgentArgs

    def _resolve_template(self, template: str) -> Path:
        try:
            return get_template_path(template)
        except ValueError as exc:
            raise AuthoringActionError(
                f"unknown template {template!r}; available: {', '.join(list_templates())}"
            ) from exc

    def plan(self, ctx: AuthoringContext, args: AddAgentArgs) -> ActionPlan:
        self._resolve_template(args.template)  # validate it exists (no write)
        dest = ctx.agent_dir(args.name)
        rel = str(dest.relative_to(ctx.project)) if dest.is_relative_to(ctx.project) else str(dest)
        exists = dest.exists()
        return ActionPlan(
            action=self.name,
            summary=(
                f"add agent {args.name!r} from template {args.template!r} at {rel}/"
                if not exists
                else f"agent {args.name!r} already exists at {rel} (would refuse)"
            ),
            diff="",
            side_effects=list(self.side_effects),
            reversible=True,
            requires_confirmation=exists,
            details={"path": rel, "exists": exists, "template": args.template},
        )

    def apply(self, ctx: AuthoringContext, args: AddAgentArgs) -> ActionResult:
        template_dir = self._resolve_template(args.template)
        dest = ctx.agent_dir(args.name)
        if dest.exists():
            raise AuthoringActionError(f"agent {args.name!r} already exists at {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(template_dir, dest)
        yaml_path = dest / "agent.yaml"
        if yaml_path.is_file():
            yaml_path.write_text(yaml_path.read_text().replace(_AGENT_NAME_PLACEHOLDER, args.name))
        rel = str(dest.relative_to(ctx.project)) if dest.is_relative_to(ctx.project) else str(dest)
        return ActionResult(
            action=self.name,
            summary=f"added agent {args.name!r} (template {args.template!r})",
            changed_paths=[rel],
        )


class ComposeWorkflowArgs(BaseModel):
    """Args for :class:`ComposeWorkflowAction`."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Workflow name (directory under workflows/).")
    agents: list[str] = Field(
        ..., min_length=1, description="Agent names wired into a sequential workflow."
    )
    description: str = Field(default="", description="Optional workflow description.")
    runtime: str = Field(default="native", description="Workflow runtime ('native'|'langgraph').")


class ComposeWorkflowAction(BaseAuthoringAction):
    """Scaffold a multi-agent ``workflow.yaml`` (the ``mdk compose`` primitive).

    Additive + reversible + free. Wires the listed agents into a sequential
    workflow; the operator edits the result for branches/parallelism.
    """

    name = "compose-workflow"
    description = (
        "Scaffold a multi-agent workflow.yaml from a list of agents (the "
        "`mdk compose` primitive). Wires them sequentially. Additive and "
        "reversible."
    )
    side_effects: tuple[SideEffect, ...] = (SideEffect.FILESYSTEM,)
    reversible = True
    args_model: type[BaseModel] = ComposeWorkflowArgs

    def _target(self, ctx: AuthoringContext, name: str) -> Path:
        return (ctx.project / "workflows" / name / "workflow.yaml").resolve()

    def plan(self, ctx: AuthoringContext, args: ComposeWorkflowArgs) -> ActionPlan:
        target = self._target(ctx, args.name)
        rel = (
            str(target.relative_to(ctx.project))
            if target.is_relative_to(ctx.project)
            else str(target)
        )
        exists = target.exists()
        return ActionPlan(
            action=self.name,
            summary=(
                f"compose workflow {args.name!r} over {len(args.agents)} agent(s) at {rel}"
                if not exists
                else f"workflow {args.name!r} already exists at {rel} (would refuse)"
            ),
            diff="",
            side_effects=list(self.side_effects),
            reversible=True,
            requires_confirmation=exists,
            details={"path": rel, "exists": exists, "agents": args.agents},
        )

    def apply(self, ctx: AuthoringContext, args: ComposeWorkflowArgs) -> ActionResult:
        target = self._target(ctx, args.name)
        if target.exists():
            raise AuthoringActionError(f"workflow {args.name!r} already exists at {target}")
        spec = _scaffold_workflow_yaml(
            workflow_name=args.name,
            agent_names=args.agents,
            runtime=args.runtime,
            description=args.description,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True))
        rel = (
            str(target.relative_to(ctx.project))
            if target.is_relative_to(ctx.project)
            else str(target)
        )
        return ActionResult(
            action=self.name,
            summary=f"composed workflow {args.name!r} over {len(args.agents)} agent(s)",
            changed_paths=[rel],
        )
