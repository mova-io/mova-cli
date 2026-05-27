"""Model catalog actions — set-model / add-fallback (ADR 025 D1).

Both mutate ``agent.yaml``'s ``model:`` block through the single canonical
round-trip (:mod:`movate.authoring._yaml_edit`) — no parallel writer (D8). The
provider string is validated against :class:`movate.core.models.ModelConfig`
(rejecting floating tags) at plan time, so a bad model id fails *before* any
write, and the post-apply verify still re-checks via ``load_agent``.

Swapping a model is a cost-relevant configuration change (it changes which
provider/price a run uses), so per D2 both actions require confirmation.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from movate.authoring._diff import diff_for_file
from movate.authoring._yaml_edit import load_agent_yaml, render_agent_yaml, write_agent_yaml
from movate.authoring.base import AuthoringActionError, AuthoringContext, BaseAuthoringAction
from movate.authoring.models import ActionPlan, ActionResult, SideEffect
from movate.core.models import ModelConfig


def _validate_provider(provider: str) -> None:
    """Reject malformed / floating provider strings before any write."""
    try:
        ModelConfig(provider=provider)
    except ValidationError as exc:
        raise AuthoringActionError(f"invalid model provider {provider!r}: {exc}") from exc


def _rel(ctx: AuthoringContext, path: Any) -> str:
    return str(path.relative_to(ctx.project)) if path.is_relative_to(ctx.project) else str(path)


class SetModelArgs(BaseModel):
    """Args for :class:`SetModelAction`."""

    model_config = ConfigDict(extra="forbid")

    agent: str = Field(..., description="Agent whose primary model is swapped.")
    provider: str = Field(
        ..., description="LiteLLM-style model string, e.g. 'anthropic/claude-sonnet-4-6'."
    )
    params: dict[str, Any] | None = Field(
        default=None, description="Optional model params (temperature, max_tokens). Replaces."
    )


class SetModelAction(BaseAuthoringAction):
    """Swap the agent's primary ``model.provider`` (+ optional params).

    Preserves the existing ``fallback`` chain. Requires confirmation (a model
    swap changes cost/behavior). Reversible via checkpoint.
    """

    name = "set-model"
    description = (
        "Set the agent's primary model (provider string), optionally replacing "
        "its params. Changes cost/behavior — requires confirmation. Reversible."
    )
    side_effects: tuple[SideEffect, ...] = (SideEffect.FILESYSTEM, SideEffect.COST)
    reversible = True
    args_model: type[BaseModel] = SetModelArgs

    def _mutate(self, data: dict[str, Any], args: SetModelArgs) -> dict[str, Any]:
        model = dict(data.get("model") or {})
        model["provider"] = args.provider
        if args.params is not None:
            model["params"] = args.params
        data = dict(data)
        data["model"] = model
        return data

    def plan(self, ctx: AuthoringContext, args: SetModelArgs) -> ActionPlan:
        _validate_provider(args.provider)
        agent_yaml = ctx.agent_yaml(args.agent)
        data = load_agent_yaml(agent_yaml)
        new_text = render_agent_yaml(self._mutate(data, args))
        rel = _rel(ctx, agent_yaml)
        return ActionPlan(
            action=self.name,
            summary=f"set {args.agent!r} model → {args.provider}",
            diff=diff_for_file(agent_yaml, new_text, rel_path=rel),
            side_effects=list(self.side_effects),
            reversible=True,
            requires_confirmation=True,
            details={"agent_yaml": rel, "provider": args.provider},
        )

    def apply(self, ctx: AuthoringContext, args: SetModelArgs) -> ActionResult:
        _validate_provider(args.provider)
        agent_yaml = ctx.agent_yaml(args.agent)
        data = load_agent_yaml(agent_yaml)
        write_agent_yaml(agent_yaml, self._mutate(data, args))
        return ActionResult(
            action=self.name,
            summary=f"set {args.agent!r} model → {args.provider}",
            changed_paths=[_rel(ctx, agent_yaml)],
        )


class AddFallbackArgs(BaseModel):
    """Args for :class:`AddFallbackAction`."""

    model_config = ConfigDict(extra="forbid")

    agent: str = Field(..., description="Agent gaining a fallback model.")
    provider: str = Field(..., description="LiteLLM-style fallback model string.")
    params: dict[str, Any] | None = Field(default=None, description="Optional fallback params.")


class AddFallbackAction(BaseAuthoringAction):
    """Append a fallback target to the agent's ``model.fallback`` chain.

    Additive but cost/behavior-relevant (a fallback can be hit on primary
    failure), so it requires confirmation. Reversible via checkpoint.
    """

    name = "add-fallback"
    description = (
        "Append a fallback model the executor tries when the primary fails. "
        "Cost-relevant — requires confirmation. Reversible."
    )
    side_effects: tuple[SideEffect, ...] = (SideEffect.FILESYSTEM, SideEffect.COST)
    reversible = True
    args_model: type[BaseModel] = AddFallbackArgs

    def _mutate(self, data: dict[str, Any], args: AddFallbackArgs) -> dict[str, Any]:
        model = dict(data.get("model") or {})
        fallback = list(model.get("fallback") or [])
        entry: dict[str, Any] = {"provider": args.provider}
        if args.params is not None:
            entry["params"] = args.params
        fallback.append(entry)
        model["fallback"] = fallback
        data = dict(data)
        data["model"] = model
        return data

    def plan(self, ctx: AuthoringContext, args: AddFallbackArgs) -> ActionPlan:
        _validate_provider(args.provider)
        agent_yaml = ctx.agent_yaml(args.agent)
        data = load_agent_yaml(agent_yaml)
        new_text = render_agent_yaml(self._mutate(data, args))
        rel = _rel(ctx, agent_yaml)
        return ActionPlan(
            action=self.name,
            summary=f"add fallback {args.provider} to {args.agent!r}",
            diff=diff_for_file(agent_yaml, new_text, rel_path=rel),
            side_effects=list(self.side_effects),
            reversible=True,
            requires_confirmation=True,
            details={"agent_yaml": rel, "provider": args.provider},
        )

    def apply(self, ctx: AuthoringContext, args: AddFallbackArgs) -> ActionResult:
        _validate_provider(args.provider)
        agent_yaml = ctx.agent_yaml(args.agent)
        data = load_agent_yaml(agent_yaml)
        write_agent_yaml(agent_yaml, self._mutate(data, args))
        return ActionResult(
            action=self.name,
            summary=f"added fallback {args.provider} to {args.agent!r}",
            changed_paths=[_rel(ctx, agent_yaml)],
        )
