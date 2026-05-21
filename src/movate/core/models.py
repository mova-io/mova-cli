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

    model_config = ConfigDict(extra="forbid")

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
    EVAL = "eval"
    """Async eval run. JobRecord.input carries the eval config
    (gate, runs, mock, baseline_id, ...). Worker loads the agent
    bundle from the registry, runs EvalEngine, persists EvalRecord.
    The job's ``result_run_id`` is left null; instead, JobRecord's
    output payload carries ``{eval_id}`` so the caller can fetch the
    completed EvalRecord via GET /api/v1/evals/{eval_id} (item 84).
    See BACKLOG Group H item 83."""


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
    thread_id: str | None = None
    """For threaded runs (Tier 10.5 / PR-Q), the
    :class:`ConversationThread` id this job belongs to. The worker
    propagates this onto the spawned :class:`RunRecord.thread_id` so
    the run joins back to its thread. ``None`` (the common case) for
    standalone non-threaded jobs."""


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
    """Permission scope. ``"fleet-admin"`` grants access to admin-only endpoints
    (POST/GET/DELETE /api/v1/auth/keys). ``None`` = standard tenant key."""


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


# Forward ref resolution
ModelConfig.model_rebuild()
