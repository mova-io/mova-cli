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

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    """Provider + params. ``provider`` is a LiteLLM model string.

    Examples:

        provider: openai/gpt-4o-mini-2024-07-18
        provider: azure/gpt-4.1
        provider: anthropic/claude-sonnet-4-6

    Floating tags (``latest``, ``stable``) are rejected at parse time so a
    silent provider rotation can't change a deployed agent's behavior.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    params: dict[str, Any] = Field(default_factory=dict)
    fallback: list[ModelFallback] = Field(default_factory=list)

    @field_validator("provider")
    @classmethod
    def _reject_floating_tags(cls, v: str) -> str:
        if "/" not in v:
            raise ValueError(
                f"provider {v!r} must be a LiteLLM model string in '<provider>/<model>' form"
            )
        _, model = v.split("/", 1)
        floating = {"latest", "stable", "newest"}
        if model.lower() in floating or model.endswith("-latest"):
            raise ValueError(f"floating model tag rejected: {v!r}; pin to a dated revision")
        return v


class SchemaPaths(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: str
    output: str


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
    """

    LITELLM = "litellm"
    NATIVE_ANTHROPIC = "native_anthropic"
    NATIVE_OPENAI = "native_openai"
    LANGCHAIN = "langchain"


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
