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
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from movate.core.models import (
    AuditFinding,
    AuditFindingSeverity,
    AuditRecord,
    ConversationThread,
    DiagnosisRecord,
    DiagnosisStatus,
    ErrorInfo,
    FeedbackRecord,
    JobKind,
    JobRecord,
    JobStatus,
    Metrics,
    Project,
    ProjectMember,
    ProjectMemberRole,
    RunRecord,
    Session,
    SessionMessage,
    WorkflowRunRecord,
    WorkflowStatus,
)
from movate.core.reporting import (
    AgentRollup,
    FailingCase,
    LatencyPercentiles,
    Report,
    Usage,
    UsageRollup,
)


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

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

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
    links: dict[str, str] = Field(default_factory=dict, alias="_links")
    """Hypermedia next-calls (ADR 061 D1), serialized as ``_links``: ``self``,
    ``trace``, ``explain``, and ``agent``. Populated by :meth:`from_record`."""

    @classmethod
    def from_record(cls, record: RunRecord) -> RunView:
        from movate.runtime.hypermedia import run_links  # noqa: PLC0415

        view = cls(
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
        # Set the aliased ``_links`` field by name post-construction (keeps the
        # pydantic-mypy plugin happy — it expects the ``_links`` alias in the
        # constructor; the field name works on assignment).
        view.links = run_links(record.run_id, record.agent)
        return view


class RunEstimatePredictionView(BaseModel):
    """The numeric prediction band of a :class:`RunEstimateView`."""

    model_config = ConfigDict(extra="forbid")

    tokens_in: int
    tokens_out_max: int
    tokens_out_expected: int
    cost_usd_min: float
    cost_usd_expected: float
    cost_usd_max: float
    latency_ms_p50: int | None = None
    latency_ms_p95: int | None = None


class RunEstimateBasisView(BaseModel):
    """How each estimate field was derived — lets a client distinguish a
    history-informed estimate from a cold-start fallback."""

    model_config = ConfigDict(extra="forbid")

    prompt_tokens_method: str
    out_expected_method: str
    latency_method: str
    sample_size: int


class RunEstimateBudgetCheckView(BaseModel):
    """Agent per-run budget comparison (``budget.max_cost_usd_per_run``)."""

    model_config = ConfigDict(extra="forbid")

    within_per_run_budget: bool
    per_run_budget_usd: float


class RunEstimateView(BaseModel):
    """``POST /api/v1/agents/{name}/runs?estimate=true`` response.

    A pre-flight cost + latency estimate. NO run is executed, NO job is
    enqueued, and the tenant is NOT charged (the only operation that can
    cost money is RAG retrieval embedding, which is opt-in via
    ``?estimate_retrieval=true`` and reflected in ``retrieval_embedded``).

    ``estimate`` is a constant ``True`` discriminator so a client can tell
    this shape apart from a :class:`RunView` / :class:`RunAccepted` on the
    same route. See :mod:`movate.core.run_estimator` for per-field
    derivation.
    """

    model_config = ConfigDict(extra="forbid")

    estimate: bool = True
    agent_name: str
    model: str
    predicted: RunEstimatePredictionView
    basis: RunEstimateBasisView
    budget_check: RunEstimateBudgetCheckView
    retrieval_embedded: bool = False
    notes: list[str] = Field(default_factory=list)

    @classmethod
    def from_estimate(cls, est: Any) -> RunEstimateView:
        """Build the wire view from a :class:`movate.core.run_estimator.RunEstimate`.

        Typed ``Any`` to keep ``run_estimator`` (a ``core`` module) out of
        this module's import graph — the runtime imports it lazily at the
        call site instead.
        """
        return cls(
            estimate=True,
            agent_name=est.agent_name,
            model=est.model,
            predicted=RunEstimatePredictionView(
                tokens_in=est.predicted.tokens_in,
                tokens_out_max=est.predicted.tokens_out_max,
                tokens_out_expected=est.predicted.tokens_out_expected,
                cost_usd_min=est.predicted.cost_usd_min,
                cost_usd_expected=est.predicted.cost_usd_expected,
                cost_usd_max=est.predicted.cost_usd_max,
                latency_ms_p50=est.predicted.latency_ms_p50,
                latency_ms_p95=est.predicted.latency_ms_p95,
            ),
            basis=RunEstimateBasisView(
                prompt_tokens_method=est.basis.prompt_tokens_method,
                out_expected_method=est.basis.out_expected_method,
                latency_method=est.basis.latency_method,
                sample_size=est.basis.sample_size,
            ),
            budget_check=RunEstimateBudgetCheckView(
                within_per_run_budget=est.budget_check.within_per_run_budget,
                per_run_budget_usd=est.budget_check.per_run_budget_usd,
            ),
            retrieval_embedded=est.retrieval_embedded,
            notes=list(est.notes),
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


class DeadLetterPurgeView(BaseModel):
    """``POST /api/v1/jobs/dead-letter/purge`` response.

    ``purged`` is the number of ``DEAD_LETTER`` rows deleted for the
    authenticated tenant. Envelope (not a bare int) so the response can
    grow back-compatibly (e.g. a future ``cutoff`` echo)."""

    model_config = ConfigDict(extra="forbid")

    purged: int


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


class CapabilityVoiceView(BaseModel):
    """The ``voice`` block of :class:`CapabilitiesView` (ADR 048/050 D4).

    Advertises this runtime's voice capability: which pipeline modes are
    available, which STT/TTS providers are configured (key present in env),
    and whether voice is effectively enabled at all.

    ``enabled`` is ``True`` when at least one STT + one TTS provider has its
    credential env var set.  When ``False`` the other fields are still
    populated (modes / provider lists) so a client knows *what would work* if
    keys were provided — this lets ``mdk voice providers list`` give useful
    guidance even on an unconfigured runtime.

    Added as an **additive, optional** field on :class:`CapabilitiesView`
    (``None`` on the minimal / unauthenticated view).  Absence means the
    runtime predates this field — callers should fall back to the flat
    ``features["voice"]`` / ``features["voice_realtime"]`` booleans.

    CLAUDE.md rule 5 — flagged: new additive field on an existing endpoint.
    No existing field is changed or removed.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    """``True`` when at least one STT + one TTS provider is keyed and the
    voice WS route is registered on this runtime.  ``False`` means voice
    is not ready (no keys, or the route is absent / mdk[voice] not installed).
    """
    modes: list[str]
    """Voice pipeline modes available on this runtime — a subset of
    ``["pipeline", "realtime"]``.  ``pipeline`` is always present when
    ``enabled`` is ``True``; ``realtime`` is only present when a
    ``RealtimeVoiceProvider`` factory is configured (ADR 048 D2b)."""
    stt_providers: list[str]
    """STT provider names whose credential env var is set on this runtime
    (e.g. ``["deepgram", "openai", "azure"]``).  Sorted.  Empty when no STT
    key is configured."""
    tts_providers: list[str]
    """TTS provider names whose credential env var is set on this runtime
    (e.g. ``["cartesia", "openai", "elevenlabs", "azure"]``).  Sorted.
    Empty when no TTS key is configured."""


class VoiceTurnView(BaseModel):
    """The JSON envelope of one one-shot voice turn (ADR 050 D2 / D10).

    Returned by ``POST /api/v1/agents/{name}/voice`` — the REST parity to the
    streaming WS. It is the *same* turn as a WS turn collapsed to a single
    request/response: STT → the UNCHANGED Executor → TTS. The envelope carries
    the transcript (what the caller said), the response text (what the agent
    answered), and a **reference** to the synthesized audio — **never** the
    audio bytes inline (ADR 050 D10 rejects base64-in-JSON; the bytes ride a
    binary side-channel / signed URL).

    The three-stage cost (STT-seconds + LLM-tokens + TTS-chars, ADR 036) and
    the per-stage latency ride the response **headers** (ADR 050 D7 /
    ``X-MDK-Cost-USD`` / ``X-MDK-Voice-Latency-*``), not this body — the body
    stays clean + codegen-friendly (ADR 045 D8).

    CLAUDE.md rule 5 — flagged: this is the response shape of a NEW additive
    ``/api/v1`` endpoint. It changes no existing endpoint's contract.
    """

    model_config = ConfigDict(extra="forbid")

    transcript: str
    """What the caller said — STT's endpointed (final) transcript of the
    inbound audio. Empty string only if STT produced no final (an ``error``
    status accompanies that case)."""
    response_text: str
    """What the agent answered — the unchanged Executor's human-readable
    output text for this turn (the text that was spoken via TTS)."""
    audio_url: str | None = None
    """A short-lived signed URL to fetch the synthesized answer audio
    (ADR 050 D10 — the batch/large-audio path). ``None`` when the audio was
    returned inline on the binary side-channel instead, or when TTS produced
    no audio (a degraded text-only turn)."""
    audio_bytes_b64: str | None = None
    """Base64 of the synthesized answer audio, populated ONLY when the caller
    explicitly opts into an inline body (``?audio=inline``) for small
    test/telephony turns where a side-channel is overkill. ``None`` by default
    — the codegen-clean shape keeps audio out of the JSON (ADR 050 D10). When
    set, ``audio_codec`` + ``audio_sample_rate`` describe the bytes."""
    audio_codec: str | None = None
    """The codec of the synthesized audio (e.g. ``pcm16``), when audio was
    produced. ``None`` on a text-only / errored turn."""
    audio_sample_rate: int | None = None
    """The sample rate (Hz) of the synthesized audio, when produced."""
    run_id: str = ""
    """The run id of this turn — a voice turn IS a run (ADR 050 D1), so it
    shows up in ``mdk runs list`` / ``/api/v1/usage`` / traces like any run."""
    status: str = ""
    """Terminal status of the turn (``success`` / ``error`` / ``interrupted``),
    mirroring the WS terminal ``done`` frame."""
    error: str | None = None
    """A human-readable failure reason when ``status == "error"`` (the stage
    that degraded — STT/agent/TTS — per ADR 048 D8), else ``None``."""


class CapabilityModelsView(BaseModel):
    """The ``models`` block of :class:`CapabilitiesView`.

    Describes which providers/models this runtime can reach. ``available``
    is the catalog the runtime already knows (the same source ``mdk models``
    /``GET /api/v1/models`` use); ``byok_configured`` lists the provider
    NAMES the calling tenant has brought a key for — never the key values
    (ADR 018). ``default`` is the runtime's fleet-default model id, or
    ``None`` when none is configured.
    """

    model_config = ConfigDict(extra="forbid")

    available: list[str]
    """Every model id the catalog knows (``provider/model``), sorted."""
    default: str | None = None
    """The fleet-default model id, or ``None`` if unconfigured."""
    byok_configured: list[str]
    """Provider NAMES (e.g. ``["openai", "anthropic"]``) the calling tenant
    has a stored BYOK key for. Names ONLY — the encrypted key value is never
    surfaced. Empty when no per-tenant key store / no tenant keys."""


class CapabilityLimitsView(BaseModel):
    """The ``limits`` block of :class:`CapabilitiesView`.

    This tenant's effective operational limits, derived from the runtime's
    actual rate-limit + batch config (not a hardcoded guess). A field is
    ``None`` when the corresponding limit is OFF/uncapped in this deploy.
    """

    model_config = ConfigDict(extra="forbid")

    rate_limit_per_min: int | None = None
    """Per-API-key request ceiling per minute, or ``None`` when rate
    limiting is disabled for this runtime."""
    tenant_rate_limit_per_min: int | None = None
    """Per-tenant aggregate request ceiling per minute (item 25), or
    ``None`` when the per-tenant limiter is OFF (the default)."""
    max_batch_size: int
    """Max rows accepted by ``POST /api/v1/agents/{name}/batch`` — the
    server-enforced ``MDK_BATCH_MAX_ROWS`` cap."""


class CapabilityResourceView(BaseModel):
    """One managed resource type in the capabilities matrix.

    Lets an API-first client (e.g. a front end or an integrator) discover the
    *manageable resource surface* — agents, projects, skills, contexts, KB —
    without crawling the route table itself. Like every other capability field,
    it's derived from the *deployed* route table: ``operations`` lists exactly
    the CRUD verbs registered on THIS build, so a half-managed resource
    (skills: create-only today) and a not-yet-shipped one (contexts, until
    ADR 060) are reported honestly rather than promised.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    """Resource family — ``agents`` / ``projects`` / ``skills`` / ``contexts``
    / ``kb``."""
    path: str
    """Base ``/api/v1`` path for the resource (the collection or, for KB, the
    per-agent sub-resource template)."""
    operations: list[str]
    """The lifecycle verbs registered for this resource on this build — a
    subset of ``list``/``create``/``get``/``update``/``delete`` (plus
    ``ingest``/``search``/``stats`` for KB). Detected from the live route
    table, sorted for a stable wire shape."""
    managed: bool
    """``True`` when the resource has a full API lifecycle here (a write verb +
    a read verb + ``delete``). ``False`` for a partial surface (e.g. skills,
    which only expose ``create`` until ADR 060 lands)."""


class CapabilitiesView(BaseModel):
    """``GET /api/v1/capabilities`` response — the runtime's self-description.

    A read-only matrix letting a client (e.g. Mova iO talking to many
    heterogeneous customer runtimes) learn exactly what THIS runtime version
    supports, without trial-and-error against every endpoint. Every field is
    derived from the *deployed* runtime (route introspection / import probing
    / live config), never a static promise.

    Two views:

    * **Authenticated (``read`` scope)** — the full matrix below.
    * **Unauthenticated** — a minimal subset (``mdk_version`` + ``api_version``,
      with the rest ``None``/empty) for health/probe use, so an orchestrator
      can fingerprint a runtime version before holding a key for it.

    The ``minimal`` flag tells a client which view it received.
    """

    model_config = ConfigDict(extra="forbid")

    mdk_version: str
    """``movate.__version__`` (CalVer) — the exact build serving this."""
    api_version: str
    """The runtime API version this matrix describes (``"v1"``)."""
    served_at: datetime
    """UTC timestamp the response was produced."""
    minimal: bool = False
    """``True`` for the unauthenticated subset (only ``mdk_version`` +
    ``api_version`` populated); ``False`` for the full ``read``-scoped view."""

    models: CapabilityModelsView | None = None
    """Reachable models / providers. ``None`` in the minimal view."""
    features: dict[str, bool] | None = None
    """Capability flags the client can branch on. Each is detected from the
    deployed code (route registered / module importable), NOT hardcoded.
    ``None`` in the minimal view."""
    scopes_supported: list[str] | None = None
    """The runtime's authorization-scope vocabulary. ``None`` in the
    minimal view."""
    limits: CapabilityLimitsView | None = None
    """This tenant's effective limits. ``None`` in the minimal view."""
    extras_installed: list[str] | None = None
    """Optional ``pyproject`` extras importable in this image (marker-module
    probe). ``None`` in the minimal view."""
    voice: CapabilityVoiceView | None = None
    """Voice capability block (ADR 048/050 D4): modes, STT/TTS providers,
    and whether voice is effectively enabled. ``None`` in the minimal
    (unauthenticated) view. Additive — absent on runtimes that predate this
    field; callers fall back to ``features["voice"]``/``features["voice_realtime"]``.

    CLAUDE.md rule 5 — flagged: new additive field on an existing endpoint."""
    resources: list[CapabilityResourceView] | None = None
    """The manageable resource surface (agents/projects/skills/contexts/kb)
    with the CRUD operations registered on this build — the API-first
    discoverability answer to "what can I manage here, and how complete is
    each?". ``None`` in the minimal view. Detected from the live route table,
    so it tracks the deployed surface as resources gain operations.

    CLAUDE.md rule 5 — flagged: new additive field on an existing endpoint."""


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

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    version: str
    description: str = ""
    skill_dir: str
    """Path-relative-to-skills-root where the bundle landed.
    E.g. ``web-search``."""
    files_persisted: list[str]
    """Sorted list of files written, relative to ``skill_dir``.
    E.g. ``["impl.py", "skill.yaml"]``."""
    id: str | None = None
    """Uniform created-resource id (ADR 061 D2) — the skill's ``name``, under
    the common ``id`` key. Additive; ``name`` stays."""
    created_at: datetime | None = None
    """Create-response timestamp (UTC, ADR 061 D2) — aligns the skill-create
    envelope with project/agent."""
    links: dict[str, str] = Field(default_factory=dict, alias="_links")
    """Hypermedia next-calls (ADR 061 D1), serialized as ``_links``. Empty
    ``{}`` until the skill GET/attach routes ship (ADR 060) — no dead links
    (ADR 061 D4)."""


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
# Judge Engineer — author + commit a judge.yaml
# (POST /api/v1/agents/{name}/judge/{generate,commit})
# ---------------------------------------------------------------------------


class JudgeGenerateRequest(BaseModel):
    """``POST /api/v1/agents/{name}/judge/generate`` request body.

    All fields are optional — an empty ``{}`` body asks the engineer to
    infer dimensions from the agent shape, use the default engineer
    model, and include anchor examples. The generation is sync (~few
    seconds) and read-only — it does NOT touch the agent's bundle on
    disk. The follow-up commit endpoint is what writes ``judge.yaml``.

    The shape of the generated YAML is the existing canonical
    :class:`~movate.core.models.JudgeConfig` (CLAUDE.md rule 5 —
    judge.yaml is a flagged surface). The dimensions are reflected
    INSIDE the rubric text, not as a new top-level YAML key.
    """

    model_config = ConfigDict(extra="forbid")

    rubric_dimensions: list[str] | None = Field(
        default=None,
        description=(
            "Optional list of scoring dimensions the rubric must cover. "
            "Each entry is a lowercase snake_case identifier (e.g. "
            "``accuracy``, ``tone``, ``schema_adherence``). When omitted "
            "or null, the engineer infers a sensible set from the agent's "
            "shape (RAG / tool-use / workflow / generic)."
        ),
    )
    include_examples: bool = Field(
        default=True,
        description=(
            "When true (the default) the rubric is anchored with 2-3 "
            "concrete scored examples drawn from the agent's domain (and "
            "from the agent's evals dataset when available). False keeps "
            "the rubric leaner — useful when the agent has no dataset yet."
        ),
    )
    model: str | None = Field(
        default=None,
        description=(
            "Optional LiteLLM-style provider/model override for the "
            "ENGINEER model — the model that authors the rubric. Defaults "
            "to a strong general-purpose model (currently "
            "``anthropic/claude-sonnet-4-6``). Distinct from the judge "
            "model the generated YAML uses at eval time."
        ),
    )
    budget_usd: float = Field(
        default=0.10,
        ge=0.0,
        le=10.0,
        description=(
            "Hard ceiling on the generation call's cost in USD. Typical "
            "generation is <$0.01; this is a safety valve. Exceeded → "
            "402 with no commit."
        ),
    )


class JudgeGenerateResponse(BaseModel):
    """``POST /api/v1/agents/{name}/judge/generate`` response.

    The ``judge_yaml`` is the complete, validated YAML body — a UI can
    render it for review, the operator hand-edits if desired, then the
    edited string POSTs back to ``/judge/commit`` (which re-validates
    before persisting).
    """

    model_config = ConfigDict(extra="forbid")

    judge_yaml: str
    """The full ``judge.yaml`` body as a string. Validated against
    :class:`~movate.core.models.JudgeConfig` before returning — the
    eval engine can load this byte-for-byte. Edit and POST to
    ``/judge/commit`` to persist."""
    rubric_dimensions: list[str]
    """The dimensions the rubric covers. Mirrored from the ``Dimensions
    covered:`` preamble inside the rubric text so a client can render
    the dimension chip set without re-parsing the markdown."""
    rationale: str
    """One or two sentences explaining why these dimensions were picked
    for this agent. Surfaced to the human reviewer; not persisted."""
    tokens_used: int = 0
    """Total tokens (input + output) consumed by the engineer LLM call."""
    cost_usd: float = 0.0
    """Best-effort cost of the engineer call in USD, looked up via the
    pricing table. 0.0 when pricing data is unavailable for the chosen
    engineer model — the call still happened."""


class JudgeCommitRequest(BaseModel):
    """``POST /api/v1/agents/{name}/judge/commit`` request body.

    The ``judge_yaml`` is the YAML the operator wants persisted at
    ``<agent_dir>/evals/judge.yaml``. The server re-validates against
    :class:`~movate.core.models.JudgeConfig` BEFORE writing — a hand
    edit that breaks the schema can't land. Idempotent: posting the
    same YAML twice overwrites with the same bytes.
    """

    model_config = ConfigDict(extra="forbid")

    judge_yaml: str = Field(
        ...,
        min_length=1,
        description=(
            "The complete ``judge.yaml`` body to persist. Validated "
            "against :class:`~movate.core.models.JudgeConfig`; a "
            "malformed body returns 422 without touching disk."
        ),
    )


class JudgeCommitResponse(BaseModel):
    """``POST /api/v1/agents/{name}/judge/commit`` response."""

    model_config = ConfigDict(extra="forbid")

    agent_name: str
    judge_path: str
    """Where the file landed, as a path RELATIVE to the agent dir
    (``evals/judge.yaml`` in the canonical layout)."""
    updated: bool
    """``True`` when an existing ``judge.yaml`` was overwritten; ``False``
    when the file was created fresh. Lets the UI surface the right
    affirmative ("Saved" vs "Created")."""


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

    The wire shape is intentionally additive across all four ingest
    kinds (``upload`` / ``text`` / ``url`` / ``generated``). The
    multipart upload path populates ``files`` + ``total_chunks_saved``
    and leaves the new JSON-mode fields at their defaults — byte-for-byte
    backwards compatible with the pre-JSON-modes client contract. JSON
    modes populate ``ingest_id`` / ``kind`` / ``chunks_added`` /
    ``tokens_in`` / ``embedding_cost_usd`` (and ``generated_content``
    for ``kind="generated"``) so a single response model serves every
    front-end branch.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    agent_name: str
    files: list[KbIngestFileResult] = Field(default_factory=list)
    """Per-file detail. Populated for the multipart ``upload`` kind;
    a single one-element entry is included for the JSON ``text`` /
    ``url`` / ``generated`` kinds so the UI can render the same
    "what was ingested" row regardless of mode."""
    total_chunks_saved: int = 0
    """Sum of chunks_saved across all files — convenience for the UI.
    Equal to ``chunks_added`` for the JSON modes."""

    # --- JSON-mode additive fields (KB ingest kinds: text / url / generated) ---
    ingest_id: str = ""
    """Unique id for this ingest operation. Empty string on the legacy
    multipart path (no id was minted historically); a uuid hex on the
    JSON paths so callers can correlate with future async-job tracking."""
    kind: str = "upload"
    """``"upload"`` (multipart, existing) | ``"text"`` | ``"url"`` |
    ``"generated"`` (new JSON modes). Lets the UI branch on response."""
    chunks_added: int = 0
    """Chunks persisted on this call. Equal to ``total_chunks_saved`` —
    surfaced separately so the JSON-mode contract matches the spec
    without forcing JSON callers to read a multipart-specific field."""
    tokens_in: int = 0
    """Approximate input tokens consumed by the embedding model for
    this ingest. Best-effort character / 4 heuristic — the OpenAI
    embeddings response does not expose token usage, so we estimate.
    Zero for skipped / empty content."""
    embedding_cost_usd: float = 0.0
    """Approximate embedding cost in USD. Derived from ``tokens_in``
    against the canonical pricing table; ``0.0`` when the model is
    not priced (unknown model) or no chunks were embedded."""
    generated_content: str | None = None
    """The LLM-authored Markdown body. Set ONLY for ``kind="generated"``
    so the caller can review (and surface in the UI) what was actually
    embedded. ``None`` for every other kind."""
    links: dict[str, str] = Field(default_factory=dict, alias="_links")
    """Hypermedia next-calls (ADR 061 D1), serialized as ``_links``: ``self``
    (the corpus), ``search``, ``stats``. Empty ``{}`` for callers/paths that
    don't populate it."""


# ---------------------------------------------------------------------------
# KB JSON ingest modes (text / url / generated) — additive to the existing
# multipart upload endpoint. The route dispatches on Content-Type: a
# ``multipart/form-data`` body hits the legacy upload path, an
# ``application/json`` body is parsed into one of these via the discriminated
# union. The route + scope are unchanged (``POST /api/v1/agents/{name}/kb``,
# ``kb:write``) so front-end clients integrated against the existing surface
# keep working byte-for-byte. See docs/front-end-api.md.
# ---------------------------------------------------------------------------

# Hard cap on the v1 synchronous-crawl ``max_pages``. Lifted out as a
# module-level constant so the schema + the handler enforce the same
# bound — keeps the OpenAPI spec honest about the limit.
KB_INGEST_URL_MAX_PAGES_CAP = 50

# Hard wall-clock budget (seconds) for the entire URL ingest path in v1
# (single page or bounded crawl). A crawl whose summed fetches exceed
# this budget short-circuits — partial pages already ingested are kept,
# the rest are skipped. Async-job variant for larger crawls is a
# follow-up.
KB_INGEST_URL_TIMEOUT_S = 60.0


class KbIngestTextRequest(BaseModel):
    """``kind="text"`` — inline document body authored by the caller.

    The simplest JSON mode: the caller hands the runtime the text to
    chunk + embed + persist directly. ``title`` is the per-chunk
    ``source`` label (shows up in ``GET /api/v1/agents/{name}/kb`` so
    a human can tell which inline doc a chunk came from); ``content``
    is the body.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["text"] = "text"
    title: str = Field(..., min_length=1, max_length=256)
    """Short label for the document — becomes the per-chunk ``source``.
    Required so chunks have a human-readable provenance string."""
    content: str = Field(..., min_length=1)
    """The raw text to ingest. Markdown is fine — the paragraph
    chunker treats ``\\n\\n`` boundaries the same way. Empty / blank
    content is rejected at the pydantic layer (``min_length=1``); a
    content body whose chunks all fall below the splitter floor yields
    a 200 with ``chunks_added=0`` and ``status="empty"`` per-file."""


class KbIngestUrlRequest(BaseModel):
    """``kind="url"`` — fetch a single page (default) or a bounded crawl.

    Reuses the same ``movate.kb.web`` front-end the CLI's
    ``mdk kb ingest <url>`` does — :func:`movate.kb.web.fetch_and_extract`
    for the single-page mode, :func:`movate.kb.web.crawl_site` for the
    bounded same-host crawl. No new extractor or HTTP client.

    Synchronous + bounded by design (v1): ``max_pages`` defaults to 1,
    is hard-capped at 50, and the crawl is wall-clock-bounded by the
    endpoint's overall timeout (60s). An async-job variant for larger
    crawls is a documented follow-up (see PR body).
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["url"] = "url"
    url: str = Field(..., min_length=1, pattern=r"^https?://")
    """The starting URL. Must be http(s); other schemes are rejected
    at the pydantic layer."""
    crawl: bool = False
    """When true and ``max_pages > 1``, follow same-host ``<a href>``
    links breadth-first up to ``max_pages``. Defaults to a single-page
    fetch — the safe default."""
    max_pages: int = Field(default=1, ge=1, le=KB_INGEST_URL_MAX_PAGES_CAP)
    """Hard cap on pages fetched. Defaults to 1 (single page);
    operators opting into a crawl typically set ``crawl=true`` + a
    small ``max_pages`` (5-25). The 50-page ceiling matches the CLI's
    safe-default bound and the v1 synchronous-execution budget."""


class KbIngestGeneratedRequest(BaseModel):
    """``kind="generated"`` — LLM authors the document from a description.

    Calls into the agent's configured provider (``bundle.spec.model.provider``)
    via the existing ``BaseLLMProvider`` seam — so the CUSTOMER's BYOK keys
    are used, never Movate's. The generated Markdown body is returned in
    the response (``generated_content``) so the caller can review what
    was actually embedded; the same body is chunked + embedded through
    the unchanged ``movate.kb.ingest.ingest_text`` pipeline.

    No new provider is added. The system prompt is fixed (see the
    handler) and instructs the model to flag uncertainty with
    ``TODO: confirm with subject matter expert`` rather than
    hallucinate facts.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["generated"] = "generated"
    title: str = Field(..., min_length=1, max_length=256)
    """Short label for the generated document — becomes the per-chunk
    ``source``. Same role as ``KbIngestTextRequest.title``."""
    description: str = Field(..., min_length=1)
    """Free-form description of what the model should write about
    (e.g. "FAQ covering pricing tiers, upgrade paths, billing cycles,
    refund policy"). Becomes the user message of the completion."""


# Discriminated union for the JSON body — FastAPI parses the right
# concrete request based on the ``kind`` field. The route's Content-Type
# inspection chooses between this union and the existing multipart path.
KbIngestRequest = Annotated[
    KbIngestTextRequest | KbIngestUrlRequest | KbIngestGeneratedRequest,
    Field(discriminator="kind"),
]


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
    session_id: str | None = Field(
        default=None,
        description=(
            "Optional stateful-session id (ADR 045 D10). When set, the "
            "runtime loads the session's prior turns as conversation "
            "context, runs the agent, then appends this turn to the "
            "session and updates its cost rollup — server-side memory, so "
            "the client need not re-send history. The session must already "
            "exist (POST /api/v1/sessions) and belong to the caller's "
            "tenant; a missing/cross-tenant id is a 404. **Additive + "
            "back-compat: omitting session_id is byte-for-byte today's "
            "stateless behavior.** Session memory is threaded on the "
            "inline (``?wait=true``) and streaming run paths, which "
            "complete in-process and can append the turn immediately."
        ),
    )
    memory: Literal["server", "client"] = Field(
        default="server",
        description=(
            "Memory mode when ``session_id`` is set (ADR 045 D10 / R3). "
            "``server`` (default) = the runtime manages history. "
            "``client`` = opt out of server-side history assembly and get "
            "exactly today's stateless behavior; the turn is still "
            "recorded on the session for the rollup, but prior turns are "
            "NOT injected as context (the client is managing memory "
            "itself). Ignored when ``session_id`` is omitted."
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
    "DeadLetterPurgeView",
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
    "VoiceTurnView",
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
# Stateful sessions (ADR 045 D10) — server-side conversation memory. A
# session is a first-class entity (distinct from the join-key thread
# surface above) that the run endpoints accept via ``session_id``. These
# schemas are the wire envelopes for the /api/v1/sessions endpoints.
# ---------------------------------------------------------------------------


class SessionCreateSubmission(BaseModel):
    """``POST /api/v1/sessions`` body — open a new server-managed
    conversation (ADR 045 D10) with one agent."""

    model_config = ConfigDict(extra="forbid")

    agent: str = Field(
        ...,
        description=(
            "Agent the session targets. Sessions are bound to one agent; "
            "open a new session to target a different agent."
        ),
    )
    title: str = Field(
        default="",
        max_length=256,
        description="Optional human-readable label for client display.",
    )


class SessionMessageView(BaseModel):
    """One turn in a session's history (ADR 045 D10)."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    role: str
    content: dict[str, Any]
    run_id: str | None = None
    cost_usd: float
    tokens_in: int
    tokens_out: int
    created_at: datetime

    @classmethod
    def from_record(cls, record: SessionMessage) -> SessionMessageView:
        return cls(
            message_id=record.message_id,
            role=record.role,
            content=record.content,
            run_id=record.run_id,
            cost_usd=record.cost_usd,
            tokens_in=record.tokens_in,
            tokens_out=record.tokens_out,
            created_at=record.created_at,
        )


class SessionView(BaseModel):
    """``POST /api/v1/sessions`` + ``GET /api/v1/sessions/{id}`` response
    envelope. 1:1 with :class:`movate.core.models.Session` plus an
    optional ``messages`` array (filled by the get-with-history endpoint,
    omitted on bare create/list responses). The rollup fields
    (``turn_count`` + ``total_*``) are the per-session economics rollup
    ADR 045 D10 mandates."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    tenant_id: str
    agent: str
    title: str
    created_at: datetime
    updated_at: datetime
    turn_count: int
    total_cost_usd: float
    total_tokens_in: int
    total_tokens_out: int
    messages: list[SessionMessageView] | None = Field(
        default=None,
        description=(
            "Chronological turn history (earliest first). Populated by "
            "GET /api/v1/sessions/{id}; omitted on create + list responses."
        ),
    )

    @classmethod
    def from_record(
        cls,
        record: Session,
        *,
        messages: list[SessionMessageView] | None = None,
    ) -> SessionView:
        return cls(
            session_id=record.session_id,
            tenant_id=record.tenant_id,
            agent=record.agent,
            title=record.title,
            created_at=record.created_at,
            updated_at=record.updated_at,
            turn_count=record.turn_count,
            total_cost_usd=record.total_cost_usd,
            total_tokens_in=record.total_tokens_in,
            total_tokens_out=record.total_tokens_out,
            messages=messages,
        )


class SessionListView(BaseModel):
    """``GET /api/v1/sessions`` response — paginated session list for a
    tenant, ``updated_at DESC`` (active conversations first)."""

    model_config = ConfigDict(extra="forbid")

    sessions: list[SessionView]
    count: int


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


class NodeNeighborView(BaseModel):
    """One 1-hop connected entity in a node-detail drill-down panel.

    ``relation`` is the predicate type of the edge; ``direction`` is
    ``out`` (focused node → neighbor) or ``in`` (neighbor → focused node).
    The client groups these by ``relation`` and renders each ``key`` as a
    clickable link that re-centers the graph / opens the neighbor's detail.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    type: str
    relation: str
    direction: str


class NodeDetailView(BaseModel):
    """``GET /api/v1/graph/nodes/{id}`` response — detail + provenance + neighbors."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    key: str
    label: str
    type: str
    description: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    provenance: list[ProvenanceView] = Field(default_factory=list)
    neighbors: list[NodeNeighborView] = Field(default_factory=list)
    """The node's 1-hop connected entities, each tagged with relation type +
    direction — the drill-down panel's clickable "connected entities" list."""
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
    project_id: str | None = Field(
        default=None,
        description=(
            "ADR 046 D1 project-scope filter. When set, the traverse is "
            "bounded to nodes/edges tagged with this project_id; omit for "
            "the full per-agent graph (backward-compatible)."
        ),
    )


# ---------------------------------------------------------------------------
# Graph analytics (ADR 046) — read-only centrality / shortest-path / community
# detection over the windowed graph the query layer builds. Additive: these
# views sit beside the graph query views and never change an existing shape.
# Computed by ``movate.core.graph.analytics`` (pure Python, no new dependency)
# over the SAME windowed + tenant/project-scoped graphology doc the query
# endpoints serve — so analytics inherits the node/edge cap and the no-leak
# scoping for free.
# ---------------------------------------------------------------------------


class CentralityScoreView(BaseModel):
    """One node's centrality score in a ``GET .../analytics/centrality`` response.

    ``score`` is normalized to ``[0, 1]`` (degree and betweenness are both
    normalized so they're comparable + map onto a size/color ramp in the
    viewer). ``key`` is the node id; ``label`` / ``type`` decorate it so the
    client can render a ranked list without a second fetch.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    type: str
    score: float


class GraphCentralityView(BaseModel):
    """``GET /api/v1/graph/analytics/centrality`` response — top-N hubs.

    ``measure`` echoes which centrality was computed (``degree`` |
    ``betweenness``); ``scores`` is highest-first, capped at the requested
    ``top_n``.
    """

    model_config = ConfigDict(extra="forbid")

    measure: str
    scores: list[CentralityScoreView] = Field(default_factory=list)
    count: int = 0


class GraphShortestPathView(BaseModel):
    """``GET /api/v1/graph/analytics/path`` response — a shortest path.

    ``found`` is ``False`` (and ``nodes`` empty) when the two endpoints are in
    different components or an endpoint is unknown / out of scope.  ``hops`` is
    ``len(nodes) - 1`` (0 for a single-node path). ``nodes`` is the inclusive
    ordered id sequence ``[from, ..., to]`` the viewer highlights.
    """

    model_config = ConfigDict(extra="forbid")

    found: bool
    nodes: list[str] = Field(default_factory=list)
    hops: int = 0


class CommunityView(BaseModel):
    """One detected community in a ``GET .../analytics/communities`` response.

    ``community_id`` is a small stable integer (largest community first);
    ``members`` is the sorted node-id list; ``size`` is ``len(members)``. The
    viewer tints each member by ``community_id``.
    """

    model_config = ConfigDict(extra="forbid")

    community_id: int
    size: int
    members: list[str] = Field(default_factory=list)


class GraphCommunitiesView(BaseModel):
    """``GET /api/v1/graph/analytics/communities`` response — cluster assignment."""

    model_config = ConfigDict(extra="forbid")

    communities: list[CommunityView] = Field(default_factory=list)
    count: int = 0


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


# ---------------------------------------------------------------------------
# Projects (ADR 040) — wire types for the /api/v1/projects + /members surface.
# Kept here rather than in ``movate.core.models`` so the API contract can
# evolve independently of the persisted shape (per module docstring).
# ---------------------------------------------------------------------------


class ProjectCreateRequest(BaseModel):
    """``POST /api/v1/projects`` request body.

    ``name`` is unique within the caller's tenant; the reserved literal
    ``"default"`` is rejected with 422 (the per-tenant default project is
    auto-created by storage at first read; clients can't materialize it
    explicitly). ``owner_principal_id`` defaults to the caller's principal
    (the API layer fills it from the auth context when omitted) — the API
    refusal is a friendlier alternative to the storage layer's synthetic
    ``"tenant-system"`` owner used only for the lazy default project.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    owner_principal_id: str | None = None
    """Optional explicit owner. Omit to default to the caller's principal
    (``api_key:<key_id>`` on the opaque-key path, or the OIDC sub claim)."""


class ProjectUpdateRequest(BaseModel):
    """``PUT /api/v1/projects/{id}`` request body — partial update.

    Either or both of ``name`` / ``description`` may be set. An
    all-``None`` body is a no-op (returns the current row). The reserved
    name ``"default"`` is rejected; renaming the per-tenant default
    project is not supported via this endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None


class ProjectView(BaseModel):
    """``GET /api/v1/projects/{id}`` (and create/update) response.

    Mirror of :class:`Project` with the ``etag`` derived from
    ``updated_at`` — clients echo it back as ``If-Match`` for optimistic
    concurrency on PUT (412 on stale).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    project_id: str
    tenant_id: str
    name: str
    description: str | None = None
    owner_principal_id: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    etag: str
    """Opaque concurrency token derived from ``updated_at`` (ISO-8601). A
    client sends it back as ``If-Match: "<etag>"`` on PUT to opt into
    optimistic concurrency; absent header → last-write-wins (back-compat
    with the rest of the runtime)."""
    id: str | None = None
    """Uniform created-resource id (ADR 061 D2) — the same value as
    ``project_id``, exposed under the common ``id`` key so a client can treat
    any created resource uniformly. Additive; the typed ``project_id`` stays."""
    links: dict[str, str] = Field(default_factory=dict, alias="_links")
    """Hypermedia next-calls (ADR 061 D1), serialized as ``_links``: ``self``,
    ``agents``, ``members``, ``graph``. Empty ``{}`` for older callers/paths
    that don't populate it."""

    @classmethod
    def from_record(cls, p: Project) -> ProjectView:
        from movate.runtime.hypermedia import project_links  # noqa: PLC0415

        view = cls(
            project_id=p.project_id,
            tenant_id=p.tenant_id,
            name=p.name,
            description=p.description,
            owner_principal_id=p.owner_principal_id,
            created_at=p.created_at,
            updated_at=p.updated_at,
            archived_at=p.archived_at,
            etag=_project_etag(p),
            id=p.project_id,
        )
        # ``_links`` is aliased — set by field name post-construction (mypy).
        view.links = project_links(p.project_id)
        return view


class ProjectListResponse(BaseModel):
    """``GET /api/v1/projects`` response — tenant-scoped, newest-first."""

    model_config = ConfigDict(extra="forbid")

    projects: list[ProjectView]
    count: int


class ProjectMemberView(BaseModel):
    """``GET /api/v1/projects/{id}/members/{principal_id}`` response."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    principal_id: str
    role: ProjectMemberRole
    added_by: str
    added_at: datetime

    @classmethod
    def from_record(cls, m: ProjectMember) -> ProjectMemberView:
        return cls(
            project_id=m.project_id,
            principal_id=m.principal_id,
            role=m.role,
            added_by=m.added_by,
            added_at=m.added_at,
        )


class ProjectMemberListView(BaseModel):
    """``GET /api/v1/projects/{id}/members`` response."""

    model_config = ConfigDict(extra="forbid")

    members: list[ProjectMemberView]
    count: int


class ProjectMemberAddRequest(BaseModel):
    """``POST /api/v1/projects/{id}/members`` request body."""

    model_config = ConfigDict(extra="forbid")

    principal_id: str = Field(..., min_length=1)
    role: ProjectMemberRole


class ProjectMemberPatchRequest(BaseModel):
    """``PATCH /api/v1/projects/{id}/members/{principal_id}`` request body."""

    model_config = ConfigDict(extra="forbid")

    role: ProjectMemberRole


def _project_etag(p: Project) -> str:
    """Derive the ``ETag`` value from a project's ``updated_at``.

    ISO-8601 down to microseconds is monotonic per-row (every storage
    write bumps it) and stable across reads — perfect for an
    optimistic-concurrency token without standing up a separate version
    column. Returned bare (no quotes) — :func:`_normalize_if_match`
    strips client-side decoration.
    """
    return p.updated_at.isoformat()


# ---------------------------------------------------------------------------
# Unified agent-creation surface
# ``POST /api/v1/projects/{project_id}/agents`` — single dispatcher endpoint
# that routes to one of five existing creation paths based on a discriminated
# union body.
#
# Why a separate endpoint vs. extending POST /agents: the five existing
# creation endpoints (multipart bundle, from-wizard, future from-spec,
# preview+commit, catalog clone) ship distinct wire shapes that customers
# have already integrated against. This unified endpoint is an additive
# convenience layer for the Mova iO Angular UI — it COMPOSES the existing
# handlers but never duplicates their logic. Backward compat per CLAUDE.md
# rule 5: every legacy endpoint continues to work byte-for-byte.
# ---------------------------------------------------------------------------


class AgentCreateBundleRequest(BaseModel):
    """Placeholder for the multipart-bundle JSON discriminator.

    The actual bundle bytes arrive via ``multipart/form-data`` (the
    file payload is too large for a JSON body). This schema exists so
    OpenAPI documents the ``source: "bundle"`` shape for callers that
    inspect the discriminated-union spec; the route handler sniffs
    ``Content-Type`` and routes multipart requests to the existing
    bundle path regardless of whether this body is present.
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["bundle"] = "bundle"
    name: str | None = Field(
        default=None,
        description=(
            "Optional name override. When omitted the bundle's ``agent.yaml`` name is used."
        ),
    )


class AgentCreateSpecRequest(BaseModel):
    """``source: "spec"`` — caller already authored a full agent spec
    + prompt + schemas as JSON.

    Use this when the caller has a complete bundle in hand and just
    wants to persist it without zipping into a multipart upload. The
    spec dict is YAML-dumped to ``agent.yaml`` and the prompt /
    schemas are written to their canonical paths. Identical
    validation gate to the multipart endpoint (``load_agent``).
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["spec"] = "spec"
    name: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Agent name. Must match ``spec['name']`` when both set.",
    )
    spec: dict[str, Any] = Field(
        ...,
        description=(
            "The agent.yaml contents as a Python dict. Must include "
            "``api_version: movate/v1`` and ``kind: Agent``."
        ),
    )
    prompt: str = Field(..., min_length=1, description="Prompt body, inlined into prompt.md.")
    schemas: dict[str, str] | None = Field(
        default=None,
        description=(
            "Optional explicit ``{input, output}`` JSON-Schema strings. "
            "When omitted, the spec is expected to carry inline-shorthand "
            "schemas under ``spec['schema']``."
        ),
    )


class AgentCreateWizardRequest(BaseModel):
    """``source: "wizard"`` — wraps the existing ``WizardAgentSubmission``
    so the discriminated-union body can carry it.

    The wizard form payload field MUST match the shape of
    :class:`WizardAgentSubmission`; we accept it as ``dict`` here so
    the dispatcher can re-parse it through the canonical wizard model
    and reuse the existing translation pipeline byte-for-byte.
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["wizard"] = "wizard"
    wizard_form: dict[str, Any] = Field(
        ...,
        description="Wizard form payload — same shape as WizardAgentSubmission.",
    )


class AgentCreateLlmRequest(BaseModel):
    """``source: "llm"`` — natural-language → agent.

    Composes the scaffold-preview / eval-generator / judge-engineer /
    unified-KB-ingest pipelines (depending on the optional flags) and
    streams progress via SSE. Returns 202 + a ``job_id`` so the
    caller can reconnect to the SSE stream if disconnected.

    All composition targets are optional — when an upstream pipeline
    isn't deployed, the corresponding stage emits a ``stage_skipped``
    SSE event with a reason rather than aborting the run.
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["llm"] = "llm"
    description: str = Field(
        ...,
        min_length=1,
        description="Natural-language description of what the agent should do.",
    )
    shape: str | None = Field(
        default=None,
        description=(
            "Optional shape hint (``rag``, ``tool-use``, ``planner``, ...). "
            "When omitted the scaffold pipeline auto-detects from the "
            "description."
        ),
    )
    model: str | None = Field(
        default=None,
        description="Optional LiteLLM model id (e.g. ``openai/gpt-4o-mini``).",
    )
    rename_to: str | None = Field(
        default=None,
        max_length=128,
        description="Optional name override; otherwise slugified from description.",
    )
    auto_seed_kb: bool = Field(
        default=False,
        description="When true, seed a starter KB context via the unified KB ingest endpoint.",
    )
    include_evals: bool = Field(
        default=False,
        description="When true, generate an eval set via the Eval Generator.",
    )
    include_judge: bool = Field(
        default=False,
        description="When true, generate a judge agent via the Judge Engineer.",
    )
    budget_usd: float | None = Field(
        default=None,
        ge=0.0,
        description="Optional cost ceiling for the LLM authoring pipeline.",
    )


class AgentCreateCatalogRequest(BaseModel):
    """``source: "catalog"`` — clone-and-decouple from the agent catalog.

    Looks up an entry in the movate catalog (or the tenant's private
    namespace), unpacks the bundle, applies overrides, optionally
    renames, and persists as a NEW agent — no auto-sync back to the
    source (ADR 041 D6 decision: catalog clones are decoupled).
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["catalog"] = "catalog"
    slug: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Catalog slug (movate or private namespace).",
    )
    version: str | None = Field(
        default=None,
        description="Optional version pin; defaults to the catalog entry's latest.",
    )
    rename_to: str | None = Field(
        default=None,
        max_length=128,
        description="Rename the cloned agent. Defaults to the catalog slug.",
    )
    overrides: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional partial-merge overrides applied to the cloned "
            "agent.yaml. E.g. ``{'model': {'provider': '...'}}``."
        ),
    )


# Discriminated-union body — Pydantic v2 picks the right concrete
# request type from the ``source`` field. We exclude the bundle variant
# from JSON dispatch because its bytes arrive via multipart; the route
# handler sniffs ``Content-Type`` and chooses the multipart path first.
AgentCreateJsonRequest = Annotated[
    AgentCreateSpecRequest
    | AgentCreateWizardRequest
    | AgentCreateLlmRequest
    | AgentCreateCatalogRequest,
    Field(discriminator="source"),
]


class AgentCreateAccepted(BaseModel):
    """Async response for ``source: "llm"`` — 202 Accepted.

    The job runs the multi-stage authoring pipeline behind the scenes;
    the caller subscribes to ``stream_url`` for SSE progress events
    and/or polls ``status_url`` for terminal state.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status_url: str = Field(
        ...,
        description="URL to poll for terminal state (mirrors ``GET /jobs/{job_id}``).",
    )
    stream_url: str = Field(
        ...,
        description="SSE endpoint streaming the authoring pipeline events.",
    )


class UnifiedAgentCreatedView(BaseModel):
    """Sync response for non-llm sources.

    Identical to :class:`AgentCreatedView` in shape but adds
    ``source`` (so the UI can tell which path produced the agent)
    and ``project_id`` (which project the agent landed in).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source: Literal["bundle", "spec", "wizard", "catalog"]
    project_id: str
    agent_name: str
    version: str
    description: str = ""
    agent_dir: str
    files_persisted: list[str]
    published_version: str | None = None
    changed: bool = True
    attached: bool = Field(
        default=False,
        description=(
            "Whether the agent was attached to the project via the "
            "projects-storage layer. ``False`` when the storage "
            "backend doesn't yet implement ``attach_agent_to_project`` "
            "(degrades cleanly per the dependency note in the PR body)."
        ),
    )
    id: str | None = None
    """Uniform created-resource id (ADR 061 D2) — the agent's ``agent_name``,
    exposed under the common ``id`` key so a client can treat any created
    resource uniformly. Additive; ``agent_name`` stays."""
    created_at: datetime | None = None
    """Create-response timestamp (UTC, ADR 061 D2). Aligns the agent-create
    envelope with the project-create one, which already carries it."""
    etag: str | None = None
    """Content-hash concurrency token for the published bundle (ADR 061 D2) —
    the registry ``content_hash``. A true content ETag; ``None`` when the
    registry write degraded (the agent is still persisted to the FS)."""
    links: dict[str, str] = Field(default_factory=dict, alias="_links")
    """Hypermedia next-calls (ADR 061 D1), serialized as ``_links``: ``self``,
    ``validate``, ``kb``, ``publish``, ``run``, ``versions`` — the build→ship→run
    path. Empty ``{}`` for callers/paths that don't populate it."""


# ---------------------------------------------------------------------------
# Workflow API parity (ADR 037 D1) — wire types for ``/api/v1/workflows``.
#
# Mirrors the agent counterparts above row-for-row so the Angular client's
# generated TypeScript types stay symmetric between the two resource kinds.
# ``WorkflowSpec`` itself (the YAML schema) is intentionally NOT changed by
# this PR — the wire types here only describe the registry envelope around
# the bundle.
# ---------------------------------------------------------------------------


class WorkflowCreateRequest(BaseModel):
    """``POST /api/v1/workflows`` JSON request body (file-list mode).

    Mirrors :class:`WizardAgentSubmission`'s role for agents — the
    non-multipart, JSON-friendly creation surface for clients that don't
    want to deal with multipart-form encoding. The caller passes the raw
    ``workflow.yaml`` text plus any other files (``schema/state.json``,
    ``evals/dataset.jsonl``) keyed by relative path.

    Multipart mode (``bundle`` upload OR individual file fields) is
    served by the same endpoint and documented separately; this is the
    pure-JSON shape.
    """

    model_config = ConfigDict(extra="forbid")

    workflow_yaml: str = Field(..., description="The raw text of the workflow.yaml file.")
    files: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Extra files keyed by canonical relative path (e.g. "
            "``schema/state.json`` or ``evals/dataset.jsonl``). Workflow "
            "bundles are narrower than agent bundles — only ``workflow.yaml`` "
            "is required at the root; sibling files live under ``schema/`` "
            "or ``evals/``."
        ),
    )


class WorkflowView(BaseModel):
    """One entry in the ``GET /api/v1/workflows`` catalog response.

    Discovery shape — name + version + description metadata so the
    Angular catalog can render a workflow tile without a follow-up
    ``GET /api/v1/workflows/{name}``. Mirrors :class:`AgentCatalogItemView`.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    """The CURRENT latest version (newest by ``created_at``)."""
    description: str = ""
    published_version: str | None = None
    """The version with ``published=True``, when any. Distinct from
    ``version`` so the UI can show "blessed != latest" drift (ADR 037 D1)."""
    tags: list[str] = []
    created_at: datetime
    """When the latest version was published."""


class WorkflowListResponse(BaseModel):
    """``GET /api/v1/workflows`` response — list of workflows for the tenant."""

    model_config = ConfigDict(extra="forbid")

    workflows: list[WorkflowView]
    count: int


class WorkflowVersionView(BaseModel):
    """One row in the durable workflow-registry version history.

    Mirrors :class:`AgentVersionView`. Distinct from a workflow RUN —
    this is the *definition* version history (the registry's immutable
    ``(name, version)`` rows), surfaced by
    ``GET /api/v1/workflows/{name}/versions``.
    """

    model_config = ConfigDict(extra="forbid")

    version: str
    created_by: str | None = None
    created_at: datetime
    content_hash: str
    is_current: bool = False
    """True for the newest version (the one a versionless resolve picks
    up). Exactly one row in a non-empty history is current."""
    is_published: bool = False
    """True for the version whose ``published`` flag is set (ADR 037 D1).
    At most one row in a non-empty history is published. Distinct from
    ``is_current`` so a UI can show "blessed != latest" drift."""


class WorkflowVersionsView(BaseModel):
    """``GET /api/v1/workflows/{name}/versions`` response."""

    model_config = ConfigDict(extra="forbid")

    name: str
    versions: list[WorkflowVersionView]
    count: int


class WorkflowDetailView(BaseModel):
    """``GET /api/v1/workflows/{name}`` response.

    The workflow analogue of :class:`AgentDetailView` — everything the
    Angular workflow-profile view needs in one round-trip. Includes the
    parsed spec metadata, the canonical files manifest, and the registry
    audit (``content_hash``, ``created_by``, ``created_at``).

    NOT included (deferred to other endpoints):

    * Run history — that's ``GET /api/v1/workflow-runs?workflow={name}``
    * Per-node trace — that's ``GET /api/v1/runs/{id}/trace`` (ADR 024)
    """

    model_config = ConfigDict(extra="forbid")

    # --- Identity + metadata ---
    name: str
    version: str
    description: str = ""
    owner: str = ""
    tags: list[str] = []

    # --- Spec snapshot ---
    entrypoint: str
    """ID of the starting node in the workflow."""
    state_schema_path: str
    """Path to the state JSON schema, relative to workflow.yaml."""
    nodes: list[dict[str, Any]] = []
    """Parsed nodes from the workflow.yaml (untyped dicts so the wire
    contract doesn't drift when the discriminated-union spec evolves)."""
    edges: list[dict[str, Any]] = []
    """Parsed edges."""

    # --- Registry audit ---
    content_hash: str
    created_by: str | None = None
    created_at: datetime
    published_version: str | None = None
    """The currently-published version (ADR 037 D1)."""
    is_published: bool = False
    """Whether ``version`` itself is the published one."""

    # --- Canonical layout ---
    files: list[str]
    """Sorted list of bundle file paths (e.g. ``["workflow.yaml",
    "schema/state.json"]``)."""


class WorkflowCreatedView(BaseModel):
    """``POST /api/v1/workflows`` response — canonical layout + spec."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    description: str = ""
    workflow_dir: str
    files_persisted: list[str]
    published_version: str | None = None
    """Registry version now serving as ``latest`` (may differ from
    ``version`` on a content-vs-version collision — mirrors the agent
    response). ``None`` only if the registry write was unavailable."""
    changed: bool = True
    """Whether this publish wrote new content (False for a no-op re-deploy
    with byte-identical files)."""


class WorkflowUpdatedView(BaseModel):
    """``PUT /api/v1/workflows/{name}`` response."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    description: str = ""
    workflow_dir: str
    files_persisted: list[str]
    previous_version: str
    published_version: str | None = None
    changed: bool = True


class WorkflowDeletedView(BaseModel):
    """``DELETE /api/v1/workflows/{name}`` response."""

    model_config = ConfigDict(extra="forbid")

    name: str
    deleted_dir: str


class WorkflowRevertSubmission(BaseModel):
    """``POST /api/v1/workflows/{name}/revert`` request body."""

    model_config = ConfigDict(extra="forbid")

    to_version: str
    """The existing version to roll back to. 404 if no such version
    exists for this workflow in this tenant."""


class WorkflowRevertedView(BaseModel):
    """``POST /api/v1/workflows/{name}/revert`` response."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    """The new latest version after the revert."""
    reverted_from: str
    """The prior version whose bundle was re-published."""
    previous_version: str
    """Version that was latest immediately BEFORE this revert."""


class WorkflowPublishedView(BaseModel):
    """``POST /api/v1/workflows/{name}/publish`` response (ADR 037 D1).

    Confirms the soft promote: the named version's ``published`` flag is
    now ``True`` and every other version of the same name is now ``False``.
    ``previous_published_version`` lets the UI label "promoted v0.3.0
    (was v0.2.1)" without a second round-trip.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    published_version: str
    previous_published_version: str | None = None


class WorkflowValidationIssue(BaseModel):
    """One finding from ``POST /api/v1/workflows/{name}/validate``.

    Mirrors :class:`AgentValidationIssue` so the Angular UI's chip
    rendering is symmetric across resources.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    severity: str
    """One of ``"error"`` or ``"warning"``."""
    message: str
    hint: str = ""


class WorkflowValidationView(BaseModel):
    """``POST /api/v1/workflows/{name}/validate`` response.

    Mirrors :class:`AgentValidationView`. Returns the structural-validation
    findings (Pydantic + compiler — duplicate ids, missing entrypoint,
    dangling edges). Cost forecast is omitted today (workflows have no
    fixed token estimate; ADR 029 D4 will add this).
    """

    model_config = ConfigDict(extra="forbid")

    passed: bool
    errors: list[WorkflowValidationIssue]
    warnings: list[WorkflowValidationIssue]


# ---------------------------------------------------------------------------
# Usage metering (ADR 036 D1) — ``GET /api/v1/usage``
#
# The billing-visibility companion to the agent-health ``/api/v1/report``
# feed: per-tenant counters (requests, tokens, cost) over a time window, with
# optional by-agent / by-provider breakdowns. Mirrors the ``ReportView`` style
# (typed Pydantic shell over the ``core.reporting`` dataclasses) so the front
# end + OpenAPI spec stay rich.
# ---------------------------------------------------------------------------


class UsageRollupView(BaseModel):
    """A single grouped usage row — one agent, one provider, or the totals.

    ``key`` carries the grouping value: tenant_id for the totals row, the
    agent name for ``by_agent`` rows, the provider/model id for
    ``by_provider`` rows. Empty string ``""`` is the deliberate sentinel for
    older records that didn't capture the value — distinguishable from a
    genuine "(unknown)" so the front end can render it explicitly.
    """

    model_config = ConfigDict(extra="forbid")

    key: str = Field(
        ...,
        description=(
            "Grouping value: tenant_id (totals), agent name (by_agent), or "
            "provider id (by_provider). '' = older record with no captured value."
        ),
    )
    requests: int = Field(0, description="Count of runs in the window.")
    tokens_in: int = Field(0, description="Sum of metrics.tokens.input.")
    tokens_out: int = Field(0, description="Sum of metrics.tokens.output.")
    cost_usd: float = Field(
        0.0,
        description=(
            "Sum of metrics.cost_usd — the **estimated** cost from pricing.yaml, "
            "NOT the actual provider invoice (ADR 036 §Risks)."
        ),
    )

    @classmethod
    def from_dataclass(cls, r: UsageRollup) -> UsageRollupView:
        return cls(
            key=r.key,
            requests=r.requests,
            tokens_in=r.tokens_in,
            tokens_out=r.tokens_out,
            cost_usd=r.cost_usd,
        )


class UsageView(BaseModel):
    """``GET /api/v1/usage`` response (ADR 036 D1).

    Per-tenant usage rollup over the requested time window — the billing /
    spend-visibility feed. Built from the same per-run records the agent-health
    report uses (ADR 024 ``RunRecord.metrics``); no new measurement plumbing.

    Tenant scoping: non-admin keys always see their own tenant; admin keys may
    pass ``tenant=<id>`` to read another tenant's rollup. Empty window → a
    zeroed rollup (200), not a 500 — billing surfaces a $0 month explicitly.

    NOTE: cost is the **estimated** cost from ``pricing.yaml`` at run time, NOT
    the actual provider invoice. ADR 036 D3 (billing export) will document the
    estimate↔actual gap when it ships.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(..., description="The tenant this rollup describes.")
    window_days: int = Field(
        30,
        description="Time window in days; 0 = all-time. Default 30.",
    )
    agent_filter: str | None = Field(
        None,
        description="Set when the caller scoped the rollup to a single agent.",
    )
    totals: UsageRollupView
    by_agent: list[UsageRollupView]
    by_provider: list[UsageRollupView]

    @classmethod
    def from_usage(cls, usage: Usage) -> UsageView:
        return cls(
            tenant_id=usage.tenant_id,
            window_days=usage.window_days,
            agent_filter=usage.agent_filter,
            totals=UsageRollupView.from_dataclass(usage.totals),
            by_agent=[UsageRollupView.from_dataclass(r) for r in usage.by_agent],
            by_provider=[UsageRollupView.from_dataclass(r) for r in usage.by_provider],
        )


# ---------------------------------------------------------------------------
# Describe / preview agent (ADR 032 D1) — wire types for
# ``POST /api/v1/agents/preview``. The endpoint runs the same LLM-scaffold
# generate+validate pipeline ``mdk init --llm`` runs (shared in
# :mod:`movate.core.scaffold_preview`) and returns the candidate WITHOUT
# committing it to disk or the runtime's storage. The front end commits via
# the existing ``POST /agents`` / ``POST /agents/from-wizard`` once the
# operator accepts the preview.
# ---------------------------------------------------------------------------


# Caps applied at the API layer so a misbehaving / malicious client can't drag
# the endpoint into long requests or oversized provider calls. Description is
# capped at 4 KB — long enough for a paragraph of intent, well below the
# meta-prompt budget. Names track AgentSpec's regex (lowercase alphanumeric +
# hyphens) at the load layer; we only bound length here.
_DESCRIBE_MAX_DESCRIPTION_CHARS = 4000
_DESCRIBE_MAX_NAME_LEN = 64
_DESCRIBE_MAX_MODEL_LEN = 128


class DescribeAgentRequest(BaseModel):
    """``POST /api/v1/agents/preview`` request body (ADR 032 D1).

    Describes an agent in natural language and returns the generated candidate
    (``agent.yaml`` + ``prompt.md`` + schemas + sample evals + cost forecast)
    so the Mova iO front end can preview before committing. The endpoint is
    read-only — nothing is written to disk or to the runtime's storage. The
    front end commits via the existing ``POST /agents`` / ``POST
    /agents/from-wizard`` after preview.
    """

    model_config = ConfigDict(extra="forbid")

    description: str = Field(
        ...,
        min_length=1,
        max_length=_DESCRIBE_MAX_DESCRIPTION_CHARS,
        description=(
            "Natural-language description of the agent. Capped at "
            f"{_DESCRIBE_MAX_DESCRIPTION_CHARS} characters at the API "
            "boundary so an oversized payload fails fast instead of "
            "ballooning the provider call."
        ),
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=_DESCRIBE_MAX_NAME_LEN,
        description=(
            "Slug the candidate's agent.yaml will declare. Same charset as "
            "the canonical AgentSpec (lowercase alphanumeric + hyphens)."
        ),
    )
    model: str | None = Field(
        default=None,
        max_length=_DESCRIBE_MAX_MODEL_LEN,
        description=(
            "LiteLLM-style model id that DRIVES the scaffold call (e.g. "
            "``openai/gpt-4o-mini-2024-07-18``). When unset, the runtime "
            "picks the same default ``mdk init --llm`` uses."
        ),
    )
    target_model: str | None = Field(
        default=None,
        max_length=_DESCRIBE_MAX_MODEL_LEN,
        description=(
            "Optional model string to embed in the GENERATED agent.yaml's "
            "``model.provider``. Defaults to ``model`` when unset — useful "
            "when the operator wants the scaffold driven by a cheap model "
            "but the resulting agent to run on a more capable one."
        ),
    )
    mock: bool = Field(
        default=False,
        description=(
            "When true, the deterministic ``MockProvider`` is used instead "
            "of a real LLM call. Useful for an offline preview / "
            "smoke-test from the UI. No tokens spent; cost is reported as "
            "the mock's zero usage."
        ),
    )
    dry_run: bool = Field(
        default=True,
        description=(
            "Reserved for forward-compat with a future ``persist`` option. "
            "Today the endpoint is always read-only — passing ``false`` "
            "still does not persist; commit via ``POST /agents`` instead. "
            "The field exists so the front end can pin its intent in the "
            "request log."
        ),
    )


class DescribeAgentTokenUsageView(BaseModel):
    """Token-usage view: input, output, cached-input (rolled over attempts).

    Mirrors :class:`~movate.core.models.TokenUsage` as a flat, OpenAPI-friendly
    response field so client codegen doesn't need to follow the persisted
    model.
    """

    model_config = ConfigDict(extra="forbid")

    input: int = 0
    output: int = 0
    cached_input: int = 0


class DescribeAgentResponse(BaseModel):
    """``POST /api/v1/agents/preview`` response body (ADR 032 D1).

    Carries the validated candidate plus the cost forecast. The candidate is
    a runnable agent bundle on the wire — the front end renders it
    (``agent_yaml`` + ``prompt_md`` + schemas + sample evals) and commits via
    ``POST /agents`` when the operator accepts.

    ``preview`` is always ``true`` for now (the endpoint is read-only). It's
    surfaced so a future ``persist`` mode (deferred per ADR 032) can flip it
    to ``false`` without churning the schema.
    """

    model_config = ConfigDict(extra="forbid")

    preview: bool = Field(
        True,
        description=(
            "Always ``true`` today — the endpoint never persists. Reserved "
            "for a future persist mode (ADR 032 deferred)."
        ),
    )
    name: str = Field(..., description="Agent slug the candidate declares.")
    target_model: str = Field(
        ...,
        description=(
            "Model string the candidate's ``agent.yaml`` declares "
            "(``model.provider``). May differ from the model that drove "
            "generation."
        ),
    )
    agent_yaml: dict[str, Any] = Field(
        ...,
        description="The candidate ``agent.yaml`` contents.",
    )
    prompt_md: str = Field(..., description="The candidate Jinja prompt template body.")
    input_schema: dict[str, Any] = Field(
        ..., description="JSON Schema 2020-12 for the input contract."
    )
    output_schema: dict[str, Any] = Field(
        ..., description="JSON Schema 2020-12 for the output contract."
    )
    sample_evals: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "2-3 sample dataset rows the meta-prompt asks for. Empty list "
            "is legal but produces a ``missing-evals`` audit finding when "
            "the committed agent runs ``mdk audit``."
        ),
    )
    tokens: DescribeAgentTokenUsageView = Field(
        ...,
        description=(
            "Token usage rolled across every LLM call (attempt 1 + retry) "
            "so the cost reflects total spend."
        ),
    )
    cost_usd: float | None = Field(
        None,
        description=(
            "Cost in USD looked up via the shipped pricing table. ``null`` "
            "when the model isn't in the table — the front end renders "
            "that as 'N/A' rather than failing the preview."
        ),
    )
    retried: bool = Field(
        False,
        description=(
            "True if attempt 2 fired (a retry happened). The front end may "
            "use this to flag slightly-flakier-than-usual scaffolds."
        ),
    )


