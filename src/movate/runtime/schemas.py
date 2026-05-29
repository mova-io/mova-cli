"""HTTP wire types for the movate runtime.

Kept separate from :mod:`movate.core.models` so the API surface can
evolve independently of the persisted schema. A change to ``JobRecord``
shouldn't force every consumer to upgrade; a change to the wire type
shouldn't force a DB migration.

Convention: every public response that names an entity ends in ``View``
(``JobView``, ``AgentView``) — distinguishes wire shape from DB model
in import sites and at code review.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from movate.core.models import (
    ConversationThread,
    ErrorInfo,
    FeedbackRecord,
    JobKind,
    JobRecord,
    JobStatus,
    Metrics,
    RunRecord,
    WorkflowRunRecord,
    WorkflowStatus,
)
from movate.core.reporting import AgentRollup, FailingCase, LatencyPercentiles, Report


class RunSubmission(BaseModel):
    """``POST /run`` request body.

    ``kind`` discriminates the dispatch path; ``target`` is the agent
    or workflow name. ``input`` is the run input for an agent kind, or
    the initial state dict for a workflow kind. Validation against the
    target's input schema happens in the worker — accepting any dict
    here keeps the HTTP layer simple and lets schema errors land in
    the persisted ``JobRecord.error`` instead of as a 4xx that's
    invisible to ``/jobs/{id}`` polling.
    """

    model_config = ConfigDict(extra="forbid")

    kind: JobKind
    target: str = Field(..., min_length=1)
    input: dict[str, Any]
    notify_email: str | None = Field(
        default=None,
        description=(
            "Optional email address. If set, the worker emails this address "
            "when the job reaches a terminal status. Notification failure "
            "is logged but never re-queues the job."
        ),
    )


class RunAccepted(BaseModel):
    """``POST /run`` response — what the client polls against."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: JobStatus
    """Always ``QUEUED`` from this endpoint; included for forward compat
    if we ever add a synchronous ``?wait=true`` mode."""

    deduplicated: bool = False
    """item 23: ``True`` only on the trigger fire endpoint when an
    ``X-Movate-Delivery-Id`` replay matched a prior delivery — the returned
    ``job_id`` is the original (no new job was enqueued). Defaulted ``False``
    so every other producer of this model (``POST /run``, the scheduler, etc.)
    is byte-for-byte unchanged."""


class JobView(BaseModel):
    """``GET /jobs/{id}`` response.

    Mirror of :class:`JobRecord` minus ``api_key_id`` (audit-only,
    never returned over the wire).
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    kind: JobKind
    target: str
    status: JobStatus
    input: dict[str, Any]
    result_run_id: str | None = None
    error: ErrorInfo | None = None
    created_at: datetime
    claimed_at: datetime | None = None
    completed_at: datetime | None = None
    notify_email: str | None = None

    @classmethod
    def from_record(cls, record: JobRecord) -> JobView:
        return cls(
            job_id=record.job_id,
            kind=record.kind,
            target=record.target,
            status=record.status,
            input=record.input,
            result_run_id=record.result_run_id,
            error=record.error,
            created_at=record.created_at,
            claimed_at=record.claimed_at,
            completed_at=record.completed_at,
            notify_email=record.notify_email,
        )


class RunView(BaseModel):
    """``GET /runs/{id}`` response.

    Mirror of :class:`RunRecord` minus ``tenant_id`` (audit-only,
    never returned over the wire — same convention as ``JobView``
    dropping ``api_key_id``). Includes ``output`` so callers can see
    what the agent actually produced; this is the whole point of the
    endpoint vs. ``GET /jobs/{id}`` which only carries pointer state.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    job_id: str
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
    created_at: datetime
    workflow_run_id: str | None = None
    node_id: str | None = None
    thread_id: str | None = None
    """For multi-turn conversational runs (PR-N), the id of the
    ConversationThread this run belongs to. ``None`` for standalone
    runs. Surfaces here so ``GET /runs/{id}`` clients can navigate
    back to the parent thread."""

    @classmethod
    def from_record(cls, record: RunRecord) -> RunView:
        return cls(
            run_id=record.run_id,
            job_id=record.job_id,
            agent=record.agent,
            agent_version=record.agent_version,
            prompt_hash=record.prompt_hash,
            provider=record.provider,
            provider_version=record.provider_version,
            pricing_version=record.pricing_version,
            status=record.status,
            input=record.input,
            output=record.output,
            metrics=record.metrics,
            error=record.error,
            created_at=record.created_at,
            workflow_run_id=record.workflow_run_id,
            node_id=record.node_id,
            thread_id=record.thread_id,
        )


class JobListView(BaseModel):
    """``GET /jobs`` response — envelope around a page of JobViews.

    Envelope (rather than a bare list) so we can grow the response in
    a backwards-compatible way: paging cursors, total counts, filter
    echoes. Right now ``count`` is the page size returned — useful for
    a quick sanity check without re-counting on the client.
    """

    model_config = ConfigDict(extra="forbid")

    jobs: list[JobView]
    count: int


class JobCancelView(BaseModel):
    """``POST /api/v1/jobs/{id}/cancel`` response (item 36, R4b).

    ``status`` is the job's status AFTER the cancel request:

    * ``cancelled`` — the job was ``QUEUED`` and is now terminally
      cancelled (it will never be claimed/executed).
    * ``running`` — the job was already ``RUNNING`` when the cancel
      landed; the cancel is *pending*. The worker honors it at its next
      checkpoint and the job becomes ``cancelled`` shortly after (poll
      ``GET /jobs/{id}`` to observe the transition).
    * a terminal status (``success`` / ``error`` / ``safety_blocked`` /
      ``dead_letter`` / ``cancelled``) — the job was already finished;
      cancel was a no-op (you can't cancel a completed job).

    Cancellation is **cooperative**: there is no mid-LLM-call
    interruption. A ``RUNNING`` job's in-flight work is allowed to
    complete, then its result is discarded in favor of ``cancelled``.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: JobStatus


# ---------------------------------------------------------------------------
# Workflow HITL signal (ADR 017 D5, PR 2 — resume-on-signal)
# ---------------------------------------------------------------------------


class WorkflowSignalRequest(BaseModel):
    """``POST /api/v1/workflow-runs/{id}/signal`` request body.

    The human approver's decision: a dict of the state keys the paused
    gate's ``output_contract`` requires. The endpoint validates every
    required key is present, merges the decision into the checkpoint's
    ``paused_state`` (decision wins), and enqueues a continuation job that
    resumes the workflow from the gate's successor. This is the contract a
    Teams Adaptive Card button (ADR 003) would POST to in a later PR.
    """

    model_config = ConfigDict(extra="forbid")

    decision: dict[str, Any] = Field(
        ...,
        description=(
            "The human's response: a mapping of the state keys named in the "
            "gate's output_contract to their values. Merged into the paused "
            "checkpoint state (decision wins) before the workflow resumes."
        ),
    )


class WorkflowRunView(BaseModel):
    """``GET /api/v1/workflow-runs`` item + signal-context view.

    Mirror of :class:`WorkflowRunRecord` minus ``tenant_id`` (audit-only,
    never returned over the wire — same convention as ``JobView`` dropping
    ``api_key_id``). The ``human_task`` block surfaces the gate's prompt +
    output_contract so an operator (or a Teams card) knows what decision to
    supply for a PAUSED run.
    """

    model_config = ConfigDict(extra="forbid")

    workflow_run_id: str
    workflow: str
    workflow_version: str
    status: WorkflowStatus
    initial_state: dict[str, Any]
    final_state: dict[str, Any] | None = None
    error_node_id: str | None = None
    error: ErrorInfo | None = None
    created_at: datetime
    paused_node_id: str | None = None
    human_task: dict[str, Any] | None = None

    @classmethod
    def from_record(cls, record: WorkflowRunRecord) -> WorkflowRunView:
        return cls(
            workflow_run_id=record.workflow_run_id,
            workflow=record.workflow,
            workflow_version=record.workflow_version,
            status=record.status,
            initial_state=record.initial_state,
            final_state=record.final_state,
            error_node_id=record.error_node_id,
            error=record.error,
            created_at=record.created_at,
            paused_node_id=record.paused_node_id,
            human_task=record.human_task,
        )


class WorkflowRunListView(BaseModel):
    """``GET /api/v1/workflow-runs`` response — envelope around a page.

    Envelope (not a bare list) for the same forward-compat reason as
    :class:`JobListView`.
    """

    model_config = ConfigDict(extra="forbid")

    workflow_runs: list[WorkflowRunView]
    count: int


class HealthView(BaseModel):
    """``GET /healthz`` response — boring on purpose."""

    model_config = ConfigDict(extra="forbid")

    status: str = "ok"
    version: str


class ReadyView(BaseModel):
    """``GET /ready`` response — readiness probe with per-check status.

    Distinct from ``/healthz`` (the liveness probe) — ``/ready`` runs
    deep checks (DB ping, etc.) and returns 503 if any fails so ACA
    stops routing traffic to a pod whose dependencies are dead,
    WITHOUT restarting it (a restart wouldn't help if the DB is the
    problem).

    The ``checks`` map surfaces each check's status individually so
    the operator can tell at a glance which dependency tripped the
    probe.
    """

    model_config = ConfigDict(extra="forbid")

    status: str
    """``"ready"`` (every check passed) or ``"not_ready"`` (at least one
    check failed). Mirrors the HTTP status (200 vs 503) for clients
    that prefer to parse JSON."""
    version: str
    checks: dict[str, str]
    """Per-check result. Keys are the check name (``"storage"``, etc.);
    values are ``"ok"`` or a short failure reason. ACA only cares
    about the HTTP status; the map is for human triage."""

    storage_backend: str | None = None
    """``"postgres"`` or ``"sqlite"`` — which provider :func:`build_storage`
    selected from environment. ``None`` only if storage isn't initialized
    yet (would be a bug). Drives ``mdk doctor target``'s durability check
    and surfaces misconfigured Container Apps (Postgres intended, SQLite
    actually picked) without needing pod log access."""

    storage_durable: bool | None = None
    """``True`` if the selected backend survives container restarts
    (Postgres), ``False`` if it doesn't (SQLite in a pod filesystem).
    A ``False`` here on a production deploy means every revision recycle
    wipes the ApiKeyRecord table → operators lose their saved keys."""


class AgentView(BaseModel):
    """One entry in the registry response.

    Returns metadata only — never prompt content or schemas. The
    full agent definition lives on disk; ``GET /agents`` is for
    discovery, not migration.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    description: str = ""


class AgentListView(BaseModel):
    """``GET /agents`` response."""

    model_config = ConfigDict(extra="forbid")

    agents: list[AgentView]


class AgentCatalogItemView(BaseModel):
    """One entry in the ``GET /api/v1/agents`` catalog response.

    Richer than the legacy :class:`AgentView` (which is discovery-only
    for the old ``GET /agents`` endpoint). Includes all marketplace
    metadata so the Angular Agent Catalog page can render role chips,
    capability badges, and tag filters without a follow-up
    ``GET /api/v1/agents/{name}`` round-trip.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    description: str = ""
    owner: str = ""
    role: str = ""
    persona: str = ""
    capabilities: list[str] = []
    tags: list[str] = []


class AgentCatalogView(BaseModel):
    """``GET /api/v1/agents`` response."""

    model_config = ConfigDict(extra="forbid")

    agents: list[AgentCatalogItemView]
    count: int


class AgentUpdatedView(BaseModel):
    """``PUT /api/v1/agents/{name}`` response."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    description: str = ""
    agent_dir: str
    files_persisted: list[str]
    previous_version: str
    """Version of the bundle that was replaced."""
    published_version: str | None = None
    """The registry version now serving as ``latest`` (ADR 021 D2). Equals
    ``version`` when the declared ``agent.yaml`` version was unique, or a
    derived ``<version>+<hash8>`` when the content changed without a version
    bump. ``None`` only if the durable registry write was unavailable."""
    changed: bool = True
    """Whether this re-deploy actually published new content (ADR 021 D2).
    ``False`` for a no-op re-deploy whose bundle bytes were unchanged — the
    served agent already matches, so no new registry row was written."""


