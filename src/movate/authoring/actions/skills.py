"""Add-skill catalog action — scaffold a starter skill (ADR 025 D1).

Composes the same primitive ``mdk skills scaffold`` uses: copy the packaged
``skill_init`` template into ``<project>/skills/<name>/`` and substitute the
skill name. The template + substitution rule are the shipped scaffold's, so the
catalog produces a byte-identical skill to the CLI command — it does not invent
a new skill writer.

Optionally wires the new skill into an agent's ``agent.yaml`` ``skills:`` list
through the canonical round-trip.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from movate.authoring._yaml_edit import load_agent_yaml, write_agent_yaml
from movate.authoring.base import AuthoringActionError, AuthoringContext, BaseAuthoringAction
from movate.authoring.models import ActionPlan, ActionResult, SideEffect
from movate.templates import TEMPLATES_DIR

# The files in the skill_init template carrying the name placeholder.
_TEMPLATED_FILES = ("skill.yaml", "impl.py", "README.md")
_NAME_PLACEHOLDER = "__SKILL_NAME__"


class AddSkillArgs(BaseModel):
    """Args for :class:`AddSkillAction`."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Skill name (lowercase, hyphen-separated).")
    agent: str | None = Field(
        default=None,
        description="Optional agent to wire the skill into (agent.yaml `skills:`).",
    )


class AddSkillAction(BaseAuthoringAction):
    """Scaffold a starter skill, optionally wiring it into an agent.

    Additive + reversible + free → may auto-apply in fast mode. The skill is a
    no-op echo until the operator edits ``impl.py``; wiring it into an agent
    just lists the name under ``skills:``.
    """

    name = "add-skill"
    description = (
        "Scaffold a new starter skill (skills/<name>/ from the packaged "
        "template) and optionally wire it into an agent's skills: list. "
        "Additive and reversible."
    )
    side_effects: tuple[SideEffect, ...] = (SideEffect.FILESYSTEM,)
    reversible = True
    args_model: type[BaseModel] = AddSkillArgs

    def _skill_dir(self, ctx: AuthoringContext, name: str) -> Path:
        return (ctx.project / "skills" / name).resolve()

    def plan(self, ctx: AuthoringContext, args: AddSkillArgs) -> ActionPlan:
        dest = self._skill_dir(ctx, args.name)
        rel = str(dest.relative_to(ctx.project)) if dest.is_relative_to(ctx.project) else str(dest)
        exists = dest.exists()
        wired = f" + wire into agent {args.agent!r}" if args.agent else ""
        summary = (
            f"scaffold skill {args.name!r} at {rel}/{wired}"
            if not exists
            else f"skill {args.name!r} already exists at {rel} (would refuse)"
        )
        return ActionPlan(
            action=self.name,
            summary=summary,
            diff="",  # a template copy isn't a single-file textual diff
            side_effects=list(self.side_effects),
            reversible=True,
            requires_confirmation=exists,  # overwriting an existing skill is confirm-gated
            details={"path": rel, "exists": exists, "agent": args.agent},
        )

    def apply(self, ctx: AuthoringContext, args: AddSkillArgs) -> ActionResult:
        template_dir = TEMPLATES_DIR / "skill_init"
        if not template_dir.is_dir():  # pragma: no cover — install-time invariant
            raise AuthoringActionError(f"skill template missing: {template_dir}")
        dest = self._skill_dir(ctx, args.name)
        if dest.exists():
            raise AuthoringActionError(
                f"skill {args.name!r} already exists at {dest}; remove it first"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(template_dir, dest)
        for fname in _TEMPLATED_FILES:
            f = dest / fname
            if f.exists():
                f.write_text(f.read_text().replace(_NAME_PLACEHOLDER, args.name))

        changed = [str(dest.relative_to(ctx.project))]
        if args.agent is not None:
            agent_yaml = ctx.agent_yaml(args.agent)
            if not agent_yaml.is_file():
                raise AuthoringActionError(f"agent not found: {args.agent}")
            self._wire_skill(agent_yaml, args.name)
            changed.append(str(agent_yaml.relative_to(ctx.project)))

        return ActionResult(
            action=self.name,
            summary=f"scaffolded skill {args.name!r}"
            + (f" + wired into {args.agent!r}" if args.agent else ""),
            changed_paths=changed,
        )

    def _wire_skill(self, agent_yaml: Path, name: str) -> None:
        """Append ``name`` to agent.yaml's ``skills:`` list via the round-trip."""
        data = load_agent_yaml(agent_yaml)
        skills: list[Any] = list(data.get("skills") or [])
        if name not in skills:
            skills.append(name)
        data = dict(data)
        data["skills"] = skills
        write_agent_yaml(agent_yaml, data)