# ---------------------------------------------------------------------------
# Eval generator — ``POST /api/v1/agents/{name}/evals/generate``
#
# Wire shapes for the new generator job pattern (review-then-commit). The
# persisted shape lives in :class:`movate.core.eval_generator.EvalGenerationJob`;
# these are the HTTP-facing views, kept separate so the wire surface can
# evolve independently of storage (same convention as JobView vs JobRecord).
# ---------------------------------------------------------------------------


class EvalGenerateRequest(BaseModel):
    """``POST /api/v1/agents/{name}/evals/generate`` request body.

    Defaults mirror the eval-generator module constants: ``count=20``,
    all three canonical categories, no judge, no budget cap. ``model``
    is optional — when omitted, the route handler uses the target
    agent's declared provider. ``budget_usd`` is a hard server-side
    ceiling; the pipeline aborts cleanly if cost crosses it.
    """

    model_config = ConfigDict(extra="forbid")

    description: str = Field(..., min_length=1, max_length=8000)
    """Plain-English agent description Claude uses to author the cases."""
    count: int = Field(default=20, ge=1, le=100)
    """How many cases to generate. ``100`` is the hard cap; the
    eval-generator module floors at ``1``."""
    categories: list[str] | None = Field(default=None)
    """Categories to include. ``None`` / empty defaults to all three
    (``happy`` / ``edge`` / ``adversarial``). Unknown categories →
    422 from the route handler."""
    include_judge: bool = Field(default=False)
    """When ``True`` an extra LLM call drafts a ``judge.yaml`` rubric."""
    model: str | None = Field(default=None)
    """Optional provider override (LiteLLM-style string). ``None`` ⇒
    use the target agent's declared model."""
    budget_usd: float | None = Field(default=None, ge=0.0)
    """Hard server-side cost ceiling. ``None`` ⇒ no cap."""


