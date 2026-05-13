"""Pydantic models for the movate runtime.

Includes the agent specification (parsed from agent.yaml), request/response
contracts, and persisted records.

v0.1 deliberately drops MDK fields that belong to later phases:

  * ``workflow`` (Phase 3 — sequential workflows)
  * ``skills``   (Phase 7 — skills/tools)
  * ``tools``    (Phase 7 — tools)

They will be re-added with proper validation when their phase ships.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

API_VERSION = "movate/v1"
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


# ---------------------------------------------------------------------------
# Agent specification (mirrors agent.yaml)
# ---------------------------------------------------------------------------


class ModelFallback(BaseModel):
    """A fallback target the executor tries after the primary fails."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(..., description="LiteLLM model string, e.g. 'openai/gpt-4o-mini'")
    params: dict[str, Any] = Field(default_factory=dict)


class ModelConfig(BaseModel):
    """Provider + params. Shape depends on the parent :class:`AgentSpec`'s
    ``runtime``:

    * ``runtime: litellm`` (default) → ``provider`` is a LiteLLM-style
      string: ``openai/gpt-4o-mini-2024-07-18`` / ``azure/gpt-4.1`` /
      ``anthropic/claude-sonnet-4-6``.
    * ``runtime: native_anthropic`` → bare Anthropic model id
      (``claude-sonnet-4-6``).
    * ``runtime: native_openai`` → bare OpenAI model id
      (``gpt-4o-mini-2024-07-18``).
    * ``runtime: langchain`` → entry-point spec (``package.module:function``).

    Floating tags (``latest``, ``stable``) are rejected at parse time
    regardless of runtime — a silent provider rotation can't change a
    deployed agent's behavior.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    params: dict[str, Any] = Field(default_factory=dict)
    fallback: list[ModelFallback] = Field(default_factory=list)

    @field_validator("provider")
    @classmethod
    def _reject_floating_tags(cls, v: str) -> str:
        """Always reject -latest / -stable / -newest tags. The
        LiteLLM-style "<provider>/<model>" requirement is runtime-specific
        and lives on :class:`AgentSpec.validate_runtime_provider_shape`
        because it depends on the agent's ``runtime`` field."""
        # Reject the floating tag in the model part (after the slash if
        # present, else the whole string).
        model = v.split("/", 1)[1] if "/" in v else v
        floating = {"latest", "stable", "newest"}
        if model.lower() in floating or model.endswith("-latest"):
            raise ValueError(f"floating model tag rejected: {v!r}; pin to a dated revision")
        return v


class SchemaPaths(BaseModel):
    """Per-agent input + output schema declaration.

    Each field is either:

    * a **path string** (``"./schema/input.json"``) pointing at a full
      JSON Schema document on disk — what every agent shipped before
      v0.6 and what complex contracts (refs, ``oneOf``, regex) still
      use; or
    * an **inline shorthand dict** (``{"message": "string"}``) that
      the loader compiles into JSON Schema. Trivial 2-3-field
      contracts skip the ``schema/`` subfolder entirely. See
      :mod:`movate.core.schema_shorthand` for the syntax.

    The loader dispatches on type — string ⇒ read file, dict ⇒
    compile — so downstream consumers (Executor, prompt linter,
    show, run) keep seeing a plain JSON Schema dict and need no
    changes.
    """

    model_config = ConfigDict(extra="forbid")

    input: str | dict[str, Any]
    output: str | dict[str, Any]


class EvalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: str | None = None
    judge: str | None = None


class Timeouts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_ms: int = Field(default=30_000, ge=1)
    total_ms: int = Field(default=60_000, ge=1)


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_cost_usd_per_run: float = Field(default=1.0, ge=0)


