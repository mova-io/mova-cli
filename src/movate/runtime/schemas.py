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

from pydantic import BaseModel, ConfigDict, Field

from movate.core.models import ErrorInfo, JobKind, JobRecord, JobStatus, Metrics, RunRecord


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


__all__ = [
    "AgentCreatedView",
    "AgentDatasetInfo",
    "AgentDetailView",
    "AgentListView",
    "AgentValidationCostForecast",
    "AgentValidationIssue",
    "AgentValidationView",
    "AgentView",
    "HealthView",
    "JobListView",
    "JobView",
    "ReadyView",
    "RunAccepted",
    "RunSubmission",
    "RunView",
]