class GeneratedEvalCaseView(BaseModel):
    """One generated eval case on the wire.

    Mirrors :class:`movate.core.eval_generator.GeneratedEvalCase`
    1:1; kept in the runtime schema module so the OpenAPI spec
    advertises this exact shape to the Angular client.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    category: str
    input: dict[str, Any]
    expected: dict[str, Any] | None = None
    rationale: str


class PreviewScoreView(BaseModel):
    """Informational ``--mock`` pass rate after generation.

    Computed by the route handler running each generated case against
    the agent under the mock provider. Doesn't fail the job — just
    surfaces a sanity-check number the operator can use to spot a
    schema-mismatched case set before they commit.
    """

    model_config = ConfigDict(extra="forbid")

    mock_pass_rate: float = Field(..., ge=0.0, le=1.0)
    tested_against_model: str = "mock"


class EvalGenerationResultView(BaseModel):
    """The terminal ``result`` payload of a completed generation job."""

    model_config = ConfigDict(extra="forbid")

    cases: list[GeneratedEvalCaseView]
    judge_yaml: str | None = None
    preview_score: PreviewScoreView | None = None


class EvalGenerateJobView(BaseModel):
    """``GET /api/v1/jobs/{job_id}`` response for an eval-generation job.

    Separate ``kind`` discriminator (``evals_generate``) from
    :class:`JobView` so the Angular client can route between job-detail
    views by the kind alone. The poll path is the same as for queue
    jobs, but the response shape carries the generated cases (the
    primary deliverable) instead of a ``result_run_id`` pointer.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = "evals_generate"
    job_id: str
    agent_name: str
    status: str  # running | completed | failed
    progress: float = Field(0.0, ge=0.0, le=1.0)
    result: EvalGenerationResultView | None = None
    error: dict[str, Any] | None = None
    tokens_used: int = 0
    cost_usd: float = 0.0