class AgentCreatedView(BaseModel):
    """``POST /api/v1/agents`` response — the canonical layout the
    runtime persisted to disk, plus the resolved spec metadata so
    the Angular UI can immediately render the new agent's profile
    without a follow-up ``GET /api/v1/agents/{name}`` round-trip.

    The ``files_persisted`` array is verbatim what landed under
    ``<agents_path>/<name>/`` — the UI uses this to render
    "your agent is at agents/faq-bot/{...}".
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    description: str = ""
    agent_dir: str
    """Path-relative-to-agents-root where the bundle landed.
    E.g. ``faq-bot`` (NOT the absolute filesystem path — the Angular
    UI doesn't care about the runtime's CWD)."""
    files_persisted: list[str]
    """Sorted list of files written, relative to ``agent_dir``.
    E.g. ``["agent.yaml", "evals/dataset.jsonl", "prompt.md",
    "schema/input.json", "schema/output.json"]``."""
    published_version: str | None = None
    """The registry version now serving as ``latest`` (ADR 021 D2). Equals
    ``version`` when the declared ``agent.yaml`` version was unique, or a
    derived ``<version>+<hash8>`` when content changed without a version
    bump. ``None`` only if the durable registry write was unavailable."""
    changed: bool = True
    """Whether this publish wrote new content to the registry (ADR 021 D2).
    ``False`` for a no-op (the bundle bytes already match the latest
    published version) — no new registry row was written."""


class AgentVersionView(BaseModel):
    """One row in the durable agent-registry version history (ADR 014 D3).

    Distinct from :class:`AgentCommitView` (the GitHub commit history,
    ADR 007): this is the **registry**'s immutable ``(name, version)``
    rows — one per publish — surfaced by ``GET /api/v1/agents/{name}/
    versions`` and ``mdk agent history``. Carries the audit fields a
    team needs ("who published which version when") without the bundle
    payload itself."""

    model_config = ConfigDict(extra="forbid")

    version: str
    """The bundle's ``agent.yaml`` version. ``(name, version)`` is unique
    within a tenant; this is what a client passes back as ``If-Match``
    on a concurrency-safe PUT or as ``to_version`` on a revert."""

    created_by: str | None = None
    """Auth identity (ADR 013 — the API key id / OIDC subject) that
    published this version, or ``None`` for a system/seed import. Drives
    the "who published what" audit column."""

    created_at: datetime
    """When this version was published (UTC)."""

    content_hash: str
    """Content-addressed hash of the bundle's files — lets a client
    detect an unchanged re-publish and is an alternate ``If-Match``
    precondition value alongside ``version``."""

    is_current: bool = False
    """True for the newest version (the one a versionless resolve / run
    would pick up). Exactly one row in a non-empty history is current."""


class AgentVersionsView(BaseModel):
    """``GET /api/v1/agents/{name}/versions`` response (ADR 014 D3).

    The durable registry's version history for one agent, newest-first.
    Mirrors the listing style of :class:`AgentCatalogView` (items +
    count) so the Angular console renders it the same way."""

    model_config = ConfigDict(extra="forbid")

    name: str
    """Echoes the URL path parameter so callers can correlate the
    response without re-parsing the path."""

    versions: list[AgentVersionView]
    """Sorted newest-first (most recent publish at index 0). Empty when
    the agent has no registry rows for this tenant (never published, or
    a different tenant's agent — same no-leak contract as a 404)."""

    count: int
    """Number of versions returned (== ``len(versions)``)."""


class AgentRevertSubmission(BaseModel):
    """``POST /api/v1/agents/{name}/revert`` request body (ADR 014 D3 / #80).

    Names the prior version to roll back to. The revert re-publishes
    that version's bundle **forward** as a new latest version — it never
    deletes or rewrites history. ``to_version`` may also be supplied as
    a ``?to_version=`` query param for curl ergonomics; the body takes
    precedence when both are present."""

    model_config = ConfigDict(extra="forbid")

    to_version: str
    """The existing version to roll back to. 404 if no such version
    exists for this agent in this tenant."""


class AgentRevertedView(BaseModel):
    """``POST /api/v1/agents/{name}/revert`` response (ADR 014 D3 / #80).

    Confirms the non-destructive rollback: the bundle from
    ``reverted_from`` was re-published as the new latest version
    (``version``), leaving every prior version intact in the history."""

    model_config = ConfigDict(extra="forbid")

    name: str
    """Echoes the URL path parameter."""

    version: str
    """The new latest version after the revert. Same value as
    ``reverted_from`` because the revert re-publishes that exact bundle
    forward (a new row with the same ``version`` string + a fresh
    ``created_at`` / ``created_by``) — so a subsequent versionless
    resolve serves the reverted bundle."""

    reverted_from: str
    """The prior version whose bundle was re-published. Equal to the
    request's ``to_version``."""

    previous_version: str
    """The version that was the latest immediately BEFORE this revert —
    handy for an "undo the undo" affordance in the UI. Empty string when
    the only existing version was the revert target itself."""


class RunTraceView(BaseModel):
    """``GET /api/v1/runs/{run_id}/trace`` response.

    Reconstructed view of a single agent run OR a workflow run +
    per-node children. Mirrors the JSON shape ``mdk trace replay``
    emits today; the Angular UI's trace-viewer component reads this
    directly.

    Discriminated by ``kind``:

    * ``"agent"`` — single agent run; ``run`` is populated, ``workflow``
      and ``nodes`` are null/empty.
    * ``"workflow"`` — workflow run; ``workflow`` is the parent record,
      ``nodes`` is the chronological list of per-node ``RunRecord``
      dicts.

    The inner dicts use ``Any`` because run input/output payloads are
    arbitrary user content (the Angular UI doesn't structure-validate
    them — it just renders the JSON tree). Authoritative shape lives
    in :mod:`movate.core.replay`.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str
    """One of ``"agent"`` or ``"workflow"``."""
    run: dict[str, Any] | None = None
    """Populated when ``kind=="agent"``. Carries run_id, agent
    name+version, provider, status, input, output, error, metrics
    (latency, cost, tokens), prompt_hash, created_at."""
    workflow: dict[str, Any] | None = None
    """Populated when ``kind=="workflow"``. Carries workflow_run_id,
    workflow name+version, status, initial+final state, error_node_id
    + error, created_at."""
    nodes: list[dict[str, Any]] = []
    """Per-node child runs for workflows. Chronological order
    (oldest first). Empty for single-agent traces."""
    total_cost_usd: float = 0.0
    """Sum of cost across all child runs (workflows) or the single
    run's cost (agents). Rounded to 6 decimals."""
    total_latency_ms: int = 0
    """Sum of latency across all child runs (workflows) or the
    single run's latency (agents)."""


class EvalSubmission(BaseModel):
    """``POST /api/v1/agents/{name}/evals`` request body.

    Eval kickoff config. Mirrors the ``mdk eval`` CLI's flag set.

    Default execution is async: the endpoint creates a ``JobRecord``
    and returns immediately with ``{job_id, status: "queued"}``. The
    worker picks it up and persists the ``EvalRecord``; poll
    ``GET /api/v1/jobs/{job_id}`` for completion and
    ``GET /api/v1/evals/{result_run_id}`` for the scorecard.

    Pass ``wait=true`` for synchronous in-request execution (useful
    for demos or CI when a worker process is not running). For large
    datasets or real-LLM evals the async path is strongly preferred.
    """

    model_config = ConfigDict(extra="forbid")

    gate: float = Field(0.7, ge=0.0, le=1.0)
    """Per-case score required to pass (0.0-1.0)."""
    gate_mode: str = Field("mean")
    """How to aggregate N runs per case: ``mean``, ``min``, ``p10``."""
    runs: int = Field(1, ge=1, le=10)
    """Runs per case. Use 3+ for LLM-as-judge to defeat sampling variance."""
    mock: bool = Field(False)
    """Use the deterministic MockProvider (no API keys, fast)."""
    wait: bool = Field(False)
    """If true, run the eval synchronously inside the request handler
    and return the completed eval_id immediately. Convenient for CI
    scripts or demos where a separate worker process is not running.
    For datasets > 20 cases or real-LLM judges use the default async
    path (``wait=false``) to avoid HTTP gateway timeouts."""
    baseline_id: str | None = Field(None)
    """Optional EvalRecord id to diff against."""
    regression_tolerance: float = Field(0.0, ge=0.0, le=1.0)
    objective: str | None = Field(None)
    """Optional objective id to filter cases by (matches
    agent.yaml: objectives[].id)."""
    skill_responses: dict[str, dict[str, Any]] | None = Field(None)
    """Global skill stubs applied to every case when set. Same shape as
    EvalCase.skill_responses — keyed by skill name, value is the stub
    response dict. Per-case ``skill_responses`` in the dataset take
    precedence. Useful for remote eval with mock=true so skill calls
    return deterministic data rather than hitting live endpoints."""


class EvalAcceptedView(BaseModel):
    """``POST /api/v1/agents/{name}/evals`` response.

    Async path (default, ``wait=false``): returns immediately with
    ``status="queued"`` and ``job_id``. Poll
    ``GET /api/v1/jobs/{job_id}`` until terminal; use ``result_run_id``
    from that response as the ``eval_id`` for the scorecard endpoint.

    Sync path (``wait=true``): returns ``status="success"`` (or
    ``"failed"``) with ``eval_id`` populated directly.
    """

    model_config = ConfigDict(extra="forbid")

    eval_id: str = ""
    """Populated on the sync (``wait=true``) success path; empty on the
    async path (use the job's ``result_run_id`` once it completes)."""
    status: str = "queued"
    """``queued`` (async default) | ``success`` | ``failed`` (sync ``wait=true``)."""
    job_id: str = ""
    """Populated on the async path; empty on the sync path."""
    message: str = ""
    """Failure message when ``status == "failed"``; empty otherwise."""


class EvalScheduleSubmission(BaseModel):
    """``PUT /api/v1/agents/{name}/eval-schedule`` request body (ADR 016 D2).

    Upserts a continuous-eval cadence for the agent. Mirrors the
    ``mdk eval-schedule set`` flags. Additive + default-off: no schedule
    means nothing runs.
    """

    model_config = ConfigDict(extra="forbid")

    cadence_seconds: int = Field(ge=1)
    """How often (seconds) to enqueue an eval. The scheduler tick enqueues
    when this interval has elapsed since the last enqueue."""
    enabled: bool = Field(True)
    mock: bool = Field(False)
    """Cheap smoke cadence — MockProvider, no tokens."""
    runs: int = Field(1, ge=1, le=10)
    gate_mode: str = Field("mean")
    gate: float = Field(0.7, ge=0.0, le=1.0)
    objective: str | None = Field(None)
    regression_tolerance: float = Field(0.05, ge=0.0, le=1.0)
    """Mean_score / pass_rate drop vs baseline before drift fires."""
    baseline_id: str | None = Field(None)
    """Pinned baseline eval_id; default diffs against the prior eval."""
    notify_email: str | None = Field(None)


class EvalScheduleView(BaseModel):
    """One continuous-eval schedule (response shape for the schedule endpoints)."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    cadence_seconds: int
    enabled: bool
    mock: bool
    runs: int
    gate_mode: str
    gate: float
    objective: str | None = None
    regression_tolerance: float
    baseline_id: str | None = None
    notify_email: str | None = None
    last_enqueued_at: str | None = None
    created_at: str


class EvalScheduleListView(BaseModel):
    """``GET /api/v1/eval-schedules`` response."""

    model_config = ConfigDict(extra="forbid")

    schedules: list[EvalScheduleView]
    count: int


class JobScheduleSubmission(BaseModel):
    """``PUT /api/v1/schedules/{name}`` request body (ADR 017 D2).

    Upserts a generic cron schedule that enqueues a ``JobKind.AGENT`` /
    ``JobKind.WORKFLOW`` job on a cadence. Mirrors the ``mdk schedule set``
    flags + the ``mdk submit`` payload shape. Additive + default-off: no
    schedule means nothing runs.
    """

    model_config = ConfigDict(extra="forbid")

    kind: JobKind = Field(JobKind.AGENT)
    """Only ``agent`` / ``workflow`` are accepted — ``eval``/``bench`` are
    rejected by the JobSchedule model validator (eval has its own scheduler)."""
    target: str
    """Agent or workflow name to run on the cadence."""
    cadence_seconds: int = Field(ge=1)
    """How often (seconds) to enqueue a job. The scheduler tick enqueues
    when this interval has elapsed since the last enqueue."""
    enabled: bool = Field(True)
    input: dict[str, Any] = Field(default_factory=dict)
    """Job payload — the ``RunRequest.input`` dict for agents, the initial
    state dict for workflows (same shape as ``mdk submit``)."""
    notify_email: str | None = Field(None)

    @field_validator("kind")
    @classmethod
    def _kind_is_schedulable(cls, v: JobKind) -> JobKind:
        """Reject ``eval``/``bench`` at request-parse time (→ 422).

        Mirrors :meth:`movate.core.models.JobSchedule._kind_is_schedulable`
        so the API returns a 422 (validation error) rather than letting the
        downstream model raise a 500. EVAL has its own scheduler; BENCH is
        not a scheduling target.
        """
        if v not in (JobKind.AGENT, JobKind.WORKFLOW):
            raise ValueError(
                f"kind must be 'agent' or 'workflow', got {v.value!r}; "
                "eval has its own scheduler and bench is not a scheduling target."
            )
        return v


class JobScheduleView(BaseModel):
    """One generic cron schedule (response shape for the schedule endpoints)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: JobKind
    target: str
    cadence_seconds: int
    enabled: bool
    input: dict[str, Any]
    notify_email: str | None = None
    last_enqueued_at: str | None = None
    created_at: str


class JobScheduleListView(BaseModel):
    """``GET /api/v1/schedules`` response."""

    model_config = ConfigDict(extra="forbid")

    schedules: list[JobScheduleView]
    count: int


# ---------------------------------------------------------------------------
# Event/webhook triggers (ADR 017 D2)
# ---------------------------------------------------------------------------


class TriggerCreateRequest(BaseModel):
    """``POST /api/v1/triggers`` request body (ADR 017 D2).

    Registers an inbound event/webhook trigger that enqueues a
    ``JobKind.AGENT`` / ``JobKind.WORKFLOW`` job when an external system
    POSTs an event. Mirrors the ``mdk trigger create`` flags. Additive +
    default-off: no trigger means nothing fires.
    """

    model_config = ConfigDict(extra="forbid")

    kind: JobKind = Field(JobKind.AGENT)
    """Only ``agent`` / ``workflow`` are accepted — ``eval``/``bench`` are
    rejected by the Trigger model validator (eval has its own scheduler)."""
    target: str
    """Agent or workflow name to run when the trigger fires."""
    name: str | None = Field(None)
    """Trigger handle (unique per tenant). Defaults to the target name."""
    input_defaults: dict[str, Any] = Field(default_factory=dict)
    """Baseline job payload, merged UNDER the inbound event body (the event
    body wins on key collisions)."""
    enabled: bool = Field(True)

    @field_validator("kind")
    @classmethod
    def _kind_is_triggerable(cls, v: JobKind) -> JobKind:
        """Reject ``eval``/``bench`` at request-parse time (→ 422).

        Mirrors :meth:`movate.core.models.Trigger._kind_is_triggerable` so the
        API returns a 422 rather than letting the model raise a 500.
        """
        if v not in (JobKind.AGENT, JobKind.WORKFLOW):
            raise ValueError(
                f"kind must be 'agent' or 'workflow', got {v.value!r}; "
                "eval has its own scheduler and bench is not a trigger target."
            )
        return v


class TriggerView(BaseModel):
    """One trigger (response shape for the management endpoints).

    Never carries the secret — only the metadata. The plaintext secret is
    returned exactly once, by :class:`TriggerCreatedView` at creation.
    """

    model_config = ConfigDict(extra="forbid")

    trigger_id: str
    name: str
    kind: JobKind
    target: str
    input_defaults: dict[str, Any]
    enabled: bool
    last_fired_at: str | None = None
    created_at: str


class TriggerCreatedView(TriggerView):
    """``POST /api/v1/triggers`` response — includes the secret ONCE.

    ``secret`` is the plaintext per-trigger secret, shown **once** and
    irrecoverable afterward (only the hash + salt persist). With ``salt`` the
    caller derives the HMAC signing key
    (``hash_secret(secret, salt)``) it signs each event body with.
    ``webhook_path`` is the relative URL the external caller POSTs events to.
    """

    secret: str
    salt: str
    """The (non-sensitive) per-trigger salt. The caller derives the HMAC
    signing key as ``sha256(salt || secret)`` to sign each event body."""
    webhook_path: str
    """Relative path of the fire endpoint, e.g.
    ``/api/v1/triggers/<trigger_id>/events``. The caller prepends its
    runtime base URL."""


class TriggerListView(BaseModel):
    """``GET /api/v1/triggers`` response."""

    model_config = ConfigDict(extra="forbid")

    triggers: list[TriggerView]
    count: int


# ---------------------------------------------------------------------------
# Per-tenant provider keys (BYOK, ADR 018)
# ---------------------------------------------------------------------------


class ProviderKeySetRequest(BaseModel):
    """``PUT /api/v1/provider-keys/{provider}`` request body (ADR 018).

    Stores the tenant's own provider API key, encrypted at rest. The plaintext
    ``api_key`` is the ONLY place the key appears on the wire — it is encrypted
    before persist and is **never** echoed back (the response carries only a
    masked fingerprint). Mirrors ``mdk keys set``.
    """

    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(min_length=1)
    """The plaintext provider key (e.g. ``sk-...``). Encrypted at the edge and
    never returned. A new value rotates the stored key in place."""


class ProviderKeyView(BaseModel):
    """One configured provider key (metadata only — never the secret).

    The plaintext key is never carried by any response; only the provider
    namespace + a masked ``fingerprint`` (e.g. ``…AbCd``) so an operator can
    recognise which key is set without decrypting it.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    fingerprint: str
    created_at: str
    updated_at: str


class ProviderKeyListView(BaseModel):
    """``GET /api/v1/provider-keys`` response — configured providers only."""

    model_config = ConfigDict(extra="forbid")

    provider_keys: list[ProviderKeyView]
    count: int


# ---------------------------------------------------------------------------
# Canary / champion-challenger rollout (ADR 016 D3)
# ---------------------------------------------------------------------------


class CanarySetRequest(BaseModel):
    """``POST /api/v1/agents/{name}/canary`` request body (ADR 016 D3).

    Opt an agent into a canary rollout: route ``weight``% of prod traffic to
    ``challenger_version`` and compare it against the champion. Additive +
    default-off — no request, no canary. Mirrors ``mdk canary set``. Gated on
    ``admin`` (it changes which version prod traffic hits).
    """

    model_config = ConfigDict(extra="forbid")

    challenger_version: str
    """The published version to receive canary traffic."""
    weight: int = Field(default=0, ge=0, le=100)
    """Percent (0-100) of traffic to the challenger. 0 = kill switch."""
    champion_version: str | None = Field(default=None)
    """Optional champion pin. None → champion is registry latest."""
    sticky: bool = Field(default=True)
    """Consistent routing per ``thread_id`` (no champion↔challenger flip
    mid-conversation)."""
    enabled: bool = Field(default=True)
    auto_promote: bool = Field(default=False)
    """Opt-in: auto-promote the challenger once it clears ``eval_gate``."""
    eval_gate: float | None = Field(default=None)
    """Min challenger quality required for auto-promote. Required (non-None)
    when ``auto_promote`` is true."""
    auto_rollback: bool = Field(default=False)
    """Opt-in: a scheduled-eval drift regression on the challenger auto-trips
    the kill switch (``weight`` → 0), reverting to the champion. Default false
    = alert-only (ADR 016 D5 safety default)."""


class CanaryView(BaseModel):
    """One canary config (response shape for set/status endpoints)."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    challenger_version: str
    champion_version: str | None = None
    weight: int
    sticky: bool
    enabled: bool
    auto_promote: bool
    eval_gate: float | None = None
    auto_rollback: bool = False
    created_at: str
    updated_at: str


class CanarySideView(BaseModel):
    """Aggregated live quality for ONE side (champion or challenger).

    Sliced by ``agent_version`` (the canary slice key). Counts come from
    ``list_runs`` (run/error counts) joined to ``list_feedback`` (👍/👎).
    """

    model_config = ConfigDict(extra="forbid")

    version: str | None = None
    """The resolved version for this side. ``None`` for the champion side
    when the champion is registry-latest (not pinned) — runs are still
    sliced by whatever ``agent_version`` they recorded."""
    run_count: int
    success_count: int
    error_count: int
    thumbs_up: int
    thumbs_down: int
    feedback_count: int
    success_rate: float
    """``success_count / run_count`` (0.0 when no runs)."""
    thumbs_up_rate: float
    """``thumbs_up / feedback_count`` (0.0 when no feedback)."""


class CanaryCompareView(BaseModel):
    """``GET /api/v1/agents/{name}/canary/compare`` response (ADR 016 D3).

    Champion-vs-challenger live quality + the delta (challenger - champion).
    Positive deltas favor the challenger.
    """

    model_config = ConfigDict(extra="forbid")

    agent: str
    champion: CanarySideView
    challenger: CanarySideView
    success_rate_delta: float
    thumbs_up_rate_delta: float
    canary: CanaryView | None = None
    """The current canary config, or None if the agent has no canary set
    (the compare still works — it slices by the challenger version supplied
    via query — but typically there is a config)."""


class CanaryPromoteRequest(BaseModel):
    """``POST /api/v1/agents/{name}/canary/promote`` request body (ADR 016 D3).

    Promote a version to champion. Assisted by default (a human calls this);
    ``auto_promote`` honors the config's eval-gate. Gated on ``admin``.
    """

    model_config = ConfigDict(extra="forbid")

    to_version: str | None = Field(default=None)
    """Version to promote. None → promote the configured challenger."""
    auto_promote: bool = Field(default=False)
    """When true, only proceed if the target's measured quality clears the
    config's ``eval_gate`` (else 409). Default (assisted) skips the gate —
    the human IS the gate."""


class CanaryPromotedView(BaseModel):
    """``POST .../canary/promote`` + ``.../canary/rollback`` response."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    promoted_version: str
    """The version now serving as champion."""
    previous_champion: str | None = None
    """The champion before this promotion (for rollback / audit)."""
    mode: str
    """``"assisted"`` or ``"auto"`` (promote), or ``"rollback"``."""
    canary: CanaryView
    """The updated canary config (challenger→champion, weight→0 on promote)."""


class EvalCaseView(BaseModel):
    """One row in the eval scorecard. Matches the shape produced by
    ``mdk eval --output json`` for per-case data."""

    model_config = ConfigDict(extra="forbid")

    case_index: int
    score: float
    """0.0 - 1.0. Pass = score >= eval's gate."""
    passed: bool
    runs: int
    """How many times this case ran (=runs_per_case)."""
    objective: str | None = None
    notes: str = ""
    """Optional explanation — e.g. LLM judge rationale."""


class EvalScorecardView(BaseModel):
    """``GET /api/v1/evals/{eval_id}`` response (item 84).

    The complete EvalRecord rendered as JSON + per-case rows + 4-dim
    means (when the dataset opted into faithfulness / coverage; legacy
    datasets stay accuracy-only).

    Mirrors what ``mdk eval`` prints to terminal but as structured
    JSON for the Angular UI to render charts + diff with baseline.
    """

    model_config = ConfigDict(extra="forbid")

    eval_id: str
    agent: str
    agent_version: str
    dataset_hash: str
    judge_method: str
    judge_provider: str | None
    runs_per_case: int
    gate_mode: str
    threshold: float
    mean_score: float
    pass_rate: float
    sample_count: int
    total_cost_usd: float
    created_at: str
    """ISO-8601 timestamp."""


class EvalListView(BaseModel):
    """``GET /api/v1/evals?agent={name}`` response (item 85).

    Paginated history of past eval runs for an agent. Powers the
    "evals over time" chart on the Angular agent-profile page.
    """

    model_config = ConfigDict(extra="forbid")

    evals: list[EvalScorecardView]
    count: int


# ---------------------------------------------------------------------------
# Bench (BACKLOG #64) — multi-model comparison. Mirrors the eval wire types:
# a submission kicks off a JobKind.BENCH job; the result + list endpoints
# render the persisted BenchRecord.
# ---------------------------------------------------------------------------


class BenchSubmission(BaseModel):
    """``POST /api/v1/bench/{agent}`` request body.

    Bench kickoff config. Mirrors the ``mdk bench`` CLI's flag set:
    compare one ``input`` across N ``models`` and report cost / latency
    / (optional) quality per model.

    Execution is async: the endpoint creates a ``JobRecord(kind=BENCH)``
    and returns immediately with ``{job_id, bench_id, status: "queued"}``.
    The worker picks it up and persists the ``BenchRecord``; poll
    ``GET /api/v1/jobs/{job_id}`` for completion and
    ``GET /api/v1/bench/{result_run_id}`` for the comparison.
    """

    model_config = ConfigDict(extra="forbid")

    models: list[str] = Field(..., min_length=1)
    """Providers to compare. At least one required."""
    input: dict[str, Any] = Field(...)
    """The single input payload run through every model (same shape as
    the agent's input schema)."""
    judge: str | None = Field(None)
    """Optional judge provider for quality scoring. Scoring is enabled
    only when both ``judge`` and ``rubric`` are supplied."""
    rubric: str | None = Field(None)
    """Optional inline scoring rubric (required to enable LLM-as-judge)."""
    runs: int = Field(1, ge=1, le=10)
    """Runs per model. Use 3+ to smooth latency/cost variance."""
    gate_mode: str = Field("mean")
    """Score aggregation across N runs per model: ``mean`` | ``min`` | ``p10``."""
    mock: bool = Field(False)
    """Use the deterministic MockProvider (no API keys, fast)."""


class BenchAcceptedView(BaseModel):
    """``POST /api/v1/bench/{agent}`` response.

    Returns immediately with ``status="queued"``, the queue ``job_id``,
    and the ``bench_id`` that the produced :class:`BenchRecord` will
    carry. Poll ``GET /api/v1/jobs/{job_id}`` until terminal, then fetch
    the comparison from ``GET /api/v1/bench/{bench_id}``.
    """

    model_config = ConfigDict(extra="forbid")

    bench_id: str = ""
    """The id the produced BenchRecord will carry (pre-generated so the
    caller can fetch it once the job completes)."""
    job_id: str = ""
    """The queue entry's id; poll ``GET /api/v1/jobs/{job_id}``."""
    status: str = "queued"


class BenchModelView(BaseModel):
    """One per-model row in a bench result. Matches the shape
    ``mdk bench --output json`` emits per model."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    score: float | None = None
    """Aggregated quality score (0.0-1.0), or ``None`` if unscored."""
    judge_skipped: bool = False
    cost_mean_usd: float
    cost_total_usd: float
    latency_p50_ms: int
    latency_p95_ms: int
    error_count: int
    sample_output: dict[str, Any] | None = None


class BenchResultView(BaseModel):
    """``GET /api/v1/bench/{bench_id}`` response.

    The complete BenchRecord rendered as JSON + per-model rows. Mirrors
    what ``mdk bench`` prints to terminal but as structured JSON for the
    Angular UI to render the comparison table.
    """

    model_config = ConfigDict(extra="forbid")

    bench_id: str
    agent: str
    agent_version: str
    input: dict[str, Any]
    judge_method: str | None
    judge_provider: str | None
    runs_per_model: int
    gate_mode: str
    models: list[BenchModelView]
    created_at: str
    """ISO-8601 timestamp."""


class BenchListView(BaseModel):
    """``GET /api/v1/bench?agent={name}`` response.

    Paginated history of past bench runs for an agent.
    """

    model_config = ConfigDict(extra="forbid")

    bench: list[BenchResultView]
    count: int


class WizardAgentSubmission(BaseModel):
    """``POST /api/v1/agents/from-wizard`` request body.

    Field set matches Deva's Mova iO "Onboard Agent" wizard (Basic
    Details step). The endpoint translates this into MDK's canonical
    agent.yaml + prompt.md + default schemas, then delegates to the
    same ``persist_bundle()`` the multipart endpoint uses. Returns the
    same ``AgentCreatedView`` so the Angular client doesn't branch
    based on submission mode.

    Why a separate endpoint vs. extending POST /agents: the multipart
    POST is canonical-layout-strict (every byte the operator sends
    lands on disk as-is). The wizard adapter is permissive — it
    generates defaults for fields the wizard doesn't collect (I/O
    schemas) and maps wizard-specific fields onto MDK extensions
    (provider / type / foundation become tag prefixes). Keeping
    them separate means a future wizard-shape change doesn't churn
    the canonical contract.

    Field mapping (wizard → agent.yaml):

    * ``name`` → ``name``
    * ``agent_provider`` (e.g. "Movate") → ``tags: ["provider-movate"]``
    * ``agent_type`` (e.g. "Task Agent") → ``tags: ["type-task-agent"]``
    * ``role`` (dropdown: "Planner" / "Assistant" / ...) → ``role``
      (marketplace metadata, item 29). Lowercased.
    * ``description`` → ``description``
    * ``agent_role`` (free-form textarea) → ``persona`` (item 29 —
      voice / tone, one sentence). Capped at 512 chars to match the
      AgentSpec validator.
    * ``agent_goal`` → ``goals: [<single-element-list>]``
    * ``agent_prompt`` → inlined into ``prompt.md``
    * ``reference_output`` → ``examples: [{output: ...}]``
    * ``mcp_connectors`` (list of names) → ``skills: [...]``
    * ``knowledge_store`` → ``contexts: [...]``
    * ``ai_model`` → ``model.provider``
    * ``ai_foundation`` (e.g. "Azure") → ``tags: ["foundation-azure"]``
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128)
    """Agent name. Same regex as the canonical AgentSpec (lowercase
    alphanumeric + hyphens); we slugify common UI inputs so a wizard
    name like ``"Code Analyzer"`` survives the round-trip."""

    agent_provider: str = Field(default="", max_length=64)
    """Dropdown value (e.g. ``"Movate"``). Slugified onto a
    ``provider-<slug>`` tag."""
    agent_type: str = Field(default="", max_length=64)
    """Dropdown value (e.g. ``"Task Agent"``). Slugified onto a
    ``type-<slug>`` tag."""
    role: str = Field(default="", max_length=64)
    """Dropdown value (e.g. ``"Planner"``). Lowercased into the
    marketplace ``role`` field (item 29)."""

    description: str = Field(default="")
    agent_role: str = Field(default="", max_length=512)
    """Free-form textarea — voice / persona description.
    Maps to AgentSpec.persona (item 29)."""

    agent_goal: str = Field(default="")
    """Single goal string from the textarea. Becomes a single-element
    ``goals`` list in agent.yaml."""

    agent_prompt: str = Field(..., min_length=1)
    """The actual prompt template the wizard collects. Inlined into
    ``prompt.md`` at persist time."""

    reference_output: str = Field(default="")
    """Optional reference output — if present, becomes a single
    ``examples`` entry with ``output: <text>``."""

    mcp_connectors: list[str] = Field(default_factory=list)
    """MCP connector names from the wizard's multi-select. Mapped
    directly to AgentSpec.skills (each entry must already exist in
    the project's skills/ registry — wizard surfacing of available
    skills is the Mova iO BFF's job)."""

    knowledge_store: list[str] = Field(default_factory=list)
    """Knowledge-store names. Mapped to AgentSpec.contexts (each
    entry must exist in the project's contexts/ folder)."""

    ai_model: str = Field(..., min_length=1)
    """LiteLLM-style provider string. Wizard's "Type AI Model" text
    field; UI is responsible for the right format
    (``openai/gpt-4o-mini-2024-07-18``, etc.)."""

    ai_foundation: str = Field(default="", max_length=64)
    """Cloud / foundation tag (e.g. ``"Azure"``). Slugified onto a
    ``foundation-<slug>`` tag."""


