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
from movate.core.auth import mint_api_key
from movate.core.loader import AgentBundle
from movate.core.models import ApiKeyEnv, JobKind, JobRecord, JobStatus
from movate.core.rate_limit import InProcessRateLimiter, NoOpRateLimiter, RateLimiter
from movate.runtime.agent_creation import (
    AgentCreationError,
    persist_bundle,
    soft_delete_agent,
    split_skills_from_bundle,
    unzip_bundle,
    wizard_to_bundle_files,
)
from movate.runtime.errors import auth_required, forbidden, not_found
from movate.runtime.middleware import AuthContext, make_auth_dependency
from movate.runtime.registry import scan_agents
from movate.runtime.schemas import (
    AgentCatalogItemView,
    AgentCatalogView,
    AgentCommitView,
    AgentCreatedView,
    AgentDatasetInfo,
    AgentDatasetUploadView,
    AgentDeletedView,
    AgentDetailView,
    AgentHistoryView,
    AgentListView,
    AgentPublishedView,
    AgentPublishSubmission,
    AgentRunSubmission,
    AgentUpdatedView,
    AgentValidationCostForecast,
    AgentValidationIssue,
    AgentValidationView,
    AgentView,
    ApiKeyListView,
    ApiKeyMintedView,
    ApiKeyMintRequest,
    ApiKeyRevokedView,
    ApiKeyView,
    AuthWhoamiView,
    EvalAcceptedView,
    EvalListView,
    EvalScorecardView,
    EvalSubmission,
    HealthView,
    JobListView,
    JobView,
    ReadyView,
    RunAccepted,
    RunSubmission,
    RunTraceView,
    RunView,
    SkillCreatedView,
    WizardAgentSubmission,
)
from movate.runtime.skill_creation import (
    SkillCreationError,
    persist_skill_bundle,
)
from movate.storage.base import StorageProvider


