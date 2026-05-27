"""Set-retrieval catalog action — opt into ADR 023 auto-RAG (ADR 025 D1).

Sets ``retrieval.auto_into`` (and optional companion fields) in ``agent.yaml``
through the canonical round-trip (:mod:`movate.authoring._yaml_edit`), turning
on the Executor's declarative pre-retrieval phase. Validated against
:class:`movate.core.models.RetrievalConfig` at plan time so a bad value fails
before any write.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from movate.authoring._diff import diff_for_file
from movate.authoring._yaml_edit import load_agent_yaml, render_agent_yaml, write_agent_yaml
from movate.authoring.base import AuthoringActionError, AuthoringContext, BaseAuthoringAction
from movate.authoring.models import ActionPlan, ActionResult, SideEffect
from movate.core.models import RetrievalConfig


def _rel(ctx: AuthoringContext, path: Any) -> str:
    return str(path.relative_to(ctx.project)) if path.is_relative_to(ctx.project) else str(path)


class SetRetrievalArgs(BaseModel):
    """Args for :class:`SetRetrievalAction`."""

    model_config = ConfigDict(extra="forbid")

    agent: str = Field(..., description="Agent to enable auto-retrieval for.")
    auto_into: str = Field(
        ...,
        description=(
            "ADR 023 — the prompt variable the retrieved context is injected "
            "into (enables opt-in pre-retrieval / auto-RAG)."
        ),
    )
    query_from: str | None = Field(
        default=None, description="Optional input field used as the retrieval query."
    )
    skill: str | None = Field(
        default=None, description="Optional retrieval skill name (defaults to kb-vector-lookup)."
    )


class SetRetrievalAction(BaseAuthoringAction):
    """Enable ADR 023 auto-RAG on an agent (``retrieval.auto_into``).

    Validated against :class:`RetrievalConfig`. Reversible via checkpoint.
    Auto-retrieval adds embedding calls at run time, so it is cost-relevant
    and requires confirmation per D2.
    """

    name = "set-retrieval"
    description = (
        "Enable opt-in auto-retrieval (ADR 023) by setting retrieval.auto_into "
        "so the executor pre-fetches KB context into the prompt. Cost-relevant "
        "at run time — requires confirmation. Reversible."
    )
    side_effects: tuple[SideEffect, ...] = (SideEffect.FILESYSTEM, SideEffect.COST)
    reversible = True
    args_model: type[BaseModel] = SetRetrievalArgs

    def _mutate(self, data: dict[str, Any], args: SetRetrievalArgs) -> dict[str, Any]:
        retrieval = dict(data.get("retrieval") or {})
        retrieval["auto_into"] = args.auto_into
        if args.query_from is not None:
            retrieval["query_from"] = args.query_from
        if args.skill is not None:
            retrieval["skill"] = args.skill
        data = dict(data)
        data["retrieval"] = retrieval
        return data

    def _validate(self, retrieval: dict[str, Any]) -> None:
        try:
            RetrievalConfig.model_validate(retrieval)
        except ValidationError as exc:
            raise AuthoringActionError(f"invalid retrieval config: {exc}") from exc

    def plan(self, ctx: AuthoringContext, args: SetRetrievalArgs) -> ActionPlan:
        agent_yaml = ctx.agent_yaml(args.agent)
        data = load_agent_yaml(agent_yaml)
        mutated = self._mutate(data, args)
        self._validate(mutated["retrieval"])
        new_text = render_agent_yaml(mutated)
        rel = _rel(ctx, agent_yaml)
        return ActionPlan(
            action=self.name,
            summary=f"enable auto-retrieval on {args.agent!r} (auto_into={args.auto_into!r})",
            diff=diff_for_file(agent_yaml, new_text, rel_path=rel),
            side_effects=list(self.side_effects),
            reversible=True,
            requires_confirmation=True,
            details={"agent_yaml": rel, "auto_into": args.auto_into},
        )

    def apply(self, ctx: AuthoringContext, args: SetRetrievalArgs) -> ActionResult:
        agent_yaml = ctx.agent_yaml(args.agent)
        data = load_agent_yaml(agent_yaml)
        mutated = self._mutate(data, args)
        self._validate(mutated["retrieval"])
        write_agent_yaml(agent_yaml, mutated)
        return ActionResult(
            action=self.name,
            summary=f"enabled auto-retrieval on {args.agent!r}",
            changed_paths=[_rel(ctx, agent_yaml)],
        )
