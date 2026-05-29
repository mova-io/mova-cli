"""Pydantic models for the movate runtime.

Includes the agent specification (parsed from agent.yaml), request/response
contracts, and persisted records.
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


class ReflectionConfig(BaseModel):
    """Self-critique / judge-in-the-loop config for an agent (Phase J-1).

    When ``enabled``, the executor runs the primary completion through
    a *judge* model after schema validation succeeds. The judge sees
    the agent's output + the rubric and emits a structured verdict:
    ``accept`` (no change) or ``revise`` (with feedback).

    On ``revise``, the executor re-prompts the agent ONCE more with
    the judge's feedback appended as a correction directive. The
    second output goes through schema validation again. If the judge
    rejects again, the loop terminates (with a tracer warning) and the
    most recent output is returned — never silently re-prompt past
    ``max_iterations``.

    **Cross-family enforcement**: when ``require_cross_family`` is true
    (the safe default), the judge's provider prefix (e.g. ``anthropic``
    for ``anthropic/claude-haiku-...``) must differ from the agent's
    primary model's provider prefix. Stops the obvious mistake of asking
    GPT-4 to grade GPT-4's own output (sycophancy bias).

    **Cost participation**: judge calls + re-prompts contribute to the
    run's total cost; the agent's :class:`Budget.max_cost_usd_per_run`
    ceiling still applies, so a reflection loop can't blow the budget.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    judge_model: str = Field(
        default="",
        description=(
            "LiteLLM model string for the judge (e.g. "
            "``anthropic/claude-haiku-4-5-20251001``). Required when "
            "``enabled``. Should differ in provider family from the "
            "agent's model — see ``require_cross_family``."
        ),
    )
    rubric: str = Field(
        default="",
        description=(
            "The rubric the judge applies to the agent's output. "
            "Be concrete (e.g. 'SQL must be read-only — reject any "
            "DROP / DELETE / UPDATE / INSERT statements'). Required "
            "when ``enabled``."
        ),
    )
    max_iterations: int = Field(
        default=1,
        ge=1,
        le=3,
        description=(
            "Cap on reflection iterations. ``1`` (default) = at most "
            "one re-prompt after a revise verdict. ``2``+ allows the "
            "loop to revise N times before giving up. Hard cap at 3 "
            "to keep cost bounded — multi-turn reflection past 3 is "
            "rarely worth the cost in practice."
        ),
    )
    require_cross_family: bool = Field(
        default=True,
        description=(
            "When true (the safe default), reject configs where the "
            "judge's provider prefix matches the agent's. Catches the "
            "common mistake of asking a model to grade its own output. "
            "Set to false only when you have a deliberate reason — e.g. "
            "a structured-output judge prompt that's grading format, "
            "not content."
        ),
    )


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
      (``claude-sonnet-4-6``). Requires ``movate-cli[anthropic]``.

    * ``native_openai`` — calls the official ``openai`` Python SDK
      directly. Unlocks Assistants API, strict structured outputs
      via ``response_format``, vision-with-tools, parallel
      function-calling. The agent's ``model.provider`` is a bare
      OpenAI model id (``gpt-4o-mini-2024-07-18``). Requires
      ``movate-cli[openai]``.

    * ``langchain`` — the agent's ``model.provider`` is an import
      path to a Python entry-point returning a LangChain
      ``Runnable``; movate invokes it with the validated input.
      Unlocks LCEL composition, LangSmith tracing, and any other
      LangChain feature inside a movate-managed shell (auth,
      persistence, deploy, eval). Requires
      ``movate-cli[langchain]``.

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

    Four backends behind one :class:`SkillBackend` Protocol — the
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

    AGENT = "agent"
    """Call a deployed MDK agent as a tool. The target agent is looked
    up in the runtime registry and called synchronously. Enables
    cross-agent orchestration without the v1.1 LangGraph machinery."""


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

    # ---- MCP-only field (ignored for python/http kinds) ----

    tool: str | None = Field(
        default=None,
        description=(
            "MCP tool name to invoke on the server. The ``entry`` field "
            "names the subprocess to spawn (the MCP server); ``tool`` "
            "names the specific tool exposed by that server to call when "
            "the skill is invoked. Required for ``kind: mcp``; ignored "
            "for python and http."
        ),
    )

    # ---- Agent-only fields (ignored for python/http/mcp kinds) ----

    target_agent: str | None = Field(
        default=None,
        description=(
            "Name of the deployed MDK agent to call. Required for "
            "``kind: agent``; ignored for all other kinds. Must match "
            "the agent's registered name in the runtime registry."
        ),
    )

    timeout_s: int = Field(
        default=30,
        ge=1,
        description=(
            "Timeout in seconds for the sub-agent call. Default 30. "
            "Applies to ``kind: agent`` only; ignored for all other kinds."
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
        if v.kind == SkillImplementationKind.MCP:
            if not v.entry:
                raise ValueError(
                    "mcp skill implementation.entry must be the subprocess "
                    "command for the MCP server (e.g. './mcp-servers/github' "
                    "or 'npx -y @some/mcp-package'); got empty string"
                )
            if not v.tool:
                raise ValueError(
                    "mcp skill implementation.tool is required (the name "
                    "of the tool to invoke on the MCP server); empty tool "
                    "would mean 'no tool selected'"
                )
        if v.kind == SkillImplementationKind.AGENT and not v.target_agent:
            raise ValueError(
                "agent skill implementation.target_agent is required "
                "(the name of the deployed MDK agent to call); "
                "empty target_agent would mean 'no agent selected'"
            )
        return v


class AgentMetadata(BaseModel):
    """Optional marketplace metadata block for an agent (``metadata:`` in agent.yaml).

    All fields are optional with defaults of ``None`` / ``[]`` so existing
    ``agent.yaml`` files that omit the block continue to load unchanged
    (backward-compatible).

    The Mova iO Agent Marketplace UI reads these fields as the source of truth
    for catalog cards, profile pages, search facets, and the example gallery.
    ``mdk show`` renders a "Marketplace metadata" section when the block is
    present; ``mdk validate`` type-checks the field values and emits advisory
    warnings for common mistakes (missing ``output`` key in examples, etc.).

    Usage in agent.yaml::

        metadata:
          persona: "A friendly FAQ bot for Acme Corp"
          role: "customer-support"
          capabilities:
            - "question-answering"
            - "knowledge-retrieval"
          tags:
            - "faq"
            - "support"
          examples:
            - input: {question: "What is your return policy?"}
              output: {answer: "30 days, no questions asked."}
          owner: "team-support@acme.com"
    """

    model_config = ConfigDict(extra="forbid")

    persona: str | None = Field(
        default=None,
        max_length=512,
        description=(
            "One-line description of the agent's role and voice. "
            "Example: 'A friendly FAQ bot for Acme Corp'. "
            "Rendered on the marketplace card; used by prompt-authoring "
            "tooling as a style anchor."
        ),
    )
    role: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Taxonomy tag for the agent's job category. "
            "Example: 'customer-support', 'data-analysis'. "
            "Used by the marketplace for grouping and filtering. "
            "Execution-semantic: none — catalog metadata only."
        ),
    )
    capabilities: list[str] = Field(
        default_factory=list,
        description=(
            "Slug-style list of capabilities. "
            "Example: ['question-answering', 'knowledge-retrieval']. "
            "Each entry must be lowercase alphanumeric with hyphens "
            "(URL-safe search facets). Use 'tags' for free-form labels."
        ),
    )
    tags: list[str] = Field(
        default_factory=list,
        description=(
            "Free-form searchable tags. No slug constraint — any string accepted. "
            "Example: ['faq', 'support', 'acme-corp']."
        ),
    )
    examples: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Sample input/output pairs for the marketplace card. "
            "Each entry must have 'input' and 'output' keys. "
            "Example: [{'input': {'q': 'What?'}, 'output': {'a': '...'}}]"
        ),
    )
    owner: str | None = Field(
        default=None,
        description=(
            "Owner email address or team name. "
            "Example: 'team-support@acme.com' or 'Platform Team'. "
            "Displayed on the marketplace card for accountability."
        ),
    )

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(cls, v: list[str]) -> list[str]:
        """Each capability must be a URL-safe slug (lowercase alphanumeric + hyphens)."""
        for cap in v:
            if not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$", cap):
                raise ValueError(
                    f"capability {cap!r} must be lowercase alphanumeric "
                    f"with hyphens (e.g. 'question-answering'); use 'tags' for "
                    f"free-form labels"
                )
        return v