class EvalGenerateAcceptedView(BaseModel):
    """``POST /api/v1/agents/{name}/evals/generate`` response (202).

    The client polls ``status_url`` for progress (or subscribes to
    ``stream_url`` for SSE). ``estimated_seconds`` is a rough hint
    based on category count + per-call latency — not a guarantee.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: str = "running"
    estimated_seconds: int
    status_url: str
    stream_url: str


class EvalCommitRequest(BaseModel):
    """``POST /api/v1/jobs/{job_id}/commit`` request body.

    ``case_ids`` selective acceptance: ``None`` / omitted ⇒ commit
    every case in the job. ``commit_judge`` writes the drafted
    ``judge.yaml`` alongside; ignored when the job didn't draft one.
    """

    model_config = ConfigDict(extra="forbid")

    case_ids: list[str] | None = None
    commit_judge: bool = False


class EvalCommitView(BaseModel):
    """``POST /api/v1/jobs/{job_id}/commit`` response.

    Mirrors :class:`movate.storage.base.EvalCommitResult` — flat
    shape, no envelope, so the Angular client can render the
    "cases committed" toast directly.
    """

    model_config = ConfigDict(extra="forbid")

    agent_name: str
    dataset_path: str
    cases_added: int
    judge_yaml_updated: bool


# ---------------------------------------------------------------------------
# Failure Pattern Diagnoser (ADR 043 D1)
#
# POST /api/v1/agents/{name}/diagnose       eval  (async; returns 202)
# GET  /api/v1/diagnoses/{diagnosis_id}     read  (poll for the result)
#
# Read-only with respect to agent state: the wire layer NEVER carries an
# apply-side action. The typed-fix discriminated union below mirrors ADR
# 043's seven fix kinds so the apply step in a later PR can dispatch on
# ``kind`` without remapping. Adding a new kind here is an ADR 043
# amendment, not a casual addition.
# ---------------------------------------------------------------------------


class DiagnoseRequest(BaseModel):
    """``POST /api/v1/agents/{name}/diagnose`` request body.

    Caps + flags are conservative defaults; tightening them keeps the
    diagnoser's spend predictable for a tenant with a high failure rate.
    ``budget_usd`` is the HARD cap — the diagnoser short-circuits before
    the LLM call if the pre-call estimate exceeds it.
    """

    model_config = ConfigDict(extra="forbid")

    window_days: int = Field(30, ge=1, le=365)
    """Lookback window. Failures older than this are ignored."""
    min_failure_count: int = Field(5, ge=1, le=1000)
    """Cluster floor — clusters smaller than this are dropped client-side
    when rendering. The diagnoser still considers them so a small but
    high-confidence cluster (e.g. a confirmed security regression) isn't
    silently lost."""
    include_canary_misses: bool = Field(True)
    include_eval_failures: bool = Field(True)
    include_drift_detections: bool = Field(True)
    max_clusters: int = Field(10, ge=1, le=50)
    """Cap on clusters returned. Bigger numbers cost more tokens."""
    model: str | None = Field(None)
    """Optional provider/model override (e.g. ``"openai/gpt-4o-mini"``).
    Defaults to the runtime's diagnoser default."""
    budget_usd: float = Field(1.0, gt=0.0, le=100.0)
    """Hard spend cap. The diagnoser raises if its pre-call estimate
    exceeds this; the request fails with ``status="error"``."""