class Objective(BaseModel):
    """A measurable success criterion for an agent.

    Goals are aspirational ("be helpful"); objectives are testable
    ("score >= 0.9 on routing accuracy"). Eval gates can target
    individual objectives by ``id``; per-objective scoring breakdowns
    will surface in the eval results table (v0.7+).

    The ``judge`` field declares HOW this objective is scored. ``exact``
    suits classifiers and structured outputs; ``llm_judge`` suits
    free-form prose — same as the project-level judge config in
    ``evals/judge.yaml``.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description=(
            "Stable identifier — used to gate evals on specific "
            "objectives (``mdk eval --objective routing-accuracy``). "
            "Lowercase, hyphen-separated."
        ),
    )
    description: str = Field(
        default="",
        description="Human-readable explanation for reports and docs.",
    )
    threshold: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Pass score for this objective (0.0-1.0).",
    )
    judge: Literal["exact", "llm_judge"] = Field(
        default="exact",
        description=(
            "Scoring method. ``exact`` for deterministic outputs "
            "(classifiers, extractors); ``llm_judge`` for free-form "
            "prose where a rubric judges semantic quality."
        ),
    )

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$", v):
            raise ValueError(
                f"objective id {v!r} must be lowercase alphanumeric with hyphens or underscores"
            )
        return v


class Example(BaseModel):
    """A sample input → output pair illustrating expected behavior.

    Three uses:
      1. **Smoke test at validate-time** — ``mdk validate`` can run
         these against the agent to catch broken wiring before any
         eval dataset exists.
      2. **In-context examples in prompts** — agents that opt in can
         template these into their prompt for few-shot.
      3. **Test-case generation seed** — scenario test generation
         (v0.7+) uses these as anchors for derived positive / negative
         / edge / red-team cases.

    ``output`` is optional — for agents whose behavior is too varied
    to pin a single expected output, leave it empty and use the example
    purely as input documentation.
    """

    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any] = Field(
        ...,
        description=(
            "Sample input matching the agent's input schema. Validated "
            "against ``schema/input.json`` at ``mdk validate`` time."
        ),
    )
    output: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Expected output. Validated against ``schema/output.json``. "
            "May be empty if the agent's output isn't deterministic enough "
            "to pin."
        ),
    )
    description: str = Field(
        default="",
        description="Human-readable context for the example.",
    )


class AgentRuntime(StrEnum):
    """Which execution path the agent uses to talk to the model.

    All runtimes return the same persisted shape (``RunRecord`` /
    ``Metrics`` / ``ErrorInfo``) — the field only selects which SDK
    or framework gets the actual API call.

    * ``litellm`` (default) — calls
      :class:`movate.providers.litellm.LiteLLMProvider`. Provider
      portability across model families. The agent's ``model.provider``
      is a LiteLLM model string (``openai/gpt-4o-mini-2024-07-18``).

    * ``native_anthropic`` — calls the official ``anthropic`` Python
      SDK directly. Unlocks tool-use, computer-use, prompt caching,
      thinking blocks, vision, and the MCP-server ecosystem. The
      agent's ``model.provider`` is a bare Anthropic model id
      (``claude-sonnet-4-6``). [v0.6 — not yet wired.]

    * ``native_openai`` — calls the official ``openai`` Python SDK
      directly. Unlocks Assistants API, strict structured outputs
      via ``response_format``, vision-with-tools, parallel
      function-calling. The agent's ``model.provider`` is a bare
      OpenAI model id (``gpt-4o-mini-2024-07-18``). [v0.6 — not yet
      wired.]

    * ``langchain`` — the agent's ``model.provider`` is an import
      path to a Python entry-point returning a LangChain
      ``Runnable``; movate invokes it with the validated input.
      Unlocks LCEL composition, LangSmith tracing, and any other
      LangChain feature inside a movate-managed shell (auth,
      persistence, deploy, eval). [v0.6 — not yet wired.]

    * ``lyzr`` — invokes a Lyzr-hosted agent via Lyzr Studio's HTTP
      inference API. The agent's ``model.provider`` is
      ``lyzr/<lyzr-agent-id>`` (e.g.
      ``lyzr/69fe0d9890de3014e9f1cf92``). Requires ``LYZR_API_KEY``
      in env. Pure HTTP — no Lyzr SDK dependency. Read-only:
      evaluates / benchmarks customer agents that already live on
      Lyzr; pairs with ``mdk import lyzr`` for migration to
      MDK-native runtimes. [v0.7]
    """

    LITELLM = "litellm"
    NATIVE_ANTHROPIC = "native_anthropic"
    NATIVE_OPENAI = "native_openai"
    LANGCHAIN = "langchain"
    LYZR = "lyzr"


# ---------------------------------------------------------------------------
# Skill spec — `kind: Skill` in skills/<name>/skill.yaml
# ---------------------------------------------------------------------------
#
# Skills are reusable callables an agent can invoke during a turn. See
# docs/adr/002-skills-and-contexts.md for the full design — input/output
# use the same shorthand schema syntax as agent.yaml (PR #47), the
# implementation backend is pluggable (python | http | mcp) behind a
# common Protocol, and skill cost participates in agent budget accounting.
#
# v0.6 ships `python` only; http + mcp arrive in follow-up PRs. The
# discriminator on `implementation.kind` makes those additions purely
# additive.


class SkillImplementationKind(StrEnum):
    """How a skill is executed when invoked.

    Three backends behind one :class:`SkillBackend` Protocol — the
    skill.yaml contract surface stays the same regardless of which one
    handles execution.
    """

    PYTHON = "python"
    """Python entrypoint resolved via importlib at registration time
    (``pkg.mod:func``). The function is called with the validated input
    dict and a :class:`SkillExecutionContext`."""

    HTTP = "http"
    """POST to a URL; the response body (JSON) is validated against the
    skill's output schema. Lands in a follow-up PR."""

    MCP = "mcp"
    """Route the call through a Model Context Protocol server. Lands in
    a follow-up PR."""


