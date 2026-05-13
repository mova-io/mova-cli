"""FastAPI app factory.

``build_app(storage)`` is the single entry point — tests build one per
test case with an :class:`InMemoryStorage`; ``movate serve`` builds
one with a :class:`SqliteProvider`. Storage is passed in (not built
inside) so the same factory works for every backend without env-var
gymnastics.

v0.5 stage 3a endpoints:

* ``GET /healthz`` — unauthed liveness check.
* ``POST /run`` — queue a job, return ``{"job_id", "status": "queued"}``.
* ``GET /jobs/{id}`` — poll a job; tenant-scoped (a tenant can never
  see another tenant's job, even with a valid key in the wrong env).

Deferred to stage 3b: ``GET /agents`` (needs an agent registry layer)
and ``movate serve`` CLI binding (uvicorn integration).
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, File, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import movate
from movate.core.loader import AgentBundle
from movate.core.models import JobRecord, JobStatus
from movate.core.rate_limit import InProcessRateLimiter, NoOpRateLimiter, RateLimiter
from movate.runtime.agent_creation import (
    AgentCreationError,
    persist_bundle,
    unzip_bundle,
)
from movate.runtime.errors import auth_required, not_found
from movate.runtime.middleware import AuthContext, make_auth_dependency
from movate.runtime.registry import scan_agents
from movate.runtime.schemas import (
    AgentCreatedView,
    AgentDatasetInfo,
    AgentDetailView,
    AgentListView,
    AgentValidationCostForecast,
    AgentValidationIssue,
    AgentValidationView,
    AgentView,
    HealthView,
    JobListView,
    JobView,
    ReadyView,
    RunAccepted,
    RunSubmission,
    RunView,
)
from movate.storage.base import StorageProvider


def _resolve_cors_origins(explicit: list[str] | None) -> list[str]:
    """Pick the effective CORS allow-list, in priority order:

    1. ``explicit`` (passed via ``build_app(cors_allowed_origins=...)``
       — primarily for tests).
    2. ``MDK_CORS_ALLOWED_ORIGINS`` env var (comma-separated, e.g.
       ``"http://localhost:4200,https://mova-io.movate.com"``).
    3. ``MOVATE_CORS_ALLOWED_ORIGINS`` env var (legacy alias).
    4. Empty list — no CORS middleware mounted (server-to-server or
       same-origin only; browser clients from other hosts will fail).

    A single ``"*"`` entry enables permissive CORS — fine for local
    dev, NEVER do this in staging/prod because ``allow_credentials=True``
    with ``*`` is rejected by browsers per the CORS spec.
    """
    if explicit is not None:
        return explicit
    raw = os.environ.get("MDK_CORS_ALLOWED_ORIGINS") or os.environ.get(
        "MOVATE_CORS_ALLOWED_ORIGINS", ""
    )
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


async def _collect_bundle_files(
    *,
    agent_yaml: UploadFile | None,
    prompt: UploadFile | None,
    input_schema: UploadFile | None,
    output_schema: UploadFile | None,
    dataset: UploadFile | None,
    bundle: UploadFile | None,
) -> dict[str, bytes]:
    """Convert the multipart form fields into a
    ``{canonical_path: bytes}`` dict :func:`persist_bundle` accepts.

    Enforces the two-mode contract: EITHER ``bundle`` OR the four
    individual files, never both, never neither. 400 with a clear
    pointer at the conflict on either error.
    """
    individual = [agent_yaml, prompt, input_schema, output_schema, dataset]
    has_individual = any(f is not None for f in individual)

    if bundle is not None and has_individual:
        raise AgentCreationError(
            "supply EITHER a zipped 'bundle' OR individual files "
            "(agent_yaml + prompt + input_schema + output_schema + "
            "optional dataset), not both",
            status_code=400,
        )
    if bundle is None and not has_individual:
        raise AgentCreationError(
            "no files in the multipart form; supply either a zipped "
            "'bundle' or the individual canonical files",
            status_code=400,
        )

    if bundle is not None:
        return unzip_bundle(await bundle.read())

    # Individual-files mode. Re-check the required fields are present
    # — the FastAPI param defaults make them all optional at the route
    # level, but the bundle contract requires the canonical 4.
    required = {
        "agent.yaml": agent_yaml,
        "prompt.md": prompt,
        "schema/input.json": input_schema,
        "schema/output.json": output_schema,
    }
    missing = [name for name, f in required.items() if f is None]
    if missing:
        raise AgentCreationError(
            f"individual-files mode requires {sorted(required)}; missing: {sorted(missing)}",
            status_code=400,
        )

    files: dict[str, bytes] = {}
    for canonical_path, upload in required.items():
        assert upload is not None  # narrowed by the missing-check above
        files[canonical_path] = await upload.read()
    if dataset is not None:
        files["evals/dataset.jsonl"] = await dataset.read()
    return files


def _agent_creation_error_code(status_code: int) -> str:
    """Map HTTP status to a stable error code the Angular client can
    branch on. Keeps the wire contract independent of the human-readable
    message (which may change as we improve the diagnostics).
    """
    return {
        400: "bad_request",
        409: "already_exists",
        422: "invalid_bundle",
        503: "agent_persistence_unavailable",
    }.get(status_code, "internal_error")


def _render_agent_validation(bundle: AgentBundle) -> AgentValidationView:
    """Build the ``AgentValidationView`` for
    ``POST /api/v1/agents/{name}/validate``.

    Runs the prompt linter + cost forecast against the bundle. The
    bundle itself was already validated structurally at load time
    (via ``load_agent()``) — by the time it's in the registry, it
    parsed cleanly. This endpoint surfaces the SOFT checks the CLI
    surfaces via ``mdk validate``: prompt-template hygiene and an
    eval-cost forecast.

    Pure function — no I/O beyond what the linter + forecaster
    already do. Safe to call repeatedly; cheap.
    """
    from movate.core.cost_forecast import estimate_eval_cost  # noqa: PLC0415
    from movate.core.prompt_linter import lint_prompt  # noqa: PLC0415
    from movate.providers.pricing import load_pricing  # noqa: PLC0415

    # Severity is a typing.Literal["error", "warning"] (NOT an Enum) —
    # compare against the bare strings.
    issues = lint_prompt(bundle)
    errors = [
        AgentValidationIssue(
            code=i.code,
            severity=i.severity,
            message=i.message,
            hint=i.hint,
        )
        for i in issues
        if i.severity == "error"
    ]
    warnings = [
        AgentValidationIssue(
            code=i.code,
            severity=i.severity,
            message=i.message,
            hint=i.hint,
        )
        for i in issues
        if i.severity == "warning"
    ]

    # Cost forecast — None when the agent has no dataset, or when the
    # pricing table doesn't know the agent's model. Wrap defensively
    # so a missing pricing.yaml doesn't 500 the endpoint.
    forecast_view: AgentValidationCostForecast | None = None
    try:
        forecast = estimate_eval_cost(bundle, pricing=load_pricing())
        if forecast is not None:
            forecast_view = AgentValidationCostForecast(
                model_provider=forecast.model_provider,
                cases=forecast.cases,
                input_tokens_per_call=forecast.input_tokens_per_call,
                output_tokens_per_call=forecast.output_tokens_per_call,
                cost_per_call_usd=forecast.cost_per_call_usd,
                total_cost_usd=forecast.total_cost_usd,
            )
    except Exception:  # pragma: no cover — defensive
        # Pricing-table load failure shouldn't sink validate.
        forecast_view = None

    return AgentValidationView(
        passed=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        cost_forecast=forecast_view,
    )


def _render_agent_detail(bundle: AgentBundle) -> AgentDetailView:
    """Build the ``AgentDetailView`` for ``GET /api/v1/agents/{name}``.

    Reads dataset stats lazily (computed only if the dataset file
    actually exists; ``None`` otherwise). Lists canonical files that
    physically exist on disk — the UI's "files in this agent" view
    should reflect reality, not the abstract canonical layout.

    Pure function — no I/O beyond reading the dataset bytes for
    digest/count + listing the bundle dir. Trivially testable.
    """
    import hashlib  # noqa: PLC0415

    spec = bundle.spec
    agent_dir = bundle.agent_dir

    # Dataset info — read once, compute digest + line count.
    dataset_info: AgentDatasetInfo | None = None
    if spec.evals.dataset:
        ds_path = (agent_dir / spec.evals.dataset).resolve()
        if ds_path.exists() and ds_path.is_file():
            raw = ds_path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()[:12]
            # Count non-empty lines — what mdk eval would walk.
            count = sum(1 for line in raw.decode().splitlines() if line.strip())
            dataset_info = AgentDatasetInfo(
                path=spec.evals.dataset,
                case_count=count,
                sha256_prefix=digest,
                size_bytes=len(raw),
            )

    # Canonical files that ACTUALLY exist on disk. Walks one level
    # deep (matches scan_agents' depth convention).
    candidate_files = [
        "agent.yaml",
        "prompt.md",
        "schema/input.json",
        "schema/output.json",
        "evals/dataset.jsonl",
    ]
    files = sorted(f for f in candidate_files if (agent_dir / f).exists())

    # Prompt body — read from disk so the response is self-contained.
    # Same path the AgentBundle.render_prompt() goes through, but we
    # want the raw template (no Jinja substitution).
    prompt_path = (agent_dir / spec.prompt).resolve()
    prompt_body = prompt_path.read_text() if prompt_path.exists() else ""

    return AgentDetailView(
        name=spec.name,
        version=spec.version,
        description=spec.description,
        owner=spec.owner,
        role=spec.role,
        persona=spec.persona,
        capabilities=list(spec.capabilities),
        tags=list(spec.tags),
        model_provider=spec.model.provider,
        model_params=dict(spec.model.params) if spec.model.params else {},
        model_fallback=[fb.provider for fb in spec.model.fallback] if spec.model.fallback else [],
        runtime=spec.runtime.value,
        prompt=prompt_body,
        prompt_hash=bundle.prompt_hash,
        input_schema=bundle.input_schema,
        output_schema=bundle.output_schema,
        skills=list(spec.skills),
        contexts=list(spec.contexts),
        dataset=dataset_info,
        timeout_call_ms=spec.timeouts.call_ms,
        timeout_total_ms=spec.timeouts.total_ms,
        max_cost_usd_per_run=spec.budget.max_cost_usd_per_run,
        agent_dir=agent_dir.name,
        files=files,
    )


def build_app(
    storage: StorageProvider,
    *,
    agents: list[AgentBundle] | None = None,
    agents_path: Path | None = None,
    rate_limit_per_minute: int | None = 60,
    cors_allowed_origins: list[str] | None = None,
) -> FastAPI:
    """Build the FastAPI app bound to ``storage`` + ``agents``.

    ``agents`` is the registry returned by :func:`scan_agents`. Scan
    happens once at app build time so each ``GET /agents`` is a
    constant-time list lookup, not a fresh disk walk. Pass ``None``
    (the default) for tests that don't care about the registry.

    ``rate_limit_per_minute`` is the per-API-key token-bucket
    capacity (and the steady-state allowed request rate). Default
    60. Pass ``None`` to disable rate limiting entirely (uses a
    :class:`NoOpRateLimiter` that always allows).

    The app's ``state`` carries collaborators so handlers can read
    them without closing over the factory's locals — keeps
    testability clean (override ``app.state.storage`` /
    ``state.agents`` / ``state.rate_limiter`` to swap mid-test if
    you really need to).
    """
    app = FastAPI(
        title="movate",
        version=movate.__version__,
        description="Declarative platform for building and running AI agents.",
    )
    app.state.storage = storage
    app.state.agents = agents or []
    # Where new agents (POST /api/v1/agents, item 76) land on disk.
    # None means the endpoint returns 503 — the runtime was built
    # without an agents_path and can't persist. mdk serve always
    # passes its --agents-path here; tests pass tmp_path.
    app.state.agents_path = agents_path

    # CORS — required for browser-side callers (the Mova iO Angular
    # app). Allow-list resolved from the explicit kwarg, then env vars,
    # then empty (= middleware not mounted). The wildcard ``"*"`` is
    # supported but only fully works with ``allow_credentials=False``
    # — browsers reject ``*`` + credentials per the CORS spec. For
    # bearer-token auth (which we use) credentials don't need to ride
    # on cookies, so ``allow_credentials=False`` is the correct default.
    # Operators with cookie-based session auth (future) flip credentials
    # on AND pin the origin list to exact hosts.
    origins = _resolve_cors_origins(cors_allowed_origins)
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
            allow_headers=["*"],
            # X-RateLimit-* + Retry-After need to be readable by browser
            # JS so the Angular client can show a "you'll be rate-limited
            # in N seconds" hint. Without expose_headers, CORS strips them.
            expose_headers=[
                "X-RateLimit-Limit",
                "X-RateLimit-Remaining",
                "X-RateLimit-Reset",
                "Retry-After",
            ],
        )

    # Build the rate limiter once at app construction so bucket state
    # persists across requests. NoOp when disabled, but the middleware
    # still calls .check() — keeps the header path uniform.
    limiter: RateLimiter
    if rate_limit_per_minute is None or rate_limit_per_minute <= 0:
        limiter = NoOpRateLimiter()
    else:
        limiter = InProcessRateLimiter(limit_per_minute=rate_limit_per_minute)
    app.state.rate_limiter = limiter

    auth_dep = make_auth_dependency(storage, rate_limiter=limiter)

    # ------------------------------------------------------------------
    # /healthz — unauthed liveness probe
    # ------------------------------------------------------------------
    @app.get("/healthz", response_model=HealthView, tags=["meta"])
    async def healthz() -> HealthView:
        """Liveness probe. Cheap on purpose — never hits storage.

        ACA's liveness probe restarts a pod if this fails. We
        deliberately don't gate on DB connectivity here because a DB
        blip would otherwise trigger a pod restart that doesn't help
        (the new pod will hit the same dead DB). Use ``/ready`` for
        readiness; let liveness stay simple.
        """
        return HealthView(status="ok", version=movate.__version__)

    # ------------------------------------------------------------------
    # /ready — unauthed readiness probe with deep checks
    # ------------------------------------------------------------------
    @app.get(
        "/ready",
        response_model=ReadyView,
        tags=["meta"],
        responses={503: {"model": ReadyView}},
    )
    async def ready(request: Request) -> Response:
        """Readiness probe with deep checks.

        ACA's readiness probe stops routing traffic to a pod when
        this fails (but doesn't restart it — that's liveness's job).
        We check the dependencies whose failure would make every
        request 5xx: storage backend connectivity, primarily.

        Returns 200 with ``{"status": "ready", "checks": {...}}`` on
        the happy path; 503 with ``{"status": "not_ready", "checks":
        {"storage": "<error>"}}`` if any check fails. The HTTP
        status is what ACA reads; the JSON body is for human triage
        and curl-by-hand debugging.
        """
        store: StorageProvider = request.app.state.storage
        checks: dict[str, str] = {}
        # Storage ping — covers DB-down, pool-exhausted, network-blip,
        # sqlite-file-missing. Any backend error here means real
        # queries will fail too, so the pod shouldn't get traffic.
        try:
            await store.ping()
            checks["storage"] = "ok"
        except Exception as exc:
            # Surface the exception class + a truncated message. We
            # don't want to leak DSNs or other internals, but the
            # class name + short message is operator-actionable.
            checks["storage"] = f"{type(exc).__name__}: {str(exc)[:120]}"

        all_ok = all(v == "ok" for v in checks.values())
        body = ReadyView(
            status="ready" if all_ok else "not_ready",
            version=movate.__version__,
            checks=checks,
        )
        return JSONResponse(
            status_code=200 if all_ok else 503,
            content=body.model_dump(),
        )

    # ------------------------------------------------------------------
    # GET /agents — registry discovery
    # ------------------------------------------------------------------
    @app.get("/agents", response_model=AgentListView, tags=["meta"])
    async def list_agents(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> AgentListView:
        """List agents available on this runtime.

        Auth-required for consistency (every non-healthz endpoint
        gates on a key); discovery is per-runtime, not per-tenant in
        v0.5 — every authenticated tenant sees the same catalog.
        Per-tenant agent visibility lands when a customer asks for it.

        Returns metadata only (name, version, description). The full
        agent definition lives on disk; this endpoint is for ``what
        can I call?``, not for fetching prompts or schemas.
        """
        _ = ctx  # auth gate; tenant attribution lives in logs/spans
        agents: list[AgentBundle] = request.app.state.agents
        return AgentListView(
            agents=[
                AgentView(
                    name=b.spec.name,
                    version=b.spec.version,
                    description=b.spec.description,
                )
                for b in agents
            ]
        )

    # ------------------------------------------------------------------
    # POST /run — queue a job
    # ------------------------------------------------------------------
    @app.post("/run", response_model=RunAccepted, tags=["jobs"], status_code=202)
    async def submit_run(
        body: RunSubmission,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> RunAccepted:
        """Queue a job for the worker to claim.

        Returns ``202 Accepted`` (not ``201 Created``) — the resource
        being created is the *job*, but it's not yet executed; clients
        poll ``/jobs/{id}`` until terminal. The 202 status code makes
        that distinction wire-visible.
        """
        job = JobRecord(
            job_id=str(uuid4()),
            tenant_id=ctx.tenant_id,
            kind=body.kind,
            target=body.target,
            status=JobStatus.QUEUED,
            input=body.input,
            api_key_id=ctx.api_key_id,
            notify_email=body.notify_email,
        )
        store: StorageProvider = request.app.state.storage
        await store.save_job(job)
        return RunAccepted(job_id=job.job_id, status=job.status)

    # ------------------------------------------------------------------
    # GET /jobs/{id} — poll
    # ------------------------------------------------------------------
    @app.get("/jobs", response_model=JobListView, tags=["jobs"])
    async def list_jobs(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        status: JobStatus | None = None,
        limit: int = 20,
    ) -> JobListView:
        """Return this tenant's recent jobs, newest first.

        Always tenant-scoped — there's no cross-tenant variant on
        this endpoint. ``status`` filters to one terminal/transient
        state; omit for "all states". ``limit`` is hard-capped at 100
        to keep the response bounded; deeper history goes through
        ``movate logs`` against the local sqlite (operator path)."""
        capped_limit = max(1, min(limit, 100))
        store: StorageProvider = request.app.state.storage
        records = await store.list_jobs(
            tenant_id=ctx.tenant_id,
            status=status,
            limit=capped_limit,
        )
        views = [JobView.from_record(r) for r in records]
        return JobListView(jobs=views, count=len(views))

    @app.get("/jobs/{job_id}", response_model=JobView, tags=["jobs"])
    async def get_job(
        job_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> JobView:
        """Return job state. Tenant-scoped at the SQL layer
        (``get_job(..., tenant_id=...)`` filters in WHERE) so a
        cross-tenant lookup returns ``None`` and we 404 — never 403,
        which would leak the existence of the id."""
        store: StorageProvider = request.app.state.storage
        record = await store.get_job(job_id, tenant_id=ctx.tenant_id)
        if record is None:
            raise not_found("job", job_id)
        return JobView.from_record(record)

    @app.get("/runs/{run_id}", response_model=RunView, tags=["runs"])
    async def get_run(
        run_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> RunView:
        """Return a single run including its ``output``.

        Companion to ``GET /jobs/{id}`` — ``JobView`` only carries the
        ``result_run_id`` pointer, not the actual agent output. Callers
        that want to *see* what the agent produced fetch the job, read
        ``result_run_id``, then hit this endpoint. Same tenant-scoping
        story as jobs: 404 on cross-tenant access (never 403, which
        would leak that the id exists)."""
        store: StorageProvider = request.app.state.storage
        record = await store.get_run(run_id, tenant_id=ctx.tenant_id)
        if record is None:
            raise not_found("run", run_id)
        return RunView.from_record(record)

    # ------------------------------------------------------------------
    # /api/v1/* — versioned API surface for the Mova iO Angular front
    # end (BACKLOG Group G item 52).
    #
    # Routing convention:
    #   * Pre-v1 endpoints above (/healthz, /ready, /agents, /run,
    #     /jobs/*, /runs/*) stay UNVERSIONED for back-compat — they
    #     shipped before the versioning policy was set, and existing
    #     `mdk submit` callers + the Teams bot depend on the URLs.
    #   * NEW resource-oriented endpoints land here, under /api/v1.
    #   * Breaking changes bump to /api/v2 (new router); additive
    #     changes (new endpoints, new optional fields, new enum values
    #     in non-discriminator positions) DON'T bump.
    #
    # The router is mounted unconditionally — empty for now, populated
    # as Group G items 55-75 land. Mounting the empty router today
    # means new endpoint PRs are pure-additive (no FastAPI wiring
    # churn) and the OpenAPI spec already exposes the /api/v1 prefix
    # for the Angular team's client generator.
    # ------------------------------------------------------------------
    v1 = APIRouter(prefix="/api/v1")

    @v1.post(
        "/agents",
        response_model=AgentCreatedView,
        status_code=201,
        tags=["agents-v1"],
    )
    async def v1_create_agent(
        request: Request,
        # Individual-files mode. Each field is optional at the FastAPI
        # level; we enforce "either bundle XOR the 4 required individual
        # files" in the handler body for a clean 422 with a hint.
        agent_yaml: UploadFile | None = File(default=None),
        prompt: UploadFile | None = File(default=None),
        input_schema: UploadFile | None = File(default=None),
        output_schema: UploadFile | None = File(default=None),
        dataset: UploadFile | None = File(default=None),
        # Zipped-bundle mode. Mutually exclusive with the individual
        # fields. The zip may contain a single top-level dir
        # (e.g. ``faq-bot/agent.yaml``) — unzip_bundle strips it.
        bundle: UploadFile | None = File(default=None),
        ctx: AuthContext = Depends(auth_dep),
    ) -> AgentCreatedView:
        """Create a new agent from a multipart-form bundle.

        Two input modes (mutually exclusive — pick ONE):

        1. **Individual files** — set ``agent_yaml`` + ``prompt`` +
           ``input_schema`` + ``output_schema``, optionally ``dataset``.
        2. **Zipped bundle** — set ``bundle`` to a .zip of the canonical
           layout.

        Persists to ``<agents_path>/<name>/`` using the canonical
        directory structure (item 76 / BACKLOG Group G). Validates via
        the same ``load_agent()`` path the CLI uses — bundles that
        fail Pydantic / prompt linter / schema sanity get rejected
        with a 422 before anything lands on disk.

        Returns the canonical layout in the response so the Angular UI
        can render "your agent is at agents/<name>/{...}" without a
        follow-up GET.

        Auth: requires a bearer token (any role). Tenant attribution
        lives on the auth context for future per-tenant agent
        isolation (deferred to v0.8 — today the runtime serves one
        global agents_path).

        Errors:

        * **400** — neither mode supplied OR both modes supplied
        * **409** — agent with this name already exists; use PUT to update
        * **422** — bundle failed validation (parse / linter / schema)
        * **503** — runtime was built without an ``agents_path`` (test
          configuration; production deploys always pass it)
        """
        agents_path: Path | None = request.app.state.agents_path
        if agents_path is None:
            raise AgentCreationError(
                "runtime was built without an agents_path; POST /api/v1/agents is unavailable",
                status_code=503,
            )

        files = await _collect_bundle_files(
            agent_yaml=agent_yaml,
            prompt=prompt,
            input_schema=input_schema,
            output_schema=output_schema,
            dataset=dataset,
            bundle=bundle,
        )

        result = persist_bundle(files, agents_path=agents_path)

        # Refresh the in-memory registry so an immediate GET /agents
        # sees the new bundle. Cheap — agents_path is a flat
        # one-level scan.
        request.app.state.agents = scan_agents(agents_path)

        # Tenant attribution is logged for the audit trail. Future
        # per-tenant filesystem isolation (v0.8) reads this back.
        # Reference ctx so the param isn't unused — and we record it
        # for the future audit log.
        _ = ctx.tenant_id

        spec = result.bundle.spec
        return AgentCreatedView(
            name=spec.name,
            version=spec.version,
            description=spec.description,
            agent_dir=result.agent_dir.name,
            files_persisted=result.files_persisted,
        )

    @v1.get(
        "/agents/{name}",
        response_model=AgentDetailView,
        tags=["agents-v1"],
    )
    async def v1_get_agent(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> AgentDetailView:
        """Return the full agent spec + bundle metadata for a single agent.

        Drives the Mova iO Angular agent-profile view: the user clicks
        an agent in the catalog and the UI fetches this single endpoint
        to render the spec, prompt body, schemas, dataset stats, model
        config, marketplace metadata (role/persona/capabilities), and
        the list of canonical files on disk.

        Source of truth is the in-memory registry populated at app
        build + refreshed after every successful ``POST /api/v1/agents``.
        Lookups are O(N) in registry size; for the typical tenant with
        < 100 agents that's a non-issue. We could index by name in a
        future revision if scan times start dominating.

        Errors:

        * **401** — missing / bad bearer token
        * **404** — agent not in the registry (never registered, or
          a different tenant's agent — today's runtime is global-
          scoped, so 404 just means "not found")
        """
        # Tenant scoping — today's runtime is single-tenant per
        # agents_path; future per-tenant filesystem isolation reads
        # ctx.tenant_id and walks <agents_path>/<tenant_id>/. The
        # reference here keeps the audit trail honest and prevents
        # ruff from flagging the param as unused.
        _ = ctx.tenant_id

        agents: list[AgentBundle] = request.app.state.agents
        bundle = next((b for b in agents if b.spec.name == name), None)
        if bundle is None:
            raise not_found("agent", name)
        return _render_agent_detail(bundle)

    @v1.post(
        "/agents/{name}/validate",
        response_model=AgentValidationView,
        tags=["agents-v1"],
    )
    async def v1_validate_agent(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> AgentValidationView:
        """Run the prompt linter + cost forecast for an agent.

        Drives the Mova iO Angular "is this agent shippable?" gate
        BEFORE the user clicks Publish or Run Eval. Returns:

        * ``passed: bool`` — green-checkmark shortcut (zero errors)
        * ``errors[]`` — block save (red chips)
        * ``warnings[]`` — informational (yellow chips, don't block)
        * ``cost_forecast`` — pricing-table estimate for the eval
          dataset; lets the UI render "running this eval will cost
          ~$0.45" alongside the Run Eval button

        Note: the structural validation (Pydantic parse + I/O schema
        sanity) already ran at POST /agents time — agents that don't
        pass that never make it into the registry. This endpoint is
        the SOFT validation layer: prompt-template hygiene + cost.

        Errors:

        * **401** — missing / bad bearer token
        * **404** — agent not in the registry
        """
        _ = ctx.tenant_id  # future per-tenant isolation
        agents: list[AgentBundle] = request.app.state.agents
        bundle = next((b for b in agents if b.spec.name == name), None)
        if bundle is None:
            raise not_found("agent", name)
        return _render_agent_validation(bundle)

    app.include_router(v1)

    # ------------------------------------------------------------------
    # Typed exception → HTTP code translator. AgentCreationError carries
    # the intended status_code; FastAPI's default handling would 500
    # everything otherwise.
    # ------------------------------------------------------------------
    @app.exception_handler(AgentCreationError)
    async def _agent_creation_error_handler(
        _request: Request, exc: AgentCreationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": {
                    "error": {
                        "code": _agent_creation_error_code(exc.status_code),
                        "message": str(exc),
                    }
                }
            },
        )

    return app


# Re-export for convenience — callers don't have to import the module
# just to suppress an "unused" lint on the auth helper above.
__all__ = ["auth_required", "build_app"]