class AgentPublishSubmission(BaseModel):
    """``POST /api/v1/agents/{name}/publish`` request body.

    Pushes the agent's canonical bundle to the per-tenant GitHub repo
    as a single commit on the configured default branch. See ADR 007
    decisions 1-4 for repo strategy, auth, cadence, and push semantics.

    All fields optional — defaults are applied at the integration
    layer (``GitHubConfig.commit_author_*``). The Angular UI's
    "Publish" dialog typically supplies a custom ``commit_message``
    summarizing what changed; the author defaults match the GitHub
    App's bot identity so unattributed publishes still have a sensible
    commit footer."""

    model_config = ConfigDict(extra="forbid")

    commit_message: str | None = None
    """Free-form commit message. Defaults to ``Update <agent-name>``
    when omitted. Conventional-commits format is welcome but not
    enforced (ADR 007 open question 4)."""

    author_name: str | None = None
    """Display name for the commit author. Defaults to the runtime's
    ``commit_author_name`` config (``Mova iO`` out of the box)."""

    author_email: str | None = None
    """Email for the commit author. Defaults to the runtime's
    ``commit_author_email`` config."""


class AgentCommitView(BaseModel):
    """One row in the agent's commit history (item 79).

    Flat wire shape matching :class:`movate.integrations.github.CommitInfo`.
    The Mova iO UI's version-history panel renders one card per entry,
    typically with ``message`` as the heading, ``author_name``+``timestamp``
    underneath, and the SHA + html_url as a "View on GitHub" link."""

    model_config = ConfigDict(extra="forbid")

    sha: str
    """Full 40-char Git SHA. Use this for the next call's ``?since=<sha>``
    cursor when only-newer-than fetching ships in v0.8."""

    message: str
    """Commit message body. May span multiple lines — the Angular
    client typically renders only the first line in the list view
    and expands on click."""

    author_name: str
    """Display name from the commit's author block. Empty string when
    GitHub returns an anonymous commit (rare; mostly happens for
    machine-generated mirror commits)."""

    author_email: str

    timestamp: str
    """ISO-8601 UTC timestamp from GitHub. The Angular client can pass
    this directly to ``new Date(...)`` without timezone-conversion
    work — GitHub always emits UTC."""

    html_url: str
    """``https://github.com/<repo>/commit/<sha>`` — same URL shape as
    :class:`AgentPublishedView.commit_url`. Surface as 'View on GitHub'."""