def _github_is_enabled() -> bool:
    """Whether the GitHub integration is turned on.

    Mirrors :func:`movate.integrations.github.is_enabled` — duplicated
    here so ``build_app`` doesn't import the integrations subpackage
    just to read an env var (the integrations module's lazy-import
    contract is "no import unless you actually want the client")."""
    raw = os.environ.get("MDK_GITHUB_ENABLED", "").strip().lower()
    return raw in ("1", "true", "yes")


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
    contexts: list[UploadFile],
    kb: list[UploadFile],
) -> dict[str, bytes]:
    """Convert the multipart form fields into a
    ``{canonical_path: bytes}`` dict :func:`persist_bundle` accepts.

    Enforces the two-mode contract: EITHER ``bundle`` OR the four
    individual files, never both, never neither. 400 with a clear
    pointer at the conflict on either error.

    ``contexts`` is always optional — zero or more ``contexts/<name>.md``
    files uploaded via the repeating ``contexts`` multipart field. They
    are merged into the bundle regardless of which mode is used.
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
        files = unzip_bundle(await bundle.read())
    else:
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

        files = {}
        for canonical_path, upload in required.items():
            assert upload is not None  # narrowed by the missing-check above
            files[canonical_path] = await upload.read()
        if dataset is not None:
            files["evals/dataset.jsonl"] = await dataset.read()

    # Context files — optional, repeating field. Each upload is stored
    # under contexts/<basename> so the loader's two-tier resolution finds
    # them inside the agent dir without a shared project volume.
    for ctx_upload in contexts:
        raw_name = (ctx_upload.filename or "").lstrip("/")
        # Safety: only the basename, prefixed with contexts/. Reject
        # any name with path separators that could escape the dir.
        basename = Path(raw_name).name
        if not basename or ".." in basename:
            continue
        canonical = f"contexts/{basename}"
        files[canonical] = await ctx_upload.read()

    # KB corpus files — optional, repeating field. Stored under
    # kb/<basename> so resolve_kb_file() finds them via its agent-local
    # tier when the skill runs inside a deployed container.
    for kb_upload in kb:
        raw_name = (kb_upload.filename or "").lstrip("/")
        basename = Path(raw_name).name
        if not basename or ".." in basename:
            continue
        canonical = f"kb/{basename}"
        files[canonical] = await kb_upload.read()

    return files


def _agent_creation_error_code(status_code: int) -> str:
    """Map HTTP status to a stable error code the Angular client can
    branch on. Keeps the wire contract independent of the human-readable
    message (which may change as we improve the diagnostics).
    """
    return {
        400: "bad_request",
        404: "not_found",
        409: "already_exists",
        422: "invalid_bundle",
        502: "upstream_unavailable",
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


def _eval_record_to_view(record: object) -> EvalScorecardView:
    """Map an :class:`EvalRecord` to the wire view. Pulled out so
    the kickoff endpoint, retrieval endpoint, and list endpoint all
    use the same field-mapping logic.

    Takes ``object`` (not a typed EvalRecord) to keep this module's
    import footprint small — eval module is imported lazily at
    request time. mypy-strict elsewhere validates the actual call
    site via attribute access.
    """
    return EvalScorecardView(
        eval_id=record.eval_id,  # type: ignore[attr-defined]
        agent=record.agent,  # type: ignore[attr-defined]
        agent_version=record.agent_version,  # type: ignore[attr-defined]
        dataset_hash=record.dataset_hash,  # type: ignore[attr-defined]
        judge_method=record.judge_method.value,  # type: ignore[attr-defined]
        judge_provider=record.judge_provider,  # type: ignore[attr-defined]
        runs_per_case=record.runs_per_case,  # type: ignore[attr-defined]
        gate_mode=record.gate_mode,  # type: ignore[attr-defined]
        threshold=record.threshold,  # type: ignore[attr-defined]
        mean_score=record.mean_score,  # type: ignore[attr-defined]
        pass_rate=record.pass_rate,  # type: ignore[attr-defined]
        sample_count=record.sample_count,  # type: ignore[attr-defined]
        total_cost_usd=record.total_cost_usd,  # type: ignore[attr-defined]
        created_at=record.created_at.isoformat(),  # type: ignore[attr-defined]
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
    skills_path: Path | None = None,
    rate_limit_per_minute: int | None = 60,
    cors_allowed_origins: list[str] | None = None,
    github_client: object | None = None,
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
    # Where new skills (POST /api/v1/skills) land. Defaults to
    # ``<agents_path>/skills/`` so the agent loader's project-root
    # fallback (``agent_dir.parent`` when no project marker is found)
    # resolves to the same directory. Explicit skills_path overrides —
    # used by tests and operators who keep skills on a sibling volume.
    if skills_path is not None:
        app.state.skills_path = skills_path
    elif agents_path is not None:
        app.state.skills_path = agents_path / "skills"
    else:
        app.state.skills_path = None
    # GitHub integration (item 78 / ADR 007). Built lazily when
    # ``MDK_GITHUB_ENABLED=1`` so the typical runtime (no GitHub) pays
    # no cost. Tests pass a pre-built mock through ``github_client``.
    # ``None`` means the endpoint returns 503.
    if github_client is not None:
        app.state.github_client = github_client
    elif _github_is_enabled():
        try:
            from movate.integrations.github import (  # noqa: PLC0415
                GitHubClient,
                GitHubConfig,
            )

            app.state.github_client = GitHubClient(GitHubConfig.from_env())
        except Exception as exc:
            # A bad config shouldn't take the whole runtime down —
            # surface as "not configured" at the endpoint. Logged loud
            # so operators see what broke at boot.
            import logging  # noqa: PLC0415

            logging.getLogger(__name__).warning("github_integration_init_failed reason=%s", exc)
            app.state.github_client = None
    else:
        app.state.github_client = None

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
    # /api/v1/openapi.json — versioned alias (item 120)
    # ------------------------------------------------------------------
    # FastAPI emits the OpenAPI spec at the unversioned /openapi.json;
    # we keep that for backward compat AND expose a versioned alias so
    # client-gen tooling that expects every v1 path under /api/v1/* can
    # point at a consistent prefix. The alias returns the SAME spec —
    # not a v1-filtered subset — because the spec already self-describes
    # via the per-route ``/api/v1/...`` paths.
    @app.get(
        "/api/v1/openapi.json",
        include_in_schema=False,
        tags=["meta"],
    )
    async def openapi_v1_alias() -> JSONResponse:
        return JSONResponse(content=app.openapi())

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

        # Surface which backend was selected + whether it's durable
        # across container restarts. Drives `mdk doctor target` and
        # makes "Postgres intended, SQLite actually picked" debuggable
        # from a single HTTP call.
        from movate.storage import selected_backend  # noqa: PLC0415

        backend_info = selected_backend()
        storage_backend = backend_info[0] if backend_info else None
        storage_durable = backend_info[2] if backend_info else None

        all_ok = all(v == "ok" for v in checks.values())
        body = ReadyView(
            status="ready" if all_ok else "not_ready",
            version=movate.__version__,
            checks=checks,
            storage_backend=storage_backend,
            storage_durable=storage_durable,
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

    @v1.get(
        "/agents",
        response_model=AgentCatalogView,
        tags=["agents-v1"],
    )
    async def v1_list_agents(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        role: str | None = None,
        capabilities: str | None = None,
        tags: str | None = None,
    ) -> AgentCatalogView:
        """List all agents in the catalog with marketplace metadata.

        Supports optional query-param filters:

        * ``?role=support-triage`` — exact match on the agent's ``role``
          field (case-insensitive).
        * ``?capabilities=pii-detection,summarisation`` — comma-separated;
          agent must declare ALL listed capabilities (subset match).
        * ``?tags=acme,production`` — comma-separated; agent must carry
          ALL listed tags (subset match).

        Filters are ANDed. Omitting a filter returns all agents.

        Drives the Mova iO Angular Agent Catalog page — every card
        on the catalog is rendered from entries in this list.

        Errors:

        * **401** — missing / bad bearer token
        """
        _ = ctx.tenant_id  # future per-tenant isolation

        agents: list[AgentBundle] = request.app.state.agents

        # Normalise filter params.
        role_filter = role.lower().strip() if role else None
        cap_filter = (
            {c.strip().lower() for c in capabilities.split(",") if c.strip()}
            if capabilities
            else None
        )
        tag_filter = {t.strip().lower() for t in tags.split(",") if t.strip()} if tags else None

        items: list[AgentCatalogItemView] = []
        for b in agents:
            spec = b.spec
            if role_filter and spec.role.lower() != role_filter:
                continue
            if cap_filter:
                agent_caps = {c.lower() for c in spec.capabilities}
                if not cap_filter.issubset(agent_caps):
                    continue
            if tag_filter:
                agent_tags = {t.lower() for t in spec.tags}
                if not tag_filter.issubset(agent_tags):
                    continue
            items.append(
                AgentCatalogItemView(
                    name=spec.name,
                    version=spec.version,
                    description=spec.description,
                    owner=spec.owner,
                    role=spec.role,
                    persona=spec.persona,
                    capabilities=list(spec.capabilities),
                    tags=list(spec.tags),
                )
            )

        return AgentCatalogView(agents=items, count=len(items))

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
        # Context files — optional repeating field. Each upload is a
        # contexts/<name>.md that overrides the same-named entry at
        # the project level inside the deployed container.
        contexts: list[UploadFile] = File(default=[]),
        # KB corpus files — optional repeating field. Each upload is a
        # kb/<name>.json that resolve_kb_file() finds via its agent-local
        # tier when the deployed skill runs inside the container.
        kb: list[UploadFile] = File(default=[]),
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
            contexts=contexts,
            kb=kb,
        )

        # Pull any nested skills/<name>/ entries out of the agent
        # bundle and persist them to the global skill registry FIRST.
        # Customer scaffolds (mdk add rag-qa → skills/web-search/)
        # ship their skill folders inside the project zip; without
        # this split they'd 422 the next time an agent declares
        # `skills: [web-search]` ("empty registry"). Skills persist
        # with PUT semantics so re-deploy is idempotent.
        agent_files, skills_per_name = split_skills_from_bundle(files)
        if skills_per_name:
            skills_path: Path | None = request.app.state.skills_path
            if skills_path is None:
                raise AgentCreationError(
                    "bundle ships skills/<name>/ entries but the runtime "
                    "was built without a skills_path; upload skills "
                    "separately via POST /api/v1/skills or restart with "
                    "--skills-path set",
                    status_code=503,
                )
            for skill_name, skill_files in skills_per_name.items():
                # Skip skills that don't ship a skill.yaml — these are
                # incomplete scaffolds (e.g. only README.md present);
                # silently ignoring keeps deploy idempotent against
                # half-built projects.
                if "skill.yaml" not in skill_files:
                    continue
                persist_skill_bundle(skill_files, skills_path=skills_path)
                _ = skill_name  # used implicitly via persist_skill_bundle

        result = persist_bundle(agent_files, agents_path=agents_path)

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

    @v1.post(
        "/agents/from-wizard",
        response_model=AgentCreatedView,
        status_code=201,
        tags=["agents-v1"],
    )
    async def v1_create_agent_from_wizard(
        body: WizardAgentSubmission,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> AgentCreatedView:
        """Create a new agent from the Mova iO "Onboard Agent" wizard.

        Accepts the wizard's JSON shape (NOT multipart) and translates
        it into the canonical agent.yaml + prompt.md + default I/O
        schemas layout. Same persist path + response shape as the
        multipart ``POST /api/v1/agents`` — sibling endpoints, two
        wire shapes, one canonical contract on disk.

        Defaults applied:

        * **Schemas** — free-form ``{input: string}`` → ``{output: string}``.
          Agents needing richer I/O shapes use the multipart endpoint.
        * **Version** — ``0.1.0``. Future revisions bump via PUT
          (item 57) or via the GitHub publish flow (item 78).
        * **Marketplace metadata** — only emitted when the wizard
          populates the corresponding field. Empty fields stay unset
          in the YAML rather than serializing as empty strings.

        Field mapping documented in WizardAgentSubmission's docstring.

        Errors:

        * **400** — wizard name can't be slugified to a valid agent
          name (no alphanumeric characters)
        * **409** — agent with this name already exists
        * **422** — bundle failed validation post-translation (e.g.
          ``ai_model`` not in LiteLLM's recognized format)
        * **503** — runtime built without an ``agents_path``
        """
        agents_path: Path | None = request.app.state.agents_path
        if agents_path is None:
            raise AgentCreationError(
                "runtime was built without an agents_path; "
                "POST /api/v1/agents/from-wizard is unavailable",
                status_code=503,
            )

        # Translate wizard JSON → canonical bundle bytes. Slugification
        # of name happens here; downstream load_agent runs the same
        # Pydantic + linter checks the multipart path uses.
        files = wizard_to_bundle_files(body)

        result = persist_bundle(files, agents_path=agents_path)

        # Refresh the in-memory registry so GET /agents + GET /agents/{name}
        # see the new bundle immediately.
        request.app.state.agents = scan_agents(agents_path)

        _ = ctx.tenant_id  # future per-tenant audit log entry

        spec = result.bundle.spec
        return AgentCreatedView(
            name=spec.name,
            version=spec.version,
            description=spec.description,
            agent_dir=result.agent_dir.name,
            files_persisted=result.files_persisted,
        )

    @v1.post(
        "/skills",
        response_model=SkillCreatedView,
        status_code=201,
        tags=["skills-v1"],
    )
    async def v1_create_skill(
        request: Request,
        skill_yaml: UploadFile = File(...),
        impl: UploadFile | None = File(default=None),
        corpus: UploadFile | None = File(default=None),
        readme: UploadFile | None = File(default=None),
        ctx: AuthContext = Depends(auth_dep),
    ) -> SkillCreatedView:
        """Create or replace a skill bundle under ``<skills_path>/<name>/``.

        Fixes the long-standing gap where agents declaring
        ``skills: [<name>]`` 422'd on upload with "skills resolution
        failed: ... Available: (empty registry)". The runtime now owns
        a real skill registry that customers can populate via this
        endpoint OR implicitly via the deploy command (PR 3 in the
        same stack).

        Multipart fields:

        * ``skill_yaml`` (required) — the spec. ``name`` field inside
          determines the on-disk directory.
        * ``impl`` (optional) — Python implementation file.
        * ``corpus`` (optional) — JSON corpus shipped alongside.
        * ``readme`` (optional) — human-facing notes.

        PUT semantics: re-uploading the same skill name overwrites
        atomically. Skills are referenced by name from agents, so an
        operator who tweaked their skill and re-deploys expects the
        runtime to follow — different conflict policy from agents
        (which 409 on conflict because agent identity is sticky).

        Errors:

        * **401** — missing / bad bearer token
        * **422** — bundle failed validation (parse / schema / shape)
        * **503** — runtime was built without a ``skills_path``
        """
        skills_path: Path | None = request.app.state.skills_path
        if skills_path is None:
            raise SkillCreationError(
                "runtime was built without a skills_path; POST /api/v1/skills is unavailable",
                status_code=503,
            )

        files: dict[str, bytes] = {"skill.yaml": await skill_yaml.read()}
        if impl is not None:
            files["impl.py"] = await impl.read()
        if corpus is not None:
            files["corpus.json"] = await corpus.read()
        if readme is not None:
            files["README.md"] = await readme.read()

        result = persist_skill_bundle(files, skills_path=skills_path)

        _ = ctx.tenant_id  # future per-tenant audit log entry

        spec = result.bundle.spec
        return SkillCreatedView(
            name=spec.name,
            version=spec.version,
            description=spec.description or "",
            skill_dir=result.skill_dir.name,
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

    @v1.delete(
        "/agents/{name}",
        response_model=AgentDeletedView,
        tags=["agents-v1"],
    )
    async def v1_delete_agent(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> AgentDeletedView:
        """Soft-delete an agent (item 117 / Tier I-U).

        Moves the canonical bundle to a sibling
        ``.deleted-<name>-<timestamp>/`` directory under the runtime's
        agents_path. Recoverable out-of-band by the operator until a
        future cron sweep removes it (7-day retention window planned).

        Refreshes the in-memory agents registry so the very next
        ``GET /agents`` no longer surfaces the deleted agent.

        Tenant attribution is logged via ``ctx.tenant_id`` (future
        per-tenant filesystem isolation reads this back); today's
        runtime is single-tenant per agents_path.

        Errors:

        * **401** — missing / bad bearer token
        * **404** — agent dir doesn't exist at the runtime's
          agents_path
        * **500** — filesystem error (permissions, mount issues)
        * **503** — runtime built without an ``agents_path``
        """
        agents_path: Path | None = request.app.state.agents_path
        if agents_path is None:
            raise AgentCreationError(
                "runtime was built without an agents_path; "
                "DELETE /api/v1/agents/{name} is unavailable",
                status_code=503,
            )

        _ = ctx.tenant_id  # future per-tenant audit log entry

        result = soft_delete_agent(name, agents_path=agents_path)
        # Refresh registry so GET /agents reflects reality on the
        # next request — agent disappears immediately from the catalog.
        request.app.state.agents = scan_agents(agents_path)

        return AgentDeletedView(
            name=result.name,
            deleted_dir=result.deleted_dir.name,
        )

    @v1.put(
        "/agents/{name}",
        response_model=AgentUpdatedView,
        tags=["agents-v1"],
    )
    async def v1_update_agent(
        name: str,
        request: Request,
        agent_yaml: UploadFile | None = File(default=None),
        prompt: UploadFile | None = File(default=None),
        input_schema: UploadFile | None = File(default=None),
        output_schema: UploadFile | None = File(default=None),
        dataset: UploadFile | None = File(default=None),
        contexts: list[UploadFile] = File(default=[]),
        kb: list[UploadFile] = File(default=[]),
        bundle: UploadFile | None = File(default=None),
        ctx: AuthContext = Depends(auth_dep),
    ) -> AgentUpdatedView:
        """Replace an existing agent bundle in-place (item 57 / BACKLOG G).

        Accepts the same multipart form as ``POST /api/v1/agents`` (either
        individual files or a zipped bundle). The ``{name}`` path param
        must match the ``name`` field in the uploaded ``agent.yaml``;
        mismatches are rejected with 422.

        Differences from POST:

        * **404** if the agent does not already exist (use POST to create).
        * Existing bundle is atomically replaced — never leaves partial
          state on disk.
        * ``previous_version`` in the response lets the caller detect the
          diff without a round-trip.

        Skills bundled inside the upload are persisted to the global
        registry with PUT semantics (idempotent re-deploy).

        Errors:

        * **400** — neither mode supplied OR both modes supplied
        * **404** — agent ``{name}`` is not registered (never created)
        * **422** — bundle failed validation OR agent_yaml name ≠ path param
        * **503** — runtime built without an ``agents_path``
        """
        agents_path: Path | None = request.app.state.agents_path
        if agents_path is None:
            raise AgentCreationError(
                "runtime was built without an agents_path; "
                "PUT /api/v1/agents/{name} is unavailable",
                status_code=503,
            )

        # 404 guard — the agent must already exist before we'll replace it.
        agents: list[AgentBundle] = request.app.state.agents
        existing = next((b for b in agents if b.spec.name == name), None)
        if existing is None:
            raise not_found("agent", name)
        previous_version = existing.spec.version

        _ = ctx.tenant_id  # future per-tenant audit log

        files = await _collect_bundle_files(
            agent_yaml=agent_yaml,
            prompt=prompt,
            input_schema=input_schema,
            output_schema=output_schema,
            dataset=dataset,
            bundle=bundle,
            contexts=contexts,
            kb=kb,
        )

        # Extract + persist bundled skills first (same as POST).
        agent_files, skills_per_name = split_skills_from_bundle(files)
        if skills_per_name:
            skills_path: Path | None = request.app.state.skills_path
            if skills_path is None:
                raise AgentCreationError(
                    "bundle ships skills/<name>/ entries but the runtime "
                    "was built without a skills_path",
                    status_code=503,
                )
            for skill_name, skill_files in skills_per_name.items():
                if "skill.yaml" not in skill_files:
                    continue
                persist_skill_bundle(skill_files, skills_path=skills_path)
                _ = skill_name

        result = persist_bundle(agent_files, agents_path=agents_path, on_conflict="replace")
        request.app.state.agents = scan_agents(agents_path)

        spec = result.bundle.spec
        return AgentUpdatedView(
            name=spec.name,
            version=spec.version,
            description=spec.description,
            agent_dir=result.agent_dir.name,
            files_persisted=result.files_persisted,
            previous_version=previous_version,
        )

    @v1.post(
        "/agents/{name}/dataset",
        response_model=AgentDatasetUploadView,
        status_code=200,
        tags=["agents-v1"],
    )
    async def v1_upload_agent_dataset(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        file: UploadFile = File(...),
    ) -> AgentDatasetUploadView:
        """Upload or replace an agent's eval dataset (item 111 / Tier I-F).

        Accepts a ``multipart/form-data`` upload with a single field
        ``file`` containing a JSONL file — one JSON object per line.
        Writes the content to ``<agents_path>/<name>/evals/dataset.jsonl``,
        creating the ``evals/`` sub-directory if needed. Replaces any
        existing dataset atomically.

        Returns row count, a SHA-256 prefix for integrity checking, and
        a preview of the first up to three rows so the caller can confirm
        the upload was parsed correctly.

        Wizard-created agents have no dataset and can't be eval'd until
        this endpoint is called at least once.

        Errors:

        * **400** — file is not valid JSONL (non-object line detected)
        * **401** — missing / bad bearer token
        * **404** — agent not found in the runtime's agents_path
        * **503** — runtime built without an agents_path
        """
        import hashlib  # noqa: PLC0415
        import json  # noqa: PLC0415

        agents_path: Path | None = request.app.state.agents_path
        if agents_path is None:
            raise AgentCreationError(
                "runtime was built without an agents_path; "
                "POST /api/v1/agents/{name}/dataset is unavailable",
                status_code=503,
            )

        _ = ctx.tenant_id

        agent_dir = agents_path / name
        if not agent_dir.is_dir():
            raise not_found("agent", name)

        raw = await file.read()

        # Validate: every non-empty line must be a JSON object.
        rows: list[dict[str, object]] = []
        for lineno, raw_line in enumerate(raw.decode().splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AgentCreationError(
                    f"dataset line {lineno} is not valid JSON: {exc}",
                    status_code=400,
                ) from exc
            if not isinstance(obj, dict):
                raise AgentCreationError(
                    f"dataset line {lineno} must be a JSON object, got {type(obj).__name__}",
                    status_code=400,
                )
            rows.append(obj)

        evals_dir = agent_dir / "evals"
        evals_dir.mkdir(exist_ok=True)
        dataset_path = evals_dir / "dataset.jsonl"
        dataset_path.write_bytes(raw)

        sha256_prefix = hashlib.sha256(raw).hexdigest()[:12]
        preview = rows[:3]

        # Refresh registry so GET /agents/{name} reflects updated dataset stats.
        request.app.state.agents = scan_agents(agents_path)

        return AgentDatasetUploadView(
            agent_name=name,
            row_count=len(rows),
            sha256_prefix=sha256_prefix,
            preview=preview,
        )

    @v1.post(
        "/agents/{name}/publish",
        response_model=AgentPublishedView,
        tags=["agents-v1"],
    )
    async def v1_publish_agent(
        name: str,
        body: AgentPublishSubmission,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> AgentPublishedView:
        """Push the agent's canonical bundle to GitHub as one commit
        (item 78, ADR 007 decisions 1-4).

        Reads the on-disk bundle from the runtime's ``agents_path``,
        sends every file through the Git Data API in a single commit
        on the configured default branch, and returns the resulting
        commit SHA + URL.

        Behavior is gated on ``MDK_GITHUB_ENABLED=1`` + a valid
        GitHubConfig pulled from env (``MDK_GITHUB_APP_ID``,
        ``MDK_GITHUB_INSTALLATION_ID``, ``MDK_GITHUB_PRIVATE_KEY``,
        ``MDK_GITHUB_REPO``). When the flag is off the endpoint
        returns 503 — the runtime advertises the route in
        ``/openapi.json`` regardless so the Angular client can
        generate against it before the integration goes live.

        Tenant attribution: today's runtime trusts the env-supplied
        installation_id (one tenant per runtime). Multi-tenant
        installation lookup ships with item 81 (``mdk github
        bootstrap``).

        Errors:

        * **401** — missing / bad bearer token
        * **404** — agent doesn't exist at the runtime's
          agents_path
        * **422** — bundle directory empty / GitHub config malformed
        * **502** — upstream GitHub call failed (token exchange,
          tree write, ref update)
        * **503** — integration disabled or runtime built without an
          agents_path
        """
        # Lazy-import the integrations module so the dispatcher path
        # (which never publishes) doesn't trigger cryptography's
        # heavy lift at import time. Only ``GitHubError`` is needed
        # here — the client type comes from app.state regardless.
        from movate.integrations.github import GitHubError  # noqa: PLC0415

        agents_path: Path | None = request.app.state.agents_path
        if agents_path is None:
            raise AgentCreationError(
                "runtime was built without an agents_path; "
                "POST /api/v1/agents/{name}/publish is unavailable",
                status_code=503,
            )

        client = getattr(request.app.state, "github_client", None)
        if client is None:
            raise AgentCreationError(
                "github integration is disabled; set MDK_GITHUB_ENABLED=1 "
                "and configure MDK_GITHUB_APP_ID / INSTALLATION_ID / "
                "PRIVATE_KEY / REPO to enable POST /api/v1/agents/{name}/publish",
                status_code=503,
            )
        # ``client`` is either a real GitHubClient (production) or a
        # duck-typed test double exposing ``publish_bundle`` — no
        # isinstance check needed; the call below fails loud either
        # way if the method is missing.
        _ = ctx.tenant_id  # future per-tenant audit log entry

        bundle_dir = agents_path / name
        if not bundle_dir.exists() or not bundle_dir.is_dir():
            raise not_found("agent", name)

        message = body.commit_message or f"Update {name}"
        try:
            result = await client.publish_bundle(
                bundle_dir,
                target_dir=name,
                message=message,
                author_name=body.author_name,
                author_email=body.author_email,
            )
        except GitHubError as exc:
            # Translate the integration error onto the right HTTP
            # response. The integration sets ``status_code`` per case
            # (422 config, 502 upstream, 503 disabled).
            raise AgentCreationError(
                str(exc),
                status_code=exc.status_code,
            ) from exc

        return AgentPublishedView(
            agent=name,
            commit_sha=result.commit_sha,
            commit_url=result.commit_url,
            branch=result.branch,
            files_changed=result.files_changed,
        )

    @v1.get(
        "/agents/{name}/history",
        response_model=AgentHistoryView,
        tags=["agents-v1"],
    )
    async def v1_agent_history(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        limit: int = 50,
        page: int = 1,
    ) -> AgentHistoryView:
        """Return the agent's commit history from GitHub (item 79,
        ADR 007).

        Drives the Mova iO version-history panel — one row per commit
        with sha / message / author / timestamp / html_url. Sorted
        newest-first. Empty list when the agent has no published
        commits yet (created via wizard, never published).

        Same feature-flag pattern as ``POST /publish``: returns 503
        with the ``agent_persistence_unavailable`` code when
        ``MDK_GITHUB_ENABLED`` is unset. The route advertises in
        ``/openapi.json`` regardless so client-gen tooling generates
        the typed method now.

        Tenant attribution: today's runtime trusts the env-supplied
        installation_id (one tenant per runtime). Multi-tenant
        installation lookup arrives with item 81.

        Query params:

        * ``limit`` — page size, default 50, clamped to 100 at the
          integration layer (GitHub's per_page max).
        * ``page`` — 1-indexed page number, default 1.

        Errors:

        * **401** — missing / bad bearer token
        * **404** — agent doesn't exist on disk (we check before
          calling GitHub so a typo doesn't burn API budget)
        * **502** — upstream GitHub call failed
        * **503** — integration disabled or runtime built without an
          agents_path
        """
        # Lazy import — same convention as the publish endpoint.
        from movate.integrations.github import GitHubError  # noqa: PLC0415

        agents_path: Path | None = request.app.state.agents_path
        if agents_path is None:
            raise AgentCreationError(
                "runtime was built without an agents_path; "
                "GET /api/v1/agents/{name}/history is unavailable",
                status_code=503,
            )

        client = getattr(request.app.state, "github_client", None)
        if client is None:
            raise AgentCreationError(
                "github integration is disabled; set MDK_GITHUB_ENABLED=1 "
                "and configure MDK_GITHUB_APP_ID / INSTALLATION_ID / "
                "PRIVATE_KEY / REPO to enable GET /api/v1/agents/{name}/history",
                status_code=503,
            )

        _ = ctx.tenant_id  # future per-tenant audit log entry

        bundle_dir = agents_path / name
        if not bundle_dir.exists() or not bundle_dir.is_dir():
            raise not_found("agent", name)

        try:
            commits = await client.list_history(
                target_dir=name,
                limit=limit,
                page=page,
            )
        except GitHubError as exc:
            raise AgentCreationError(
                str(exc),
                status_code=exc.status_code,
            ) from exc

        commit_views = [
            AgentCommitView(
                sha=c.sha,
                message=c.message,
                author_name=c.author_name,
                author_email=c.author_email,
                timestamp=c.timestamp,
                html_url=c.html_url,
            )
            for c in commits
        ]
        # has_more heuristic: full page returned → there might be
        # more. Doesn't guarantee — the next fetch could come back
        # empty. The UI uses this as a "show Load More button" hint.
        return AgentHistoryView(
            agent=name,
            commits=commit_views,
            page=page,
            limit=limit,
            has_more=len(commit_views) == limit,
        )

    @v1.post(
        "/agents/{name}/runs",
        # Union response: 202 + RunAccepted in async mode (default);
        # 200 + RunView when ?wait=true. FastAPI auto-generates a
        # oneOf in OpenAPI so the Angular client can branch.
        response_model=RunAccepted | RunView,
        tags=["agents-v1"],
    )
    async def v1_agent_run(
        name: str,
        body: AgentRunSubmission,
        request: Request,
        response: Response,
        ctx: AuthContext = Depends(auth_dep),
        wait: bool = False,
    ) -> RunAccepted | RunView:
        """Run an agent. Two modes:

        * **Default (?wait=false):** queue a job for the worker pool
          to claim. Returns 202 + ``{job_id, status: queued}``. Angular
          polls ``GET /jobs/{job_id}`` until terminal.

        * **Inline mode (?wait=true):** execute synchronously inside
          the API request and return the resulting ``RunView`` (200).
          Same Executor + provider stack the worker uses, but the run
          happens in-process so wizard-created agents (which don't
          ship to the worker pod yet — see BACKLOG item 109) work
          end-to-end. Trade-off: the request blocks for the full
          agent duration (typically a few seconds for one LLM call;
          can be longer with tool-use loops).

        URL-anchored variant of ``POST /run`` — the agent name comes
        from the path, ``kind=AGENT`` is implicit. REST-clean for
        Angular's resource-oriented mental model (``POST /agents/
        faq-bot/runs`` reads as "create a run under faq-bot").

        Friday-demo path uses ``wait=true`` for the wizard→run
        verb so wizard-created agents respond. Worker-queue path
        (default) is for production load where the client polls.

        Errors (both modes):

        * **401** — missing / bad bearer token
        * **404** — agent not in the registry
        * **422** — body shape failure (FastAPI handles this for us)
        * **500** — (inline mode only) execution failure surfaces
          here; the RunView's ``error`` field carries the typed info
        """
        agents: list[AgentBundle] = request.app.state.agents
        bundle = next((b for b in agents if b.spec.name == name), None)
        if bundle is None:
            raise not_found("agent", name)

        store: StorageProvider = request.app.state.storage

        if wait:
            # Inline mode — same Executor stack the worker uses.
            # Lazy imports keep cold-start light for the async path.
            from movate.core.executor import Executor  # noqa: PLC0415
            from movate.core.models import RunRequest as _RunRequest  # noqa: PLC0415
            from movate.providers.litellm import LiteLLMProvider  # noqa: PLC0415
            from movate.providers.mock import MockProvider  # noqa: PLC0415
            from movate.providers.pricing import load_pricing  # noqa: PLC0415
            from movate.tracing import build_tracer  # noqa: PLC0415

            # mock=true → deterministic MockProvider (sub-second, no
            # API keys). Default uses the agent's declared model via
            # LiteLLM. Same pattern the eval endpoint uses.
            provider: object = MockProvider() if body.mock else LiteLLMProvider()

            executor = Executor(
                provider=provider,  # type: ignore[arg-type]
                pricing=load_pricing(),
                storage=store,
                tracer=build_tracer(),
                tenant_id=ctx.tenant_id,
            )
            run_request = _RunRequest(agent=name, input=body.input)
            run_response = await executor.execute(bundle, run_request)
            # Try to fetch the persisted RunRecord. On success the
            # executor always persists; on error it persists a
            # FailureRecord instead (no RunRecord). We handle both:
            # success → return the canonical RunView from storage;
            # error → synthesize a RunView shape from the RunResponse
            # + ErrorInfo so the wire contract is consistent.
            run_record = await store.get_run(run_response.run_id, tenant_id=ctx.tenant_id)
            response.status_code = 200
            if run_record is not None:
                return RunView.from_record(run_record)
            # Error path — build a minimal RunView. Status / error /
            # metrics come from the RunResponse; identifiers reflect
            # what the executor stamped during the failed attempt.
            from datetime import UTC  # noqa: PLC0415
            from datetime import datetime as _datetime  # noqa: PLC0415

            return RunView(
                run_id=run_response.run_id,
                job_id="",
                agent=bundle.spec.name,
                agent_version=bundle.spec.version,
                prompt_hash=bundle.prompt_hash,
                provider=bundle.spec.model.provider,
                provider_version="",
                pricing_version="",
                status=JobStatus.ERROR if run_response.status == "error" else JobStatus.SUCCESS,
                input=body.input,
                output=None,
                metrics=run_response.metrics,
                error=run_response.error,
                created_at=_datetime.now(UTC),
            )

        # Default async path — same as before.
        job = JobRecord(
            job_id=str(uuid4()),
            tenant_id=ctx.tenant_id,
            kind=JobKind.AGENT,
            target=name,
            status=JobStatus.QUEUED,
            input=body.input,
            api_key_id=ctx.api_key_id,
            notify_email=body.notify_email,
        )
        await store.save_job(job)
        response.status_code = 202
        return RunAccepted(job_id=job.job_id, status=job.status)

    @v1.get(
        "/jobs",
        response_model=JobListView,
        tags=["jobs-v1"],
    )
    async def v1_list_jobs(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        status: JobStatus | None = None,
        agent: str | None = None,
        limit: int = 20,
    ) -> JobListView:
        """Filterable + paginatable job history for the Angular UI's
        run-history table.

        Extends the legacy ``GET /jobs`` (which only filtered by
        ``status``) with:

        * ``agent=<name>`` — drives the agent-profile page's
          "recent runs" tab. Filters server-side via the new
          ``list_jobs(target=...)`` storage method.
        * Same tenant-scoping as the legacy endpoint — a tenant
          can never see another tenant's jobs.

        Limit is hard-capped at 100 for response size + perf.

        Errors:

        * **401** — missing / bad bearer token
        """
        capped_limit = max(1, min(limit, 100))
        store: StorageProvider = request.app.state.storage
        records = await store.list_jobs(
            tenant_id=ctx.tenant_id,
            status=status,
            target=agent,
            limit=capped_limit,
        )
        views = [JobView.from_record(r) for r in records]
        return JobListView(jobs=views, count=len(views))

    @v1.get(
        "/runs/{run_id}/trace",
        response_model=RunTraceView,
        tags=["runs-v1"],
    )
    async def v1_run_trace(
        run_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> RunTraceView:
        """Reconstructed view of a run for the Angular trace-viewer.

        Wraps the existing :func:`movate.core.replay.load_replay`
        engine (same path ``mdk trace replay`` uses) and returns the
        structured JSON the Angular trace component renders.

        Resolves ``run_id`` against BOTH the runs table and the
        workflow_runs table — the same id space is shared, so a
        single endpoint serves both single-agent and workflow trace
        replays. Discriminator is the ``kind`` field in the response.

        Tenant-scoped: a cross-tenant id returns 404 (never 403),
        which would leak the existence of the id.

        Errors:

        * **401** — missing / bad bearer token
        * **404** — neither a run nor workflow_run matches the id
          for this tenant
        """
        # Lazy import — keeps the runtime module's import-time cost low
        # for callers (workers, tests) that never hit this endpoint.
        from movate.core.replay import (  # noqa: PLC0415
            ReplayNotFoundError,
            load_replay,
        )

        store: StorageProvider = request.app.state.storage
        try:
            replay = await load_replay(store, run_id, tenant_id=ctx.tenant_id)
        except ReplayNotFoundError as exc:
            raise not_found("run", run_id) from exc

        # Mirror the JSON shape render_replay_json produces but as a
        # typed view. ``_run_to_dict`` / ``_workflow_to_dict`` live in
        # core.replay alongside the engine — re-use them here so the
        # Angular client and the CLI's ``mdk trace replay`` see byte-
        # for-byte identical data.
        from movate.core.replay import _run_to_dict, _workflow_to_dict  # noqa: PLC0415

        if replay.kind == "agent":
            assert replay.run is not None  # narrowed by replay.kind
            return RunTraceView(
                kind="agent",
                run=_run_to_dict(replay.run),
                total_cost_usd=replay.total_cost_usd,
                total_latency_ms=replay.total_latency_ms,
            )
        # workflow path
        assert replay.workflow is not None
        return RunTraceView(
            kind="workflow",
            workflow=_workflow_to_dict(replay.workflow),
            nodes=[_run_to_dict(r) for r in (replay.children or [])],
            total_cost_usd=replay.total_cost_usd,
            total_latency_ms=replay.total_latency_ms,
        )

    # ------------------------------------------------------------------
    # Eval endpoints (BACKLOG Group H items 83-85)
    # ------------------------------------------------------------------
    @v1.post(
        "/agents/{name}/evals",
        response_model=EvalAcceptedView,
        status_code=202,
        tags=["evals-v1"],
    )
    async def v1_kick_off_eval(
        name: str,
        body: EvalSubmission,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> EvalAcceptedView:
        """Run an eval against an agent's dataset and persist the EvalRecord.

        **Default (``wait=false``):** creates a ``JobRecord(kind=EVAL)``
        and returns 202 immediately with ``{job_id, status: "queued"}``.
        The worker process claims and executes the job; poll
        ``GET /api/v1/jobs/{job_id}`` until terminal, then fetch the
        scorecard from ``GET /api/v1/evals/{result_run_id}``.

        **Synchronous (``wait=true``):** runs the eval inline and
        returns ``{eval_id, status: "success"}`` directly. Convenient
        for demos or CI scripts where a separate worker is not running.
        Avoid for large datasets (risk of HTTP gateway timeout).

        Errors:

        * **401** — bad bearer token
        * **404** — agent not in the registry
        * **422** — eval config / dataset error (``wait=true`` path only;
          async path surfaces the error via the job's error field)
        """
        agents: list[AgentBundle] = request.app.state.agents
        bundle = next((b for b in agents if b.spec.name == name), None)
        if bundle is None:
            raise not_found("agent", name)

        store: StorageProvider = request.app.state.storage

        # ── Async path (default) ──────────────────────────────────────────
        if not body.wait:
            job = JobRecord(
                job_id=str(uuid4()),
                tenant_id=ctx.tenant_id,
                kind=JobKind.EVAL,
                target=name,
                input={
                    "mock": body.mock,
                    "runs": body.runs,
                    "gate_mode": body.gate_mode,
                    "gate": body.gate,
                    "objective": body.objective,
                    "baseline_id": body.baseline_id,
                    "regression_tolerance": body.regression_tolerance,
                },
                api_key_id=ctx.api_key_id,
            )
            await store.save_job(job)
            return EvalAcceptedView(
                job_id=job.job_id,
                status="queued",
            )

        # ── Sync path (wait=true) ─────────────────────────────────────────
        from movate.core.eval import EvalConfigError, EvalEngine  # noqa: PLC0415
        from movate.core.executor import Executor  # noqa: PLC0415
        from movate.providers.litellm import LiteLLMProvider  # noqa: PLC0415
        from movate.providers.mock import MockProvider  # noqa: PLC0415
        from movate.providers.pricing import load_pricing  # noqa: PLC0415
        from movate.tracing import build_tracer  # noqa: PLC0415

        provider: object = MockProvider() if body.mock else LiteLLMProvider()
        executor = Executor(
            provider=provider,  # type: ignore[arg-type]
            pricing=load_pricing(),
            storage=store,
            tracer=build_tracer(),
            tenant_id=ctx.tenant_id,
        )

        try:
            engine = EvalEngine(
                executor=executor,
                provider=provider,  # type: ignore[arg-type]
                runs_per_case=body.runs,
                gate_mode=body.gate_mode,
                objective_filter=body.objective,
                global_skill_responses=body.skill_responses,
            )
            summary = await engine.run(bundle)
        except EvalConfigError as exc:
            return EvalAcceptedView(
                status="failed",
                message=str(exc),
            )

        record = summary.to_record(tenant_id=ctx.tenant_id)
        await store.save_eval(record)

        return EvalAcceptedView(
            eval_id=record.eval_id,
            status="success",
        )

    @v1.get(
        "/evals/{eval_id}",
        response_model=EvalScorecardView,
        tags=["evals-v1"],
    )
    async def v1_get_eval(
        eval_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> EvalScorecardView:
        """Retrieve a completed eval's scorecard.

        Tenant-scoped at the storage layer (a cross-tenant id probe
        returns 404, never 403, to avoid leaking that the id exists).

        Errors:

        * **401** — bad bearer token
        * **404** — no eval record matches the id for this tenant
        """
        store: StorageProvider = request.app.state.storage
        record = await store.get_eval(eval_id, tenant_id=ctx.tenant_id)
        if record is None:
            raise not_found("eval", eval_id)
        return _eval_record_to_view(record)

    @v1.get(
        "/evals",
        response_model=EvalListView,
        tags=["evals-v1"],
    )
    async def v1_list_evals(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        agent: str | None = None,
        limit: int = 20,
    ) -> EvalListView:
        """Paginated history of eval runs. Filter by ``agent=<name>``
        to drive the agent-profile "evals over time" chart.

        Same tenant scoping as every other endpoint; limit hard-
        capped at 100.

        Errors:

        * **401** — bad bearer token
        """
        capped_limit = max(1, min(limit, 100))
        store: StorageProvider = request.app.state.storage
        records = await store.list_evals(
            tenant_id=ctx.tenant_id,
            agent=agent,
            limit=capped_limit,
        )
        views = [_eval_record_to_view(r) for r in records]
        return EvalListView(evals=views, count=len(views))

    # ------------------------------------------------------------------
    # Auth key management — admin-only (scope="fleet-admin" required).
    #
    # The calling key must carry scope="fleet-admin". Regular keys
    # without that scope receive 403. Tenant isolation is still enforced:
    # admin keys only see/manage keys for their own tenant.
    # ------------------------------------------------------------------

    _ADMIN_SCOPE = "fleet-admin"  # noqa: N806 — local constant inside register-routes

    @v1.post(
        "/auth/keys",
        response_model=ApiKeyMintedView,
        status_code=201,
        summary="Mint a new API key for the calling tenant (admin only).",
    )
    async def v1_mint_key(
        request: Request,
        body: ApiKeyMintRequest,
        ctx: AuthContext = Depends(auth_dep),
    ) -> ApiKeyMintedView:
        """Mint a new bearer key for the calling tenant.

        The ``full_key`` in the response is shown **once** — it cannot
        be recovered. Store it immediately in your secrets vault.

        The calling key must have ``scope="fleet-admin"``.

        Errors:

        * **401** — bad or missing bearer token
        * **403** — authenticated but key lacks ``fleet-admin`` scope
        """
        if ctx.scope != _ADMIN_SCOPE:
            raise forbidden()
        store: StorageProvider = request.app.state.storage
        try:
            env = ApiKeyEnv(ctx.env)
        except ValueError:
            env = ApiKeyEnv.LIVE
        minted = mint_api_key(
            tenant_id=ctx.tenant_id,
            env=env,
            label=body.label,
            ttl_days=body.ttl_days,
        )
        await store.save_api_key(minted.record)
        return ApiKeyMintedView(
            key_id=minted.record.key_id,
            full_key=minted.full_key,
            tenant_id=minted.record.tenant_id,
            env=minted.record.env.value,
            label=minted.record.label,
            expires_at=minted.record.expires_at,
        )

    @v1.get(
        "/auth/keys",
        response_model=ApiKeyListView,
        summary="List active API keys for the calling tenant (admin only).",
    )
    async def v1_list_keys(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        include_revoked: bool = False,
    ) -> ApiKeyListView:
        """List API keys belonging to the calling tenant, newest first.

        Pass ``include_revoked=true`` to show revoked keys too.

        The calling key must have ``scope="fleet-admin"``.

        Errors:

        * **401** — bad or missing bearer token
        * **403** — authenticated but key lacks ``fleet-admin`` scope
        """
        if ctx.scope != _ADMIN_SCOPE:
            raise forbidden()

        from datetime import UTC, datetime  # noqa: PLC0415

        store: StorageProvider = request.app.state.storage
        records = await store.list_api_keys(
            tenant_id=ctx.tenant_id,
            include_revoked=include_revoked,
        )
        now = datetime.now(UTC)
        views = [
            ApiKeyView(
                key_id=r.key_id,
                tenant_id=r.tenant_id,
                env=r.env.value,
                label=r.label,
                created_at=r.created_at,
                last_used_at=r.last_used_at,
                expires_at=r.expires_at,
                status=(
                    "revoked"
                    if r.revoked_at is not None
                    else (
                        "expired" if r.expires_at is not None and r.expires_at < now else "active"
                    )
                ),
            )
            for r in records
        ]
        return ApiKeyListView(keys=views, count=len(views))

    @v1.delete(
        "/auth/keys/{key_id}",
        response_model=ApiKeyRevokedView,
        summary="Revoke an API key (admin only).",
    )
    async def v1_revoke_key(
        request: Request,
        key_id: str,
        ctx: AuthContext = Depends(auth_dep),
    ) -> ApiKeyRevokedView:
        """Revoke the API key with the given ``key_id``.

        Idempotent — revoking an already-revoked key returns 200.
        Tenant-scoped: you can only revoke keys belonging to your tenant.

        The calling key must have ``scope="fleet-admin"``.

        Errors:

        * **401** — bad or missing bearer token
        * **403** — authenticated but key lacks ``fleet-admin`` scope
        * **404** — key not found or belongs to a different tenant
        """
        if ctx.scope != _ADMIN_SCOPE:
            raise forbidden()
        store: StorageProvider = request.app.state.storage
        record = await store.get_api_key(key_id)
        if record is None or record.tenant_id != ctx.tenant_id:
            raise not_found("api_key", key_id)
        await store.revoke_api_key(key_id, tenant_id=ctx.tenant_id)
        return ApiKeyRevokedView(key_id=key_id)

    @v1.get(
        "/auth/me",
        response_model=AuthWhoamiView,
        summary="Return the identity of the calling API key.",
    )
    async def v1_auth_whoami(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> AuthWhoamiView:
        """Return identity of the calling bearer key: key_id, tenant, env, scope, expiry.

        Useful for CLI ``mdk auth whoami`` and for operators to verify
        which key they are authenticating with before minting new ones.

        Errors:

        * **401** — bad or missing bearer token
        """
        store: StorageProvider = request.app.state.storage
        record = await store.get_api_key(ctx.api_key_id)
        return AuthWhoamiView(
            key_id=ctx.api_key_id,
            tenant_id=ctx.tenant_id,
            env=ctx.env,
            scope=None,
            label=record.label if record is not None else None,
            expires_at=record.expires_at if record is not None else None,
        )

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

    # SkillCreationError uses the same status_code → wire-code mapping
    # as AgentCreationError (409/422/500/503 all carry the same
    # operator-facing semantics regardless of resource); shared handler
    # would couple the two unnecessarily, so keep them parallel.
    @app.exception_handler(SkillCreationError)
    async def _skill_creation_error_handler(
        _request: Request, exc: SkillCreationError
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