class SkillImplementation(BaseModel):
    """Backend declaration for a skill.

    Two required fields (``kind`` + ``entry``) plus backend-specific
    optional fields. The model keeps ``extra='allow'`` so future
    backends (MCP and beyond) can ship their own fields without
    forcing a model update on every existing skill.yaml.

    HTTP-specific fields (``method``, ``auth``, ``headers``,
    ``timeout_seconds``) are first-class here as of PR #54 — they're
    no-ops for ``kind: python`` and ``kind: mcp``. Validating them
    upfront catches typos at load time rather than per-invocation.
    """

    model_config = ConfigDict(extra="allow")

    kind: SkillImplementationKind = Field(
        default=SkillImplementationKind.PYTHON,
        description="Which backend executes this skill.",
    )
    entry: str = Field(
        default="",
        description=(
            "Backend-specific entrypoint. For ``kind: python`` this is a "
            "``pkg.mod:func`` reference resolved via importlib. For "
            "``kind: http`` it's the URL (may contain ``{{ input.* }}`` "
            "Jinja placeholders). For ``kind: mcp`` it's the server "
            "connection string. Empty string is invalid except when "
            "forward-compatible backends extend the model."
        ),
    )

    # ---- HTTP-only fields (ignored for python/mcp kinds) ----

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = Field(
        default="POST",
        description=(
            "HTTP method for ``kind: http`` skills. Default POST because "
            "most agent tools send a JSON body. GET is fine for pure "
            "lookups."
        ),
    )

    auth: str | None = Field(
        default=None,
        description=(
            "Auth spec for ``kind: http`` skills. Format: "
            "``bearer-from-env:VAR_NAME``. The named env var's value is "
            "sent as ``Authorization: Bearer <value>``. ``None`` = no auth."
            " More auth shapes (basic, header-from-env) ship later."
        ),
    )

    headers: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Static headers added to every HTTP skill invocation. Use for "
            "API versioning, custom user-agents, request-id propagation. "
            "Operator-controlled; the model can't influence these."
        ),
    )

    timeout_seconds: int | None = Field(
        default=None,
        ge=1,
        description=(
            "HTTP-specific timeout override in seconds. ``None`` (default) "
            "falls through to the skill's ``timeout_call_ms`` and ultimately "
            "the calling agent's ``timeouts.call_ms``. Use when an external "
            "API needs longer than the model would otherwise allow."
        ),
    )


class SkillCost(BaseModel):
    """Cost accounting for a skill invocation.

    Each skill call is added to the run's ``metrics.cost_usd`` so the
    existing per-tenant budget and ``policy.max_cost_per_run_usd``
    ceiling enforce skills without any extra plumbing. ``0.0`` is fine
    — most read-only skills (calculator, lookup) genuinely cost
    nothing.
    """

    model_config = ConfigDict(extra="forbid")

    per_call_usd: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "USD charged each time this skill is invoked. Summed into "
            "RunRecord.metrics.cost_usd alongside model token cost."
        ),
    )