class AgentHistoryView(BaseModel):
    """``GET /api/v1/agents/{name}/history`` response.

    Paginated wrapper around :class:`AgentCommitView` rows. The UI
    fetches page 1 by default; pages 2+ via ``?page=N``. ``has_more``
    is a heuristic — true when the runtime got back a full page,
    suggesting the next page MAY have more rows. False guarantees
    no more rows."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    """Echoes the URL path parameter so callers can correlate the
    response without re-parsing the path. Same convention as
    :class:`AgentPublishedView`."""

    commits: list[AgentCommitView]
    """Sorted by GitHub's default (newest first — most recent commit
    at index 0). Empty list when the agent has no published commits
    yet (created via wizard but never published) OR when ``page`` is
    past the last page."""

    page: int
    """1-indexed page number. Echoes the request's ``?page=N``."""

    limit: int
    """Page size. Echoes the request's ``?limit=N`` (clamped to
    GitHub's max of 100 at the integration layer)."""

    has_more: bool
    """Heuristic: true iff ``len(commits) == limit``. The Angular
    client uses this to decide whether to show a "Load more" button."""


class AgentPublishedView(BaseModel):
    """``POST /api/v1/agents/{name}/publish`` response.

    Returned on a successful publish. ``commit_sha`` + ``commit_url``
    are what the Angular UI shows in the "Published" toast + history
    list. ``files_changed`` is the per-publish set of repo-relative
    paths the runtime wrote — handy for a "files in this commit"
    panel without a second GitHub API call."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    """Name of the agent that was published. Echoes the URL path
    parameter so callers can correlate without re-parsing the path."""

    commit_sha: str
    """Full 40-char Git SHA of the new commit on the default branch."""

    commit_url: str
    """``https://github.com/<repo>/commit/<sha>`` — direct link the UI
    surfaces as 'View on GitHub'."""

    branch: str
    """Branch the commit landed on. ``main`` in v0.7 (ADR 007
    decision 4 reserves branch routing for the protected-paths
    flow shipping in v0.8)."""

    files_changed: list[str]
    """Repo-relative paths included in this commit (sorted). Includes
    the ``<agent-name>/`` prefix that lives under the tenant repo
    root, e.g. ``faq-bot/agent.yaml``."""


class SkillCreatedView(BaseModel):
    """``POST /api/v1/skills`` response — what landed under
    ``<skills_path>/<name>/`` after a successful upload.

    The Angular UI doesn't currently render a skills profile, but
    deploy tooling (and operators running curl) need a confirmation
    payload with the resolved name so a subsequent agent upload that
    references the skill can be done with confidence the resolution
    will succeed.

    Mirror of :class:`AgentCreatedView` for the skill resource.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    description: str = ""
    skill_dir: str
    """Path-relative-to-skills-root where the bundle landed.
    E.g. ``web-search``."""
    files_persisted: list[str]
    """Sorted list of files written, relative to ``skill_dir``.
    E.g. ``["impl.py", "skill.yaml"]``."""


class AgentDeletedView(BaseModel):
    """``DELETE /api/v1/agents/{name}`` response.

    Soft-delete result. The agent's bundle has been moved to a
    sibling ``.deleted-<name>-<timestamp>/`` directory under the
    runtime's agents_path — recoverable out-of-band by the operator
    until a future cron sweep removes it (7-day window TBD).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    deleted_dir: str
    """Path-relative-to-agents-root where the bundle now lives.
    E.g. ``.deleted-faq-bot-1747178400``. Operators looking to
    restore can ``mv`` it back to the original name."""


class AgentDatasetUploadView(BaseModel):
    """``POST /api/v1/agents/{name}/dataset`` response.

    Returned after a successful dataset upload. Lets callers verify
    the upload landed correctly before kicking off an eval.
    """

    model_config = ConfigDict(extra="forbid")

    agent_name: str
    row_count: int
    """Number of JSONL rows accepted."""
    sha256_prefix: str
    """First 12 hex chars of the SHA-256 of the written file — enough
    for a quick integrity spot-check without sending the full hash."""
    preview: list[dict[str, Any]]
    """First up to 3 rows, for a quick sanity-check in the UI."""


class HarvestedCaseView(BaseModel):
    """One *proposed* eval case in a harvest response (ADR 016 D1).

    A superset of an ``evals/dataset.jsonl`` row: ``input`` + optional
    ``expected`` are the eval-case fields; ``needs_review`` + ``provenance``
    are the harvest audit fields a human reads before accepting. This view is
    a *proposal* — the harvest endpoint never writes it to the stored dataset.
    """

    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any]
    """The source run's input — becomes the eval case input verbatim."""
    expected: dict[str, Any] | None = None
    """Suggested reference output. Set ONLY for thumbs-up golden cases (the
    prod output); ``None`` for needs-review cases so a human supplies it."""
    needs_review: bool
    """``True`` when a human must supply / confirm ``expected`` before the
    case is trustworthy — always ``True`` except thumbs-up golden cases."""
    provenance: dict[str, Any]
    """Audit block: ``source_run_id``, the ``source`` signal, the feedback
    score/comment (when any), and the known prod output."""


class HarvestView(BaseModel):
    """``POST /api/v1/agents/{name}/dataset/harvest`` response (ADR 016 D1).

    Returns the proposed cases as JSON. **Does NOT modify the stored
    dataset** — acceptance is a deliberate follow-up call to the existing
    dataset-upload endpoint (``POST /api/v1/agents/{name}/dataset``). The
    human-review gate is the core anti-poisoning safety property.
    """

    model_config = ConfigDict(extra="forbid")

    agent_name: str
    source: str
    """The selection signal used: thumbs-down | thumbs-up | low-score | sample."""
    proposed_count: int
    """Number of proposed cases returned (== ``len(cases)``)."""
    needs_review_count: int
    """How many proposed cases need a human to supply / confirm ``expected``."""
    runs_considered: int
    """How many candidate runs the selection looked at."""
    applied: bool = False
    """Always ``False`` — a harvest proposes, it never applies. Present so the
    contract makes the proposed-not-applied guarantee explicit to clients."""
    cases: list[HarvestedCaseView]
    """The proposed cases. Review, then POST the accepted subset to the
    dataset-upload endpoint to land them in ``evals/dataset.jsonl``."""


# ---------------------------------------------------------------------------
# KB upload wire types
# ---------------------------------------------------------------------------


