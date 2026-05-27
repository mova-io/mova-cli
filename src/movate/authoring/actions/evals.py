"""Add / edit eval-case catalog action — append to ``evals/dataset.jsonl`` (ADR 025 D1).

An eval case is one ``{"input": {...}, "expected": {...}}`` JSON object per line
in the agent's dataset. This action appends a new case (or replaces the case at
a given index) using the dataset path declared in ``agent.yaml`` ``evals.dataset``
so it edits exactly the file ``mdk eval`` reads.

Adding an eval case is purely additive + free → may auto-apply in fast mode.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from movate.authoring._diff import unified
from movate.authoring._yaml_edit import load_agent_yaml
from movate.authoring.base import AuthoringActionError, AuthoringContext, BaseAuthoringAction
from movate.authoring.models import ActionPlan, ActionResult, SideEffect

_DEFAULT_DATASET = "./evals/dataset.jsonl"


def _dataset_path(ctx: AuthoringContext, agent: str) -> Path:
    """Resolve the agent's dataset from its agent.yaml ``evals.dataset`` ref."""
    agent_dir = ctx.agent_dir(agent)
    data = load_agent_yaml(agent_dir / "agent.yaml")
    evals = data.get("evals") or {}
    ref = evals.get("dataset", _DEFAULT_DATASET) if isinstance(evals, dict) else _DEFAULT_DATASET
    return (agent_dir / ref).resolve()


def _read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _render(lines: list[str]) -> str:
    return ("\n".join(lines) + "\n") if lines else ""


class AddEvalCaseArgs(BaseModel):
    """Args for :class:`AddEvalCaseAction`."""

    model_config = ConfigDict(extra="forbid")

    agent: str = Field(..., description="Agent whose eval dataset gains a case.")
    input: dict[str, Any] = Field(..., description="The eval case input payload.")
    expected: dict[str, Any] = Field(..., description="The expected output payload.")
    replace_index: int | None = Field(
        default=None,
        description="0-based line index to replace (edit). Omit to append a new case.",
    )


class AddEvalCaseAction(BaseAuthoringAction):
    """Append (or replace) an eval case in the agent's ``dataset.jsonl``.

    Additive + reversible + free when appending → may auto-apply in fast mode.
    Replacing an existing case is an edit (still reversible, still free).
    """

    name = "add-eval-case"
    description = (
        "Append a new eval case ({input, expected}) to the agent's "
        "evals/dataset.jsonl, or replace one at a given index. Additive and "
        "reversible — strengthens the agent's test coverage."
    )
    side_effects: tuple[SideEffect, ...] = (SideEffect.FILESYSTEM,)
    reversible = True
    args_model: type[BaseModel] = AddEvalCaseArgs

    def _new_lines(self, lines: list[str], args: AddEvalCaseArgs) -> list[str]:
        case = json.dumps({"input": args.input, "expected": args.expected})
        lines = list(lines)
        if args.replace_index is None:
            lines.append(case)
        else:
            if not 0 <= args.replace_index < len(lines):
                raise AuthoringActionError(
                    f"replace_index {args.replace_index} out of range "
                    f"(dataset has {len(lines)} case(s))"
                )
            lines[args.replace_index] = case
        return lines

    def plan(self, ctx: AuthoringContext, args: AddEvalCaseArgs) -> ActionPlan:
        path = _dataset_path(ctx, args.agent)
        rel = str(path.relative_to(ctx.project)) if path.is_relative_to(ctx.project) else str(path)
        old_lines = _read_lines(path)
        new_lines = self._new_lines(old_lines, args)
        diff = unified(_render(old_lines), _render(new_lines), rel_path=rel)
        verb = "replace eval case" if args.replace_index is not None else "add eval case"
        return ActionPlan(
            action=self.name,
            summary=f"{verb} on agent {args.agent!r}",
            diff=diff,
            side_effects=list(self.side_effects),
            reversible=True,
            requires_confirmation=False,
            details={"path": rel, "case_count": len(new_lines)},
        )

    def apply(self, ctx: AuthoringContext, args: AddEvalCaseArgs) -> ActionResult:
        agent_dir = ctx.agent_dir(args.agent)
        if not (agent_dir / "agent.yaml").is_file():
            raise AuthoringActionError(f"agent not found: {args.agent}")
        path = _dataset_path(ctx, args.agent)
        old_lines = _read_lines(path)
        new_lines = self._new_lines(old_lines, args)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render(new_lines), encoding="utf-8")
        rel = str(path.relative_to(ctx.project)) if path.is_relative_to(ctx.project) else str(path)
        return ActionResult(
            action=self.name,
            summary=f"eval dataset for {args.agent!r} now has {len(new_lines)} case(s)",
            changed_paths=[rel],
            details={"case_count": len(new_lines)},
        )