class SkillSideEffects(StrEnum):
    """How disruptive a skill is — surfaced for operator review.

    Today these are documentary only (rendered in ``mdk show <skill>``).
    A future PR adds project-policy gates so operators can declare
    "agents in this project may not use ``mutates-state`` skills."
    """

    READ_ONLY = "read-only"
    NETWORK = "network"
    FILESYSTEM = "filesystem"
    MUTATES_STATE = "mutates-state"


class SkillSpec(BaseModel):
    """Parsed ``skills/<name>/skill.yaml`` (api_version: movate/v1, kind: Skill).

    Mirrors :class:`AgentSpec` for the fields that overlap (name,
    version, description, owner, schemas) so operators don't have to
    learn a second mini-format. The differences live in
    :class:`SkillImplementation` (backend pointer) and
    :class:`SkillCost` (budget participation).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    api_version: Literal["movate/v1"] = Field(..., alias="api_version")
    kind: Literal["Skill"] = "Skill"

    name: str = Field(..., min_length=1, max_length=128)
    version: str
    description: str = ""
    owner: str = ""

    # Input/output reuse the same shorthand-OR-path syntax as AgentSpec.
    # See SchemaPaths (PR #47) — string => external JSON Schema file,
    # dict => inline shorthand compiled to JSON Schema at load time.
    schemas: SchemaPaths = Field(..., alias="schema")

    implementation: SkillImplementation
    cost: SkillCost = Field(default_factory=SkillCost)
    side_effects: SkillSideEffects = Field(
        default=SkillSideEffects.READ_ONLY,
        description=(
            "Documentary annotation rendered in ``mdk show <skill>`` and "
            "available for project-policy enforcement in a future PR."
        ),
    )

    tags: list[str] = Field(default_factory=list)

    # Per-skill timeout override. Inherits the agent's
    # ``timeouts.call_ms`` when absent (ADR 002 D3). The agent's
    # ``timeouts.total_ms`` still caps the whole tool-use loop —
    # an individual skill can't bust the per-run total budget even if
    # its own call_ms is generous.
    timeout_call_ms: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Per-call timeout in milliseconds for this skill. ``None`` "
            "(default) inherits the calling agent's ``timeouts.call_ms``."
        ),
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$", v):
            raise ValueError(f"skill name {v!r} must be lowercase alphanumeric with hyphens")
        return v

    @field_validator("version")
    @classmethod
    def _validate_semver(cls, v: str) -> str:
        if not SEMVER_RE.match(v):
            raise ValueError(f"skill version {v!r} must be semver (MAJOR.MINOR.PATCH)")
        return v

    @field_validator("implementation")
    @classmethod
    def _validate_implementation_shape(cls, v: SkillImplementation) -> SkillImplementation:
        """Per-kind shape checks on ``implementation``.

        Each backend has its own constraints on ``entry`` and on which
        sibling fields make sense. We enforce them at load time so a
        typo in skill.yaml fails ``mdk validate`` rather than the
        first per-call dispatch (where the error message would be
        deep in a stack trace instead of "here's the bad field").
        """
        if v.kind == SkillImplementationKind.PYTHON and (not v.entry or ":" not in v.entry):
            raise ValueError(
                f"python skill implementation.entry must be 'pkg.mod:func'; got {v.entry!r}"
            )
        if v.kind == SkillImplementationKind.HTTP:
            if not v.entry:
                raise ValueError(
                    "http skill implementation.entry must be a URL "
                    "(http:// or https://); got empty string"
                )
            lower = v.entry.lower()
            if not (lower.startswith("http://") or lower.startswith("https://")):
                raise ValueError(
                    f"http skill implementation.entry must start with "
                    f"http:// or https://; got {v.entry!r}"
                )
            if v.auth is not None and not v.auth.startswith("bearer-from-env:"):
                raise ValueError(
                    f"http skill implementation.auth must be 'bearer-from-env:<VAR>'; "
                    f"got {v.auth!r}"
                )
        return v


class AgentSpec(BaseModel):
    """Parsed ``agent.yaml`` contents (api_version: movate/v1, kind: Agent)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    api_version: Literal["movate/v1"] = Field(..., alias="api_version")
    kind: Literal["Agent"] = "Agent"

    name: str = Field(..., min_length=1, max_length=128)
    version: str
    description: str = ""
    owner: str = ""

    runtime: AgentRuntime = Field(
        default=AgentRuntime.LITELLM,
        description=(
            "Execution path used to invoke the model. Defaults to "
            "``litellm`` (provider-portable via LiteLLM). Set to "
            "``native_anthropic`` / ``native_openai`` to use the "
            "official SDK directly (unlocks tool-use, structured "
            "outputs, etc.) or ``langchain`` to delegate to a "
            "LangChain Runnable."
        ),
    )

    model: ModelConfig
    prompt: str  # path relative to agent dir
    schemas: SchemaPaths = Field(..., alias="schema")

    evals: EvalConfig = Field(default_factory=EvalConfig)
    timeouts: Timeouts = Field(default_factory=Timeouts)
    budget: Budget = Field(default_factory=Budget)
    tags: list[str] = Field(default_factory=list)

    # ---- v0.7 forward-compatible additions (Deva's strategic feedback) ----
    # All three default to empty lists; existing agent.yaml files that
    # omit them continue to load unchanged. Test generation, per-objective
    # eval scoring, and in-context-example use of these fields lands
    # in v0.7 — the fields themselves are forward-compatible additions
    # only, no behavior change in this PR.

    goals: list[str] = Field(
        default_factory=list,
        description=(
            "Aspirational outcomes the agent achieves. Free-form natural "
            "language; rendered in `mdk show <agent>` and surfaced to "
            "test-case generators in v0.7+. Compare with :data:`objectives` "
            "(measurable success criteria)."
        ),
    )
    objectives: list[Objective] = Field(
        default_factory=list,
        description=(
            "Measurable success criteria with thresholds. Eval gates "
            "can target individual objectives by id (v0.7+); the eval "
            "results table will break out per-objective scores."
        ),
    )
    examples: list[Example] = Field(
        default_factory=list,
        description=(
            "Sample input → output pairs. Used at validate-time as "
            "smoke tests, as in-context examples for prompts that opt in, "
            "and as seeds for scenario test generation in v0.7+."
        ),
    )

    # ---- v0.6 skills + tool-use (ADR 002) ----
    # Names referencing `skills/<name>/skill.yaml` entries in the project's
    # skill registry. The loader resolves these at agent load time; the
    # executor enters a tool-use loop on requests that have skills wired.
    # An agent with `skills: []` (the default) keeps the v0.5 single-shot
    # behavior. See docs/adr/002-skills-and-contexts.md.

    skills: list[str] = Field(
        default_factory=list,
        description=(
            "Names of skills (from the project's skills/ registry) this "
            "agent may invoke during a tool-use loop. Each name must resolve "
            "to a `skills/<name>/skill.yaml` at load time. Empty list keeps "
            "the agent in single-shot mode (no tool-use loop)."
        ),
    )

    # ---- v0.6 shared contexts (ADR 002) ----
    # Names referencing `contexts/<name>.md` files in the project's
    # contexts/ folder. Each named context's body is prepended to the
    # rendered prompt at execution time, in declaration order, with a
    # `\n\n---\n\n` separator. Solves the "stop copy-pasting the style
    # guide into every prompt.md" pain. Pure markdown — no templating,
    # no Python, no Jinja side effects. See docs/adr/002-skills-and-contexts.md.

    contexts: list[str] = Field(
        default_factory=list,
        description=(
            "Names of shared markdown contexts (from the project's contexts/ "
            "folder) prepended to this agent's prompt at render time, in "
            "declaration order. Each name must resolve to a "
            "`contexts/<name>.md` at load time. Empty list = no contexts; "
            "prompt renders exactly as written (v0.5 behavior)."
        ),
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$", v):
            raise ValueError(f"agent name {v!r} must be lowercase alphanumeric with hyphens")
        return v

    @field_validator("version")
    @classmethod
    def _validate_semver(cls, v: str) -> str:
        if not SEMVER_RE.match(v):
            raise ValueError(f"agent version {v!r} must be semver (MAJOR.MINOR.PATCH)")
        return v

    @model_validator(mode="after")
    def _validate_unique_objective_ids(self) -> AgentSpec:
        """Objective IDs must be unique within an agent. Duplicate ids
        would make per-objective eval gating ambiguous — which row does
        ``--objective routing-accuracy`` target? We fail at parse time
        so the duplicate is caught by ``mdk validate``."""
        ids = [obj.id for obj in self.objectives]
        if len(ids) != len(set(ids)):
            duplicates = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(
                f"duplicate objective id(s): {duplicates}. "
                f"Each objective must have a unique id within the agent."
            )
        return self

    @model_validator(mode="after")
    def _validate_runtime_provider_shape(self) -> AgentSpec:
        """Cross-field check: ``model.provider`` shape depends on
        ``runtime``. We enforce here (instead of on :class:`ModelConfig`)
        because the constraint involves both fields.

        * LiteLLM agents need the ``<provider>/<model>`` slash form so
          LiteLLM can route the call.
        * Native-anthropic / native-openai agents take a bare model id
          — the adapter prepends the family prefix for pricing.
        * LangChain agents use an entry-point spec ``package.module:func``
          which must contain a colon (the adapter rejects no-colon
          values, but failing here gives a nicer parse-time error)."""
        provider = self.model.provider
        if self.runtime == AgentRuntime.LITELLM:
            if "/" not in provider:
                raise ValueError(
                    f"provider {provider!r} must be a LiteLLM string in "
                    f"'<provider>/<model>' form (or set "
                    f"`runtime: native_anthropic` / `native_openai` / "
                    f"`langchain` to use a different naming convention)"
                )
        elif self.runtime == AgentRuntime.LANGCHAIN and ":" not in provider:
            raise ValueError(
                f"provider {provider!r} for runtime: langchain must be a "
                f"Python entry-point spec like 'package.module:function'"
            )
        elif self.runtime == AgentRuntime.LYZR and (
            # `lyzr/<agent_id>` — the path is the Lyzr Studio agent ID.
            # Lyzr IDs are 24-hex Mongo ObjectIds; we accept any non-empty
            # path-suffix here and let the adapter surface a clean HTTP
            # error if the ID is wrong.
            not provider.startswith("lyzr/") or len(provider) <= len("lyzr/")
        ):
            raise ValueError(
                f"provider {provider!r} for runtime: lyzr must look like "
                f"'lyzr/<lyzr-agent-id>' (e.g. "
                f"'lyzr/69fe0d9890de3014e9f1cf92')"
            )
        # Native runtimes (anthropic / openai) accept bare or prefixed —
        # adapters tolerate both via pricing_key() normalization.
        return self


# ---------------------------------------------------------------------------
# Runtime request / response contract
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str
    input: dict[str, Any]
    session_id: str | None = None
    request_id: str = Field(default_factory=lambda: str(uuid4()))


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: int = 0
    output: int = 0
    cached_input: int = 0


class Metrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latency_ms: int = 0
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float = 0.0
    provider: str = ""
    pricing_version: str = ""


class ErrorInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    message: str
    retryable: bool = False


class RunResponse(BaseModel):
    """Strict output contract — every agent run returns this shape."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "error", "safety_blocked"]
    run_id: str = ""
    """The run_id of the persisted ``RunRecord`` (v0.5+). Empty on
    pre-v0.5 callers that don't populate it; the worker reads this
    to set ``JobRecord.result_run_id`` after dispatching a job."""
    data: dict[str, Any] = Field(default_factory=dict)
    human_readable: str = ""
    trace_id: str = ""
    metrics: Metrics = Field(default_factory=Metrics)
    error: ErrorInfo | None = None


# ---------------------------------------------------------------------------
# Persisted records
# ---------------------------------------------------------------------------


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    SAFETY_BLOCKED = "safety_blocked"
    DEAD_LETTER = "dead_letter"
    """Terminal: the job exhausted its retry budget on transient errors.

    Distinct from ``ERROR`` (which is "failed once, won't retry") —
    ``DEAD_LETTER`` is "we tried N times and gave up." Operators
    triage with ``movate jobs list --status dead_letter``.
    """


def _now() -> datetime:
    return datetime.now(UTC)


class TenantBudget(BaseModel):
    """Monthly cost ceiling per tenant.

    ``Executor.execute`` queries this at the top of every run; if the
    tenant's current-month cost (sum of ``RunRecord.metrics.cost_usd``
    for runs created since the 1st of the month UTC) meets or exceeds
    ``monthly_usd_limit``, the run is aborted with
    :class:`TenantBudgetExceededError`.

    A tenant with no row in the ``tenant_budgets`` table is
    **unlimited** by default — backwards compatible with v0.x where
    there was no budget enforcement. Operators opt in per-tenant via
    ``movate tenants set-budget``.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    monthly_usd_limit: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "Monthly cost ceiling in USD. ``None`` means unlimited (the row "
            "exists for the audit trail but enforces no cap)."
        ),
    )
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class RunRecord(BaseModel):
    """Persisted record of an agent execution.

    When a run is part of a workflow, ``workflow_run_id`` links it back to
    the parent :class:`WorkflowRunRecord`. Standalone (non-workflow) runs
    leave the field ``None``.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    job_id: str
    tenant_id: str
    agent: str
    agent_version: str
    prompt_hash: str
    provider: str
    provider_version: str
    pricing_version: str
    status: JobStatus
    input: dict[str, Any]
    output: dict[str, Any] | None = None
    metrics: Metrics
    error: ErrorInfo | None = None
    created_at: datetime = Field(default_factory=_now)
    workflow_run_id: str | None = None
    node_id: str | None = None
    """For workflow runs, the id of the workflow node that produced this run."""


class FailureRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_id: str
    run_id: str | None
    tenant_id: str
    agent: str
    failure_type: str
    message: str
    retryable: bool
    created_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Judge config (parsed from agent's evals/judge.yaml)
# ---------------------------------------------------------------------------


class JudgeMethod(StrEnum):
    EXACT = "exact"
    LLM_JUDGE = "llm_judge"


class JudgeConfig(BaseModel):
    """Eval scoring config. Cross-family enforcement happens at eval time."""

    model_config = ConfigDict(extra="forbid")

    method: JudgeMethod = JudgeMethod.EXACT
    model: ModelConfig | None = None
    rubric: str | None = None
    threshold: float = Field(default=0.7, ge=0.0, le=1.0)

    @field_validator("rubric")
    @classmethod
    def _strip_rubric(cls, v: str | None) -> str | None:
        return v.strip() if v else v


class WorkflowStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    """Terminal: at least one node failed; partial state retained."""


class WorkflowRunRecord(BaseModel):
    """Persisted record of one workflow execution.

    Each child agent run carries this id in its ``workflow_run_id`` field;
    join on that to reconstruct the timeline.
    """

    model_config = ConfigDict(extra="forbid")

    workflow_run_id: str
    tenant_id: str
    workflow: str
    workflow_version: str
    status: WorkflowStatus
    initial_state: dict[str, Any]
    final_state: dict[str, Any] | None = None
    error_node_id: str | None = None
    error: ErrorInfo | None = None
    created_at: datetime = Field(default_factory=_now)


class EvalRecord(BaseModel):
    """Persisted summary of one eval run (one dataset, one agent version, N cases)."""

    model_config = ConfigDict(extra="forbid")

    eval_id: str
    tenant_id: str
    agent: str
    agent_version: str
    dataset_hash: str
    judge_method: JudgeMethod
    judge_provider: str | None
    runs_per_case: int
    gate_mode: str
    threshold: float
    mean_score: float
    pass_rate: float
    sample_count: int
    total_cost_usd: float
    created_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Job queue (v0.5+)
#
# A ``JobRecord`` is a queue entry — created on ``POST /run``, claimed by a
# worker, then transitioned to a terminal state. The actual execution
# produces a ``RunRecord`` (or ``WorkflowRunRecord``) that is the source of
# truth for *what happened*; the job table is the source of truth for *what
# was asked for and is it done yet*. They link via ``RunRecord.job_id`` →
# ``JobRecord.job_id`` and ``JobRecord.result_run_id`` →
# ``RunRecord.run_id`` (or ``WorkflowRunRecord.workflow_run_id``).
# ---------------------------------------------------------------------------


class JobKind(StrEnum):
    """What a queued job will execute when claimed."""

    AGENT = "agent"
    WORKFLOW = "workflow"


class JobRecord(BaseModel):
    """Queue entry for an async run.

    Lifecycle:

    * ``QUEUED`` (just inserted, waiting for a worker)
    * ``RUNNING`` (claimed by a worker, ``claimed_at`` set)
    * ``SUCCESS`` / ``ERROR`` / ``SAFETY_BLOCKED`` / ``DEAD_LETTER``
      (terminal, ``completed_at`` and (for success) ``result_run_id`` set)
    * ``QUEUED`` again — re-queue after a transient failure
      (``attempt_count`` incremented, ``next_retry_at`` set in the
      future; ``claim_next_job`` skips until then)

    Re-uses :class:`JobStatus` (defined for ``RunRecord``) so the queue
    and the produced run share a single status vocabulary.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    tenant_id: str
    kind: JobKind
    target: str
    """Agent name or workflow name. Discriminator pairs with ``kind``."""
    status: JobStatus = JobStatus.QUEUED
    input: dict[str, Any]
    """For agent kind: the ``RunRequest.input`` payload. For workflow kind:
    the initial state dict (matches ``WorkflowRunRecord.initial_state``)."""
    result_run_id: str | None = None
    """``run_id`` for agent jobs, ``workflow_run_id`` for workflow jobs.
    Set when the job transitions to a terminal status."""
    error: ErrorInfo | None = None
    api_key_id: str | None = None
    """Which API key submitted the job. Useful for audit + per-key
    rate limiting (which lands later)."""
    created_at: datetime = Field(default_factory=_now)
    claimed_at: datetime | None = None
    completed_at: datetime | None = None
    notify_email: str | None = None
    """Optional email address to notify when the job transitions to a
    terminal status. The worker fires-and-forgets the notification via
    the configured :class:`NotificationDispatcher` — failure to
    deliver never re-queues the job. SMS notifications are deferred
    to a future release (phone-number provisioning + regulatory
    approval are out of band of code)."""
    attempt_count: int = Field(default=0, ge=0)
    """Number of times this job has been dispatched. Starts at 0 on
    insert; incremented every time the worker re-queues after a
    transient failure (``RUNNING`` → ``QUEUED``). When it reaches
    the per-job retry budget, the job lands in ``DEAD_LETTER``
    instead of going back to ``QUEUED``."""
    next_retry_at: datetime | None = None
    """When set, ``claim_next_job`` must skip this row until
    ``now() >= next_retry_at``. ``None`` (the common case for fresh
    jobs and jobs that don't need retry) means "claim immediately."
    Set when the worker re-queues a transient failure; the value is
    ``now + backoff(attempt_count)`` from the retry policy."""