class KbIngestFileResult(BaseModel):
    """Per-file outcome from ``POST /api/v1/agents/{name}/kb``.

    Mirrors :class:`movate.kb.ingest.IngestSummary` for the wire,
    plus a status field so the caller can tell which files were
    accepted vs. skipped (empty / unsupported extension).
    """

    model_config = ConfigDict(extra="forbid")

    source: str
    """The uploaded filename (basename). Empty string for inline text."""

    status: str
    """One of ``"ingested"`` (chunks saved), ``"empty"`` (file had no
    extractable text), ``"skipped"`` (unsupported extension). The
    endpoint returns 200 with a mix of statuses rather than 400'ing
    a multi-file upload on a single bad file."""

    chunks_total: int = 0
    """Total chunks produced by the splitter. 0 for empty/skipped."""

    chunks_saved: int = 0
    """Chunks persisted to storage. Equal to ``chunks_total`` in v0.9
    since the storage layer always upserts."""

    embedding_model: str | None = None
    """The full ``provider/model`` identifier used. ``None`` when the
    file was empty/skipped (no embedding call made)."""


class KbIngestView(BaseModel):
    """``POST /api/v1/agents/{name}/kb`` response.

    Aggregate plus per-file detail. Total counts make the success
    summary trivial to render in the playground UI; per-file detail
    lets the operator see exactly which uploads contributed.
    """

    model_config = ConfigDict(extra="forbid")

    agent_name: str
    files: list[KbIngestFileResult]
    total_chunks_saved: int
    """Sum of chunks_saved across all files — convenience for the UI."""


# ---------------------------------------------------------------------------
# KB remote-management wire types (Task 4) — list / stats / delete / search.
# These power the ``--target`` paths on ``mdk kb list/stats/search/clear`` so
# operators can manage a deployed agent's KB without SSH-ing to the host.
# The embedding vectors are deliberately omitted from the wire shapes — they
# bloat payloads (1536 floats/chunk) and no remote consumer needs them.
# ---------------------------------------------------------------------------


class KbChunkView(BaseModel):
    """A single KB chunk's metadata for ``GET /api/v1/agents/{name}/kb``.

    Mirrors the load-bearing fields of :class:`movate.core.models.KbChunk`
    MINUS the ``embedding`` vector — list payloads are for inspection
    ("is my content actually in there?"), not retrieval, so shipping
    1536 floats per chunk is pure waste. Callers needing vectors run a
    search instead.
    """

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    source: str
    text: str
    embedding_model: str
    content_hash: str
    ocr: bool = False
    metadata: dict[str, Any] | None = None
    created_at: str
    """ISO-8601 timestamp, serialised on the wire as a string."""


class KbListView(BaseModel):
    """``GET /api/v1/agents/{name}/kb`` response — chunk metadata list."""

    model_config = ConfigDict(extra="forbid")

    agent_name: str
    chunks: list[KbChunkView]
    count: int
    """Number of chunks returned (post ``?limit=`` / ``?source=`` filter)."""


class KbStatsSourceView(BaseModel):
    """Per-source aggregate row for ``GET /api/v1/agents/{name}/kb/stats``."""

    model_config = ConfigDict(extra="forbid")

    source: str
    chunks: int
    chars: int


class KbStatsView(BaseModel):
    """``GET /api/v1/agents/{name}/kb/stats`` response.

    Aggregated SERVER-SIDE so the runtime never ships the whole corpus
    over the wire just to count it — the CLI / Angular console render
    this directly. ``models`` carries every distinct ``embedding_model``
    present (usually one; more than one means a mixed-model KB that
    needs a re-embed).
    """

    model_config = ConfigDict(extra="forbid")

    agent_name: str
    total_chunks: int
    total_chars: int
    ocr_chunks: int
    sources: list[KbStatsSourceView]
    models: list[str]


class KbDeletedView(BaseModel):
    """``DELETE /api/v1/agents/{name}/kb`` response — count removed."""

    model_config = ConfigDict(extra="forbid")

    agent_name: str
    deleted: int
    source: str | None = None
    """Echoes the ``?source=`` filter when one was supplied; ``None``
    means a full-KB wipe."""


class KbSearchSubmission(BaseModel):
    """``POST /api/v1/agents/{name}/kb/search`` request body."""

    model_config = ConfigDict(extra="forbid")

    question: str
    k: int = Field(default=5, ge=1, le=50)
    hybrid: bool = False
    """Combine vector + BM25 lexical search via reciprocal rank fusion —
    same flag as ``mdk kb search --hybrid``."""


class KbSearchResultView(BaseModel):
    """One scored chunk in a ``kb/search`` response. Like
    :class:`KbChunkView` plus the similarity ``score``; the embedding
    vector is omitted for the same payload-size reason."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    source: str
    text: str
    embedding_model: str
    score: float
    ocr: bool = False
    metadata: dict[str, Any] | None = None


class KbSearchView(BaseModel):
    """``POST /api/v1/agents/{name}/kb/search`` response — scored hits."""

    model_config = ConfigDict(extra="forbid")

    agent_name: str
    question: str
    results: list[KbSearchResultView]
    count: int


class KbReindexSubmission(BaseModel):
    """``POST /api/v1/agents/{name}/kb/reindex`` request body.

    ``reembed=false`` (the default) rebuilds the vector index from the
    chunks already in storage — no embedding calls, no API key. Set
    ``reembed=true`` to first re-run the embedding model over every
    stored chunk's text (overwriting each vector) and THEN rebuild the
    index — the expensive path, required only when the embedding
    model / dimension changes.
    """

    model_config = ConfigDict(extra="forbid")

    reembed: bool = False
    """When true, re-embed every stored chunk before rebuilding the
    index (costs money + needs an embedding key on the runtime)."""


class KbReindexView(BaseModel):
    """``POST /api/v1/agents/{name}/kb/reindex`` response — what was done."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    reembed: bool
    chunks_reembedded: int
    """How many chunks had their vector re-computed (0 unless
    ``reembed`` was set)."""

    index_rebuilt: bool
    """True when the backend rebuilt a real vector index; False for
    brute-force backends (sqlite / in-memory) that have none."""

    backend: str
    """The storage backend name (``postgres`` / ``sqlite`` /
    ``memory``) — clarifies whether a real index rebuild happened."""


# ---------------------------------------------------------------------------
# Auth key management wire types
# ---------------------------------------------------------------------------


class ApiKeyMintRequest(BaseModel):
    """``POST /api/v1/auth/keys`` request body."""

    model_config = ConfigDict(extra="forbid")

    label: str | None = None
    """Optional human-readable note (e.g. ``"ci-bot"``)."""
    ttl_days: int = 90
    """Validity in days. 0 = no expiry (service-account use)."""
    scopes: list[str] | None = None
    """Least-privilege scope grant (ADR 013 L2). Drawn from the flat set
    ``read``, ``run``, ``eval``, ``kb:write``, ``admin``, ``fleet-admin``.
    ``None``/omitted mints a key with the legacy default
    ``{read, run, eval}``. An unknown scope string is rejected with 400."""


class ApiKeyMintedView(BaseModel):
    """``POST /api/v1/auth/keys`` response.

    ``full_key`` is shown **once** — it is irrecoverable after this
    response. Callers must store it immediately.
    """

    model_config = ConfigDict(extra="forbid")

    key_id: str
    full_key: str
    tenant_id: str
    env: str
    label: str | None
    expires_at: datetime | None


class ApiKeyView(BaseModel):
    """One row in ``GET /api/v1/auth/keys`` — no plaintext secret."""

    model_config = ConfigDict(extra="forbid")

    key_id: str
    tenant_id: str
    env: str
    label: str | None
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    status: str
    """``active`` | ``revoked`` | ``expired``"""


class ApiKeyListView(BaseModel):
    """``GET /api/v1/auth/keys`` response."""

    model_config = ConfigDict(extra="forbid")

    keys: list[ApiKeyView]
    count: int


class ApiKeyRevokedView(BaseModel):
    """``DELETE /api/v1/auth/keys/{key_id}`` response."""

    model_config = ConfigDict(extra="forbid")

    key_id: str
    revoked: bool = True


class ApiKeyRotateRequest(BaseModel):
    """``POST /api/v1/auth/keys/{key_id}/rotate`` request body (ADR 013 D5)."""

    model_config = ConfigDict(extra="forbid")

    grace_seconds: int | None = None
    """How long the OLD key stays valid after rotation (the grace window).
    ``None``/omitted → the server default (24h). Clamped server-side to
    ``[0, 30d]``: ``0`` is an immediate cutover, the cap bounds how long a
    rotated-away key lingers."""
    ttl_days: int | None = None
    """Validity of the NEW (successor) key in days. ``None``/omitted →
    the server default (90). ``0`` = non-expiring successor."""


class ApiKeyRotatedView(BaseModel):
    """``POST /api/v1/auth/keys/{key_id}/rotate`` response (ADR 013 D5).

    ``full_key`` is the successor and is shown **once** — irrecoverable
    after this response. Both keys authenticate until ``old_expires_at``
    passes (zero downtime).
    """

    model_config = ConfigDict(extra="forbid")

    key_id: str
    """The NEW (successor) key's id."""
    full_key: str
    """The NEW key's full secret — shown once."""
    tenant_id: str
    env: str
    label: str | None
    expires_at: datetime | None
    """The NEW key's expiry."""
    old_key_id: str
    """The rotated (old) key's id."""
    old_expires_at: datetime
    """When the OLD key stops authenticating (now + grace)."""


class ApiKeyBulkRevokedView(BaseModel):
    """``POST /api/v1/auth/keys/revoke-all`` response (ADR 013 D5)."""

    model_config = ConfigDict(extra="forbid")

    revoked_count: int
    """How many active keys were revoked."""
    spared_key_id: str | None = None
    """The key (if any) deliberately spared from the bulk revoke — the
    caller's own key by default, so the operator isn't locked out."""


class AuthWhoamiView(BaseModel):
    """``GET /api/v1/auth/me`` response — identity of the calling key."""

    model_config = ConfigDict(extra="forbid")

    key_id: str
    tenant_id: str
    env: str
    scope: str | None
    """**Legacy** single-scope field: ``"fleet-admin"`` when that scope is
    present, else ``None``. Kept for back-compat; prefer :attr:`scopes`."""
    scopes: list[str] = Field(default_factory=list)
    """Resolved least-privilege scopes for the calling identity (ADR 013
    L2), sorted. A scopeless legacy key reports the default
    ``["eval", "read", "run"]``."""
    label: str | None
    expires_at: datetime | None


class AgentRunSubmission(BaseModel):
    """``POST /api/v1/agents/{name}/runs`` request body.

    Agent-scoped run (REST-clean: the resource being created is a
    *run* under the *agent* parent). Body just carries the input
    payload — the agent name lives in the URL, ``kind=AGENT`` is
    implicit, no target field needed.

    Same shape as :class:`RunSubmission` minus ``kind`` and
    ``target`` (URL-anchored).
    """

    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any]
    notify_email: str | None = Field(
        default=None,
        description=(
            "Optional email address. If set, the worker emails this "
            "address when the run reaches a terminal status."
        ),
    )
    mock: bool = Field(
        default=False,
        description=(
            "Only meaningful with ``?wait=true`` (inline mode). When "
            "true, runs the agent against the deterministic MockProvider "
            "instead of LiteLLM — no API keys needed, sub-second output. "
            "Default false uses the agent's declared model. Ignored in "
            "async/worker mode (the worker has its own provider "
            "configuration)."
        ),
    )
    thread_id: str | None = Field(
        default=None,
        description=(
            "Optional conversation/session id. Additive + back-compat "
            "(omitting it is unchanged behavior). When a canary (ADR 016 "
            "D3) with sticky routing is configured for this agent, this is "
            "the key that keeps every turn of one conversation on the same "
            "champion/challenger side. Ignored when no sticky canary is in "
            "play."
        ),
    )


class BatchInlineSubmission(BaseModel):
    """Inline JSON body for ``POST /api/v1/agents/{name}/batch`` (item 17).

    The programmatic alternative to a JSONL ``UploadFile`` — a caller that
    already has the dataset in memory POSTs ``{"inputs": [ {...}, ... ]}``
    instead of streaming a file. Each element of ``inputs`` becomes ONE
    ordinary ``JobKind.AGENT`` job's input, exactly as one JSONL line would.
    """

    model_config = ConfigDict(extra="forbid")

    inputs: list[dict[str, Any]] = Field(
        ...,
        description=(
            "Dataset rows — one run's input per element. Each becomes one "
            "queued AGENT job carrying the batch's shared ``batch_id``."
        ),
    )
    notify_email: str | None = Field(
        default=None,
        description=(
            "Optional email address propagated onto EVERY child job, so the "
            "worker emails this address as each row reaches a terminal status."
        ),
    )


class BatchAcceptedView(BaseModel):
    """``POST /api/v1/agents/{name}/batch`` response — 202 Accepted.

    The caller polls ``GET /api/v1/batches/{batch_id}`` for the per-status
    aggregate, exactly as a single run polls ``GET /jobs/{id}``.
    """

    model_config = ConfigDict(extra="forbid")

    batch_id: str
    total: int
    """Number of rows enqueued = number of child AGENT jobs created."""
    status: str = "queued"
    """Always ``"queued"`` from this endpoint — every child job starts QUEUED.
    A literal field (not :class:`JobStatus`) because a batch's overall state
    has its own small vocabulary (queued / running / complete), distinct from a
    single job's lifecycle."""


class BatchStatusCounts(BaseModel):
    """Per-status counts over a batch's child jobs.

    One field per :class:`JobStatus` member so the wire shape is stable and
    self-documenting — a client doesn't have to know the enum to render the
    progress bar.
    """

    model_config = ConfigDict(extra="forbid")

    queued: int = 0
    running: int = 0
    success: int = 0
    error: int = 0
    safety_blocked: int = 0
    dead_letter: int = 0


