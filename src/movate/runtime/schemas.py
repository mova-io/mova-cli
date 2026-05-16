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

    Eval kickoff config. Mirrors the ``mdk eval`` CLI's flag set —
    the Angular UI's "Run Eval" form translates to this shape.

    Note on execution: for v0.7 the eval runs synchronously inside
    the request handler (small datasets + mock mode = sub-second
    response). The wire contract — ``{eval_id, status}`` reply +
    ``GET /api/v1/evals/{eval_id}`` retrieval — is the same as the
    eventual async-job semantics (BACKLOG item 89), so the Angular
    client doesn't change when we move to the worker-queue path.
    """

    model_config = ConfigDict(extra="forbid")

    gate: float = Field(0.7, ge=0.0, le=1.0)
    """Per-case score required to pass (0.0-1.0)."""
    gate_mode: str = Field("mean")
    """How to aggregate N runs per case: ``mean``, ``min``, ``p10``."""
    runs: int = Field(1, ge=1, le=10)
    """Runs per case. Use 3+ for LLM-as-judge to defeat sampling variance."""
    mock: bool = Field(False)
    """Use the deterministic MockProvider (no API keys, fast).
    Required-true for the Friday demo path; real-LLM eval mode lands
    once we wire the async-worker path (item 89)."""
    baseline_id: str | None = Field(None)
    """Optional EvalRecord id to diff against; CLI exits 1 on
    regression beyond ``regression_tolerance``. Angular UI shows a
    regression badge."""
    regression_tolerance: float = Field(0.0, ge=0.0, le=1.0)
    objective: str | None = Field(None)
    """Optional objective id to filter cases by (matches
    agent.yaml: objectives[].id)."""


class EvalAcceptedView(BaseModel):
    """``POST /api/v1/agents/{name}/evals`` response.

    Identifies the persisted ``EvalRecord``. Angular polls
    ``GET /api/v1/evals/{eval_id}`` (item 84) to retrieve the
    full scorecard.

    ``status`` is ``"success"`` (sync execution succeeded) or
    ``"failed"`` (eval engine raised; check ``message``). Future
    async path adds ``"queued"`` / ``"running"`` for in-flight evals
    when the wire shape is identical but execution model differs.
    """

    model_config = ConfigDict(extra="forbid")

    eval_id: str
    status: str
    """``success`` | ``failed`` (v0.7 sync) or ``queued`` |
    ``running`` (future async)."""
    message: str = ""
    """Failure message when ``status == "failed"``; empty on success."""


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
    "AgentCommitView",
    "AgentCreatedView",
    "AgentDatasetInfo",
    "AgentDeletedView",
    "AgentDetailView",
    "AgentHistoryView",
    "AgentListView",
    "AgentPublishSubmission",
    "AgentPublishedView",
    "AgentRunSubmission",
    "AgentValidationCostForecast",
    "AgentValidationIssue",
    "AgentValidationView",
    "AgentView",
    "EvalAcceptedView",
    "EvalCaseView",
    "EvalListView",
    "EvalScorecardView",
    "EvalSubmission",
    "HealthView",
    "JobListView",
    "JobView",
    "ReadyView",
    "RunAccepted",
    "RunSubmission",
    "RunTraceView",
    "RunView",
    "SkillCreatedView",
    "WizardAgentSubmission",
]
