"""Project-wide movate configuration loaded from ``movate.yaml``.

Lets a teammate run ``movate bench faq-agent --input ...`` without remembering
every model id. Defaults from ``movate.yaml`` at the project root; CLI flags
always override.

Also home to the **model policy** (v1.0 stage 3) — an org-wide set of rules
about which providers / models / cost ceilings an agent may use. Enforced at:

* ``movate validate`` — static check on every ``agent.yaml`` before merge
* ``Executor.execute()`` entry — runtime check at every invocation, so a
  bundle that skipped ``validate`` (e.g. loaded over HTTP by ``movate serve``)
  still can't bypass the rules

The policy is intentionally additive: an empty / absent ``policy:`` block is
the permissive default (everything allowed, no cost ceiling).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, ConfigDict, Field

from movate.core.models import AgentRuntime

if TYPE_CHECKING:
    from movate.core.models import AgentSpec


class BenchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[str] = Field(default_factory=list)
    judges: list[str] = Field(default_factory=list)
    runs: int = 1


class EvalDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gate: float | None = None


class ModelPolicy(BaseModel):
    """Project-wide model policy.

    All three fields are optional; absent fields = no restriction. The
    permissive default (everything empty / None) is equivalent to no
    ``policy:`` block at all, so projects without policy needs see zero
    behavior change.

    Examples (in ``movate.yaml``)::

        policy:
          allowed_providers: [openai, azure, anthropic]
          deny_models:
            - openai/gpt-3.5-turbo
            - openai/gpt-4-0314          # superseded; deny pre-0314 fallbacks
          max_cost_per_run_usd: 0.50

    Fields:

    * ``allowed_providers``: provider *prefixes* (the part before ``/`` in
      a LiteLLM model string). Empty list = all providers allowed.
      ``openai/gpt-4o-mini`` matches prefix ``openai``; ``azure/gpt-4.1``
      matches ``azure``.
    * ``deny_models``: full LiteLLM model strings to reject outright.
      Takes precedence over ``allowed_providers`` — a model can be in
      an allowed provider but still denied by exact match.
    * ``max_cost_per_run_usd``: hard ceiling on per-run cost. Each
      agent's ``budget.max_cost_usd_per_run`` is capped at this value
      at runtime (operator can't accidentally ship an agent with a
      higher cap than the org allows). ``None`` = no ceiling.
    """

    model_config = ConfigDict(extra="forbid")

    allowed_providers: list[str] = Field(
        default_factory=list,
        description=(
            "Provider prefixes (before '/') that an agent's model may use. "
            "Empty list = no restriction."
        ),
    )
    deny_models: list[str] = Field(
        default_factory=list,
        description=(
            "Full LiteLLM model strings that are blocked outright "
            "(e.g. 'openai/gpt-3.5-turbo'). Takes precedence over allowed_providers."
        ),
    )
    max_cost_per_run_usd: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Hard ceiling on per-run cost. Caps each agent's "
            "budget.max_cost_usd_per_run at runtime. None = no ceiling."
        ),
    )

    def is_permissive(self) -> bool:
        """True if the policy imposes no restrictions — handy for fast-paths."""
        return (
            not self.allowed_providers
            and not self.deny_models
            and self.max_cost_per_run_usd is None
        )

    def check_model(self, provider: str) -> str | None:
        """Validate one LiteLLM model string against the policy.

        Returns a human-readable error message if the model violates the
        policy, or ``None`` if it's allowed. Returning a string (not
        raising) so callers can aggregate violations across an agent's
        primary + fallback chain in one pass.
        """
        if provider in self.deny_models:
            return f"model {provider!r} is in deny_models"
        if self.allowed_providers:
            prefix = provider.split("/", 1)[0] if "/" in provider else provider
            if prefix not in self.allowed_providers:
                allowed = ", ".join(sorted(self.allowed_providers))
                return (
                    f"provider prefix {prefix!r} (from {provider!r}) "
                    f"not in allowed_providers [{allowed}]"
                )
        return None

    def check_agent(self, spec: AgentSpec) -> list[str]:
        """Validate every model an agent might use + its budget.

        Returns a list of violation strings (empty list = compliant).
        Checks: primary model, every fallback model, and budget ceiling.
        """
        violations: list[str] = []
        if err := self.check_model(spec.model.provider):
            violations.append(f"primary model: {err}")
        for fb in spec.model.fallback:
            if err := self.check_model(fb.provider):
                violations.append(f"fallback {fb.provider!r}: {err}")
        if (
            self.max_cost_per_run_usd is not None
            and spec.budget.max_cost_usd_per_run > self.max_cost_per_run_usd
        ):
            violations.append(
                f"budget.max_cost_usd_per_run={spec.budget.max_cost_usd_per_run} "
                f"exceeds policy ceiling {self.max_cost_per_run_usd}"
            )
        return violations

    def effective_max_cost(self, agent_budget: float) -> float:
        """The cost ceiling to enforce for one run.

        Min of the agent's budget and the policy's ceiling. If the policy
        has no ceiling, the agent's budget passes through unchanged.
        """
        if self.max_cost_per_run_usd is None:
            return agent_budget
        return min(agent_budget, self.max_cost_per_run_usd)


class RuntimePolicy(BaseModel):
    """Project-wide gate on which ``AgentRuntime`` values an agent may use.

    Default is permissive (``allowed=None``): any runtime an agent declares
    is fine, provided its adapter is installed. Locking to a specific
    subset enforces architectural direction. The canonical "A by default"
    setup is::

        # movate.yaml
        runtime:
          allowed: [litellm]

    With that in place, ``movate validate`` rejects any agent that declares
    ``runtime: native_anthropic`` (etc.) — operators have to either remove
    the field (fall back to LiteLLM) or change the project policy explicitly.
    Same pattern :class:`ModelPolicy` uses for provider gating.
    """

    model_config = ConfigDict(extra="forbid")

    allowed: list[AgentRuntime] | None = Field(
        default=None,
        description=(
            "If set, agents may only declare runtimes in this list. "
            "Permissive default (None): every installed runtime is fair game. "
            "Set to ``[litellm]`` to enforce 'A by default' — every agent goes "
            "through LiteLLM, no native-SDK or LangChain escape hatches."
        ),
    )

    def is_permissive(self) -> bool:
        return self.allowed is None

    def check_agent(self, spec: AgentSpec) -> str | None:
        """Return a human-readable violation string, or None if the agent
        complies. Returns ``None`` on permissive default."""
        if self.allowed is None:
            return None
        if spec.runtime in self.allowed:
            return None
        allowed_str = ", ".join(sorted(r.value for r in self.allowed))
        return (
            f"agent declares runtime={spec.runtime.value!r} but project policy "
            f"only allows: {allowed_str}"
        )


class ModelParamDefaults(BaseModel):
    """Project-wide model param defaults — agent.yaml fills the rest in.

    Holds whatever LiteLLM-style params the operator wants applied
    across every agent in the project. Each key is the param name
    (``temperature``, ``max_tokens``, ``top_p``, ...); each value is
    its default. Agent-level ``model.params`` always wins on key
    conflict; defaults only fill keys the agent didn't specify.
    """

    model_config = ConfigDict(extra="forbid")

    params: dict[str, object] = Field(
        default_factory=dict,
        description=(
            "Per-key default values for `model.params` across every agent. "
            "Empty dict = no defaults; each agent.yaml supplies its own."
        ),
    )


class TimeoutDefaults(BaseModel):
    """Project-wide default timeouts — agent.yaml fields override per-field.

    Both fields are optional so the operator can pin one (e.g.
    ``call_ms: 15000``) without inadvertently bumping ``total_ms``.
    Agent-level ``timeouts.*`` always wins per-field.
    """

    model_config = ConfigDict(extra="forbid")

    call_ms: int | None = Field(default=None, ge=1)
    total_ms: int | None = Field(default=None, ge=1)


class BudgetDefaults(BaseModel):
    """Project-wide default budget cap — agent.yaml overrides.

    Distinct from :class:`ModelPolicy.max_cost_per_run_usd` (which is
    an enforced ceiling — agents can't exceed it). This is a *default*
    that fills in for agents whose ``budget`` block is absent.
    Operators get tighter defaults plus the policy ceiling as two
    independent controls.
    """

    model_config = ConfigDict(extra="forbid")

    max_cost_usd_per_run: float | None = Field(default=None, ge=0)


class AgentDefaults(BaseModel):
    """Project-wide defaults that fill gaps in each agent.yaml.

    Layered semantics — agent.yaml always wins, defaults only fill
    keys the agent didn't specify. Concretely:

    * ``model.params``: deep merge per-key. Default
      ``temperature: 0.0`` applies to every agent that doesn't set
      ``temperature`` in its own ``model.params``.
    * ``timeouts.call_ms`` / ``timeouts.total_ms``: per-field. Default
      ``call_ms: 15000`` applies to agents whose ``timeouts`` block
      omits ``call_ms``.
    * ``budget.max_cost_usd_per_run``: same per-field pattern.

    Headline use case: set ``temperature: 0.0`` once at the project
    level instead of repeating it across every agent.yaml.

    Empty / absent ``defaults:`` block = permissive default, no merge
    happens, every agent.yaml is loaded verbatim. Pre-existing
    projects without a ``defaults:`` block see zero behavior change.
    """

    model_config = ConfigDict(extra="forbid")

    model: ModelParamDefaults = Field(default_factory=ModelParamDefaults)
    timeouts: TimeoutDefaults = Field(default_factory=TimeoutDefaults)
    budget: BudgetDefaults = Field(default_factory=BudgetDefaults)


class ProjectConfig(BaseModel):
    """Project-wide defaults — overrideable via CLI flags."""

    model_config = ConfigDict(extra="forbid")

    agents_dir: str = "./agents"
    workflows_dir: str = "./workflows"
    bench: BenchConfig = Field(default_factory=BenchConfig)
    eval: EvalDefaults = Field(default_factory=EvalDefaults)
    defaults: AgentDefaults = Field(
        default_factory=AgentDefaults,
        description=(
            "Per-agent defaults applied at load time. Agent.yaml always "
            "wins per-key; defaults only fill what the agent didn't "
            "specify. Headline use: pin temperature / max_tokens / budget "
            "once at the project level without copy-pasting to every "
            "agent.yaml. See AgentDefaults for the layered semantics."
        ),
    )
    policy: ModelPolicy = Field(
        default_factory=ModelPolicy,
        description=(
            "Org-wide model policy (allowed providers, deny-list, cost ceiling). "
            "Empty/absent = permissive default."
        ),
    )
    runtime: RuntimePolicy = Field(
        default_factory=RuntimePolicy,
        description=(
            "Project-wide gate on AgentRuntime values. Empty/absent = permissive "
            "default (any installed runtime). Set ``runtime.allowed: [litellm]`` "
            "to enforce 'A by default' — see RuntimePolicy."
        ),
    )


def load_project_config(path: Path | str | None = None) -> ProjectConfig:
    """Load the project-level config from the project root (or provided path).

    File lookup precedence (when ``path`` is not explicit):

    1. ``policy.yaml`` — the canonical name going forward (MDK naming).
    2. ``movate.yaml`` — transitional alias. Logs a deprecation warning
       on first load so operators see the suggested rename without
       breaking existing repos.

    If both files exist, ``policy.yaml`` wins and ``movate.yaml`` is
    quietly ignored. This is the "migration mid-flight" state for repos
    that have started the rename but haven't deleted the old file yet.

    Returns defaults if neither file exists. Errors out clearly on a
    malformed file — never silently degrades on a typo.
    """
    if path is not None:
        # Explicit operator override — load exactly what they asked for,
        # whatever it's named.
        p = Path(path)
        if not p.exists():
            return ProjectConfig()
        data = yaml.safe_load(p.read_text()) or {}
        return ProjectConfig.model_validate(data)

    policy = Path("policy.yaml")
    legacy = Path("movate.yaml")

    if policy.exists():
        data = yaml.safe_load(policy.read_text()) or {}
        return ProjectConfig.model_validate(data)

    if legacy.exists():
        # One-time deprecation warning per process. Don't spam stderr if
        # the loader runs multiple times (validate + run + deploy all
        # call this).
        _warn_legacy_movate_yaml_once()
        data = yaml.safe_load(legacy.read_text()) or {}
        return ProjectConfig.model_validate(data)

    return ProjectConfig()


_LEGACY_WARN_FIRED = False


def _warn_legacy_movate_yaml_once() -> None:
    """Print a one-shot deprecation warning the first time we load
    ``movate.yaml`` (legacy name) instead of ``policy.yaml`` (canonical).

    Uses stderr (not the logging framework) so the warning is visible
    even when logging is configured to WARNING-only — config-rename is
    operator-actionable, not a debug detail.
    """
    global _LEGACY_WARN_FIRED  # noqa: PLW0603 — single-process one-shot warning state
    if _LEGACY_WARN_FIRED:
        return
    _LEGACY_WARN_FIRED = True
    import sys  # noqa: PLC0415

    print(
        "⚠ movate.yaml is deprecated — rename to policy.yaml. "
        "movate.yaml will continue to load through v1.x; "
        "removed in a future major release.",
        file=sys.stderr,
    )