class BatchStatusView(BaseModel):
    """``GET /api/v1/batches/{batch_id}`` response — the aggregate.

    ``counts`` breaks the children down per status; ``state`` is the derived
    overall state: ``running`` if ANY child is still non-terminal (QUEUED or
    RUNNING), else ``complete``. ``job_ids`` lists the child job ids so a
    caller can drill into any one via ``GET /jobs/{id}`` / ``GET /runs/{id}``.
    """

    model_config = ConfigDict(extra="forbid")

    batch_id: str
    agent: str
    total: int
    """The row count recorded at submit time (from the BatchRecord)."""
    counts: BatchStatusCounts
    state: str
    """Derived overall state: ``"running"`` while any child is non-terminal
    (QUEUED / RUNNING), otherwise ``"complete"``. A literal, not
    :class:`JobStatus` — a batch is not itself a job."""
    created_at: datetime
    job_ids: list[str] = Field(default_factory=list)
    """The child job ids, newest-first. Drill in via ``GET /jobs/{id}``."""


class BatchListItemView(BaseModel):
    """One row in the ``GET /api/v1/batches`` list — parent metadata only.

    Deliberately omits the per-status aggregate (which requires fetching every
    child) so the list view stays cheap — clients fetch
    ``GET /api/v1/batches/{id}`` for a specific batch's progress.
    """

    model_config = ConfigDict(extra="forbid")

    batch_id: str
    agent: str
    total: int
    created_at: datetime


class BatchListView(BaseModel):
    """``GET /api/v1/batches`` response — envelope around a page of batches."""

    model_config = ConfigDict(extra="forbid")

    batches: list[BatchListItemView]
    count: int


class AgentValidationIssue(BaseModel):
    """One finding from ``POST /api/v1/agents/{name}/validate``.

    Mirrors :class:`movate.core.prompt_linter.LintIssue` but flat for
    wire-friendliness. The Angular UI groups by severity and renders
    a chip per issue.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    """Stable enum code (e.g. ``UNDECLARED_INPUT_REF``). The Angular
    UI can branch on this for special-case rendering or suppress
    via a project-level allow-list."""
    severity: str
    """One of ``"error"``, ``"warning"`` — matches
    :class:`movate.core.prompt_linter.Severity`."""
    message: str
    """Human-readable explanation. May change wording between
    releases; codes are the stable contract."""
    hint: str = ""
    """Optional fix pointer (e.g. "did you mean `input.text`?")."""


class AgentValidationCostForecast(BaseModel):
    """Cost forecast for an eval run, surfaced by the validate
    endpoint. Lets the Angular UI render a "running this eval will
    cost ~$X" chip BEFORE the user clicks the Run Eval button.

    ``None`` (omitted at the parent level) when the agent has no
    dataset or its pricing entry is missing.
    """

    model_config = ConfigDict(extra="forbid")

    model_provider: str
    cases: int
    input_tokens_per_call: int
    output_tokens_per_call: int
    cost_per_call_usd: float
    total_cost_usd: float


class AgentValidationView(BaseModel):
    """``POST /api/v1/agents/{name}/validate`` response.

    Drives the Mova iO Angular "is this agent shippable?" gate. The
    UI uses ``errors``  to block save (red chips); ``warnings`` show
    as yellow chips but don't block. ``cost_forecast`` is the
    pricing-table estimate the UI displays alongside the Run Eval
    button.

    ``passed`` is the boolean shortcut — true when there are zero
    errors. The Angular UI uses this for the green checkmark badge
    on the agent card.
    """

    model_config = ConfigDict(extra="forbid")

    passed: bool
    """``True`` when zero errors. Warnings don't affect this — they're
    informational. UI shows a green check when ``passed``."""
    errors: list[AgentValidationIssue]
    warnings: list[AgentValidationIssue]
    cost_forecast: AgentValidationCostForecast | None = None


class AgentDatasetInfo(BaseModel):
    """Dataset metadata (size + sample row count + digest) for the
    agent-detail view. Excludes row contents — the Angular UI shows
    "150 cases" but doesn't render the full dataset inline.
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    """Path relative to the agent dir, e.g. ``evals/dataset.jsonl``."""
    case_count: int
    """Non-empty lines in the JSONL — what ``mdk eval`` would walk."""
    sha256_prefix: str
    """First 12 chars of the dataset's SHA-256, for change detection."""
    size_bytes: int


class AgentDetailView(BaseModel):
    """``GET /api/v1/agents/{name}`` response — everything the Angular
    agent-profile view renders, in one round-trip.

    Mirrors what ``mdk show <agent>`` prints, but as structured JSON
    for the Angular UI to consume. Includes:

    * Spec metadata (name, version, description, owner, marketplace
      fields from item 29)
    * Model config (provider + params + fallback chain)
    * Prompt body + content-addressed hash (so the UI can show a
      "prompt changed" badge when re-fetching)
    * Resolved I/O schemas (the dicts MDK would feed to its validator
      — the UI renders these as collapsible JSON blocks)
    * Skills / contexts metadata
    * Dataset stats (if present)
    * The full canonical bundle's relative paths so the UI can show
      "files in this agent"

    NOT included (deferred to follow-up endpoints):

    * Recent eval scores — that's ``GET /api/v1/evals?agent={name}``
      (item 62)
    * Run history — that's ``GET /api/v1/jobs?agent={name}`` (item 74)
    * Trace replay — that's ``GET /api/v1/runs/{run_id}/trace``
      (item 65)
    """

    model_config = ConfigDict(extra="forbid")

    # --- Identity + metadata ---
    name: str
    version: str
    description: str = ""
    owner: str = ""

    # Marketplace metadata (item 29, Group F). Always present (empty
    # strings / empty list for agents that haven't opted in).
    role: str = ""
    persona: str = ""
    capabilities: list[str] = []
    tags: list[str] = []

    # --- Model config ---
    model_provider: str
    """LiteLLM-style provider string, e.g. ``"openai/gpt-4o-mini-2024-07-18"``."""
    model_params: dict[str, Any] = {}
    """Optional temperature / max_tokens / etc."""
    model_fallback: list[str] = []
    """Ordered fallback provider strings; empty for single-model agents."""
    runtime: str
    """Which AgentRuntime adapter the agent targets: litellm,
    native_anthropic, native_openai, langchain, lyzr."""

    # --- Prompt + schemas (rendered for the UI) ---
    prompt: str
    """The prompt template body — rendered as-is (no Jinja
    substitution). The UI shows this in a code editor."""
    prompt_hash: str
    """SHA-256 of the prompt body. UI uses this to detect changes
    between fetches and show a "prompt changed" badge."""
    input_schema: dict[str, Any]
    """Resolved input JSON schema (inline or loaded from
    schema/input.json). UI renders as a collapsible JSON block."""
    output_schema: dict[str, Any]
    """Resolved output JSON schema."""

    # --- Skills + contexts (item 29 / ADR 002) ---
    skills: list[str] = []
    """Names referencing this project's skills/ registry. Empty list
    = single-shot agent (no tool-use loop)."""
    contexts: list[str] = []
    """Names referencing this project's contexts/ folder. Empty list
    = no shared context prepended."""

    # --- Eval dataset stats ---
    dataset: AgentDatasetInfo | None = None
    """Dataset metadata if ``evals/dataset.jsonl`` exists. ``None``
    means the agent has no dataset yet — the UI shows "no eval set
    configured" and disables the "Run Eval" button."""

    # --- Operational budgets / timeouts ---
    timeout_call_ms: int
    timeout_total_ms: int
    max_cost_usd_per_run: float

    # --- Canonical layout (mirrors AgentCreatedView.files_persisted) ---
    agent_dir: str
    """Path-relative-to-agents-root. Matches what POST returned."""
    files: list[str]
    """Sorted list of files in the canonical layout that exist on
    disk for this agent. UI uses this to render "files in this
    agent" + "View on GitHub" links per file."""


# ---------------------------------------------------------------------------
# Model catalog + pricing wire types (BACKLOG #67 / #68)
#
# Read-only mirrors of the ``mdk models`` / ``mdk pricing`` CLI surfaces over
# HTTP. The underlying catalogue (pricing + capabilities) is the shared
# :mod:`movate.providers.model_catalog` module — same source of truth the CLI
# uses, so the API and the CLI never drift. These views are static (no storage
# / tenant scoping) but the endpoints still require auth for consistency.
# ---------------------------------------------------------------------------


class ModelInfoView(BaseModel):
    """One model in ``GET /api/v1/models`` / ``GET /api/v1/models/{id}``.

    Combined pricing (per-1M tokens) + capability metadata. Mirrors the
    shape ``mdk models show -o json`` emits, field-for-field, so a client
    can switch between the CLI and the API without reshaping.
    """

    model_config = ConfigDict(extra="forbid")

    model_id: str
    provider: str
    context_window: int
    """Maximum context window in tokens. ``0`` when unknown (model not in
    the capability catalogue and no provider default applies)."""
    input_per_1m: float
    """Input price in USD per 1,000,000 tokens."""
    output_per_1m: float
    """Output price in USD per 1,000,000 tokens."""
    cached_input_per_1m: float | None = None
    """Cached-input price per 1M tokens, when the model supports prompt
    caching; ``None`` otherwise."""
    supports_tools: bool
    supports_vision: bool
    notes: str = ""
    in_pricing_table: bool = True
    """Always ``True`` for catalog entries — every model in the catalog is
    sourced from the pricing table. Kept for parity with the CLI JSON shape."""


class ModelCatalogView(BaseModel):
    """``GET /api/v1/models`` response — the full model catalog.

    Sorted by ``(provider, model_id)`` ascending, matching ``mdk models
    list``. ``count`` echoes the number of entries for a quick sanity
    check without re-counting client-side.
    """

    model_config = ConfigDict(extra="forbid")

    models: list[ModelInfoView]
    count: int


class PricingEntryView(BaseModel):
    """One model's raw pricing row in ``GET /api/v1/pricing``.

    Direct serialisation of :class:`movate.providers.pricing.ModelPrice`
    (per-1K-token units, as stored in the packaged ``pricing.yaml``). Use
    ``GET /api/v1/models`` for the per-1M-token + capability view.
    """

    model_config = ConfigDict(extra="forbid")

    model_id: str
    input_per_1k: float
    output_per_1k: float
    cached_input_per_1k: float | None = None


class PricingView(BaseModel):
    """``GET /api/v1/pricing`` response — the versioned pricing table.

    Mirrors :class:`movate.providers.pricing.PricingTable` over the wire:
    the table ``version`` + ``last_verified`` date plus one entry per model.
    Entries are sorted by ``model_id`` for stable output.
    """

    model_config = ConfigDict(extra="forbid")

    version: str
    last_verified: str
    entries: list[PricingEntryView]
    count: int


# ---------------------------------------------------------------------------
# Run explain wire type (BACKLOG #66)
#
# Read-only mirror of ``mdk explain --json``: the decision chain for a
# stored run. The record→dict logic is the shared
# :func:`movate.core.explain.explain_run` seam (reused by the CLI), so the
# API and CLI emit byte-identical chains. Tenant-scoped at the storage layer.
# ---------------------------------------------------------------------------


class RunExplainLlmCallView(BaseModel):
    """The single LLM-call summary inside a run's decision chain."""

    model_config = ConfigDict(extra="forbid")

    model: str
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    latency_ms: int
    cost_usd: float


