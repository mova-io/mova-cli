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
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from movate.core.models import AgentRuntime, SkillSideEffects

if TYPE_CHECKING:
    from movate.core.models import AgentSpec
    from movate.core.skill_loader import SkillBundle


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

    All fields are optional; absent fields = no restriction. The
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
          input_guardrails:
            - prompt_injection

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
    * ``input_guardrails``: list of named input-guardrail checks to run
      BEFORE any provider call. Currently supports ``"prompt_injection"``.
      A detection raises :class:`~movate.core.failures.GuardrailViolationError`
      with zero LLM cost incurred.
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
    input_guardrails: list[Literal["prompt_injection"]] = Field(
        default_factory=list,
        description=(
            "Named input-guardrail checks executed BEFORE any provider call. "
            "Supported values: ['prompt_injection']. "
            "A detection raises GuardrailViolationError with zero LLM cost."
        ),
    )

    def is_permissive(self) -> bool:
        """True if the policy imposes no restrictions — handy for fast-paths."""
        return (
            not self.allowed_providers
            and not self.deny_models
            and self.max_cost_per_run_usd is None
            and not self.input_guardrails
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


class SkillPolicy(BaseModel):
    """Project-wide gate on which skill ``side_effects`` categories agents may use.

    Each skill declares its blast radius in ``skill.yaml: side_effects:`` —
    one of ``read-only``, ``network``, ``filesystem``, ``mutates-state``.
    SkillPolicy lets operators carve up which categories are allowed in
    this project. Two example use cases:

    * **Strict-prod policy:** ``allowed_side_effects: [read-only]``. Only
      pure-lookup skills allowed; any agent referencing a skill that hits
      the network or mutates state fails ``mdk validate``.
    * **Default-deny on a new project:** ``allowed_side_effects: []``. No
      skills at all — agents must declare ``skills: []``. Useful when
      bringing up a sensitive workflow before any skills are vetted.

    Permissive default (``allowed_side_effects=None``) accepts every
    side-effects category; existing projects see zero behavior change.

    Sibling to :class:`ModelPolicy` (which gates models) and
    :class:`RuntimePolicy` (which gates runtimes). Enforced at the same
    two layers: ``mdk validate`` (static check before merge) and
    ``Executor.execute()`` entry (runtime check so a bundle that skipped
    validate can't bypass).
    """

    model_config = ConfigDict(extra="forbid")

    allowed_side_effects: list[SkillSideEffects] | None = Field(
        default=None,
        description=(
            "Allowlist of skill ``side_effects`` categories that agents in "
            "this project may use. ``None`` (default) accepts every "
            "category. An empty list ``[]`` rejects every skill — agents "
            "must declare ``skills: []`` to validate."
        ),
    )

    def is_permissive(self) -> bool:
        """True if the policy imposes no restrictions on skill side-effects."""
        return self.allowed_side_effects is None

    def check_skill(self, skill_name: str, side_effects: SkillSideEffects) -> str | None:
        """Return a violation message if ``side_effects`` isn't allowed, or None.

        Doesn't raise — returning a string lets callers aggregate
        violations across every skill an agent declares in one pass.
        """
        if self.allowed_side_effects is None:
            return None
        if side_effects in self.allowed_side_effects:
            return None
        allowed_str = ", ".join(sorted(s.value for s in self.allowed_side_effects))
        if not self.allowed_side_effects:
            return (
                f"skill {skill_name!r} has side_effects={side_effects.value!r} but "
                f"project policy allows no skill side-effects (empty allowlist)"
            )
        return (
            f"skill {skill_name!r} has side_effects={side_effects.value!r} but "
            f"project policy only allows: {allowed_str}"
        )

    def check_agent_skills(self, skills: list[SkillBundle]) -> list[str]:
        """Aggregate every per-skill violation for an agent's resolved skill list.

        Returns a list of human-readable messages (empty if the agent's
        skills all comply, or if the policy is permissive). The shape
        mirrors :meth:`ModelPolicy.check_agent` — callers raise a
        single :class:`PolicyViolationError` summarizing all violations.
        """
        if self.is_permissive():
            return []
        violations: list[str] = []
        for skill in skills:
            err = self.check_skill(skill.spec.name, skill.spec.side_effects)
            if err is not None:
                violations.append(err)
        return violations


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


class PiiGuardrailConfig(BaseModel):
    """PII regex-based input/output guardrail.

    ``types`` selects which PII categories to scan for; empty list
    means "all supported types." Adding a new type to the underlying
    regex pack (see :mod:`movate.guardrails.pii`) doesn't require a
    config change — the type just becomes available in ``types``.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    mode: Literal["redact", "block", "warn"] = "redact"
    types: list[Literal["email", "phone", "ssn", "credit_card"]] = Field(
        default_factory=list,
        description=(
            "PII categories to scan for. Empty list = all supported "
            "categories. Trim this list to reduce false positives on "
            "inputs where (e.g.) order numbers look like credit cards."
        ),
    )


class TopicGuardrailConfig(BaseModel):
    """Topic restriction — allowlist (must contain) + denylist (must not).

    Both lists are optional and combinable: an allowlist defines
    "stay on topic" (text must mention at least one), a denylist
    forbids specific topics. Terms may be substrings (matched
    case-insensitively) or regex patterns prefixed with ``re:``.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    allowed_topics: list[str] = Field(
        default_factory=list,
        description=(
            "Text must contain at least one of these terms/patterns. "
            "Empty = no allowlist constraint."
        ),
    )
    banned_topics: list[str] = Field(
        default_factory=list,
        description=(
            "Text must not contain any of these terms/patterns. Empty = no denylist constraint."
        ),
    )
    on_violation: Literal["block", "warn"] = "block"


class ContentGuardrailConfig(BaseModel):
    """Banned-terms scan over the text.

    Simpler than topic — no allowlist, just a denylist. Use this for
    profanity, leaked internal labels, competitor names, etc.
    Operator supplies their own list; we don't ship one.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    banned_terms: list[str] = Field(
        default_factory=list,
        description=(
            "Substrings (case-insensitive) or regex patterns (prefix "
            "with ``re:``) that may not appear in the text."
        ),
    )
    on_violation: Literal["block", "warn"] = "block"


class GuardrailDirection(BaseModel):
    """The three guardrail modules, bundled for one direction
    (input OR output).

    Each direction has its own copy so an operator can tune them
    independently — typical pattern is *stricter* on output (the
    leaky model speaking to the customer) than on input (the
    customer asking a question we then redact).
    """

    model_config = ConfigDict(extra="forbid")

    pii: PiiGuardrailConfig = Field(default_factory=PiiGuardrailConfig)
    topic: TopicGuardrailConfig = Field(default_factory=TopicGuardrailConfig)
    content: ContentGuardrailConfig = Field(default_factory=ContentGuardrailConfig)

    def is_permissive(self) -> bool:
        """True if every sub-guardrail is disabled — fast-path skip."""
        return not self.pii.enabled and not self.topic.enabled and not self.content.enabled


class GuardrailsConfig(BaseModel):
    """Project-wide Safe-AI guardrails for input AND output text.

    Loaded from ``movate.yaml`` (or ``policy.yaml``) under the
    ``guardrails:`` key. The permissive default — every sub-block
    ``enabled: false`` — leaves behavior unchanged from pre-v0.7,
    so existing projects upgrade without action.

    Enforced at two layers (same belt-and-braces shape as
    :class:`ModelPolicy` / :class:`RuntimePolicy`):

    * :meth:`movate.core.executor.Executor.execute` entry — input
      guardrails run BEFORE prompt rendering, so a blocked request
      doesn't bill latency or trigger side effects.
    * :meth:`movate.core.executor.Executor.execute` exit — output
      guardrails run AFTER completion lands but BEFORE schema
      validation + persist, so a leaky output never reaches the
      caller.

    Blocking verdicts surface as
    :class:`movate.core.failures.ContentFilterError` →
    ``FailureType.CONTENT_FILTER`` → ``RunRecord.status="safety_blocked"``
    — the same propagation path the rest of the pipeline already
    treats as terminal-non-retryable.

    Example ``movate.yaml``::

        guardrails:
          input:
            pii:
              enabled: true
              mode: redact
              types: [email, phone, ssn]
            topic:
              enabled: true
              allowed_topics: ["Sandisk", "storage", "data backup"]
              on_violation: block
          output:
            pii:
              enabled: true
              mode: redact
            content:
              enabled: true
              banned_terms: ["internal only", "do not share"]
              on_violation: block
    """

    model_config = ConfigDict(extra="forbid")

    input: GuardrailDirection = Field(default_factory=GuardrailDirection)
    output: GuardrailDirection = Field(default_factory=GuardrailDirection)

    def is_permissive(self) -> bool:
        """True if BOTH directions are fully disabled. Lets the
        executor short-circuit the guardrails calls entirely on
        the common case (no guardrails configured)."""
        return self.input.is_permissive() and self.output.is_permissive()


class KnowledgeConfig(BaseModel):
    """Stub for v0.7+ RAG / knowledge-base configuration.

    Shipped today as a placeholder so the canonical file slot
    (``knowledge.yaml``) exists in the project layout — operators
    can drop the file in and reserve the path. Real fields (vector
    store backend, embedding model, re-index policy) arrive when
    pgvector / Apache AGE land in Tier 3.

    ``extra="allow"`` for forward compatibility: a partially-filled
    ``knowledge.yaml`` with keys we haven't formalized yet doesn't
    error — keeps experimental projects unblocked. Strict validation
    enables when the schema firms up.
    """

    model_config = ConfigDict(extra="allow")


class ProjectConfig(BaseModel):
    """Project-wide defaults — overrideable via CLI flags.

    Loaded from up to four files at the project root:

    * ``policy.yaml`` — the canonical file. May contain every block
      (works exactly as v0.5 for backward compat). The recommended
      content is ``policy:`` (enforced rules) + ``defaults:``
      (suggestions) + project-layout fields.
    * ``runtime.yaml`` — the ``runtime:`` block (RuntimePolicy).
      Optional; takes precedence over policy.yaml's ``runtime:`` block
      if both exist (with a deprecation warning on the policy.yaml side).
    * ``eval.yaml`` — the ``eval:`` + ``bench:`` blocks. Same
      precedence pattern.
    * ``knowledge.yaml`` — the ``knowledge:`` block. Stub today;
      reserved slot for Tier 3 RAG config.

    Dedicated files always win on conflict. See
    :func:`load_project_config` for the merge rules and
    deprecation behavior.
    """

    model_config = ConfigDict(extra="forbid")

    agents_dir: str = "./agents"
    workflows_dir: str = "./workflows"
    skills_dir: str = Field(
        default="./skills",
        description=(
            "Where the project's skill definitions live. Each subdir is a "
            "skill (``skill.yaml`` + ``impl.py``). Agents reference skills "
            "by name via ``skills: [skill-name]``; the resolver walks this "
            "directory at load time. See :mod:`movate.core.skill_loader`."
        ),
    )
    contexts_dir: str = Field(
        default="./contexts",
        description=(
            "Where the project's reusable Markdown contexts live (one "
            "``<name>.md`` per context). Agents reference contexts by "
            "filename (minus extension) via ``contexts: [name]``; the "
            "loader walks this directory and prepends each declared "
            "context to the prompt. See :mod:`movate.core.context_loader`."
        ),
    )
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
    skills: SkillPolicy = Field(
        default_factory=SkillPolicy,
        description=(
            "Project-wide gate on skill ``side_effects`` categories. "
            "Empty/absent = permissive default. Set "
            "``skills.allowed_side_effects: [read-only]`` to restrict "
            "agents to pure-lookup skills — see SkillPolicy."
        ),
    )
    knowledge: KnowledgeConfig = Field(
        default_factory=KnowledgeConfig,
        description=(
            "Reserved slot for v0.7+ knowledge / RAG configuration. "
            "Operators can drop a ``knowledge.yaml`` in the project root "
            "today, but only forward-compatible fields are accepted "
            "until the schema firms up."
        ),
    )
    guardrails: GuardrailsConfig = Field(
        default_factory=GuardrailsConfig,
        description=(
            "Project-wide Safe-AI guardrails (PII / topic / content) for "
            "input + output text. Empty/absent = permissive default. "
            "Enforced at Executor.execute() entry (input) and exit "
            "(output). See GuardrailsConfig for the full schema."
        ),
    )


def load_project_config(path: Path | str | None = None) -> ProjectConfig:
    """Load the project-level config from the project root (or provided path).

    Two layers:

    1. **Base file** — ``policy.yaml`` (canonical) or ``movate.yaml``
       (legacy, deprecation warning). May carry every ProjectConfig
       block for backward compatibility with v0.5 projects.
    2. **Canonical-split files** (v0.6+) — ``runtime.yaml``,
       ``eval.yaml``, ``knowledge.yaml`` at the same project root.
       Each carries only its relevant top-level block(s):

       * ``runtime.yaml`` → the ``runtime:`` block
       * ``eval.yaml`` → the ``eval:`` and/or ``bench:`` blocks
       * ``knowledge.yaml`` → the ``knowledge:`` block

    **Dedicated-file-wins:** if ``runtime.yaml`` exists AND
    ``policy.yaml`` also has a ``runtime:`` block, the dedicated file
    wins and the policy.yaml field triggers a one-shot deprecation
    warning per moved field. Same pattern for ``eval:`` / ``bench:``
    (eval.yaml) and ``knowledge:`` (knowledge.yaml).

    Operators can migrate incrementally: cut a block from policy.yaml,
    paste into its dedicated file, drop the now-empty key from
    policy.yaml. Or stay on the unified policy.yaml indefinitely; the
    canonical split is opt-in.

    When ``path`` is explicit, only that file is read — the canonical
    split applies only to the default-discovery flow at the project
    root.

    Returns defaults if no config files exist. Errors out clearly on
    a malformed file — never silently degrades on a typo.
    """
    if path is not None:
        # Explicit operator override — load exactly what they asked
        # for, whatever it's named. No canonical-split merging in
        # this path; the caller is asking for one specific file.
        p = Path(path)
        if not p.exists():
            return ProjectConfig()
        data = yaml.safe_load(p.read_text()) or {}
        return ProjectConfig.model_validate(data)

    # Resolve the base file. Three accepted names in precedence order:
    #
    # 1. `project.yaml` — canonical (post-MVP rename, May 2026)
    # 2. `policy.yaml`  — legacy v1.x canonical; preserved through v1.x
    # 3. `movate.yaml`  — original v0.x name; preserved through v1.x
    #
    # First-match wins. Each legacy name emits a one-shot deprecation
    # warning when found so operators know to migrate.
    base_data: dict[str, Any] = {}
    project_path = Path("project.yaml")
    policy_path = Path("policy.yaml")
    legacy_path = Path("movate.yaml")
    if project_path.exists():
        base_data = yaml.safe_load(project_path.read_text()) or {}
    elif policy_path.exists():
        # policy.yaml is still supported but is no longer the headline
        # name; surface a soft deprecation so operators migrate when
        # convenient. Same one-shot pattern as movate.yaml's warning.
        _warn_legacy_policy_yaml_once()
        base_data = yaml.safe_load(policy_path.read_text()) or {}
    elif legacy_path.exists():
        _warn_legacy_movate_yaml_once()
        base_data = yaml.safe_load(legacy_path.read_text()) or {}

    # Layer in canonical-split files. Each file's content replaces the
    # corresponding block(s) in base_data and emits a deprecation
    # warning if the operator hadn't yet migrated. Empty / missing
    # split files are silent no-ops.
    merged = _apply_canonical_split(
        base_data,
        runtime_path=Path("runtime.yaml"),
        eval_path=Path("eval.yaml"),
        knowledge_path=Path("knowledge.yaml"),
    )

    return ProjectConfig.model_validate(merged)


# Project-root marker filenames recognized by ``mdk add`` /
# ``mdk validate`` / ``mdk snapshot`` / etc. when walking up from
# cwd to find a project. Listed in canonical-first order; ALL three
# get accepted indefinitely (the legacy ones fire a one-shot
# deprecation warning when read via :func:`load_project_config`).
PROJECT_MARKER_FILES: tuple[str, ...] = (
    "project.yaml",  # canonical (post-MVP rename, May 2026)
    "policy.yaml",  # legacy v1.x canonical
    "movate.yaml",  # original v0.x name
)


def is_project_root(path: Path | str) -> bool:
    """True if ``path`` contains any of the recognized project-root
    marker files. Used by every walk-up convention in the CLI so the
    three accepted names stay synchronized in one place."""
    p = Path(path)
    return any((p / marker).is_file() for marker in PROJECT_MARKER_FILES)


# Fields that have moved out of policy.yaml. When both the dedicated
# file AND policy.yaml carry the field, the dedicated file wins and
# the operator gets a one-shot deprecation warning per field.
_MOVED_FIELDS: dict[str, str] = {
    "runtime": "runtime.yaml",
    "eval": "eval.yaml",
    "bench": "eval.yaml",
    "knowledge": "knowledge.yaml",
}


def _apply_canonical_split(
    base_data: dict[str, Any],
    *,
    runtime_path: Path,
    eval_path: Path,
    knowledge_path: Path,
) -> dict[str, Any]:
    """Layer dedicated split files on top of the base policy.yaml data.

    Per-file merge rules:

    * ``runtime.yaml`` content → top-level ``runtime:`` block.
    * ``eval.yaml`` content → top-level ``eval:`` and/or ``bench:`` blocks.
    * ``knowledge.yaml`` content → top-level ``knowledge:`` block.

    Each dedicated file's top-level keys must be a subset of
    {their expected blocks}. Anything else in a dedicated file is
    an error (we surface it as ``ProjectConfig.model_validate``
    rejecting unknown fields at the merge layer).

    Conflicts (field present in both base + dedicated) — dedicated
    wins, base field triggers a one-shot deprecation warning.
    """
    out = dict(base_data)

    _layer_file(
        out,
        path=runtime_path,
        allowed_keys={"runtime"},
        base_data=base_data,
    )
    _layer_file(
        out,
        path=eval_path,
        allowed_keys={"eval", "bench"},
        base_data=base_data,
    )
    _layer_file(
        out,
        path=knowledge_path,
        allowed_keys={"knowledge"},
        base_data=base_data,
    )
    return out


def _layer_file(
    merged: dict[str, Any],
    *,
    path: Path,
    allowed_keys: set[str],
    base_data: dict[str, Any],
) -> None:
    """Read one canonical-split file and overlay its keys onto ``merged``.

    Unknown keys in a dedicated file are passed through to the final
    Pydantic validation — the resulting error message names the
    bad key, which is the right operator experience for a typo.
    """
    if not path.exists():
        return
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a top-level object; got {type(raw).__name__}")
    for key, value in raw.items():
        if key in allowed_keys and key in base_data:
            # Dedicated file wins, but warn the operator that the
            # field is being read from two places.
            _warn_field_moved_once(field=key, dedicated_file=path.name)
        merged[key] = value


_LEGACY_WARN_FIRED = False
_POLICY_LEGACY_WARN_FIRED = False
_MOVED_FIELD_WARNINGS: set[str] = set()


def _warn_legacy_movate_yaml_once() -> None:
    """Print a one-shot deprecation warning the first time we load
    ``movate.yaml`` (oldest name) instead of ``project.yaml`` (canonical).

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
        "⚠ movate.yaml is deprecated — rename to project.yaml. "
        "movate.yaml will continue to load through v1.x; "
        "removed in a future major release.",
        file=sys.stderr,
    )