class RetrievalConfig(BaseModel):
    """KB retrieval pipeline configuration for an agent's
    ``kb-vector-lookup`` skill.

    Mirrors the flags ``mdk kb search`` exposes on the command line
    (``--hybrid`` / ``--rewrite N`` / ``--rerank`` / ``--multi-hop N``)
    so an operator who tuned retrieval interactively can lock in the
    same settings for their deployed agent. All fields default to
    "off" → the v0.9 default of vector-only is preserved for agents
    that don't opt in.

    Example ``agent.yaml``::

        api_version: movate/v1
        kind: Agent
        name: refund-helper
        ...
        retrieval:
          hybrid: true
          rewrite: 3
          rerank: true
          multi_hop: 0

    Cost / latency stacking (per agent call):

    * vector only: ~50ms embedding round-trip
    * +hybrid: ~+5ms (BM25 is in-memory)
    * +rewrite=N: ~+200ms (one rewriter LLM call) + N+1 retrieval
      fan-outs
    * +rerank: ~+200ms (one reranker LLM call)
    * +multi-hop=N: up to N planner LLM calls + N retrieval passes

    The operator decides the right cost trade-off for their use case.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    hybrid: bool = Field(
        default=False,
        description=(
            "Combine vector + BM25 lexical search via RRF. Typically "
            "15-25% better recall on real corpora — catches rare-term "
            "hits (product names, error codes) that vector alone "
            "blurs out. No extra API cost; BM25 runs locally."
        ),
    )
    rewrite: int = Field(
        default=0,
        ge=0,
        le=8,
        description=(
            "Expand the question into N alternative paraphrases via a "
            "small LLM, fan out retrieval across all N+1 variants, "
            "RRF-fuse the rankings. Catches vague queries that miss "
            "specific KB terminology. Adds ~200ms + ~$0.0001/query. "
            "0 = disabled (default)."
        ),
    )
    rerank: bool = Field(
        default=False,
        description=(
            "Add a rerank stage that re-scores upstream candidates "
            "by relevance, correcting 'noisy top-K' where vector / BM25 "
            "scores rank irrelevant chunks high. See ``rerank_mode`` for "
            "which backend to use."
        ),
    )
    rerank_mode: str = Field(
        default="llm",
        description=(
            "Which rerank backend to use when ``rerank=true``. "
            "``llm`` (default) — one batched LLM call via LiteLLM "
            "(~200ms, ~$0.0002/query, zero extra deps). "
            "``cross_encoder`` — local sentence-transformers "
            "cross-encoder (~50ms CPU, zero API cost, requires "
            "``pip install movate-cli[cross-encoder]`` ~300MB). "
            "``rerank_model`` controls the specific model for "
            "whichever mode is active."
        ),
    )
    multi_hop: int = Field(
        default=0,
        ge=0,
        le=5,
        description=(
            "Iterative retrieve → reason → retrieve loop. Best on "
            "multi-fact questions ('how does X interact with Y?'). "
            "Each hop runs the full pipeline; a planner LLM decides "
            "'done' or generates a refined sub-query. Adds N planner "
            "calls + N retrieval passes. 0 = disabled (default)."
        ),
    )

    # ---- per-agent budget overrides (PR-W) ----
    # Optional overrides for the process-wide defaults. ``None``
    # means "use the runtime's default"; integers override. Lets
    # operators with verbose-turn threads dial budgets up + operators
    # with simple FAQ agents dial down to save tokens.

    multi_hop_max_total_chunks: int | None = Field(
        default=None,
        ge=1,
        le=30,
        description=(
            "Per-agent override for the multi-hop aggregated-chunks "
            "cap. ``None`` = use the process default (15). Capped at "
            ":data:`movate.kb.multi_hop.MAX_TOTAL_CHUNKS_CAP` (30)."
        ),
    )
    history_turns: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description=(
            "Per-agent override for the number of prior turns the "
            "messages endpoint injects under "
            "``input.conversation_history``. ``None`` = use the "
            "process default (20)."
        ),
    )
    history_char_budget: int | None = Field(
        default=None,
        ge=1000,
        le=200_000,
        description=(
            "Per-agent override for the char-budget cap on injected "
            "conversation history. ``None`` = use the process default "
            "(40000 ≈ 10k tokens). Larger budgets pack more context "
            "but eat into the model's available output budget."
        ),
    )
    history_summarize: bool = Field(
        default=False,
        description=(
            "Smarter alternative to PR-U's raw budget truncation. When "
            "True AND the raw history exceeds the char budget, the "
            "OLDEST turns get compressed via a small LLM into a single "
            "synthetic 'earlier in this conversation: ...' entry so "
            "the agent sees the GIST of earlier context instead of "
            "losing it entirely. Adds ~200ms + ~$0.0002 per message "
            "(only when over budget). Default False keeps the v0.9 "
            "raw-truncation behavior for back-compat."
        ),
    )

    # ---- ADR 023: opt-in declarative pre-retrieval (auto-RAG) ----
    # These fields configure the Executor's pre-retrieval phase — a
    # deterministic "fetch grounding for me before the model sees the
    # prompt" directive. They are ORTHOGONAL to the pipeline-tuning
    # fields above (hybrid / rewrite / rerank / multi_hop), which the
    # `kb-vector-lookup` skill reads regardless of how it's invoked.
    #
    # The feature is OFF unless `auto_into` is set — with `auto_into`
    # unset (the default), `Executor.execute` runs byte-for-byte as
    # today: no pre-retrieval, no extra embedding call, no behavior
    # change. This keeps the dominant non-RAG path untouched
    # (CLAUDE.md compat rule 5; ADR 023 D1).

    auto_into: str | None = Field(
        default=None,
        description=(
            "ADR 023 — REQUIRED to enable opt-in pre-retrieval. Names "
            "the input field that receives the retrieved chunk texts "
            "(as a `list[string]`) BEFORE the prompt is rendered. With "
            "this unset (default), the Executor's pre-retrieval phase "
            "is skipped entirely and behavior is byte-for-byte "
            "unchanged. The named field's schema must accept a "
            "`list[string]` — checked by `mdk validate`."
        ),
    )
    query_from: str | None = Field(
        default=None,
        description=(
            "ADR 023 — the input field whose string value seeds the "
            "retrieval query. Optional; defaults to the agent's primary "
            "(sole, or canonically-named) string input field. `mdk "
            "validate` errors if unset AND the primary field is "
            "ambiguous. Only meaningful when `auto_into` is set."
        ),
    )
    auto_skill: str = Field(
        default="kb-vector-lookup",
        alias="skill",
        description=(
            "ADR 023 — the retrieval skill the Executor pre-invokes "
            "through the existing SkillBackend dispatch seam. Must be "
            "one of the agent's declared `skills:` and resolve in the "
            "skill registry (checked by `mdk validate`). Defaults to "
            "`kb-vector-lookup`. Only meaningful when `auto_into` is set."
        ),
    )
    top_k: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description=(
            "ADR 023 — passed through to the retrieval skill's input "
            "(as `k`) when set. Controls how many chunks the "
            "pre-retrieval phase requests. `None` leaves the skill's "
            "own default. Only meaningful when `auto_into` is set."
        ),
    )
    when: Literal["if_empty", "always"] = Field(
        default="if_empty",
        description=(
            "ADR 023 — when the pre-retrieval phase fires. `if_empty` "
            "(default) retrieves only when `auto_into` is absent/empty "
            "in the request input, so an explicitly-passed value is "
            "respected (preserves eval determinism). `always` "
            "re-retrieves unconditionally. Only meaningful when "
            "`auto_into` is set."
        ),
    )
    on_error: Literal["warn", "fail"] = Field(
        default="warn",
        description=(
            "ADR 023 — failure policy for the pre-retrieval phase. "
            "`warn` (default) proceeds ungrounded with a stderr notice "
            "if retrieval errors; `fail` aborts the run with a typed "
            "error. A missing retriever / empty KB is always a no-op "
            "notice (never a failure) regardless of this setting. Only "
            "meaningful when `auto_into` is set."
        ),
    )

    @property
    def auto_retrieval_enabled(self) -> bool:
        """True when ADR 023 pre-retrieval is opted in (``auto_into`` set).

        The single gate the Executor checks. With this False, the whole
        pre-retrieval phase is skipped and the non-RAG path is
        byte-for-byte unchanged.
        """
        return bool(self.auto_into)

    def is_default(self) -> bool:
        """True when every field is at its default value.

        Used by the skill template to skip kwargs entirely when the
        operator hasn't opted in — keeps ``kb_search()`` calls byte-
        for-byte unchanged from the pre-PR-I default path.
        """
        return (
            not self.hybrid
            and self.rewrite == 0
            and not self.rerank
            and self.rerank_mode == "llm"
            and self.multi_hop == 0
            and self.multi_hop_max_total_chunks is None
            and self.history_turns is None
            and self.history_char_budget is None
            and not self.history_summarize
        )


class AgentSpec(BaseModel):
    """Parsed ``agent.yaml`` contents (api_version: movate/v1, kind: Agent)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    api_version: Literal["movate/v1"] = Field(..., alias="api_version")
    kind: Literal["Agent"] = "Agent"

    name: str = Field(..., min_length=1, max_length=128)
    version: str
    description: str = ""
    owner: str = ""

    # ---- v0.8 Mova iO marketplace metadata (item 29 / Group F) ----
    # All three default to empty; existing agent.yaml files load
    # unchanged. The Mova iO Agent Marketplace UI (separate product)
    # reads these as the source of truth for catalog / profiles /
    # search / reviews; mdk show renders them and mdk validate
    # type-checks them. Free-form natural language for persona + role
    # (consumers don't enumerate values); capabilities is a slug list
    # for search facets.

    persona: str = Field(
        default="",
        max_length=512,
        description=(
            "Voice / tone / character of the agent's responses, in one "
            "sentence. Example: 'Concise, technical, slightly dry — "
            "answers in 1-2 lines, never apologetic.' Free-form natural "
            "language; used by the marketplace to render a Profile and "
            "by prompt-authoring tooling as a style anchor."
        ),
    )
    role: str = Field(
        default="",
        max_length=128,
        description=(
            "Job category this agent fills, as a short noun phrase. "
            "Example: 'support-triage', 'data-analyst', 'returns-processor'. "
            "Used by the marketplace for grouping + filtering. "
            "Execution-semantic: none — role-based routing is not implemented; "
            "this field is catalog metadata only."
        ),
    )
    capabilities: list[str] = Field(
        default_factory=list,
        description=(
            "Slug-style list of things this agent can do. Example: "
            "['faq-lookup', 'ticket-routing', 'language-detection']. "
            "Each entry must be lowercase alphanumeric with hyphens "
            "(matches tag rules). Used by the marketplace as search "
            "facets; complements free-form `tags` (which can be any "
            "string). Execution-semantic: none — catalog metadata only."
        ),
    )

    # ---- v0.8 nested metadata block (item 29 / Group F) ----
    # Optional nested block consolidating ALL marketplace discovery fields.
    # When present, ``mdk show`` renders a dedicated "Marketplace metadata"
    # section and ``mdk validate`` type-checks each sub-field. When absent,
    # the existing flat-field behavior (persona / role / capabilities above)
    # is unchanged — backward-compatible.

    metadata: AgentMetadata | None = Field(
        default=None,
        description=(
            "Optional nested marketplace metadata block. When present, "
            "``mdk show`` renders a dedicated 'Marketplace metadata' section "
            "and ``mdk validate`` checks field values (owner email shape, "
            "examples have input+output keys, etc.). Omit entirely to keep "
            "the pre-v0.8 compact table (no section rendered, no checks run). "
            "See :class:`AgentMetadata` for the full field list."
        ),
    )

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
    reflection: ReflectionConfig = Field(
        default_factory=ReflectionConfig,
        description=(
            "Self-critique / judge-in-the-loop config (Phase J-1). When "
            "enabled, a different model grades the agent's output against "
            "a rubric and may trigger one or more re-prompts. Disabled by "
            "default — opt in per-agent. See :class:`ReflectionConfig`."
        ),
    )
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

    knowledge: str | None = Field(
        default=None,
        description=(
            "Path (relative to the agent dir) to a `knowledge.yaml` "
            "declaring a retriever + corpus this agent can query. When "
            "set, the loader resolves the file into a `KnowledgeConfig`, "
            "builds the configured retriever, and stashes it on the "
            "loaded `AgentBundle` as `bundle.retriever`. v0.7 ships "
            "BM25 + substring in-memory retrievers; v0.8 will add "
            "embedding backends (pgvector / Azure AI Search) using the "
            "same interface — agents won't need to migrate. "
            "Omit (the default) for agents that don't need RAG."
        ),
    )

    # ---- v0.9 KB retrieval configuration (Tier 10 RAG enhancements) ----
    # Optional `retrieval:` block that the `kb-vector-lookup` skill reads
    # at agent run time. Without this block, the skill uses pure vector
    # retrieval (the v0.9 default). With it, agents opt into hybrid +
    # BM25 + rewriter + rerank + multi-hop on a per-agent basis — the
    # same flags the CLI exposes via `mdk kb search --hybrid ...`.
    #
    # Design choice: per-AGENT config (not per-call), because the
    # operator wants their PRODUCTION agent to use the full stack
    # consistently, not toggle it per request. Per-call override stays
    # available via direct `kb_search(...)` kwargs for advanced uses.

    retrieval: RetrievalConfig = Field(
        default_factory=RetrievalConfig,
        description=(
            "KB retrieval pipeline configuration for the agent's "
            "`kb-vector-lookup` skill. Defaults to vector-only "
            "(`hybrid=false, rewrite=0, rerank=false, multi_hop=0`) "
            "which preserves pre-v0.9 behavior — existing agents "
            "load unchanged. See :class:`RetrievalConfig` for fields."
        ),
    )

    grounding_enforcement: Literal["off", "warn", "strict"] = Field(
        default="off",
        description=(
            "Post-execution grounding check mode for RAG agents. "
            "Checks that the output's `grounded` flag is consistent with "
            "the retrieved KB context, and that all `citations` indices are "
            "valid.\n\n"
            "* ``off`` (default) — no check; existing agents unaffected.\n"
            "* ``warn`` — violations logged as tracer events; run succeeds.\n"
            "* ``strict`` — a grounding violation raises a "
            "``GroundingViolationError`` and the run status becomes "
            "``safety_blocked``. Use for production RAG agents where "
            "hallucinated answers are worse than a refusal.\n\n"
            "Only meaningful for agents that use a `kb-vector-lookup` "
            "skill or otherwise produce a `grounded` / `citations` output "
            "field. Non-RAG agents should leave this ``off``."
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

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(cls, v: list[str]) -> list[str]:
        """Each capability must be a slug (matches tag rules).

        Same regex as agent name. Strict because the marketplace uses
        these as URL-safe search facets; a stray space or uppercase
        char breaks the catalog query. Free-form descriptive text
        belongs in ``description`` or ``persona``.
        """
        for cap in v:
            if not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$", cap):
                raise ValueError(
                    f"capability {cap!r} must be lowercase alphanumeric "
                    f"with hyphens (e.g. 'faq-lookup'); use 'tags' for "
                    f"free-form labels"
                )
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

    @model_validator(mode="after")
    def _validate_reflection(self) -> AgentSpec:
        """Cross-field checks for :class:`ReflectionConfig` (Phase J-1).

        When ``reflection.enabled`` is true, the operator must supply
        ``judge_model`` and ``rubric`` — silently no-opping a disabled
        reflection because a field is empty would be confusing. Cross-
        family enforcement guards against the sycophancy-bias mistake
        of asking the agent to grade itself.

        Skipped entirely when reflection is disabled (the default) so
        existing agent.yaml files load unchanged.
        """
        if not self.reflection.enabled:
            return self

        if not self.reflection.judge_model.strip():
            raise ValueError(
                "reflection.enabled=true requires reflection.judge_model "
                "(e.g. 'anthropic/claude-haiku-4-5-20251001')"
            )
        if not self.reflection.rubric.strip():
            raise ValueError(
                "reflection.enabled=true requires a non-empty "
                "reflection.rubric describing the judge's criteria"
            )

        if self.reflection.require_cross_family:
            # Provider family = the prefix before the first '/' in the
            # LiteLLM model string. ``openai/gpt-4o-mini`` → ``openai``;
            # ``anthropic/claude-...`` → ``anthropic``. Bare model ids
            # (native runtimes) are normalised to the runtime's family
            # for this comparison.
            agent_family = _provider_family(self.model.provider, self.runtime)
            judge_family = _provider_family(self.reflection.judge_model, None)
            if agent_family and judge_family and agent_family == judge_family:
                raise ValueError(
                    f"reflection.judge_model {self.reflection.judge_model!r} "
                    f"comes from the same provider family ({judge_family!r}) "
                    f"as the agent's model ({self.model.provider!r}). "
                    f"Cross-family judging defeats sycophancy bias; set "
                    f"reflection.require_cross_family=false to override "
                    f"(rarely the right call)."
                )

        return self


def _provider_family(provider: str, runtime: AgentRuntime | None) -> str:
    """Extract the provider-family prefix from a model string.

    LiteLLM strings (``openai/gpt-4o-mini``) split on '/'. Bare model
    ids (used by native runtimes) map to the runtime's family. Returns
    empty string when no family can be inferred — callers should treat
    empty as "skip the cross-family check rather than guess."
    """
    if "/" in provider:
        return provider.split("/", 1)[0].lower()
    if runtime == AgentRuntime.NATIVE_ANTHROPIC:
        return "anthropic"
    if runtime == AgentRuntime.NATIVE_OPENAI:
        return "openai"
    return ""


# ---------------------------------------------------------------------------
# Runtime request / response contract
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str
    input: dict[str, Any]
    session_id: str | None = None
    """Groups runs into a conversation in Langfuse. Propagated to the root trace."""
    user_id: str | None = None
    """Identifies the end-user making the request. Propagated to the root trace
    for per-user filtering in Langfuse. Optional — CLI runs omit it."""
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
    trace_id: str = ""
    """Langfuse / OTel trace ID for the run. Populated by the executor from
    ``span.trace_id`` so feedback endpoints can attach scores to the correct
    trace without a separate lookup. Empty when tracing is off (SilentTracer)."""


class ErrorInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    message: str
    retryable: bool = False
    hint: str | None = None
    """Optional operator-facing remediation pointer.

    Surfaced beneath the bare message to reduce confusion when the
    underlying cause is a known limitation with a workaround already
    documented in the runbook. Set sparingly — every hint here is a
    bet that the error class is recurring + that the pointer text
    won't go stale as the platform evolves. Example: the worker
    ``unknown_agent`` outcome points callers at the cross-pod
    bundle-sync gap (item 109) and the ``?wait=true`` workaround
    (item 110)."""


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
    CANCELLED = "cancelled"
    """Terminal: the job was cancelled by an operator (item 36, R4b).

    Cancellation is **cooperative**, not pre-emptive — there is no
    mid-LLM-call interruption. A ``QUEUED`` job is cancelled atomically
    (the claim path only takes ``queued`` rows, so it's never picked up).
    A ``RUNNING`` job is flagged via ``JobRecord.cancel_requested``; the
    worker honors the flag at a checkpoint (either skips a not-yet-started
    job, or — if it was claimed before the request — finishes the in-flight
    work but discards the outcome and writes ``CANCELLED`` instead of the
    dispatch result). ``CANCELLED`` is terminal: it is NEVER retried.
    Operators triage with ``movate jobs list --status cancelled``.
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


class TenantProviderKey(BaseModel):
    """A tenant's own provider API key, encrypted at rest (ADR 018, BYOK).

    Each tenant can store its own OpenAI/Anthropic/etc. provider key so its
    LLM spend, blast radius, and rotation are its own — resolved
    tenant-key-first at run time, with the shared fleet key as a back-compat
    fallback (see :class:`movate.core.provider_keys.ProviderKeyResolver`).

    ``(tenant_id, provider)`` is the unique key — one stored key per provider
    per tenant; a re-set overwrites in place (rotation). Unlike an API key
    (which we only ever *verify*, so a one-way hash suffices) a provider key
    must be *decrypted to use*, so the secret is stored as a Fernet
    ``ciphertext`` — symmetric encryption keyed by ``MOVATE_PROVIDER_KEY_SECRET``
    (:mod:`movate.core.provider_keys`). The plaintext key is **never**
    persisted, **never** returned by any API, and **redacted** in logs/traces;
    ``fingerprint`` is a non-sensitive masked tail (e.g. ``…AbCd``) shown in
    listings so an operator can recognise which key is configured without ever
    decrypting it.

    Additive + default-off: a row exists only when a tenant brings its own
    key. With no row (and the shared-key fallback on, the default) the run
    path is byte-for-byte today's — the provider uses its env-default key.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    provider: str
    """Provider credential namespace the key belongs to — ``openai`` /
    ``anthropic`` / etc. This is the LiteLLM-style *head* prefix (the part
    before the first ``/`` in a ``model.provider`` string), so a tenant's
    ``openai`` key applies to every ``openai/<model>`` an agent runs. Paired
    with ``tenant_id`` as the unique key."""
    ciphertext: str
    """Fernet token (url-safe base64 text) of the plaintext provider key.
    Decrypted only inside :class:`ProviderKeyResolver`; never returned by an
    API or logged. Stored as TEXT (Fernet tokens are ASCII)."""
    fingerprint: str
    """Non-sensitive masked tail of the plaintext key (e.g. last 4 chars,
    ``…AbCd``) for display. Lets a listing show *which* key is configured
    without decrypting — a human affordance, not a security boundary."""
    created_by: str | None = None
    """Auth identity that set the key (ADR 013), or ``None`` for a local/CLI
    write."""
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class SkillCallRecord(BaseModel):
    """One tool/skill invocation captured inside a run's tool-use loop.

    Stored as a JSON array in ``RunRecord.skill_calls`` so ``mdk explain
    --steps`` can render per-step breakdown without requiring a Langfuse
    backend.  Optional — agents with no skills leave the list empty.
    """

    model_config = ConfigDict(extra="forbid")

    step: int
    """1-based position in the tool-use loop (step 1 = first tool call)."""
    skill: str
    """Skill name as it appears in the agent's ``skills:`` list."""
    input: dict[str, Any]
    """The arguments the LLM passed to the tool call."""
    output: dict[str, Any] | None = None
    """The skill's return value on success."""
    error: str | None = None
    """Short error description when the skill raised a ``SkillError``."""
    latency_ms: float = 0.0
    """Wall-clock time from dispatch to result, in milliseconds."""
    cost_usd: float = 0.0
    """Per-call skill cost (ADR 002 ``cost.per_call_usd``) for THIS invocation.

    Additive (ADR 024 D2 / D5). Defaults to ``0.0`` so older persisted
    records — and free / errored skill calls — load and render unchanged.
    Summed into ``Metrics.cost_usd`` alongside the per-turn LLM costs."""
    turn: int = 0
    """1-based index of the LLM turn that requested this skill call.

    Ties each tool call back to the :class:`TurnRecord` whose
    ``index`` matches, so ``mdk explain`` can nest ``skill.*`` under the
    turn that dispatched it (ADR 024 D1/D2). The tool-use loop derives
    this from the same per-turn counter that drives the nested
    ``agent.turn[i]`` spans — i.e. it equals the historical ``step``
    value. ``0`` on legacy records / the pre-retrieval phase (turn 0)."""


class TurnRecord(BaseModel):
    """One LLM round-trip ("turn") inside a run's tool-use loop (ADR 024 D2).

    A run is a sequence of turns; each turn may dispatch zero or more
    :class:`SkillCallRecord` tool calls (linked by ``turn`` == ``index``).
    Persisted as a JSON array on :attr:`RunRecord.turns` so ``mdk explain``
    can reconstruct the per-step cost / latency / token breakdown **offline**
    — no Langfuse / OTel backend required (the same offline-first rationale
    that already justifies persisting ``skill_calls``).

    Additive + backward-compatible: a run with no turns (legacy record, or
    an agent whose single completion predates this field) leaves
    ``RunRecord.turns`` an empty list and renders as a single node.
    """

    model_config = ConfigDict(extra="forbid")

    index: int
    """1-based position of this turn in the tool-use loop (turn 1 = first
    LLM round-trip)."""
    model: str
    """The provider string that produced this turn (the chosen fallback)."""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    """LLM cost for THIS turn's completion (Σ over turns + Σ skill costs =
    ``Metrics.cost_usd``). ``0.0`` for an LLM-cache-hit turn or a runtime
    with no pricing key — matching the run-level cost semantics."""
    latency_ms: int = 0
    """Wall-clock time of this turn's ``provider.complete`` round-trip."""
    finish_reason: str | None = None
    """Optional model-reported stop reason (``"final"`` / ``"tool_use"`` in
    the executor's vocabulary). ``None`` when not captured."""


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
    thread_id: str | None = None
    """For multi-turn conversational runs (Tier 10.5 / PR-N), the id of the
    :class:`ConversationThread` this run belongs to. Standalone runs leave
    this ``None``. The runtime joins on this column to fetch prior turns
    when rendering the next message's prompt."""
    skill_calls: list[SkillCallRecord] = Field(
        default_factory=list,
        description=(
            "Per-step skill/tool invocations captured during the tool-use loop. "
            "Empty for single-shot agents (no skills). Populated by the executor "
            "and persisted as a JSON blob so `mdk explain --steps` can render the "
            "decision chain without a Langfuse backend."
        ),
    )
    turns: list[TurnRecord] = Field(
        default_factory=list,
        description=(
            "Per-turn LLM round-trips captured during the tool-use loop (ADR 024 "
            "D2). One entry per `provider.complete` call, carrying that turn's "
            "model / tokens / cost / latency. Empty on legacy records and renders "
            "as a single node — additive, backward-compatible. Persisted as a JSON "
            "blob so `mdk explain` can reconstruct the per-step breakdown offline, "
            "without a Langfuse / OTel backend."
        ),
    )


class ConversationThread(BaseModel):
    """A multi-turn conversation with a single agent (Tier 10.5, PR-N).

    Threads group runs together so the runtime can fetch the last N turns
    when rendering the next message's prompt. Without threads, every
    ``mdk submit`` is a fresh single-shot call — operators can't ask
    follow-up questions ("and what about the prorated case?") that
    reference earlier context.

    Storage contract:

    * One thread per agent per tenant. Cross-agent threads aren't a
      thing in v0.9 — an operator who wants to swap agents mid-thread
      should start a new one.
    * ``thread_id`` is the public identifier (URL-safe hex uuid).
    * ``RunRecord.thread_id`` references this when the run was
      submitted as part of the thread.
    * Threads accumulate forever in v0.9 — no built-in TTL or message
      count cap. Operators with thread-cleanup needs can call
      ``storage.list_conversation_threads`` and prune by ``created_at``.

    Design choices:

    * **No message-body storage on the thread itself**. The thread is
      a join key; the actual conversation lives in the per-run
      ``RunRecord.input`` + ``RunRecord.output`` fields. This keeps
      the schema small + lets us evolve message shape without
      migrating thread rows.
    * **Optional ``title``** rendered by clients (Chainlit playground,
      Mova iO Angular). Empty string = no title; clients should fall
      back to the first message's truncated text.
    """

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    tenant_id: str
    agent: str
    title: str = ""
    """Optional human-readable label for client display."""
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    """Refreshed on every appended message so clients can sort
    threads most-recently-active first."""


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


class ProductionReadiness(StrEnum):
    """Four-band production readiness verdict from the 10-category weighted scorecard.

    Composite score (0-100) maps to:
      ≥ 90  → PRODUCTION_READY   — approve for production with standard monitoring
      80-89 → PILOT_READY        — limited pilot with human review on flagged failures
      70-79 → NEEDS_IMPROVEMENT  — hold promotion; address top failure clusters
      < 70  → NOT_READY          — do not promote; resolve critical issues first

    Hard gates (safety ≥ 0.95, no critical deterministic failure) can override
    the numeric verdict downward regardless of composite score.
    """

    PRODUCTION_READY = "production_ready"
    PILOT_READY = "pilot_ready"
    NEEDS_IMPROVEMENT = "needs_improvement"
    NOT_READY = "not_ready"


class JudgeMethod(StrEnum):
    EXACT = "exact"
    LLM_JUDGE = "llm_judge"
    PANEL = "panel"
    """Multi-judge panel: N judges score independently, variance check,
    optional escalation to a tiebreaker when std_dev > variance_threshold."""


class JudgeConfig(BaseModel):
    """Eval scoring config. Cross-family enforcement happens at eval time."""

    model_config = ConfigDict(extra="forbid")

    method: JudgeMethod = JudgeMethod.EXACT
    model: ModelConfig | None = None
    rubric: str | None = None
    threshold: float = Field(default=0.7, ge=0.0, le=1.0)

    # Panel-mode fields (ignored unless method == "panel")
    judges: list[ModelConfig] = Field(default_factory=list)
    """Panel judges. Requires >= 2 entries when method=panel.
    All judges must be from different families than the agent (cross-family
    enforcement applies individually to each panel member).
    Example:
      judges:
        - provider: anthropic/claude-opus-4-7
          params: {max_tokens: 256}
        - provider: openai/gpt-4o
          params: {max_tokens: 256}
    """
    variance_threshold: float = Field(default=0.3, ge=0.0, le=1.0)
    """Std-dev threshold above which the escalation judge is called.
    E.g. 0.3 means: if judges disagree by more than 0.3 std-dev, escalate."""
    escalation: ModelConfig | None = None
    """Tiebreaker model called when judge std_dev > variance_threshold.
    Should be a high-capability model from yet another family.
    When None and variance exceeds threshold, the panel mean is used with
    a 'high-variance' rationale annotation."""

    @field_validator("rubric")
    @classmethod
    def _strip_rubric(cls, v: str | None) -> str | None:
        return v.strip() if v else v


class KnowledgeRetrieverKind(StrEnum):
    """Retriever family for an agent's declared knowledge source.

    BM25 is the v0.7 surface default; embeddings + reranking land in
    v0.8 as separate kinds (pgvector, azure_search). The interface
    here is deliberately retriever-agnostic — same ``query()`` signature
    regardless of backend so the agent's prompt template doesn't change
    when the retriever swaps.
    """

    BM25 = "bm25"
    """In-memory BM25 over a JSON corpus. No external deps; loads at
    process start. The MVP / v0.7 default."""

    SUBSTRING = "substring"
    """Token-overlap scorer — even cheaper than BM25, useful when the
    corpus is tiny (< 50 entries) and BM25's IDF is noisy."""


class KnowledgeConfig(BaseModel):
    """Per-agent knowledge declaration parsed from ``knowledge.yaml``.

    The MVP surface (v0.7) locks the interface for v0.8's production
    retrievers (pgvector / Azure AI Search). Agents reference a single
    knowledge source by ``corpus`` path; the runtime loads it into the
    chosen retriever at process start and exposes a uniform
    ``retrieve(query, top_k)`` to skills + workflow nodes.

    Example ``knowledge.yaml`` (agent-local):

        api_version: movate/v1
        kind: Knowledge
        retriever: bm25
        corpus: ./kb/kb-lookup-corpus.json
        top_k: 5
        body_fields: [title, symptom, resolution]
        tag_field: tags

    Embeddings + reranking + non-JSON ingestion land in v0.8 — this
    config will gain ``embedding_model``, ``reranker``, and
    ``chunker`` fields then. Existing agents won't need to migrate
    because BM25 + ``top_k`` are sensible defaults.
    """

    model_config = ConfigDict(extra="forbid")

    retriever: KnowledgeRetrieverKind = KnowledgeRetrieverKind.BM25
    """Which retriever to instantiate. Default ``bm25`` is the right
    pick for any agent until corpus size + recall needs justify the
    embedding-pipeline cost."""

    corpus: str
    """Path to the corpus file, relative to the agent directory. JSON
    array of objects today; v0.8 will add ``.jsonl`` + markdown
    directories. The retriever loads this exactly once at agent boot."""

    top_k: int = Field(default=5, ge=1, le=50)
    """Max documents the retriever returns per query. Tuned per agent —
    too low misses context, too high pollutes the prompt window. 5 is
    a reasonable default for most RAG-QA shapes."""

    body_fields: list[str] = Field(default_factory=lambda: ["title", "body"])
    """Corpus entry fields concatenated for the BM25 body index. The
    default matches the canonical KB shape (``title`` + ``body``);
    legacy KB-lookup corpora using ``symptom`` + ``resolution`` should
    list those instead. Field weights are uniform in v0.7; the v0.8
    config will add per-field weights."""

    tag_field: str | None = "tags"
    """Corpus entry field whose values are treated as exact-match tags
    (high-weight matches when a query token equals one of them). Set
    to None to disable tag matching."""

    id_field: str = "id"
    """Corpus entry field carrying the stable document id surfaced in
    retrieval results. Defaults to ``id``."""

    @field_validator("body_fields")
    @classmethod
    def _at_least_one_body_field(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("body_fields must contain at least one field name")
        return v


class WorkflowStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    """Terminal: at least one node failed; partial state retained."""
    PAUSED = "paused"
    """Non-terminal: the runner reached a ``HUMAN`` gate node (ADR 017 D5),
    persisted a durable checkpoint, and stopped. Resumable — PR 2's
    resume-on-signal path loads the checkpoint, merges the human's decision,
    and continues from the gate's successor. Distinct from ``SUCCESS``
    (ran to the sink) and ``ERROR`` (a node failed)."""


class WorkflowRunRecord(BaseModel):
    """Persisted record of one workflow execution.

    Each child agent run carries this id in its ``workflow_run_id`` field;
    join on that to reconstruct the timeline.

    HITL checkpoint (ADR 017 D5): when ``status`` is ``PAUSED`` the three
    ``paused_*`` / ``human_task`` fields below carry the durable checkpoint
    that PR 2's resume-on-signal path consumes — the node the runner paused
    at, the state captured at that gate, and the human-task spec shown to the
    operator. They are nullable + additive: a non-paused record (SUCCESS or
    ERROR) leaves them ``None``, so existing rows and code paths are
    byte-for-byte unchanged.
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

    # --- HITL checkpoint (ADR 017 D5, PR 1) -------------------------------
    # Populated only on a PAUSED record. PR 2 (resume-on-signal) reads these
    # to reconstruct the runner state and continue from the gate's successor.
    paused_node_id: str | None = None
    """ID of the ``HUMAN`` node the runner paused at. PR 2 resumes from this
    node's successor once the human's decision is merged."""
    paused_state: dict[str, Any] | None = None
    """The workflow state captured at the gate (post-merge of every node up to
    but NOT including the human node). PR 2 merges the human's response into
    this and continues. Mirrors ``final_state`` on a paused record but kept as
    a distinct field so the checkpoint's intent is explicit and PR 2 doesn't
    overload the dual-purpose ``final_state``."""
    human_task: dict[str, Any] | None = None
    """The human-task spec for the gate: ``{"prompt": <str>, "output_contract":
    [<state keys the human's response contributes>]}``. PR 2 renders this to
    the operator (Teams card / API) and validates the returned decision
    against ``output_contract`` before merging into ``paused_state``."""


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
    dimension_means: dict[str, float] | None = None
    """Per-dimension mean scores (0.0-1.0) across the eval's cases (item 24).

    Maps a :class:`movate.core.eval.Dimension` name (e.g. ``"faithfulness"``,
    ``"coverage"``, ``"safety"``) to its mean over the cases/runs where that
    dimension was scored. A dimension that *no* case scored is omitted from
    the dict entirely (never stored as ``0.0``); the aggregate ``mean_score``
    above stays the gate-relevant headline.

    Additive + nullable: ``None`` for legacy ``EvalRecord`` rows persisted
    before this field existed (and for any record built without dimensional
    scoring). Drift detection (:func:`movate.core.drift.detect_drift`) uses it
    to catch a single-dimension regression the aggregate would mask, and falls
    back to aggregate-only comparison when either side is ``None`` — so
    pre-item-24 behaviour is byte-for-byte unchanged.
    """
    created_at: datetime = Field(default_factory=_now)


class EvalSchedule(BaseModel):
    """A per-(tenant, agent) cadence for continuous, scheduled eval (ADR 016 D2).

    Additive + default-off: nothing is created unless an operator runs
    ``mdk eval-schedule set``. When a schedule exists, an external cron
    (a Container Apps Job on Azure; any cron locally) periodically invokes
    the **tick** entrypoint (``mdk eval-scheduler-tick``), which finds
    schedules whose cadence has elapsed and enqueues a ``JobKind.EVAL``
    job for each — reusing the existing eval-as-job path. There is **no
    in-process timer daemon**; the tick is a stateless one-shot driven by
    an external scheduler.

    The cadence is an **interval in seconds** (``cadence_seconds``) — a
    portable primitive that any cron can satisfy (run the tick every N
    minutes; the tick only enqueues schedules that are actually due).
    A richer cron-expression cadence is a documented follow-up; the
    interval covers the D2 "run on a cadence" requirement without a new
    dependency.

    ``(tenant_id, agent)`` is unique — one active schedule per agent per
    tenant. Re-running ``set`` upserts (overwrites) the row.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    agent: str
    """Agent name (``agent.yaml`` ``name``). Paired with ``tenant_id`` as
    the unique schedule key."""
    cadence_seconds: int = Field(ge=1)
    """How often, in seconds, an eval should be enqueued for this agent.
    The tick enqueues a job when ``now - last_enqueued_at >= cadence_seconds``
    (or when ``last_enqueued_at`` is null — first tick). Keeping it an
    interval (not a cron expression) makes the tick trivially portable and
    idempotent."""
    enabled: bool = True
    """Soft on/off. A disabled schedule is retained (history + quick
    re-enable) but never enqueues. ``mdk eval-schedule clear`` deletes
    the row entirely."""
    # ---- eval kickoff config (mirrors EvalSubmission / the EVAL job input) ----
    mock: bool = False
    """Use the deterministic MockProvider for the scheduled eval — a cheap
    smoke cadence that spends no tokens. Cost-aware default-off; flip on
    for high-frequency canaries."""
    runs: int = Field(default=1, ge=1, le=10)
    """Runs per case for the scheduled eval (3+ defeats LLM-judge variance)."""
    gate_mode: str = "mean"
    """N-run aggregation: ``mean`` | ``min`` | ``p10``."""
    gate: float = Field(default=0.7, ge=0.0, le=1.0)
    """Per-case score required to pass — carried onto the enqueued job."""
    objective: str | None = None
    """Optional objective id to filter cases by (sampling a subset of the
    dataset between full runs — cost-aware)."""
    # ---- drift detection knobs ----
    regression_tolerance: float = Field(default=0.05, ge=0.0, le=1.0)
    """Allowable mean_score / pass_rate drop vs. the baseline before the
    result is flagged as drift. Slightly loose by default (0.05) to absorb
    LLM-judge noise on the scheduled path; tighten for critical agents."""
    baseline_id: str | None = None
    """Optional pinned baseline ``eval_id`` to diff scheduled runs against.
    When null, drift compares against the *prior* eval for this agent
    (the most recent ``EvalRecord`` before the new one)."""
    notify_email: str | None = None
    """Where to send a drift alert when a scheduled eval regresses. When
    null, the alert is still emitted as a structured log + the console
    backend; SMTP delivery needs an address."""
    created_by: str | None = None
    """Auth identity that created the schedule (ADR 013), or ``None`` for
    a local/CLI write."""
    created_at: datetime = Field(default_factory=_now)
    last_enqueued_at: datetime | None = None
    """When the tick last enqueued a job for this schedule. Drives the
    due-check + idempotency (no double-enqueue inside one cadence window).
    Null until the first enqueue."""


class BenchModelResult(BaseModel):
    """Per-model row inside a :class:`BenchRecord`.

    One per provider compared in the bench, mirroring the shape
    ``mdk bench --output json`` emits (see ``cli.bench._model_to_json``)
    so the persisted record and the CLI's JSON output stay aligned.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    score: float | None = None
    """Aggregated quality score (0.0-1.0) across the model's runs, or
    ``None`` when no judge scored it (no rubric, or judge skipped for a
    same-family conflict — see ``judge_skipped``)."""
    judge_skipped: bool = False
    """True when the judge was skipped for this model because it shared
    a family with the configured judge (cross-family enforcement)."""
    cost_mean_usd: float
    cost_total_usd: float
    latency_p50_ms: int
    latency_p95_ms: int
    error_count: int
    sample_output: dict[str, Any] | None = None
    """First successful run's output payload (or ``None`` if every run
    errored). Kept for a quick eyeball of what the model produced."""


class BenchRecord(BaseModel):
    """Persisted summary of one bench run (one agent version, N models compared).

    The bench analogue of :class:`EvalRecord`: where an eval scores one
    agent against a dataset, a bench compares one input across several
    models and reports cost / latency / (optional) quality per model.
    Per-model rows live in ``models`` as a JSON-serializable list, the
    same way ``EvalRecord`` flattens per-case data.
    """

    model_config = ConfigDict(extra="forbid")

    bench_id: str
    tenant_id: str
    agent: str
    agent_version: str
    input: dict[str, Any]
    """The single input payload run through every model."""
    judge_method: JudgeMethod | None = None
    """Judge method when quality was scored (``llm_judge``); ``None`` for
    cost+latency-only benches."""
    judge_provider: str | None = None
    """Judge provider when scoring was enabled; ``None`` otherwise."""
    runs_per_model: int
    gate_mode: str
    """Score aggregation across the N runs per model: ``mean`` | ``min`` | ``p10``."""
    models: list[BenchModelResult]
    """Per-model comparison rows, in the order they were benched."""
    created_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Durable agent registry (ADR 014 D1) — a published agent bundle persisted
# as one immutable (name, version) row behind the StorageProvider Protocol.
#
# The bundle is the small text files that make up an agent on disk
# (``agent.yaml``, ``prompt.md``, schemas, dataset, skills, contexts),
# carried in the JSON-serializable ``files`` map keyed by relative path. KB
# is NOT part of the bundle — it already lives durably in pgvector storage
# (ADR 009); the registry records which agent expects a KB, not the chunks.
# ---------------------------------------------------------------------------


class AgentBundleRecord(BaseModel):
    """One published, versioned agent bundle (the bundle analogue of
    :class:`BenchRecord`).

    A row is **immutable**: each publish of an agent writes a new
    ``(name, version)`` row, so the table doubles as the version history.
    Tenant-scoped like every other durable record. The bundle's files are
    small text (yaml / md / json / jsonl / skill files) carried in
    ``files`` and serialized to a single JSON column on every backend.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    """The agent name (``agent.yaml`` ``name``) — the registry key paired
    with ``version``."""
    tenant_id: str
    version: str
    """The bundle's ``agent.yaml`` version. ``(name, tenant_id, version)``
    is unique; a new publish bumps this and writes a new row."""
    created_by: str | None = None
    """Auth identity that published this version (ADR 013), or ``None`` for
    a system/seed import. Drives the "who published what when" audit."""
    content_hash: str
    """Content-addressed hash of the bundle (over ``files``), so an
    unchanged re-publish is detectable and a version can be verified."""
    files: dict[str, str]
    """The bundle's text files keyed by relative path, e.g.
    ``agent.yaml``, ``prompt.md``, ``schema/input.json``,
    ``schema/output.json``, ``evals/dataset.jsonl``, ``skills/*.py``,
    ``contexts/*.md``. JSON-serializable (path -> file contents). KB is
    excluded — it lives in pgvector (ADR 009)."""
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
    EVAL = "eval"
    """Async eval run. JobRecord.input carries the eval config
    (gate, runs, mock, baseline_id, ...). Worker loads the agent
    bundle from the registry, runs EvalEngine, persists EvalRecord.
    The job's ``result_run_id`` is left null; instead, JobRecord's
    output payload carries ``{eval_id}`` so the caller can fetch the
    completed EvalRecord via GET /api/v1/evals/{eval_id} (item 84).
    See BACKLOG Group H item 83."""
    BENCH = "bench"
    """Async multi-model bench. JobRecord.input carries the bench config
    (models, judge, rubric, mock, runs, gate_mode, input). Worker loads
    the agent bundle, runs BenchEngine, persists a BenchRecord. As with
    EVAL, ``result_run_id`` carries the produced ``bench_id`` so the
    caller can fetch the completed BenchRecord via
    GET /api/v1/bench/{bench_id}. See BACKLOG item 64."""


class JobRecord(BaseModel):
    """Queue entry for an async run.

    Lifecycle:

    * ``QUEUED`` (just inserted, waiting for a worker)
    * ``RUNNING`` (claimed by a worker, ``claimed_at`` set)
    * ``SUCCESS`` / ``ERROR`` / ``SAFETY_BLOCKED`` / ``DEAD_LETTER`` /
      ``CANCELLED`` (terminal, ``completed_at`` and (for success)
      ``result_run_id`` set)
    * ``QUEUED`` again — re-queue after a transient failure
      (``attempt_count`` incremented, ``next_retry_at`` set in the
      future; ``claim_next_job`` skips until then)

    A ``QUEUED`` job can also be cancelled straight to ``CANCELLED``
    (never claimed); a ``RUNNING`` job carries ``cancel_requested`` so
    the worker writes ``CANCELLED`` at its terminal checkpoint (item 36).

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
    cancel_requested: bool = False
    """Cooperative-cancel flag (item 36, R4b). Set by
    ``StorageProvider.request_job_cancel`` when an operator cancels a
    job that is already ``RUNNING`` (a ``QUEUED`` job is flipped straight
    to ``CANCELLED`` instead — the worker never sees it). The worker
    honors this at a checkpoint: a job claimed while the flag is already
    set is skipped (never dispatched) and written ``CANCELLED``; a job
    flagged mid-dispatch finishes its in-flight work but the outcome is
    discarded in favor of ``CANCELLED``. There is NO mid-LLM-call
    interruption — that's explicitly out of scope.

    Additive + default-off: ``False`` for every job that was never
    cancelled, so pre-cancel rows (and the overwhelming common case)
    read back as ``False`` and behave byte-for-byte as before."""
    thread_id: str | None = None
    """For threaded runs (Tier 10.5 / PR-Q), the
    :class:`ConversationThread` id this job belongs to. The worker
    propagates this onto the spawned :class:`RunRecord.thread_id` so
    the run joins back to its thread. ``None`` (the common case) for
    standalone non-threaded jobs."""
    target_version: str | None = None
    """Pinned agent version the worker must resolve for this job (ADR 016
    D3 — canary rollout). The enqueue path (API) decides champion vs
    challenger via :func:`movate.core.canary.choose_version` and stamps the
    chosen concrete version here so the worker runs the SAME version the
    routing decision picked — async runs can't re-decide at claim time
    (the canary config may have changed, or the weighted/sticky draw must
    not be re-rolled). The worker passes this to ``resolve_agent_bundle(
    version=...)``. ``None`` (the overwhelming common case — no canary
    config, or champion-by-latest) means "resolve latest", so a job with no
    canary in play is byte-for-byte identical to a pre-canary job. Additive
    + nullable: pre-canary rows read back as ``None``."""
    resume_workflow_run_id: str | None = None
    """Target ``workflow_run_id`` for a HITL resume continuation job (ADR 017
    D5, PR 2). When set on a ``JobKind.WORKFLOW`` job, the worker loads that
    PAUSED :class:`WorkflowRunRecord` checkpoint (the human's decision is
    already merged into its ``paused_state`` by the signal endpoint) and calls
    ``WorkflowRunner.resume`` to continue from the gate's successor instead of
    the normal ``run(initial_state=job.input)`` path. The signal endpoint
    enqueues this so execution stays in the worker (execution plane), not the
    API pod. ``None`` (every non-resume job: every agent job, every
    fresh/scheduled workflow job) means "run from the entrypoint", byte-for-
    byte the pre-PR-2 path. Additive + nullable: pre-PR-2 rows read back as
    ``None``."""
    batch_id: str | None = None
    """Parent batch this job belongs to (item 17 — batch inference). Each row
    of a submitted dataset becomes ONE ordinary ``JobKind.AGENT`` job carrying
    the shared ``batch_id`` of its :class:`BatchRecord`; the batch-status
    endpoint aggregates over the children via ``list_jobs(batch_id=...)``. The
    worker never reads this — a batch row is byte-for-byte a normal agent job,
    so it inherits retry / dead-letter / canary / observability with no new
    execution path. ``None`` (every non-batch job: every single run, every
    scheduled / triggered / threaded / workflow job) means "not part of a
    batch", byte-for-byte the pre-batch path. Additive + nullable: pre-batch
    rows read back as ``None``. Mirrors the ``target_version`` /
    ``resume_workflow_run_id`` additive-column pattern above."""
    trace_context: dict[str, str] = Field(default_factory=dict)
    """W3C trace-context carrier (traceparent/tracestate) captured at enqueue
    so the worker can continue the originating distributed trace. Empty = no
    propagated parent (pre-R2 rows, or OTel not active) → the worker starts a
    fresh root span (today's behavior). Populated explicitly at the enqueue
    edge via :func:`movate.tracing.inject_current_trace_context` — NOT via an
    ambient-capturing default_factory (no implicit magic); the default is an
    empty dict. The worker re-attaches it via
    :func:`movate.tracing.continue_trace_context` before starting the job's
    root span (ADR 019, item 32). Additive + JSONB/TEXT column: pre-R2 rows
    read back as ``{}``."""


class BatchRecord(BaseModel):
    """Parent metadata for a batch-inference submission (item 17).

    A caller submits a whole dataset of inputs for one agent; movate mints a
    ``batch_id``, persists this record (``total`` = the number of rows it
    enqueued), and enqueues ONE ordinary ``JobKind.AGENT`` job per row, each
    stamped with :attr:`JobRecord.batch_id` = ``batch_id``. Because each row
    is a normal queue job, it is observable, retryable, dead-letter-handled,
    and canary-aware for free — no new execution path. The batch-status
    endpoint loads this record and aggregates over the child jobs fetched via
    ``list_jobs(batch_id=...)``.

    Immutable after creation — there is no per-row mutation here; the live
    per-status counts always come from the children, never from this record.
    """

    model_config = ConfigDict(extra="forbid")

    batch_id: str
    tenant_id: str
    agent: str
    """The agent every row of this batch runs against (the ``target`` of each
    child :class:`JobRecord`)."""
    total: int = Field(..., ge=0)
    """Number of rows enqueued = number of child jobs. The status endpoint
    cross-checks this against the child count it fetches."""
    created_by: str | None = None
    """The api_key_id (or other identity) that submitted the batch — audit
    only, mirrors :attr:`JobRecord.api_key_id`. Never returned over the wire."""
    created_at: datetime = Field(default_factory=_now)


class JobSchedule(BaseModel):
    """A per-(tenant, name) cadence for cron-driven agent/workflow runs (ADR 017 D2).

    This generalizes the continuous-eval scheduler (ADR 016 D2 /
    :class:`EvalSchedule`) from the eval-only case to enqueuing arbitrary
    ``JobKind.AGENT`` and ``JobKind.WORKFLOW`` jobs on a cadence. It reuses
    the exact same enqueue-on-cron primitive: an operator records a
    schedule (``mdk schedule set``), and an external cron (a Container Apps
    **Job** on Azure; any cron locally) periodically invokes the **tick**
    (``mdk scheduler-tick``), which finds schedules whose cadence has
    elapsed and enqueues a job for each — reusing the existing
    ``mdk submit`` / ``POST /run`` job path so the worker executes them
    with no new dispatch branch. There is **no in-process timer daemon**;
    the tick is a stateless one-shot driven by an external scheduler.

    Additive + default-off: nothing is created unless an operator sets a
    schedule, and existing agent/workflow/job behaviour is unchanged for
    everything without one.

    The cadence is an **interval in seconds** (``cadence_seconds``) — a
    portable primitive any cron can satisfy (run the tick every N minutes;
    the tick only enqueues schedules that are actually due). A richer
    cron-expression cadence is a documented follow-up; the interval covers
    the D2 "run on a cadence" requirement without a new dependency.

    **Idempotency.** The tick stamps ``last_enqueued_at`` and a schedule is
    "due" only once ``now - last_enqueued_at >= cadence_seconds``, so
    running the tick more often than the cadence never double-enqueues
    inside a window.

    ``(tenant_id, name)`` is unique — one active schedule per handle per
    tenant. Re-running ``set`` upserts (overwrites) the row.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    name: str
    """The schedule's handle — a stable name the operator picks (defaults
    to the target). Paired with ``tenant_id`` as the unique schedule key,
    so one target can have multiple schedules (e.g. different cadences /
    inputs) under distinct names."""
    kind: JobKind
    """Which job kind the tick enqueues — only ``AGENT`` or ``WORKFLOW``.
    ``EVAL`` has its own richer scheduler (:class:`EvalSchedule`) and
    ``BENCH`` is not a scheduling target; both are rejected at construction
    (see the validator below)."""
    target: str
    """Agent name or workflow name to run — pairs with ``kind`` exactly as
    ``JobRecord.target`` does."""
    cadence_seconds: int = Field(ge=1)
    """How often, in seconds, to enqueue a job for this schedule. The tick
    enqueues when ``now - last_enqueued_at >= cadence_seconds`` (or when
    ``last_enqueued_at`` is null — first tick). An interval (not a cron
    expression) keeps the tick trivially portable and idempotent."""
    enabled: bool = True
    """Soft on/off. A disabled schedule is retained (history + quick
    re-enable) but never enqueues. ``mdk schedule clear`` deletes the
    row entirely."""
    input: dict[str, Any] = Field(default_factory=dict)
    """The job payload enqueued each tick. For an ``AGENT`` schedule this
    is the ``RunRequest.input`` dict; for a ``WORKFLOW`` schedule it is the
    initial-state dict — the same shapes ``mdk submit`` / ``POST /run``
    set, so the enqueued job runs through the existing dispatch path
    unchanged."""
    notify_email: str | None = None
    """Optional email the worker notifies when an enqueued job reaches a
    terminal status. Rides onto each enqueued :class:`JobRecord`."""
    created_by: str | None = None
    """Auth identity that created the schedule (ADR 013), or ``None`` for
    a local/CLI write."""
    created_at: datetime = Field(default_factory=_now)
    last_enqueued_at: datetime | None = None
    """When the tick last enqueued a job for this schedule. Drives the
    due-check + idempotency (no double-enqueue inside one cadence window).
    Null until the first enqueue."""

    @field_validator("kind")
    @classmethod
    def _kind_is_schedulable(cls, v: JobKind) -> JobKind:
        """Only ``AGENT``/``WORKFLOW`` jobs are schedulable here.

        ``EVAL`` has its own richer scheduler (:class:`EvalSchedule` —
        cadence + drift knobs), and ``BENCH`` is not a scheduling target.
        Rejecting them at parse time keeps the generic scheduler focused
        and avoids a second, weaker eval-scheduling path.
        """
        if v not in (JobKind.AGENT, JobKind.WORKFLOW):
            raise ValueError(
                f"JobSchedule.kind must be 'agent' or 'workflow', got {v.value!r}; "
                "EVAL has its own scheduler (EvalSchedule) and BENCH is not a "
                "scheduling target."
            )
        return v


class Trigger(BaseModel):
    """A registered inbound event/webhook trigger (ADR 017 D2).

    The trigger sibling of :class:`JobSchedule`: both register a *standing*
    way to enqueue an agent/workflow job into the existing queue, so the run
    is observable + retryable for free (it becomes a normal job). The
    scheduler fires on a cron cadence; a trigger fires on an **inbound
    event** — an external system (a ticketing tool, a CI webhook, a queue
    consumer) POSTs an event to a stable movate URL and movate enqueues a
    run ("process this incoming ticket").

    Crucially the external caller has **no** ``mvt_*`` API key. It
    authenticates with a **per-trigger secret** instead: the fire endpoint
    (``POST /api/v1/triggers/{trigger_id}/events``) verifies an HMAC-SHA256
    signature of the raw request body keyed by that secret. The secret is
    minted + hashed-at-rest exactly like an API key
    (:func:`movate.core.auth.hash_secret`) — the plaintext is shown once at
    creation and **never stored**; only ``secret_hash`` + ``salt`` persist,
    and verification is a constant-time compare. Hashing at rest means a
    storage compromise never yields a usable trigger secret.

    Additive + default-off: nothing is created unless an operator registers
    a trigger, and every existing endpoint is unchanged for everything
    without one.

    ``(tenant_id, name)`` is the unique management key (an operator's stable
    handle). ``trigger_id`` is the separate **public** id embedded in the
    webhook URL — the fire endpoint looks the trigger up by ``trigger_id``
    *without* a tenant context (an unauthenticated external caller has no
    tenant), and the enqueued job inherits the trigger's own ``tenant_id``.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    name: str
    """The trigger's handle — a stable name the operator picks (defaults to
    the target). Paired with ``tenant_id`` as the unique management key, so
    one target can have multiple triggers (e.g. different default inputs)
    under distinct names."""
    trigger_id: str = Field(default_factory=lambda: uuid4().hex)
    """Stable **public** id embedded in the webhook URL
    (``/api/v1/triggers/{trigger_id}/events``). Distinct from ``name`` so
    the fire endpoint can resolve the trigger (and its tenant) from the URL
    alone, with no tenant context — the external caller is unauthenticated
    in the ``mvt_*`` sense. A uuid hex (unguessable, but the secret — not
    obscurity — is the actual auth)."""
    kind: JobKind
    """Which job kind the trigger enqueues — only ``AGENT`` or ``WORKFLOW``.
    ``EVAL`` has its own scheduler (:class:`EvalSchedule`) and ``BENCH`` is
    not a trigger target; both are rejected at construction (see the
    validator below), exactly as :class:`JobSchedule` does."""
    target: str
    """Agent name or workflow name to run — pairs with ``kind`` exactly as
    ``JobRecord.target`` does."""
    secret_hash: str
    """SHA-256 hex digest of ``salt || secret`` (the per-trigger secret).
    The plaintext secret is never stored — shown once at creation, then
    irrecoverable, like an API key."""
    salt: str
    """Per-trigger salt (URL-safe base64). Prevents rainbow tables across
    the table; the high-entropy secret makes brute force pointless."""
    input_defaults: dict[str, Any] = Field(default_factory=dict)
    """Baseline job payload, merged **UNDER** the inbound event body to form
    the enqueued job's ``input`` (the event body wins on key collisions).
    Lets an operator pin fixed fields (e.g. ``{"source": "zendesk"}``) while
    the event supplies the per-event payload."""
    enabled: bool = True
    """Soft on/off. A disabled trigger is retained (history + quick
    re-enable) but the fire endpoint treats it as absent (404, no existence
    leak to an unauthenticated caller). ``mdk trigger delete`` removes the
    row entirely."""
    created_by: str | None = None
    """Auth identity that created the trigger (ADR 013), or ``None`` for a
    local/CLI write."""
    created_at: datetime = Field(default_factory=_now)
    last_fired_at: datetime | None = None
    """When the fire endpoint last enqueued a job for this trigger. Stamped
    by :meth:`StorageProvider.touch_trigger`; observational (unlike the
    scheduler's ``last_enqueued_at``, it does not gate firing — every valid
    event fires). Null until the first fire."""

    @field_validator("kind")
    @classmethod
    def _kind_is_triggerable(cls, v: JobKind) -> JobKind:
        """Only ``AGENT``/``WORKFLOW`` jobs are triggerable here.

        ``EVAL`` has its own richer scheduler (:class:`EvalSchedule`), and
        ``BENCH`` is not a trigger target. Rejecting them at parse time keeps
        the trigger surface focused — mirrors
        :meth:`JobSchedule._kind_is_schedulable`.
        """
        if v not in (JobKind.AGENT, JobKind.WORKFLOW):
            raise ValueError(
                f"Trigger.kind must be 'agent' or 'workflow', got {v.value!r}; "
                "EVAL has its own scheduler (EvalSchedule) and BENCH is not a "
                "trigger target."
            )
        return v


class CanaryConfig(BaseModel):
    """Per-(tenant, agent) canary / champion-challenger rollout (ADR 016 D3).

    With versioned agents (ADR 014 registry), this enables a *safe,
    progressive rollout*: publish a challenger version, route a configurable
    slice of prod traffic to it, compare live quality champion-vs-challenger,
    then promote the winner. The whole thing is **additive + default-off** —
    an agent with no :class:`CanaryConfig` row routes 100% to its
    champion-by-default (registry latest), and the run/enqueue/dispatch path
    is byte-for-byte identical to today.

    **Version is the slice key (not a new run field).** Every run is already
    tagged with ``RunRecord.agent_version`` (ADR 014). Canary doesn't add a
    column to a run — it *chooses which version a run executes* (champion vs
    challenger) and then slices feedback/runs by ``agent_version`` to compare
    them. The config knows which version is which.

    **The kill switch is ``weight == 0``.** Setting the weight to 0 routes
    100% of traffic back to the champion *instantly* — no version delete, no
    redeploy. It's the panic button when a challenger misbehaves.

    **Routing** (pure, in :func:`movate.core.canary.choose_version`):

    * No config / ``enabled is False`` / ``weight == 0`` → champion
      (``champion_version`` if pinned, else ``None`` → registry latest).
    * ``sticky`` (default) + a ``thread_id`` → a *deterministic* hash of the
      thread decides, so every turn of one conversation stays on the same
      side (a session never flips champion↔challenger mid-thread).
    * Otherwise a weighted random draw at ``weight``%.

    **Promotion** is *assisted by default* (a human calls
    ``POST .../canary/promote``). ``auto_promote`` is opt-in and gated: the
    challenger's measured quality must clear ``eval_gate`` before an
    auto-promote proceeds, and the endpoint requires the ``admin`` scope
    (ADR 013) either way. Promotion sets ``champion_version`` to the promoted
    version and zeroes ``weight`` (the canary has concluded); the prior
    champion is recorded so rollback is instant.

    ``(tenant_id, agent)`` is unique — one canary per agent per tenant.
    Re-running ``set`` upserts the row. Agent versions stay immutable (ADR
    014); promotion moves a *pointer*, never rewrites history.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    agent: str
    """Agent name this canary applies to. Paired with ``tenant_id`` as the
    unique key — one active canary per agent per tenant."""
    challenger_version: str
    """The version receiving canary traffic. Must be a published version in
    the registry (the API confirms it exists before honoring the config /
    promoting it). Runs that land on the challenger are tagged with this
    ``agent_version`` so they can be sliced out for comparison."""
    champion_version: str | None = None
    """Optional pin for the champion side. ``None`` (the common case) means
    "champion is whatever the registry resolves as latest" — so a freshly
    published patch becomes the champion without touching this config. Pin it
    only when you need the champion held at a specific version while the
    challenger is evaluated."""
    weight: int = Field(default=0, ge=0, le=100)
    """Percent of traffic (0-100) routed to the challenger. **0 is the kill
    switch** — 100% to the champion, instantly. Defaults to 0 so a
    just-created config is dormant until an operator dials traffic up."""
    sticky: bool = True
    """When true (default), routing is *consistent per* ``thread_id`` — a
    deterministic hash decides the side so a multi-turn conversation never
    flips champion↔challenger mid-thread. False = an independent weighted
    draw per run. With no ``thread_id`` available, routing falls back to the
    weighted draw regardless."""
    enabled: bool = True
    """Soft on/off. A disabled canary is retained (history + quick re-enable)
    but routes 100% to the champion, exactly like ``weight == 0``. ``mdk
    canary off`` can disable or delete it."""
    auto_promote: bool = False
    """Opt-in: when true, the promote endpoint MAY promote the challenger
    automatically once its measured quality clears ``eval_gate``. Default
    false = **assisted** promotion (a human approves every promote). A bad
    auto-promote ships a regression to all users — the exact failure canary
    exists to prevent — so it stays off by default and gated."""
    eval_gate: float | None = None
    """Minimum challenger quality (a pass-rate / score in ``[0, 1]``, or a
    feedback-derived rate) required before an ``auto_promote`` proceeds. Only
    meaningful when ``auto_promote`` is true. ``None`` + ``auto_promote`` →
    the gate is unsatisfiable, so auto-promote is refused (fail-safe: never
    auto-ship without a measurable bar)."""
    auto_rollback: bool = False
    """Opt-in: when true, a scheduled-eval *regression on the
    ``challenger_version``* auto-trips the kill switch (``weight`` → 0),
    reverting all traffic to the champion. Default false = **alert-only** — a
    drift alert *informs*, a human pulls the kill switch (ADR 016 D5 safety
    default: "a drift alert informs; auto-rollback is opt-in, not default").
    Rollback is the existing kill switch (``weight`` → 0), never a version
    delete; the challenger row is retained for re-enable / audit."""
    created_by: str | None = None
    """Auth identity that created/updated the canary (ADR 013), or ``None``
    for a local/CLI write."""
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    """Refreshed on every upsert (set / promote / rollback) so operators can
    see when the canary last changed."""


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
    expires_at: datetime | None = None
    """UTC expiry. None = no expiry (legacy keys minted before v0.7.1).
    New keys default to 90 days from mint time via :func:`movate.core.auth.mint_api_key`."""
    scope: str | None = None
    """**Legacy** single permission scope (pre-ADR-013). ``"fleet-admin"``
    was the only meaningful value — an all-powerful admin grant.
    Superseded by :attr:`scopes`; retained for back-compat reads. A row
    whose ``scopes`` is empty but ``scope == "fleet-admin"`` is expanded to
    the full scope set by :func:`movate.core.auth.effective_scopes`."""
    scopes: list[str] = Field(default_factory=list)
    """Least-privilege scope grant (ADR 013 L2). A flat set drawn from
    :data:`movate.core.auth.ALL_SCOPES` (``read``, ``run``, ``eval``,
    ``kb:write``, ``admin``, ``fleet-admin``). **Empty = no explicit
    grant**: :func:`movate.core.auth.effective_scopes` resolves an empty
    list to the legacy default ``{read, run, eval}`` at check time, so
    existing keys keep working on read/run/eval but 403 on admin. New keys
    + OIDC tokens carry explicit scopes."""


class FeedbackRecord(BaseModel):
    """Operator-supplied feedback for a single run.

    Captured via the Chainlit playground (or any client that POSTs to
    ``/api/v1/runs/{run_id}/feedback``). Stored in Postgres for
    aggregation + analysis. Optionally mirrored to Langfuse as a score
    attached to the matching trace so traces + feedback can be queried
    side-by-side in the Langfuse UI.

    The feedback is intentionally schemaless on ``dimensions`` so each
    agent role can score what matters to it (e.g. RAG agents may
    capture ``faithfulness`` + ``citation_quality``; lead-qualifier
    might capture ``next_action_correctness``). The top-level ``score``
    is the single 👍/👎 (thumbs) or 1-5 (stars) signal that aggregates
    cleanly across all agents.
    """

    model_config = ConfigDict(extra="forbid")

    feedback_id: str = Field(default_factory=lambda: uuid4().hex)
    """Stable id; doubles as the table primary key."""

    run_id: str
    """References ``runs.run_id``. The run must exist before feedback
    can be attached — enforced at the endpoint, not the schema."""

    tenant_id: str
    """Tenant that owns the run. Mirrored on the feedback row so
    list_feedback can stay tenant-scoped without joining ``runs``."""

    agent: str
    """Denormalized from the referenced run for fast per-agent
    aggregation queries. Set at write time; trusts ``runs.agent``."""

    user_id: str
    """Identity of the operator giving feedback. Azure AD object_id /
    email / SSO subject — whatever the auth layer surfaces. Free-text
    in the schema to keep auth-mechanism choice open."""

    score: int
    """Single composite signal. Two conventions supported:

    * ``-1`` = 👎, ``+1`` = 👍 (binary thumbs)
    * ``1..5`` = star rating

    Callers stay consistent within their UI; aggregation queries
    bucket on the convention they care about. Validation pins to
    ``[-1, 5]`` (covers both shapes without forcing a discriminator
    column)."""

    dimensions: dict[str, float] | None = None
    """Optional per-dimension scores keyed by free-text dimension name
    (``{"helpfulness": 0.8, "accuracy": 1.0, "format": 0.6}``). Each
    value is a float in [0, 1] by convention. JSONB in Postgres so
    queries can pivot any dimension without schema migrations."""

    comment: str | None = None
    """Optional free-text comment from the operator. The most useful
    field for qualitative analysis — sample these for tuning the
    agent's prompt or refining the eval rubric."""

    langfuse_score_id: str | None = None
    """When Langfuse is configured AND a trace exists for the run,
    the feedback is mirrored as a Langfuse score and the returned id
    is persisted here. Lets the dashboard cross-link Postgres rows
    back to Langfuse traces."""

    created_at: datetime = Field(default_factory=_now)

    @field_validator("score")
    @classmethod
    def _score_in_range(cls, v: int) -> int:
        """Accept the two supported conventions: ``-1`` / ``+1`` thumbs
        OR ``1..5`` stars. Any other value (e.g. 0, 6, -5) is rejected
        at the schema layer so bad clients fail fast."""
        # Thumbs: -1 or +1. Stars: 2-5 (1 already covered as "thumbs up").
        star_min, star_max = 2, 5
        if v in (-1, 1) or star_min <= v <= star_max:
            return v
        raise ValueError(
            f"score must be -1 (thumbs down), 1 (thumbs up), or 1-5 (star rating); got {v}"
        )


class KbChunk(BaseModel):
    """A single retrievable chunk of a knowledge-base document.

    Written by ``mdk kb ingest <agent> <path>`` and queried by the
    ``kb-vector-lookup`` skill at agent run time. Lets an agent
    answer questions over a corpus without the caller having to
    pre-fetch context (today's ``rag-qa`` template requires the
    caller to pass ``context: list[str]`` in the input — this
    flips the model so the agent retrieves its own context).

    Storage strategy (0.8.2.13 MVP): embeddings persisted as plain
    float arrays — JSONB on Postgres, JSON-encoded TEXT on sqlite.
    NO native vector index yet (pgvector / sqlite-vss); cosine
    similarity is computed in Python at query time. This keeps the
    MVP infrastructure-free + works on Azure Postgres Flex without
    a server-parameter change. The storage protocol is shaped so a
    later pgvector swap is the only diff — callers don't change.
    """

    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(default_factory=lambda: uuid4().hex)
    """Stable id; doubles as the table primary key."""

    tenant_id: str
    """Tenant that owns the chunk. Same scoping convention as runs/
    feedback — never list / search across tenants."""

    agent: str
    """Which agent this chunk's KB belongs to. One KB per agent is
    the v0.9 mental model; future expansion (shared / cross-agent
    KBs) is deferred until we have a real need."""

    source: str
    """Source identifier (e.g. file path, URL). The combination of
    ``(agent, tenant_id, content_hash)`` is unique — see
    ``content_hash`` for the dedup story."""

    text: str
    """The chunk's text content. Length-bounded by the chunker's
    target size (default ~500 tokens; concrete byte cap is left to
    the chunker, not the storage layer)."""

    embedding: list[float]
    """Vector representation, length matches the producer model's
    output dim (OpenAI text-embedding-3-small → 1536). Stored as
    JSONB on Postgres / JSON-encoded TEXT on sqlite. Cosine similarity
    against this vector is the retrieval primitive."""

    embedding_model: str
    """Model identifier that produced ``embedding`` (e.g.
    ``openai/text-embedding-3-small``). At query time we MUST embed
    the question with the same model — different models produce
    incomparable vector spaces. The skill rejects cross-model
    queries explicitly rather than silently degrading retrieval."""

    content_hash: str
    """SHA-256 of ``text``. Combined with ``(agent, tenant_id)``
    forms the dedup key — re-ingesting the same file is idempotent
    (existing chunks are upserted, not duplicated)."""

    metadata: dict[str, Any] | None = None
    """Optional bag of source-document attributes — e.g. heading
    path, page number, section index. Used by citation rendering;
    not part of the dedup key."""

    ocr: bool = False
    """True iff the chunk's text was produced by Tesseract OCR rather
    than native text extraction (pypdf text layer, docx paragraphs,
    readability HTML strip). Set by the ingest pipeline when
    :func:`~movate.kb.parsers.parse_pdf` falls back to OCR for a
    scanned-image PDF. Always False for non-PDF formats."""

    created_at: datetime = Field(default_factory=_now)


class KbChunkWithScore(BaseModel):
    """A retrieved :class:`KbChunk` plus its similarity score.

    Search results carry both — the chunk's own data + the cosine
    similarity (0.0 to 1.0) against the query embedding. Callers use
    the score to threshold or rank results; CLIs display it for
    operator inspection.
    """

    model_config = ConfigDict(extra="forbid")

    chunk: KbChunk
    score: float = Field(
        ...,
        ge=-1.0,
        le=1.0,
        description="Cosine similarity. Higher = closer match. 1.0 = identical.",
    )


class Entity(BaseModel):
    """A node in an agent's knowledge graph (GraphRAG).

    Extracted from KB chunks by ``mdk kb ingest --build-graph`` and used
    as the seed for graph-augmented retrieval. Same per-agent + per-tenant
    scoping as :class:`KbChunk`; the graph is an index layered over the
    same corpus, not a separate store.

    Storage strategy mirrors :class:`KbChunk`: ``embedding`` persisted as a
    plain float array (JSONB on Postgres, JSON-encoded TEXT on sqlite),
    cosine computed in Python at query time so the pgvector swap is the
    only future diff. ``source_chunk_ids`` links each entity back to the
    chunks it was extracted from so GraphRAG answers can cite source.
    """

    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(default_factory=lambda: uuid4().hex)
    """Stable id; doubles as the table primary key and the edge endpoint
    referenced by :class:`Relation`."""

    tenant_id: str
    """Tenant that owns the entity. Same scoping convention as KbChunk —
    never list / search / expand across tenants."""

    agent: str
    """Which agent's KB graph this entity belongs to. One graph per agent,
    matching the one-KB-per-agent model."""

    name: str
    """Canonical surface form of the entity (e.g. ``"SAML SSO"``). The
    extraction pipeline normalizes aliases to one canonical name so the
    same real-world entity dedups to a single node."""

    type: str
    """Free-form entity type from extraction (e.g. ``"Policy"``,
    ``"Product"``, ``"Tier"``). Not an enum — the taxonomy is corpus-
    dependent and the LLM picks it; callers treat it as an opaque label."""

    description: str | None = None
    """LLM-generated summary of the entity, aggregated across the chunks
    it appears in. Surfaced in the assembled graph context at retrieval
    time. Optional — a bare node (name+type) is valid."""

    embedding: list[float]
    """Vector representation of ``name`` (+ ``description`` when present),
    same producer model as the chunk embeddings. The seed primitive:
    ``search_entities`` ranks by cosine against this."""

    embedding_model: str
    """Model identifier that produced ``embedding``. Query embeddings MUST
    use the same model — different models are incomparable vector spaces
    (same contract as KbChunk)."""

    content_hash: str
    """SHA-256 of the normalized ``(name, type)``. Combined with
    ``(agent, tenant_id)`` forms the dedup key — re-ingesting the same
    corpus upserts entities in place rather than duplicating nodes."""

    source_chunk_ids: list[str] = Field(default_factory=list)
    """``KbChunk.chunk_id`` values this entity was extracted from. Drives
    citation provenance: a GraphRAG answer can trace each node back to the
    source passages. Not part of the dedup key — merged (union) across
    re-ingests."""

    metadata: dict[str, Any] | None = None
    """Optional source-document attributes (e.g. salience, mention count).
    Not part of the dedup key."""

    created_at: datetime = Field(default_factory=_now)


class Relation(BaseModel):
    """A directed edge between two :class:`Entity` nodes in an agent's
    knowledge graph.

    The traversal substrate for ``expand_neighbors``. Endpoints reference
    :class:`Entity.entity_id`; the caller upserts both endpoint entities
    before the relation (the storage layer does not create dangling
    endpoints). Same per-agent + per-tenant scoping as the rest of the KB.
    """

    model_config = ConfigDict(extra="forbid")

    relation_id: str = Field(default_factory=lambda: uuid4().hex)
    """Stable id; table primary key."""

    tenant_id: str
    """Tenant that owns the edge. Never traverse across tenants."""

    agent: str
    """Which agent's KB graph this edge belongs to."""

    src_entity_id: str
    """``Entity.entity_id`` of the edge's source (tail) node."""

    dst_entity_id: str
    """``Entity.entity_id`` of the edge's destination (head) node."""

    type: str
    """Free-form predicate from extraction (e.g. ``"REQUIRES"``,
    ``"SUPERSEDES"``, ``"PART_OF"``). Opaque label like ``Entity.type``."""

    description: str | None = None
    """Evidence / rationale sentence the LLM extracted the edge from.
    Surfaced in the assembled graph context for grounding."""

    weight: float = 1.0
    """Extraction confidence / co-occurrence strength in ``[0, 1]``-ish
    range (not hard-bounded). ``expand_neighbors`` orders traversal by
    descending weight so the budget spends on the strongest edges first."""

    content_hash: str
    """SHA-256 of the normalized ``(src_entity_id, dst_entity_id, type)``.
    Combined with ``(agent, tenant_id)`` forms the dedup key — re-ingesting
    upserts the edge in place."""

    source_chunk_ids: list[str] = Field(default_factory=list)
    """``KbChunk.chunk_id`` values this edge was extracted from. Citation
    provenance, unioned across re-ingests. Not part of the dedup key."""

    metadata: dict[str, Any] | None = None
    """Optional edge attributes. Not part of the dedup key."""

    created_at: datetime = Field(default_factory=_now)


class EntityWithScore(BaseModel):
    """A retrieved :class:`Entity` plus its similarity score.

    Returned by ``search_entities`` — the vector-seed step of GraphRAG.
    Mirrors :class:`KbChunkWithScore`: the score is cosine similarity
    against the query embedding, used to threshold / rank seed nodes
    before expansion.
    """

    model_config = ConfigDict(extra="forbid")

    entity: Entity
    score: float = Field(
        ...,
        ge=-1.0,
        le=1.0,
        description="Cosine similarity. Higher = closer match. 1.0 = identical.",
    )


class Subgraph(BaseModel):
    """The result of ``expand_neighbors``: the entities reached and the
    relations traversed during a bounded k-hop expansion.

    Deliberately a flat pair of lists rather than an adjacency structure —
    the retrieval layer assembles its own context string from these, and a
    flat shape maps cleanly onto every backend (relational rows, in-memory
    dicts, a future Neo4j result) without leaking traversal mechanics.
    """

    model_config = ConfigDict(extra="forbid")

    entities: list[Entity]
    """All distinct entities in the expanded subgraph, including the seed
    nodes the expansion started from."""

    relations: list[Relation]
    """All distinct edges traversed to reach ``entities``. Every edge's
    ``src_entity_id`` / ``dst_entity_id`` is present in ``entities``."""


# ---------------------------------------------------------------------------
# Projects (ADR 040) — tenant-scoped first-class container for agents,
# workflows, and KBs, with a member/role model layered on top of ADR 013
# tenant scopes. Agents and workflows are M:N with projects (D2); a KB is
# created under exactly one owning project and may be reference- or
# copy-shared into others (D3). Project deletion is soft (D6); a per-tenant
# ``default`` project absorbs unattached resources for D5 back-compat.
# Storage-only here — the /api/v1 endpoints and the composed RBAC layer ship
# in separate PRs on top of this one.
# ---------------------------------------------------------------------------


_DEFAULT_PROJECT_NAME = "default"
"""Reserved project name per tenant — auto-created on first read and
guarded against deletion at the storage layer (D5 + D6, ADR 040)."""

_DEFAULT_PROJECT_DESCRIPTION = "Auto-assigned for agents created without an explicit project"
"""Stable description for the per-tenant default project; surfaced in the
front-end so users understand why an un-projected agent shows up here."""

_TENANT_SYSTEM_PRINCIPAL = "tenant-system"
"""Synthetic principal recorded as the default project's owner when no
tenant-admin principal is discoverable from the auth/key infrastructure.
Projects are tenant-scoped containers (D1), and ``movate.core.auth`` only
exposes per-key principals (``api_key:<key_id>``) — there is no separate
"tenant admin user" registry to look up here, so the synthetic principal
mirrors ADR 040's migration text ("owned by a synthetic ``tenant-system``
principal")."""


class ProjectMemberRole(StrEnum):
    """Project-level RBAC roles (ADR 040 D4).

    ``viewer`` reads project + attached resources; ``editor`` adds CRUD on
    those resources; ``owner`` adds membership + archive. Composes with
    (never weakens) tenant scopes (ADR 013).
    """

    VIEWER = "viewer"
    EDITOR = "editor"
    OWNER = "owner"


class ProjectKbMode(StrEnum):
    """How a KB row is bound to a project (ADR 040 D3).

    ``owned`` — the project that originally created the KB (exactly one
    such row per kb_id).
    ``shared_reference`` — a read-only reference share to another project
    in the same tenant; no chunks are duplicated.
    ``shared_copy`` — a copy-on-attach share that forks chunks under the
    consuming project so it can diverge (re-ingest / re-chunk) without
    touching the upstream.
    """

    OWNED = "owned"
    SHARED_REFERENCE = "shared_reference"
    SHARED_COPY = "shared_copy"


class Project(BaseModel):
    """A tenant-scoped Project (ADR 040 D1).

    Unique on ``(tenant_id, name)``. ``archived_at`` is NULL while live;
    soft-delete (D6) sets it. The default project per tenant
    (``name == "default"``) cannot be archived — :meth:`storage.archive_project`
    rejects it at the storage layer in addition to the API guard.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(default_factory=lambda: f"prj_{uuid4().hex[:16]}")
    """Stable id minted at create time; the API surface keys by this."""
    tenant_id: str
    """Owning tenant — never NULL, never crosses a tenant boundary."""
    name: str
    """Human-facing name; unique within ``tenant_id``."""
    description: str | None = None
    owner_principal_id: str
    """Principal that created the project (or the synthetic
    ``tenant-system`` for the default project). The API layer maps this to
    an initial ``owner`` member row."""
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    archived_at: datetime | None = None
    """Set by :meth:`storage.archive_project` for soft delete (D6); the
    project disappears from default listings but its attachments + history
    remain."""


class ProjectMember(BaseModel):
    """One member row on a project (ADR 040 D1 + D4).

    Composite PK ``(project_id, principal_id)``; role transitions (e.g.
    viewer → editor → owner) are in-place updates via
    :meth:`storage.update_project_member`.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str
    principal_id: str
    role: ProjectMemberRole
    added_by: str
    """Principal that performed the invite — distinct from
    ``Project.owner_principal_id`` so a later membership audit can attribute
    every grant to its actor."""
    added_at: datetime = Field(default_factory=_now)


class ProjectAgent(BaseModel):
    """Junction row attaching an agent name to a project (ADR 040 D2).

    M:N: one agent can attach to multiple projects within the same tenant.
    The agent's tenant-scoped registry row (ADR 014) is unchanged; this is
    a membership relation, not a re-parenting.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str
    agent_name: str
    """The agent's :class:`AgentBundleRecord` ``name`` — the registry key."""
    added_at: datetime = Field(default_factory=_now)


class ProjectWorkflow(BaseModel):
    """Junction row attaching a workflow name to a project (ADR 040 D2).

    M:N, identical shape to :class:`ProjectAgent`; the workflow's
    tenant-scoped definition is unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str
    workflow_name: str
    added_at: datetime = Field(default_factory=_now)


class ProjectKb(BaseModel):
    """Junction row attaching a KB id to a project (ADR 040 D3).

    Unlike agents/workflows, KBs carry a ``mode`` indicating whether this
    project is the owner, a read-only reference share, or a forked copy.
    Exactly one ``owned`` row per ``kb_id`` is the invariant; the API layer
    enforces it on share, the storage layer just round-trips ``mode``.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str
    kb_id: str
    mode: ProjectKbMode
    added_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Agent catalog (ADR 041)
#
# Three namespaces in one schema, distinguished by ``source``:
#
# * ``movate``    — curated public catalog (synced from ``catalog.movate.io``).
#                    ``tenant_id`` is NULL.
# * ``private``   — tenant-private entries that NEVER sync upward.
#                    ``tenant_id`` is set to the owning tenant.
# * ``community`` — column-ready slot for a future user-contributed namespace.
#                    ``tenant_id`` NULL. No rows are written in v1.
#
# Uniqueness key ``(slug, source, tenant_id)`` covers all three (the
# pydantic models do not enforce uniqueness — the storage layer does, via
# the table's PK / unique index).
# ---------------------------------------------------------------------------


class CatalogSource(StrEnum):
    """Catalog namespace. See module docstring for the semantics of each."""

    MOVATE = "movate"
    PRIVATE = "private"
    COMMUNITY = "community"


class CatalogRatingsSummary(BaseModel):
    """Aggregate of all ratings for one catalog entry. Carried inline on
    :class:`CatalogEntry` so a list view can render it without a second
    query. Recomputed by :meth:`StorageProvider.record_catalog_rating`."""

    model_config = ConfigDict(extra="forbid")

    count: int = 0
    avg: float = 0.0


class CatalogEntry(BaseModel):
    """A single entry in the agent catalog (ADR 041 D2).

    One row per (slug, source, tenant_id). ``latest_version`` points at
    the most-recently-published row in :class:`CatalogEntryVersion`. The
    bundle bytes for any version live in the version table; this record
    is the catalog **manifest** — what the browse / search / detail
    endpoints render.
    """

    model_config = ConfigDict(extra="forbid")

    slug: str
    """Stable id (URL-safe). E.g. ``ticket_triager``."""
    source: CatalogSource
    """Namespace (``movate`` | ``private`` | ``community``)."""
    tenant_id: str | None = None
    """The owning tenant for ``private`` entries; ``None`` for ``movate`` and
    ``community`` (public namespaces).

    Storage enforces ``NOT NULL`` for ``private`` and ``NULL`` for the public
    namespaces; the Pydantic model accepts either because callers serialize
    public entries with no ``tenant_id`` field at all."""
    latest_version: str
    """Semver string. The version that detail / clone uses by default."""
    name: str
    """Short human label (~one or two words)."""
    title: str
    """Display title for the catalog card."""
    description: str
    """Plain-text description — the catalog card's body."""
    tags: list[str] = Field(default_factory=list)
    """Free-form tags used for filtering (ADR 028 taxonomy + custom)."""
    shape: str | None = None
    """One of the ADR 028 shape taxonomy values (``faq``, ``rag_qa``, ...)."""
    recommended_for: str | None = None
    """One-line use-case statement (rendered under the title)."""
    ratings_summary: CatalogRatingsSummary = Field(default_factory=CatalogRatingsSummary)
    """Aggregate of all ratings — count + mean."""
    popularity: int = 0
    """Add-from-catalog count (ADR 041 D6). Maintained by future work; an
    integer here so the storage column exists from day one."""
    synced_at: datetime = Field(default_factory=_now)
    """When this row was last written by the sync job (or by the tenant
    on a private submission). Drives the watermark + cache TTL."""


class CatalogEntryVersion(BaseModel):
    """One published version of a :class:`CatalogEntry`.

    The bundle bytes live here (``bundle_tar``) — fetched lazily on add.
    Versions are immutable once published; a re-publish writes a new row.
    """

    model_config = ConfigDict(extra="forbid")

    slug: str
    version: str
    """SemVer (``MAJOR.MINOR.PATCH``)."""
    source: CatalogSource
    tenant_id: str | None = None
    """Tenant scope for ``private`` versions; ``None`` for public."""
    bundle_tar: bytes
    """Raw tar bytes of the entry bundle. Customer-side storage; the
    contents are opaque to the catalog service (ADR 041)."""
    digest: str
    """SHA-256 of ``bundle_tar`` (hex). Stable id for caching + integrity."""
    published_at: datetime = Field(default_factory=_now)
    deprecated_at: datetime | None = None


class CatalogEntryRating(BaseModel):
    """One tenant's rating + (optional) comment for one catalog entry.

    PK ``(slug, source, tenant_id)`` — one rating per tenant per entry.
    Re-rating overwrites the prior row.
    """

    model_config = ConfigDict(extra="forbid")

    slug: str
    source: CatalogSource = CatalogSource.MOVATE
    """Which namespace the rating targets. ``movate`` is the common case
    today; ``private`` is allowed for tenants who want internal feedback
    on their own catalog entries."""
    tenant_id: str
    """The rating tenant. Required (a rating is always attributable)."""
    rating: int = Field(ge=1, le=5)
    """1-5 stars."""
    comment: str | None = None
    created_at: datetime = Field(default_factory=_now)


class CatalogSyncWatermark(BaseModel):
    """Per-source watermark — the last time we synced from ``source``.

    A row is created the first time a sync runs for that source. The sync
    job advances this in lockstep with the upserts so the next run can
    incrementally fetch deltas (ADR 041 D4)."""

    model_config = ConfigDict(extra="forbid")

    source: CatalogSource
    last_synced_at: datetime = Field(default_factory=_now)


# Forward ref resolution
ModelConfig.model_rebuild()