class RunExplainView(BaseModel):
    """``GET /api/v1/runs/{run_id}/explain`` response — the decision chain.

    Mirrors ``mdk explain <run_id> --json``: identity + status, the input,
    the LLM-call summary, the output (or error), and the per-step
    ``skill_calls``. With ``?steps=true`` the full skill-call breakdown is
    included; otherwise ``skill_calls_hint`` summarises the count.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    agent: str
    agent_version: str
    status: str
    input: dict[str, Any]
    llm_call: RunExplainLlmCallView
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    skill_calls: list[dict[str, Any]] | None = None
    """Full per-step breakdown — populated only when ``?steps=true``."""
    skill_calls_hint: str | None = None
    """One-line summary of the skill-call count — populated when
    ``?steps`` is omitted/false (mutually exclusive with ``skill_calls``)."""


__all__ = [
    "AgentCatalogItemView",
    "AgentCatalogView",
    "AgentCommitView",
    "AgentCreatedView",
    "AgentDatasetInfo",
    "AgentDeletedView",
    "AgentDetailView",
    "AgentHistoryView",
    "AgentListView",
    "AgentPublishSubmission",
    "AgentPublishedView",
    "AgentRevertSubmission",
    "AgentRevertedView",
    "AgentRunSubmission",
    "AgentUpdatedView",
    "AgentValidationCostForecast",
    "AgentValidationIssue",
    "AgentValidationView",
    "AgentVersionView",
    "AgentVersionsView",
    "AgentView",
    "BenchAcceptedView",
    "BenchListView",
    "BenchModelView",
    "BenchResultView",
    "BenchSubmission",
    "EvalAcceptedView",
    "EvalCaseView",
    "EvalListView",
    "EvalScheduleListView",
    "EvalScheduleSubmission",
    "EvalScheduleView",
    "EvalScorecardView",
    "EvalSubmission",
    "FeedbackListView",
    "FeedbackSubmission",
    "FeedbackView",
    "HealthView",
    "JobListView",
    "JobScheduleListView",
    "JobScheduleSubmission",
    "JobScheduleView",
    "JobView",
    "ModelCatalogView",
    "ModelInfoView",
    "PricingEntryView",
    "PricingView",
    "ReadyView",
    "RunAccepted",
    "RunExplainLlmCallView",
    "RunExplainView",
    "RunSubmission",
    "RunTraceView",
    "RunView",
    "SkillCreatedView",
    "TriggerCreateRequest",
    "TriggerCreatedView",
    "TriggerListView",
    "TriggerView",
    "WizardAgentSubmission",
    "WorkflowRunListView",
    "WorkflowRunView",
    "WorkflowSignalRequest",
]


# ---------------------------------------------------------------------------
# Feedback (Chainlit playground — added 2026-05-19)
# ---------------------------------------------------------------------------


class FeedbackSubmission(BaseModel):
    """Payload for ``POST /api/v1/runs/{run_id}/feedback``.

    ``user_id`` is set server-side from the authenticated context when
    auth is on — the client must NOT supply one. If your deployment
    doesn't require auth (rare in production), the client supplies
    ``user_id`` directly. The endpoint enforces this either way.
    """

    model_config = ConfigDict(extra="forbid")

    score: int = Field(
        ...,
        description="Thumbs (-1 or +1) OR star rating (1-5). Other values rejected.",
    )
    dimensions: dict[str, float] | None = Field(
        default=None,
        description="Optional per-dimension scores (e.g. {helpfulness: 0.8}). Each value in [0,1].",
    )
    comment: str | None = Field(
        default=None,
        max_length=4000,
        description="Optional free-text comment.",
    )
    user_id: str | None = Field(
        default=None,
        description="Operator id. Optional — auth context overrides this when present.",
    )

    @field_validator("score")
    @classmethod
    def _score_in_range(cls, v: int) -> int:
        """Mirror FeedbackRecord's validator at the HTTP boundary so
        bad scores surface as 422 (validation error) instead of 500
        (downstream pydantic error during record construction).
        """
        star_min, star_max = 2, 5
        if v in (-1, 1) or star_min <= v <= star_max:
            return v
        raise ValueError(
            f"score must be -1 (thumbs down), 1 (thumbs up), or 1-5 (star rating); got {v}"
        )


class FeedbackView(BaseModel):
    """Response shape after creating / listing feedback. 1:1 with
    :class:`movate.core.models.FeedbackRecord`."""

    model_config = ConfigDict(extra="forbid")

    feedback_id: str
    run_id: str
    tenant_id: str
    agent: str
    user_id: str
    score: int
    dimensions: dict[str, float] | None = None
    comment: str | None = None
    langfuse_score_id: str | None = None
    created_at: datetime

    @classmethod
    def from_record(cls, record: FeedbackRecord) -> FeedbackView:
        return cls(
            feedback_id=record.feedback_id,
            run_id=record.run_id,
            tenant_id=record.tenant_id,
            agent=record.agent,
            user_id=record.user_id,
            score=record.score,
            dimensions=record.dimensions,
            comment=record.comment,
            langfuse_score_id=record.langfuse_score_id,
            created_at=record.created_at,
        )


class FeedbackListView(BaseModel):
    """Multiple feedback rows for a run (or filtered query)."""

    model_config = ConfigDict(extra="forbid")

    feedback: list[FeedbackView]
    count: int


# ---------------------------------------------------------------------------
# Conversation thread wire types (PR-O, Tier 10.5)
# ---------------------------------------------------------------------------


class ThreadCreateSubmission(BaseModel):
    """``POST /api/v1/threads`` body — operator opens a new
    multi-turn conversation with one agent."""

    model_config = ConfigDict(extra="forbid")

    agent: str = Field(
        ...,
        description=(
            "Agent the thread targets. Threads are bound to one agent — "
            "swap to a different agent by opening a new thread."
        ),
    )
    title: str = Field(
        default="",
        max_length=256,
        description=(
            "Optional human-readable label for client display. Empty "
            "string is fine; clients fall back to the first message's "
            "truncated text when rendering."
        ),
    )


class ThreadView(BaseModel):
    """``POST /api/v1/threads`` + ``GET /api/v1/threads/{id}`` response
    envelope. 1:1 with :class:`movate.core.models.ConversationThread`
    plus an optional ``runs`` array (filled by the get-with-history
    endpoint, omitted on bare create/list)."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    tenant_id: str
    agent: str
    title: str
    created_at: datetime
    updated_at: datetime
    runs: list[RunView] | None = Field(
        default=None,
        description=(
            "Chronological run history (earliest turn first). Populated "
            "by GET /api/v1/threads/{id}; omitted on create + list "
            "responses so the operator can fetch just the thread "
            "metadata without paying for the history scan."
        ),
    )

    @classmethod
    def from_record(
        cls,
        record: ConversationThread,
        *,
        runs: list[RunView] | None = None,
    ) -> ThreadView:
        return cls(
            thread_id=record.thread_id,
            tenant_id=record.tenant_id,
            agent=record.agent,
            title=record.title,
            created_at=record.created_at,
            updated_at=record.updated_at,
            runs=runs,
        )


class ThreadListView(BaseModel):
    """``GET /api/v1/threads`` response — paginated thread list for
    a tenant. Threads are returned ``updated_at DESC`` so the active
    conversations float to the top of the operator's view."""

    model_config = ConfigDict(extra="forbid")

    threads: list[ThreadView]
    count: int


class ThreadMessageSubmission(BaseModel):
    """``POST /api/v1/threads/{thread_id}/messages`` body.

    Same shape as the standalone ``/run`` submission's input field —
    the runtime queues a JobRecord with the thread's agent + the
    operator-supplied input. The worker propagates the thread_id
    onto the spawned RunRecord so subsequent
    ``GET /api/v1/threads/{id}`` calls see the new turn in chronological
    order.

    The thread's agent is fixed at thread creation (PR-O); messages
    don't carry an agent override. To target a different agent,
    open a new thread.
    """

    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any] = Field(
        ...,
        description=(
            "The agent's input payload — same shape as a standalone "
            "``POST /run`` submission. Matches the agent's declared "
            "input schema."
        ),
    )
    notify_email: str | None = Field(
        default=None,
        description=(
            "Optional email address to notify when the job terminates. "
            "Same semantics as the standalone /run flow."
        ),
    )


# ---------------------------------------------------------------------------
# Aggregate monitor feed (ADR 032 D2) — the in-product "how are my agents
# doing?" rollup the Mova iO front end renders. Wire mirror of the pure
# ``movate.core.reporting.Report`` dataclass tree (the SAME aggregation
# ``mdk report`` uses), wrapped in typed Pydantic so OpenAPI / the front-end's
# generated client + the contract test stay rich. The runtime never imports
# ``cli``; the shared rollup lives in ``core`` (``cli ⊥ runtime``).
# ---------------------------------------------------------------------------


class LatencyPercentilesView(BaseModel):
    """p50 / p95 / p99 of run latency (ms) over the windowed runs.

    ``None`` on every field when no run in scope carried a recorded
    latency — distinguishes "no signal" from a genuine ``0`` so the
    front end can render "N/A" rather than a misleading instant.
    """

    model_config = ConfigDict(extra="forbid")

    p50: float | None = None
    p95: float | None = None
    p99: float | None = None
    count: int = Field(0, description="How many runs contributed a latency sample.")

    @classmethod
    def from_dataclass(cls, lp: LatencyPercentiles) -> LatencyPercentilesView:
        return cls(p50=lp.p50, p95=lp.p95, p99=lp.p99, count=lp.count)


class ReportTotalsView(BaseModel):
    """Cross-scope headline numbers for the windowed rollup."""

    model_config = ConfigDict(extra="forbid")

    runs: int
    failed_runs: int
    cost_usd: float
    eval_runs: int
    latest_pass_rate: float | None = Field(
        None, description="Pass-rate of the single most recent eval run; null if none."
    )
    latency_ms: LatencyPercentilesView


class AgentMetricsRollupView(BaseModel):
    """Per-agent (or per-workflow) rollup row.

    Pass-rate fields are ``null`` when the agent has no eval runs in the
    window (cost / latency come from agent *runs*, which can exist without
    any eval). Workflows surface here too — a workflow eval is persisted
    under the workflow's name in the same column, so it groups transparently.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    runs: int
    failed_runs: int
    failure_rate: float = Field(..., description="failed_runs / runs (0 when no runs).")
    total_cost_usd: float
    mean_cost_usd: float = Field(..., description="total_cost_usd / runs (0 when no runs).")
    latency_ms: LatencyPercentilesView
    last_run_at: str = Field(
        "", description="ISO-8601 timestamp of the most recent run; '' if none."
    )
    eval_runs: int
    latest_pass_rate: float | None = None
    mean_pass_rate: float | None = None
    latest_eval_at: str = Field(
        "", description="ISO-8601 timestamp of the most recent eval; '' if none."
    )

    @classmethod
    def from_rollup(cls, a: AgentRollup) -> AgentMetricsRollupView:
        return cls(
            name=a.name,
            runs=a.runs,
            failed_runs=a.failed_runs,
            failure_rate=a.failure_rate,
            total_cost_usd=a.total_cost_usd,
            mean_cost_usd=a.mean_cost_usd,
            latency_ms=LatencyPercentilesView.from_dataclass(a.latency),
            last_run_at=a.last_run_at,
            eval_runs=a.eval_runs,
            latest_pass_rate=a.latest_pass_rate,
            mean_pass_rate=a.mean_pass_rate,
            latest_eval_at=a.latest_eval_at,
        )


class FailingCaseView(BaseModel):
    """One recurring failing input clustered across runs (offline failure signal).

    The store keeps eval *summaries* not per-case detail, so the offline
    per-instance failure signal is failing *runs* grouped by a stable input
    key — surfacing the inputs that fail most often.
    """

    model_config = ConfigDict(extra="forbid")

    case: str = Field(..., description="A short, stable rendering of the failing input.")
    failures: int
    agents: list[str]
    last_error: str = ""

    @classmethod
    def from_dataclass(cls, c: FailingCase) -> FailingCaseView:
        return cls(case=c.case, failures=c.failures, agents=list(c.agents), last_error=c.last_error)


class ReportView(BaseModel):
    """``GET /api/v1/report`` response (ADR 032 D2).

    The cross-agent, tenant-scoped monitor feed: pass-rate, cost, latency
    percentiles, top failing cases, and a per-agent/workflow rollup over the
    requested time window. Identical aggregation to ``mdk report`` — the front
    end renders this directly (no external infra; complements Grafana / Azure /
    Langfuse, ADR 031).
    """

    model_config = ConfigDict(extra="forbid")

    agent_filter: str | None = Field(
        None, description="Set only on the per-agent endpoint; null for the cross-agent feed."
    )
    window_days: int = Field(0, description="Time window in days; 0 = all-time.")
    totals: ReportTotalsView
    agents: list[AgentMetricsRollupView]
    top_failing_cases: list[FailingCaseView]

    @classmethod
    def from_report(cls, report: Report) -> ReportView:
        return cls(
            agent_filter=report.agent_filter,
            window_days=report.window_days,
            totals=ReportTotalsView(
                runs=report.total_runs,
                failed_runs=report.total_failed_runs,
                cost_usd=report.total_cost_usd,
                eval_runs=report.total_eval_runs,
                latest_pass_rate=report.overall_latest_pass_rate,
                latency_ms=LatencyPercentilesView.from_dataclass(report.overall_latency),
            ),
            agents=[AgentMetricsRollupView.from_rollup(a) for a in report.agents],
            top_failing_cases=[FailingCaseView.from_dataclass(c) for c in report.top_failing_cases],
        )


class AgentMetricsView(BaseModel):
    """``GET /api/v1/agents/{name}/metrics`` response (ADR 032 D2).

    The per-agent slice of the monitor feed: the named agent's rollup row
    (or a zeroed row when it has no runs/evals in the window) plus the same
    totals + top-failing-cases scoped to that agent. Powers the agent-profile
    page's health panel in the front end.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    window_days: int = Field(0, description="Time window in days; 0 = all-time.")
    totals: ReportTotalsView
    rollup: AgentMetricsRollupView = Field(
        ..., description="The agent's own rollup row; zeroed when it has no data in the window."
    )
    top_failing_cases: list[FailingCaseView]

    @classmethod
    def from_report(cls, name: str, report: Report) -> AgentMetricsView:
        # The report was built scoped to this agent, so at most one rollup row
        # exists. A queried agent with no runs/evals in the window yields an
        # empty ``agents`` list — surface a zeroed row (not a 404) so the front
        # end renders an empty panel rather than erroring (failure-mode rule).
        rollup = next((a for a in report.agents if a.name == name), None)
        rollup_view = (
            AgentMetricsRollupView.from_rollup(rollup)
            if rollup is not None
            else AgentMetricsRollupView(
                name=name,
                runs=0,
                failed_runs=0,
                failure_rate=0.0,
                total_cost_usd=0.0,
                mean_cost_usd=0.0,
                latency_ms=LatencyPercentilesView(count=0),
                last_run_at="",
                eval_runs=0,
                latest_pass_rate=None,
                mean_pass_rate=None,
                latest_eval_at="",
            )
        )
        return cls(
            name=name,
            window_days=report.window_days,
            totals=ReportTotalsView(
                runs=report.total_runs,
                failed_runs=report.total_failed_runs,
                cost_usd=report.total_cost_usd,
                eval_runs=report.total_eval_runs,
                latest_pass_rate=report.overall_latest_pass_rate,
                latency_ms=LatencyPercentilesView.from_dataclass(report.overall_latency),
            ),
            rollup=rollup_view,
            top_failing_cases=[FailingCaseView.from_dataclass(c) for c in report.top_failing_cases],
        )