class DiagnoseAcceptedView(BaseModel):
    """``POST /api/v1/agents/{name}/diagnose`` response (202).

    The diagnose work runs asynchronously inside the runtime process
    (FastAPI background task) — no new worker / dispatch path. Poll
    ``status_url`` until ``status == "completed"`` (or ``"error"``).
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    """The diagnosis id — usable as ``GET /api/v1/diagnoses/{id}``.
    Named ``job_id`` on the wire to match the ADR 043 D1 spec."""
    status: str = "running"
    status_url: str
    """Absolute path to poll for the result (``/api/v1/diagnoses/{id}``)."""


# ---- Proposed-fix discriminated union (seven typed kinds) -----------------


class _ProposedFixBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rationale: str
    """One-paragraph why-this-fix explanation."""
    expected_improvement: dict[str, Any] = Field(default_factory=dict)
    """Optional ``{"metric": str, "delta": number, "based_on": str}``.
    Empty dict when the diagnoser couldn't estimate (no fabrication)."""


class PromptEditDiffView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    before: str = ""
    after: str = ""
    patch_text: str = ""


class PromptEditFixView(_ProposedFixBase):
    """Apply a unified diff to the agent's ``prompt.md``."""

    kind: Literal["prompt_edit"] = "prompt_edit"
    diff: PromptEditDiffView = Field(default_factory=PromptEditDiffView)