# ---------------------------------------------------------------------------
# API keys (v0.5+)
# ---------------------------------------------------------------------------


class ApiKeyEnv(StrEnum):
    """Hard-separated environments. ``live`` keys MUST NOT work on
    test infra and vice versa — checked at parse time before any DB hit."""

    LIVE = "live"
    TEST = "test"


class ApiKeyRecord(BaseModel):
    """Persisted half of an API key pair.

    The *plaintext* secret is never stored — only the hash + salt. The
    full key string is shown to the user once at mint time and
    permanently irrecoverable after that.
    """

    model_config = ConfigDict(extra="forbid")

    key_id: str
    """13-char base32 random id; doubles as the table primary key."""
    tenant_id: str
    env: ApiKeyEnv
    secret_hash: str
    """SHA-256 hex digest of ``salt || secret``."""
    salt: str
    """16 bytes URL-safe base64. Per-key, prevents rainbow tables."""
    label: str | None = None
    """Optional human-readable note (e.g. ``"ci-bot"``, ``"backfill-script"``)."""
    created_at: datetime = Field(default_factory=_now)
    last_used_at: datetime | None = None
    """Updated async on every successful verify; useful for "stale key" cleanup."""
    revoked_at: datetime | None = None
    """Set by ``movate auth revoke <key-id>``. ``None`` = active."""


# Forward ref resolution
ModelConfig.model_rebuild()