# ---------------------------------------------------------------------------
# Agent catalog (ADR 041) — wire types for /api/v1/catalog/...
#
# Three namespaces (movate / private / community) share one read API. The
# view types are flat / additive — new optional fields can land without a
# version bump (ADR 041 Resolved decision #3).
# ---------------------------------------------------------------------------


class CatalogRatingsSummaryView(BaseModel):
    """Aggregate of all ratings for a catalog entry."""

    model_config = ConfigDict(extra="forbid")

    count: int = 0
    avg: float = 0.0


class CatalogEntryView(BaseModel):
    """Catalog-card payload for one entry.

    ``source`` carries the namespace; ``tenant_id`` is ``None`` for public
    namespaces (``movate`` / ``community``). The bundle bytes are NOT
    inlined — fetch a specific version via
    ``/api/v1/catalog/agents/{slug}/versions/{ver}``.
    """

    model_config = ConfigDict(extra="forbid")

    slug: str
    source: str
    tenant_id: str | None = None
    latest_version: str
    name: str
    title: str
    description: str
    tags: list[str] = Field(default_factory=list)
    shape: str | None = None
    recommended_for: str | None = None
    ratings_summary: CatalogRatingsSummaryView = Field(default_factory=CatalogRatingsSummaryView)
    popularity: int = 0
    synced_at: str


class CatalogEntryDetailView(CatalogEntryView):
    """Detail view — same shape as the list view today plus a guard for
    forward-compat fields (e.g. the latest version's digest) without
    forcing the list path to grow."""

    model_config = ConfigDict(extra="forbid")

    latest_version_digest: str | None = None
    """SHA-256 hex of the latest version's bundle, when available."""


class CatalogEntryListResponse(BaseModel):
    """List view envelope."""

    model_config = ConfigDict(extra="forbid")

    entries: list[CatalogEntryView]
    count: int
    next_after_slug: str | None = None
    """The slug to pass back as ``?after_slug=`` for the next page.
    ``None`` when the caller has reached the end (the page returned fewer
    rows than ``limit``)."""


class CatalogEntryVersionView(BaseModel):
    """One version of a catalog entry. ``bundle_tar`` is base64-encoded
    on the wire (HTTP/JSON has no native bytes)."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    source: str
    tenant_id: str | None = None
    version: str
    digest: str
    published_at: str
    deprecated_at: str | None = None
    bundle_tar_b64: str | None = None
    """Base64-encoded bundle bytes. Inlined ONLY on the
    ``/versions/{ver}`` endpoint where the caller asked for it; the
    list-versions endpoint omits it to keep the response small."""


class CatalogSubmitRequest(BaseModel):
    """Body for ``POST /api/v1/catalog/agents`` — create a tenant-private
    entry. Server forces ``source='private'`` and
    ``tenant_id=caller_tenant`` regardless of any client-supplied value
    (ADR 041 D5)."""

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1)
    name: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    shape: str | None = None
    recommended_for: str | None = None
    version: str = Field(default="0.1.0")
    bundle_tar_b64: str = Field(
        ...,
        description="Base64-encoded tar of the entry bundle.",
    )


class CatalogPublishVersionRequest(BaseModel):
    """Body for ``POST /api/v1/catalog/agents/{slug}/versions`` — publish a
    new version of an existing tenant-private entry (ADR 041 D5)."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    bundle_tar_b64: str = Field(...)


class CatalogRatingRequest(BaseModel):
    """Body for ``POST /api/v1/catalog/agents/{slug}/ratings`` — record a
    rating against a catalog entry. ``source`` defaults to ``movate`` (the
    common path); pass ``private`` to rate an internal entry."""

    model_config = ConfigDict(extra="forbid")

    rating: int = Field(ge=1, le=5)
    comment: str | None = None
    source: str = "movate"


class CatalogSyncRequest(BaseModel):
    """Body for ``POST /api/v1/catalog/sync`` — trigger a sync.

    ``source`` MUST be ``movate`` for v1 (the only namespace that has an
    upstream service to sync from). Sending ``private`` is a 400; sending
    ``community`` is a 501 (the namespace is column-ready but disabled —
    ADR 041 D7)."""

    model_config = ConfigDict(extra="forbid")

    source: str = "movate"


class CatalogSyncResponse(BaseModel):
    """Sync stub response.

    v1 returns 202 + ``status='stub'`` + the bumped watermark; the
    production wiring against ``catalog.movate.io`` will swap in behind
    this same contract (ADR 041 D4)."""

    model_config = ConfigDict(extra="forbid")

    source: str
    status: str
    """One of:

    * ``"stub"`` — the v1 stub ran (logged + watermark bumped, no upstream
      fetch).
    * ``"synced"`` — reserved for the production handler that will replace
      the stub.
    """
    watermark: str
    """ISO 8601 timestamp of the watermark AFTER this call. Production will
    set this to the latest entry's ``synced_at`` rather than ``now()``;
    the v1 stub uses ``now()``."""
    detail: str
    """Human note explaining the response. The v1 stub returns a fixed
    string describing the missing upstream wiring (so an operator running
    the endpoint by hand sees what's happening)."""


# ---------------------------------------------------------------------------
# Knowledge-graph query API (ADR 046) — read-only, graphology-native.
#
# The subgraph / neighbors / stream wire shapes ARE the graphology import
# contract: ``GraphologyView.model_dump(mode="json")`` feeds straight into
# a sigma.js client's ``graph.import(...)`` with zero transform. These
# views are structural pass-throughs over the ``core.graph`` models — kept
# here so the HTTP surface stays in one place and OpenAPI documents the
# exact node/edge attribute bag.
# ---------------------------------------------------------------------------


class GraphNodeView(BaseModel):
    """One graphology node: ``{"key", "attributes": {...}}``."""

    model_config = ConfigDict(extra="forbid")

    key: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class GraphEdgeView(BaseModel):
    """One graphology edge: ``{"key", "source", "target", "attributes"}``."""

    model_config = ConfigDict(extra="forbid")

    key: str
    source: str
    target: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class GraphologyView(BaseModel):
    """A graphology import document — the zero-transform sigma.js contract.

    Returned by the windowed-subgraph + neighbors endpoints and emitted as
    the payload of every SSE growth event. Shape is pinned by
    ``test_runtime_graph_v1`` so a client never needs a transform layer.
    """

    model_config = ConfigDict(extra="forbid")

    attributes: dict[str, Any] = Field(default_factory=dict)
    nodes: list[GraphNodeView] = Field(default_factory=list)
    edges: list[GraphEdgeView] = Field(default_factory=list)


class ProvenanceView(BaseModel):
    """One source-chunk citation for a node detail panel."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    url: str | None = None
    snippet: str | None = None
    extraction_confidence: float | None = None


class NodeDetailView(BaseModel):
    """``GET /api/v1/graph/nodes/{id}`` response — node detail + provenance."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    key: str
    label: str
    type: str
    description: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    provenance: list[ProvenanceView] = Field(default_factory=list)
    neighbor_count: int = 0
    referenced_by_agents: list[str] = Field(default_factory=list)
    links: dict[str, str] = Field(default_factory=dict, alias="_links")
    """HATEOAS links; serialized as ``_links`` on the wire. ``expand`` →
    the neighbors endpoint for this node."""


class GraphSearchResult(BaseModel):
    """One matching node in a ``GET /api/v1/graph/search`` response."""

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    type: str


class GraphSearchView(BaseModel):
    """``GET /api/v1/graph/search`` response — matching nodes for fly-to."""

    model_config = ConfigDict(extra="forbid")

    query: str
    results: list[GraphSearchResult] = Field(default_factory=list)
    count: int = 0


class GraphQueryRequest(BaseModel):
    """``POST /api/v1/graph/query`` body — a bounded traverse/subgraph.

    ``project`` is the agent (graph owner); ``root`` is the node to
    traverse from. ``depth`` / ``limit`` are bounded server-side
    regardless of what the client sends (depth ≤ 6 hops, limit ≤ 5000).
    """

    model_config = ConfigDict(extra="forbid")

    project: str = Field(..., description="Agent that owns the graph.")
    root: str = Field(..., description="Node id to traverse from.")
    mode: str = Field(default="knowledge", description="knowledge | topology.")
    type: str | None = Field(default=None, description="Optional node-type filter.")
    depth: int | None = Field(default=None, ge=1, description="Hops; capped server-side at 6.")
    limit: int | None = Field(
        default=None, ge=1, description="Node/edge budget; capped server-side at 5000."
    )


# ---------------------------------------------------------------------------
# Observability Intelligence layer (ADR 047) — wire types for the
# /api/v1/observability/* endpoints. Kept here (not in core/observability) so
# the HTTP surface evolves independently of the persisted insight model.
# ---------------------------------------------------------------------------


class ObservabilityInsightView(BaseModel):
    """``GET /api/v1/observability/insights`` row — one daily insight."""

    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    date: str
    health_score: float
    anomalies: list[dict[str, Any]] = Field(default_factory=list)
    top_failures: list[dict[str, Any]] = Field(default_factory=list)
    usage_rollup: dict[str, Any] = Field(default_factory=dict)
    trends: dict[str, Any] = Field(default_factory=dict)
    narrative_digest: str = ""
    created_at: str

    @classmethod
    def from_record(cls, insight: Any) -> ObservabilityInsightView:
        return cls(
            id=insight.id,
            project_id=insight.project_id,
            date=insight.date.isoformat(),
            health_score=insight.health_score,
            anomalies=list(insight.anomalies),
            top_failures=list(insight.top_failures),
            usage_rollup=dict(insight.usage_rollup),
            trends=dict(insight.trends),
            narrative_digest=insight.narrative_digest,
            created_at=insight.created_at.isoformat(),
        )


class ObservabilityInsightListView(BaseModel):
    """``GET /api/v1/observability/insights`` envelope."""

    model_config = ConfigDict(extra="forbid")

    insights: list[ObservabilityInsightView] = Field(default_factory=list)
    count: int


class ObservabilityHealthView(BaseModel):
    """``GET /api/v1/observability/health`` — the latest health score + digest.

    Named ``ObservabilityHealthView`` (not ``HealthView``) to avoid clashing
    with the existing ``/healthz`` liveness ``HealthView``.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str | None = None
    date: str | None = None
    health_score: float | None = None
    narrative_digest: str = ""
    anomaly_count: int = 0
    has_insight: bool = False
    """False when no insight exists yet for the project (cold start)."""


class EvidenceView(BaseModel):
    """One citation backing a grounded answer (ADR 047 — citations mandatory)."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    reference: str
    detail: str = ""
    data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_record(cls, ev: Any) -> EvidenceView:
        return cls(
            kind=str(ev.kind),
            reference=ev.reference,
            detail=ev.detail,
            data=dict(ev.data),
        )


class GroundedAnswerView(BaseModel):
    """``POST /observability/ask`` + ``/troubleshoot`` response."""

    model_config = ConfigDict(extra="forbid")

    answer: str
    evidence: list[EvidenceView] = Field(default_factory=list)
    confidence: float = 0.0
    suggested_action: str = ""
    cost_usd: float = 0.0

    @classmethod
    def from_record(cls, ans: Any) -> GroundedAnswerView:
        return cls(
            answer=ans.answer,
            evidence=[EvidenceView.from_record(e) for e in ans.evidence],
            confidence=ans.confidence,
            suggested_action=ans.suggested_action,
            cost_usd=ans.cost_usd,
        )


class AskRequest(BaseModel):
    """``POST /api/v1/observability/ask`` body."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    project_id: str = Field(default="default", max_length=200)
    budget_usd: float = Field(default=0.05, ge=0.0, le=5.0)
    """Hard cap on the LLM spend of this query's synthesis call."""
    mock: bool = False
    """Use the deterministic MockProvider (no real spend / API key) — the
    hermetic-test path, mirroring the eval/bench endpoints' ``mock`` flag."""


class TroubleshootRequest(BaseModel):
    """``POST /api/v1/observability/troubleshoot`` body."""

    model_config = ConfigDict(extra="forbid")

    symptom: str = Field(min_length=1, max_length=2000)
    time_window_days: int = Field(default=7, ge=1, le=90)
    project_id: str = Field(default="default", max_length=200)
    budget_usd: float = Field(default=0.05, ge=0.0, le=5.0)
    mock: bool = False
    """Use the deterministic MockProvider (no real spend / API key)."""


class AnalyzeRequest(BaseModel):
    """``POST /api/v1/observability/analyze`` body (admin — on-demand trigger)."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(default="default", max_length=200)
    date: str | None = None
    """ISO YYYY-MM-DD. Omitted → the worker analyzes *yesterday* (nightly case)."""
    budget_usd: float = Field(default=0.10, ge=0.0, le=5.0)


class AnalyzeAcceptedView(BaseModel):
    """``POST /api/v1/observability/analyze`` 202 response — the enqueued job."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    kind: str
    project_id: str