class KbIngestPayloadView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    """The unified KB-ingest target kind, e.g. ``"url"`` / ``"file"``."""
    source: str


class KbIngestFixView(_ProposedFixBase):
    """Ingest a new KB source via the unified KB ingest endpoint."""

    kind: Literal["kb_ingest"] = "kb_ingest"
    payload: KbIngestPayloadView


class ContextAddFixView(_ProposedFixBase):
    """Add a named context body to the agent."""

    kind: Literal["context_add"] = "context_add"
    name: str
    body: str


class ContextRemoveFixView(_ProposedFixBase):
    """Remove a named context from the agent."""

    kind: Literal["context_remove"] = "context_remove"
    name: str


class ModelSwapFixView(_ProposedFixBase):
    """Swap the agent's provider/model to a different one."""

    kind: Literal["model_swap"] = "model_swap"
    provider: str


class TemperatureChangeFixView(_ProposedFixBase):
    """Adjust the agent's sampling temperature by ``delta``."""

    kind: Literal["temperature_change"] = "temperature_change"
    delta: float


class RetrievalKChangeFixView(_ProposedFixBase):
    """Adjust the agent's retrieval top-K by ``delta`` (integer)."""

    kind: Literal["retrieval_k_change"] = "retrieval_k_change"
    delta: int


