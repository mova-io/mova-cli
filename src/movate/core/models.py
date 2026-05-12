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


class AgentSpec(BaseModel):
    """Parsed ``agent.yaml`` contents (api_version: movate/v1, kind: Agent)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    api_version: Literal["movate/v1"] = Field(..., alias="api_version")
    kind: Literal["Agent"] = "Agent"

    name: str = Field(..., min_length=1, max_length=128)
    version: str
    description: str = ""
    owner: str = ""

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


class BenchModelRow(BaseModel):
    """Per-model aggregate row inside a :class:`BenchRecord`.

    One row per provider/model that `movate bench` exercised. Field
    semantics mirror the live ``ModelBenchResult`` properties on the
    bench engine — same numbers, persisted instead of recomputed.

    ``score`` is ``None`` when no judge was configured for the bench
    OR when the judge was skipped for this provider (e.g. cross-family
    enforcement). ``skipped_reason`` carries the human-readable
    explanation in that case; ``skipped_score`` is the boolean — both
    distinguish "no judge at all" (``score=None``, ``skipped_score=False``)
    from "judge skipped for cross-family reasons" (``score=None``,
    ``skipped_score=True``).
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    successful_runs: int
    error_count: int
    cost_total_usd: float
    cost_mean_usd: float
    latency_p50_ms: int
    latency_p95_ms: int
    score: float | None = None
    skipped_reason: str | None = None
    skipped_score: bool = False


class BenchRecord(BaseModel):
    """Persisted summary of one bench run (one input, one agent version, N models).

    Sister to :class:`EvalRecord` but for the bench surface. Eval is
    dataset * runs across ONE provider; bench is ONE input * runs
    across N providers. Both shapes serve as drift-tracking anchors —
    ``movate bench --baseline <id>`` diffs current scores / costs /
    latencies against a stored baseline the same way
    ``movate eval --baseline`` does.

    ``input_hash`` is sha256 of the canonical JSON of the input dict
    (sorted keys, no whitespace). Lets you spot when a baseline diff
    was computed against a different input — without storing the full
    input in the row (PII consideration on shared envs).

    ``judge_method`` is optional because bench can run with no judge at
    all (cost/latency-only comparison). When ``None``, all per-model
    ``score`` fields are also ``None``.

    The ``models`` field is JSON-serialized in the storage row — sqlite
    uses TEXT + json.loads on read, postgres uses JSONB natively.
    """

    model_config = ConfigDict(extra="forbid")

    bench_id: str
    tenant_id: str
    agent: str
    agent_version: str
    input_hash: str
    judge_method: JudgeMethod | None = None
    judge_provider: str | None = None
    rubric: str | None = None
    runs_per_model: int
    gate_mode: str
    total_cost_usd: float
    models: list[BenchModelRow] = Field(default_factory=list)
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
    deliver never re-queues the job."""
    notify_sms: str | None = None
    """Optional E.164 phone number to notify by SMS when the job
    reaches a terminal status. Same fire-and-forget contract as
    ``notify_email`` — SMS-provider errors log but never re-queue.
    The actual delivery vendor (Azure Communication Services for v1.0,
    per docs/v1.0-azure-design.md §10) is selected by
    :func:`movate.core.notify.build_dispatcher` from env vars at worker
    startup; this column only stores the destination."""
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