def _warn_legacy_policy_yaml_once() -> None:
    """Print a one-shot deprecation warning the first time we load
    ``policy.yaml`` (legacy v1.x name) instead of ``project.yaml``.

    Same shape as the movate.yaml warning. Both fire to stderr so
    operators see migration prompts even under quiet logging.
    """
    global _POLICY_LEGACY_WARN_FIRED  # noqa: PLW0603 — one-shot warning state
    if _POLICY_LEGACY_WARN_FIRED:
        return
    _POLICY_LEGACY_WARN_FIRED = True
    import sys  # noqa: PLC0415

    print(
        "⚠ policy.yaml is deprecated — rename to project.yaml. "
        "policy.yaml will continue to load through v1.x; "
        "removed in a future major release.",
        file=sys.stderr,
    )


def _warn_field_moved_once(*, field: str, dedicated_file: str) -> None:
    """Warn (per-field, once per process) that a policy.yaml field has
    a dedicated home and the operator should migrate.

    The dedicated file's value still wins — we don't silently
    discard. The warning's only job is to tell the operator they're
    maintaining the same data in two places and which file we read."""
    if field in _MOVED_FIELD_WARNINGS:
        return
    _MOVED_FIELD_WARNINGS.add(field)
    import sys  # noqa: PLC0415

    print(
        f"⚠ policy.yaml contains a `{field}:` block, but {dedicated_file} "
        f"also defines `{field}`. {dedicated_file} wins; remove "
        f"`{field}:` from policy.yaml to silence this warning.",
        file=sys.stderr,
    )