ProposedFixView = (
    PromptEditFixView
    | KbIngestFixView
    | ContextAddFixView
    | ContextRemoveFixView
    | ModelSwapFixView
    | TemperatureChangeFixView
    | RetrievalKChangeFixView
)


class FailureClusterView(BaseModel):
    """One root-cause cluster with its typed fix proposal."""

    model_config = ConfigDict(extra="forbid")

    id: str
    summary: str
    example_count: int = Field(..., ge=0)
    example_run_ids: list[str] = Field(default_factory=list)
    """Representative source ids — run_ids for the RUN/CANARY sources,
    eval_ids for EVAL/DRIFT. Capped at 5 for response-size hygiene."""
    confidence: Literal["high", "medium", "low"] = "medium"
    proposed_fix: ProposedFixView = Field(..., discriminator="kind")


class DiagnoseInputSummaryView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_failures_examined: int = Field(..., ge=0)
    clusters_identified: int = Field(..., ge=0)
    examples_per_cluster_max: int = Field(..., ge=0)


class DiagnoseResultView(BaseModel):
    """The structured analysis the GET endpoint returns when complete."""

    model_config = ConfigDict(extra="forbid")

    input_summary: DiagnoseInputSummaryView
    clusters: list[FailureClusterView] = Field(default_factory=list)


class DiagnoseJobView(BaseModel):
    """``GET /api/v1/diagnoses/{id}`` response.

    Mirror of :class:`DiagnosisRecord` minus ``tenant_id`` (audit-only,
    never returned over the wire — same convention as ``JobView`` /
    ``RunView``). ``result`` is populated when ``status == "completed"``;
    ``error`` is populated when ``status == "error"``.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["diagnose"] = "diagnose"
    job_id: str
    """Mirrors :attr:`DiagnosisRecord.diagnosis_id` on the wire as
    ``job_id`` to match ADR 043 D1's spec."""
    agent_name: str
    status: DiagnosisStatus
    result: DiagnoseResultView | None = None
    error: ErrorInfo | None = None
    tokens_used: int = Field(0, ge=0)
    cost_usd: float = Field(0.0, ge=0.0)
    model: str = ""
    created_at: datetime
    completed_at: datetime | None = None

    @classmethod
    def from_record(cls, record: DiagnosisRecord) -> DiagnoseJobView:
        result_view: DiagnoseResultView | None = None
        if record.result is not None:
            # Persisted as an opaque dict so the typed-fix taxonomy can
            # evolve without a DB migration. Validate at the wire edge
            # against the discriminated union — anything malformed is a
            # bug surface (a downgrade of the schema, an operator
            # manually editing the row) rather than user input.
            result_view = DiagnoseResultView.model_validate(record.result)
        return cls(
            job_id=record.diagnosis_id,
            agent_name=record.agent,
            status=record.status,
            result=result_view,
            error=record.error,
            tokens_used=record.tokens_used,
            cost_usd=record.cost_usd,
            model=record.model,
            created_at=record.created_at,
            completed_at=record.completed_at,
        )


# ---------------------------------------------------------------------------
# Claude-orchestrated audit endpoints (POST /api/v1/agents/{name}/audit/from-llm
# + POST /api/v1/projects/{id}/audit/from-llm). Read-only by construction; the
# Auditor (movate.core.auditor) never mutates the agent registry.
# ---------------------------------------------------------------------------


class AuditRequest(BaseModel):
    """``POST /api/v1/agents/{name}/audit/from-llm`` request body.

    All fields are optional — an empty body runs every category at the
    default severity floor against the tenant's default model. The
    ``categories`` field is validated against the auditor's open
    vocabulary in :data:`movate.core.auditor.CATEGORIES` server-side;
    unknown categories are silently dropped (logged at warning).
    """

    model_config = ConfigDict(extra="forbid")

    categories: list[str] | None = Field(
        default=None,
        description=(
            "Categories to run. Default is all seven the auditor ships: "
            "ambiguous_prompts, missing_eval_coverage, security_smells, "
            "cost_outliers, kb_quality, schema_drift, model_choice."
        ),
    )
    severity_floor: AuditFindingSeverity = Field(
        default=AuditFindingSeverity.INFO,
        description="Findings below this severity are filtered out before persistence.",
    )
    model: str | None = Field(
        default=None,
        description=(
            "Override the audit sub-agents' provider string (LiteLLM-style, "
            "e.g. 'openai/gpt-4o-mini'). Defaults to the tenant's audit "
            "model — currently 'openai/gpt-4o-mini'."
        ),
    )
    budget_usd: float = Field(
        default=1.0,
        ge=0.0,
        description=(
            "Server-side spend cap. ``0.0`` disables the cap. When the "
            "running spend would exceed this, remaining categories are "
            "skipped + the audit is marked ``partial=true``."
        ),
    )


class AuditAcceptedView(BaseModel):
    """``POST /api/v1/agents/{name}/audit/from-llm`` response (202).

    Async by default — returns immediately with ``job_id`` + the URLs
    the client polls (``status_url``) and streams progress from
    (``stream_url``). The final findings live at
    ``GET /api/v1/audits/{audit_id}`` once the job's ``result_run_id``
    is populated.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: str = "queued"
    status_url: str
    """Pre-built ``GET /api/v1/jobs/{job_id}`` URL the client polls."""
    stream_url: str
    """Pre-built ``GET /api/v1/jobs/{job_id}/stream`` SSE URL for
    incremental progress events. Audit-only — non-audit jobs do not
    expose this endpoint."""


class AuditFindingLocationView(BaseModel):
    """Wire shape for :class:`movate.core.models.AuditFindingLocation`."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    line: int | None = None
    path: str | None = None
    chunk_id: str | None = None


class FindingView(BaseModel):
    """Wire shape for one :class:`movate.core.models.AuditFinding`.

    Mirrors :class:`AuditFinding`; kept separate so the wire and the
    durable model can evolve independently (same convention as
    :class:`JobView` vs :class:`JobRecord`).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    category: str
    severity: AuditFindingSeverity
    agent_name: str
    location: AuditFindingLocationView | None = None
    title: str
    description: str
    suggestion: str
    confidence: str = "medium"

    @classmethod
    def from_finding(cls, f: AuditFinding) -> FindingView:
        loc: AuditFindingLocationView | None = None
        if f.location is not None:
            loc = AuditFindingLocationView(
                kind=f.location.kind,
                line=f.location.line,
                path=f.location.path,
                chunk_id=f.location.chunk_id,
            )
        return cls(
            id=f.id,
            category=f.category,
            severity=f.severity,
            agent_name=f.agent_name,
            location=loc,
            title=f.title,
            description=f.description,
            suggestion=f.suggestion,
            confidence=f.confidence,
        )


class AuditSummaryView(BaseModel):
    """Headline counts for one completed audit.

    Both rollups are dicts (open key sets) so adding a new category or
    severity doesn't require a schema bump on consumers.
    """

    model_config = ConfigDict(extra="forbid")

    total_findings: int
    by_category: dict[str, int]
    by_severity: dict[str, int]


class AuditScopeView(BaseModel):
    """``{"type": "agent" | "project", "id": "..."}``."""

    model_config = ConfigDict(extra="forbid")

    type: str
    id: str


class AuditJobView(BaseModel):
    """``GET /api/v1/audits/{audit_id}`` response — the rich, completed view.

    The standard ``GET /api/v1/jobs/{job_id}`` continues to return
    :class:`JobView` (back-compat); this is the dedicated audit view a
    completed audit's findings live on.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = "audit"
    audit_id: str
    scope: AuditScopeView
    status: str = "completed"
    categories: list[str]
    severity_floor: AuditFindingSeverity
    model: str
    budget_usd: float
    findings: list[FindingView]
    summary: AuditSummaryView
    partial: bool = False
    tokens_used: int = 0
    cost_usd: float = 0.0

    @classmethod
    def from_record(cls, record: AuditRecord) -> AuditJobView:
        by_category: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        for f in record.findings:
            by_category[f.category] = by_category.get(f.category, 0) + 1
            by_severity[f.severity.value] = by_severity.get(f.severity.value, 0) + 1
        return cls(
            kind="audit",
            audit_id=record.audit_id,
            scope=AuditScopeView(type=record.scope_kind, id=record.scope_id),
            status="completed",
            categories=list(record.categories),
            severity_floor=record.severity_floor,
            model=record.model,
            budget_usd=record.budget_usd,
            findings=[FindingView.from_finding(f) for f in record.findings],
            summary=AuditSummaryView(
                total_findings=len(record.findings),
                by_category=by_category,
                by_severity=by_severity,
            ),
            partial=record.partial,
            tokens_used=record.tokens_used,
            cost_usd=record.cost_usd,
        )
