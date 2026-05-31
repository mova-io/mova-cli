"""HTTP client for the movate runtime.

Thin wrapper around ``httpx`` that handles auth, base-URL composition,
and translating wire JSON back into the same Pydantic models the
server uses. CLI commands like ``movate submit`` and ``movate jobs``
talk to a deployed runtime through this class.

Both sync (CLI) and async (workers, integration tests, future agent
SDK) usage are supported via the dual ``httpx.Client`` /
``httpx.AsyncClient`` instances under the hood; for v0.5 CLI work we
expose only the async API — sync wrappers are a follow-up if a
non-async consumer materializes.
"""

from __future__ import annotations

from typing import Any

import httpx

from movate.core.models import JobKind, JobStatus, ProjectMemberRole, WorkflowStatus
from movate.core.webhooks import (
    WebhookAttemptListView,
    WebhookCreatedView,
    WebhookListView,
    WebhookView,
)
from movate.runtime.schemas import (
    AgentListView,
    AnalyzeAcceptedView,
    BatchAcceptedView,
    BatchListView,
    BatchStatusView,
    CapabilitiesView,
    ContextListResponse,
    ContextVersionsResponse,
    ContextView,
    DeadLetterPurgeView,
    GroundedAnswerView,
    HealthView,
    JobCancelView,
    JobListView,
    JobView,
    JudgeCommitRequest,
    JudgeCommitResponse,
    JudgeGenerateRequest,
    JudgeGenerateResponse,
    ObservabilityHealthView,
    ObservabilityInsightListView,
    ProjectListResponse,
    ProjectMemberListView,
    ProjectMemberView,
    ProjectView,
    ResourceAttachView,
    RunAccepted,
    RunSubmission,
    RunView,
    SkillListResponse,
    SkillVersionsResponse,
    SkillView,
    WorkflowCreateRequest,
    WorkflowDetailView,
    WorkflowListResponse,
    WorkflowPublishedView,
    WorkflowRevertedView,
    WorkflowRevertSubmission,
    WorkflowRunListView,
    WorkflowSignalRequest,
    WorkflowValidationView,
    WorkflowVersionsView,
)


class MovateClientError(Exception):
    """Raised when the runtime returns a non-2xx response.

    Surfaces the HTTP status + the parsed error envelope so CLI
    callers can render a human-readable message without re-parsing.
    """

    def __init__(self, *, status_code: int, code: str, message: str) -> None:
        super().__init__(f"{status_code} {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


class MovateClient:
    """Async HTTP client bound to one runtime + one bearer token.

    Construct once per target. Reuses the underlying httpx connection
    pool so back-to-back calls share keepalive; close it with
    ``await client.aclose()`` or use as an async context manager.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # Tests pass a custom transport (``ASGITransport(app=...)``) to
        # talk directly to a FastAPI app without bringing up a real
        # server. Production passes ``transport=None`` (default —
        # real network).
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> MovateClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    async def healthz(self) -> HealthView:
        """``GET /healthz`` — unauthed liveness."""
        # Tolerate the missing Authorization header for /healthz; the
        # runtime ignores it for this endpoint anyway, but explicit is
        # cleaner.
        r = await self._client.get("/healthz")
        self._raise_for_status(r)
        return HealthView.model_validate(r.json())

    async def list_agents(self) -> AgentListView:
        """``GET /agents`` — registered agents on this runtime."""
        r = await self._client.get("/agents")
        self._raise_for_status(r)
        return AgentListView.model_validate(r.json())

    async def capabilities(self) -> CapabilitiesView:
        """``GET /api/v1/capabilities`` — what THIS runtime version supports.

        Read-only self-description: reachable models, feature flags (derived
        from the deployed route table / importable modules), the scope
        vocabulary, this tenant's effective limits, and installed extras.

        Sends the bearer (so a ``read``-scoped key gets the full matrix); a
        keyless/under-scoped caller gets the minimal subset (``minimal:
        true``) rather than a 401/403 — the endpoint is probe-friendly. We
        still ``_raise_for_status`` to surface a genuine 5xx / transport
        error, but the auth-degradation path returns 200.
        """
        r = await self._client.get("/api/v1/capabilities")
        self._raise_for_status(r)
        return CapabilitiesView.model_validate(r.json())

    async def submit_job(
        self,
        *,
        kind: JobKind,
        target: str,
        input: dict[str, Any],
        notify_email: str | None = None,
    ) -> RunAccepted:
        """``POST /run`` — queue a job; returns ``{job_id, status}``.

        ``notify_email`` is optional; when set, the worker emails this
        address on terminal status. The CLI surfaces this via
        ``movate submit --notify-email <addr>``.
        """
        body = RunSubmission(
            kind=kind,
            target=target,
            input=input,
            notify_email=notify_email,
        )
        r = await self._client.post("/run", json=body.model_dump(mode="json"))
        self._raise_for_status(r)
        return RunAccepted.model_validate(r.json())

    async def get_job(self, job_id: str) -> JobView:
        """``GET /jobs/{id}`` — single job's current state."""
        r = await self._client.get(f"/jobs/{job_id}")
        self._raise_for_status(r)
        return JobView.model_validate(r.json())

    async def cancel_job(self, job_id: str) -> JobCancelView:
        """``POST /api/v1/jobs/{id}/cancel`` — cooperatively cancel a job.

        Body-less; requires the ``run`` scope. The returned ``status``
        is the state AFTER the request: ``cancelled`` (was queued, now
        terminal), ``running`` (was running — cancel is pending; the
        worker finalizes it as ``cancelled`` at its next checkpoint), or
        an already-terminal status (no-op). Cancellation is cooperative —
        a running job's in-flight work isn't interrupted; its result is
        discarded in favor of ``cancelled``."""
        r = await self._client.post(f"/api/v1/jobs/{job_id}/cancel")
        self._raise_for_status(r)
        return JobCancelView.model_validate(r.json())

    async def list_jobs(
        self,
        *,
        status: JobStatus | None = None,
        limit: int = 20,
    ) -> JobListView:
        """``GET /jobs`` — paginated list of this tenant's jobs.

        ``status`` filters to one state (e.g. ``ERROR`` to triage
        failures); omit for all states. ``limit`` is hard-capped at
        100 server-side."""
        params: dict[str, str | int] = {"limit": limit}
        if status is not None:
            params["status"] = status.value
        r = await self._client.get("/jobs", params=params)
        self._raise_for_status(r)
        return JobListView.model_validate(r.json())

    async def list_dead_letter_jobs(
        self,
        *,
        agent: str | None = None,
        limit: int = 20,
    ) -> JobListView:
        """``GET /api/v1/jobs/dead-letter`` — this tenant's retry-exhausted
        jobs, newest-first.

        ``agent`` narrows to one agent/workflow ``target``; ``limit`` is
        hard-capped at 100 server-side."""
        params: dict[str, str | int] = {"limit": limit}
        if agent is not None:
            params["agent"] = agent
        r = await self._client.get("/api/v1/jobs/dead-letter", params=params)
        self._raise_for_status(r)
        return JobListView.model_validate(r.json())

    async def requeue_dead_letter_job(self, job_id: str) -> JobView:
        """``POST /api/v1/jobs/{id}/requeue`` — recover a DEAD_LETTER job.

        Body-less; requires the ``run`` scope. Resets the job to a fresh
        ``QUEUED`` state (attempt budget cleared) so the worker reclaims
        it. Returns the requeued :class:`JobView`. 404 if the job is not a
        dead-letter for this tenant."""
        r = await self._client.post(f"/api/v1/jobs/{job_id}/requeue")
        self._raise_for_status(r)
        return JobView.model_validate(r.json())

    async def purge_dead_letter_jobs(self, *, before: str | None = None) -> DeadLetterPurgeView:
        """``POST /api/v1/jobs/dead-letter/purge`` — delete this tenant's
        DEAD_LETTER jobs.

        Destructive; requires the ``admin`` scope. ``before`` (an ISO-8601
        instant) restricts the purge to dead-letters completed strictly
        before it; omit to purge all. Returns the count purged."""
        params: dict[str, str] = {}
        if before is not None:
            params["before"] = before
        r = await self._client.post("/api/v1/jobs/dead-letter/purge", params=params)
        self._raise_for_status(r)
        return DeadLetterPurgeView.model_validate(r.json())

    async def get_run(self, run_id: str) -> RunView:
        """``GET /runs/{id}`` — full run record including ``output``.

        Use after ``get_job`` returns a terminal status with
        ``result_run_id`` set; this is the only way for a client to
        retrieve the actual agent output (``JobView`` deliberately
        omits it — runs may be large and live on a separate retention
        track from job-state polling)."""
        r = await self._client.get(f"/runs/{run_id}")
        self._raise_for_status(r)
        return RunView.model_validate(r.json())

    # ------------------------------------------------------------------
    # Observability Intelligence (ADR 047)
    # ------------------------------------------------------------------

    async def observability_insights(
        self,
        *,
        project_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 90,
    ) -> ObservabilityInsightListView:
        """``GET /api/v1/observability/insights`` — daily insight feed."""
        params: dict[str, str | int] = {"limit": limit}
        if project_id is not None:
            params["project_id"] = project_id
        if since is not None:
            params["since"] = since
        if until is not None:
            params["until"] = until
        r = await self._client.get("/api/v1/observability/insights", params=params)
        self._raise_for_status(r)
        return ObservabilityInsightListView.model_validate(r.json())

    async def observability_health(self, *, project_id: str = "default") -> ObservabilityHealthView:
        """``GET /api/v1/observability/health`` — latest health score + digest."""
        r = await self._client.get(
            "/api/v1/observability/health", params={"project_id": project_id}
        )
        self._raise_for_status(r)
        return ObservabilityHealthView.model_validate(r.json())

    async def observability_ask(
        self,
        question: str,
        *,
        project_id: str = "default",
        budget_usd: float = 0.05,
        mock: bool = False,
    ) -> GroundedAnswerView:
        """``POST /api/v1/observability/ask`` — grounded NL answer + citations."""
        body = {
            "question": question,
            "project_id": project_id,
            "budget_usd": budget_usd,
            "mock": mock,
        }
        r = await self._client.post("/api/v1/observability/ask", json=body)
        self._raise_for_status(r)
        return GroundedAnswerView.model_validate(r.json())

    async def observability_troubleshoot(
        self,
        symptom: str,
        *,
        time_window_days: int = 7,
        project_id: str = "default",
        budget_usd: float = 0.05,
        mock: bool = False,
    ) -> GroundedAnswerView:
        """``POST /api/v1/observability/troubleshoot`` — root-cause narrative."""
        body = {
            "symptom": symptom,
            "time_window_days": time_window_days,
            "project_id": project_id,
            "budget_usd": budget_usd,
            "mock": mock,
        }
        r = await self._client.post("/api/v1/observability/troubleshoot", json=body)
        self._raise_for_status(r)
        return GroundedAnswerView.model_validate(r.json())

    async def observability_analyze(
        self,
        *,
        project_id: str = "default",
        date: str | None = None,
        budget_usd: float = 0.10,
    ) -> AnalyzeAcceptedView:
        """``POST /api/v1/observability/analyze`` — enqueue an analyst run (admin)."""
        body: dict[str, Any] = {"project_id": project_id, "budget_usd": budget_usd}
        if date is not None:
            body["date"] = date
        r = await self._client.post("/api/v1/observability/analyze", json=body)
        self._raise_for_status(r)
        return AnalyzeAcceptedView.model_validate(r.json())

    # ------------------------------------------------------------------
    # Batch inference (item 17)
    # ------------------------------------------------------------------

    async def submit_batch(
        self,
        *,
        agent: str,
        rows: list[dict[str, Any]],
        notify_email: str | None = None,
    ) -> BatchAcceptedView:
        """``POST /api/v1/agents/{name}/batch`` — enqueue one job per row.

        Sends the dataset as the inline JSON body
        (``{"inputs": [...], "notify_email"?: ...}``). The server mints a
        ``batch_id`` and enqueues one ordinary AGENT job per row; returns
        ``{batch_id, total, status: "queued"}`` (202). Poll
        ``get_batch(batch_id)`` for progress.
        """
        body: dict[str, Any] = {"inputs": rows}
        if notify_email is not None:
            body["notify_email"] = notify_email
        r = await self._client.post(f"/api/v1/agents/{agent}/batch", json=body)
        self._raise_for_status(r)
        return BatchAcceptedView.model_validate(r.json())

    async def get_batch(self, batch_id: str) -> BatchStatusView:
        """``GET /api/v1/batches/{id}`` — per-status aggregate of a batch."""
        r = await self._client.get(f"/api/v1/batches/{batch_id}")
        self._raise_for_status(r)
        return BatchStatusView.model_validate(r.json())

    async def list_batches(self, *, limit: int = 20) -> BatchListView:
        """``GET /api/v1/batches`` — this tenant's recent batches, newest-first."""
        r = await self._client.get("/api/v1/batches", params={"limit": limit})
        self._raise_for_status(r)
        return BatchListView.model_validate(r.json())

    async def wait_for_batch(
        self,
        batch_id: str,
        *,
        poll_interval_seconds: float = 1.0,
        max_wait_seconds: float | None = None,
    ) -> BatchStatusView:
        """Poll ``GET /api/v1/batches/{id}`` until the batch is ``complete``.

        ``complete`` = every child job reached a terminal status (the server
        derives ``state``). ``max_wait_seconds=None`` waits indefinitely;
        otherwise a ``TimeoutError`` is raised once elapsed exceeds it (the
        batch keeps running server-side).
        """
        import asyncio  # noqa: PLC0415

        elapsed = 0.0
        while True:
            view = await self.get_batch(batch_id)
            if view.state == "complete":
                return view
            if max_wait_seconds is not None and elapsed >= max_wait_seconds:
                raise TimeoutError(
                    f"batch {batch_id} still {view.state} after {elapsed:.0f}s — "
                    f"abandoning poll, server will keep working"
                )
            await asyncio.sleep(poll_interval_seconds)
            elapsed += poll_interval_seconds

    # ------------------------------------------------------------------
    # Workflow HITL — resume-on-signal (ADR 017 D5, PR 2)
    # ------------------------------------------------------------------

    async def list_workflow_runs(
        self,
        *,
        status: WorkflowStatus | None = None,
        limit: int = 20,
    ) -> WorkflowRunListView:
        """``GET /api/v1/workflow-runs`` — this tenant's workflow runs.

        ``status=PAUSED`` finds the HITL queue (runs awaiting a human
        signal). Each PAUSED row carries its ``human_task`` (prompt +
        output_contract). ``limit`` is hard-capped at 100 server-side.
        """
        params: dict[str, str | int] = {"limit": limit}
        if status is not None:
            params["status"] = status.value
        r = await self._client.get("/api/v1/workflow-runs", params=params)
        self._raise_for_status(r)
        return WorkflowRunListView.model_validate(r.json())

    async def signal_workflow_run(
        self,
        workflow_run_id: str,
        *,
        decision: dict[str, Any],
    ) -> RunAccepted:
        """``POST /api/v1/workflow-runs/{id}/signal`` — resume a paused run.

        ``decision`` is a dict of the state keys the gate's ``output_contract``
        requires. The server validates, merges the decision into the
        checkpoint, and enqueues a continuation job; returns ``{job_id,
        status}`` (the continuation job to poll). 202 on success."""
        body = WorkflowSignalRequest(decision=decision)
        r = await self._client.post(
            f"/api/v1/workflow-runs/{workflow_run_id}/signal",
            json=body.model_dump(mode="json"),
        )
        self._raise_for_status(r)
        return RunAccepted.model_validate(r.json())

    # ------------------------------------------------------------------
    # Projects (ADR 040)
    # ------------------------------------------------------------------

    async def create_project(
        self,
        *,
        name: str,
        description: str | None = None,
        owner_principal_id: str | None = None,
    ) -> ProjectView:
        """``POST /api/v1/projects`` — create a project in the tenant."""
        body: dict[str, Any] = {"name": name}
        if description is not None:
            body["description"] = description
        if owner_principal_id is not None:
            body["owner_principal_id"] = owner_principal_id
        r = await self._client.post("/api/v1/projects", json=body)
        self._raise_for_status(r)
        return ProjectView.model_validate(r.json())

    async def list_projects(
        self,
        *,
        include_archived: bool = False,
        limit: int = 100,
        after_id: str | None = None,
    ) -> ProjectListResponse:
        """``GET /api/v1/projects`` — tenant-scoped, newest-first."""
        params: dict[str, str | int] = {
            "include_archived": "true" if include_archived else "false",
            "limit": limit,
        }
        if after_id is not None:
            params["after_id"] = after_id
        r = await self._client.get("/api/v1/projects", params=params)
        self._raise_for_status(r)
        return ProjectListResponse.model_validate(r.json())

    async def get_project(self, project_id: str) -> ProjectView:
        """``GET /api/v1/projects/{id}``."""
        r = await self._client.get(f"/api/v1/projects/{project_id}")
        self._raise_for_status(r)
        return ProjectView.model_validate(r.json())

    async def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        if_match: str | None = None,
    ) -> ProjectView:
        """``PUT /api/v1/projects/{id}`` — rename / re-describe.

        ``if_match`` opts into optimistic concurrency (412 on stale).
        """
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        headers = {"If-Match": if_match} if if_match else None
        r = await self._client.put(f"/api/v1/projects/{project_id}", json=body, headers=headers)
        self._raise_for_status(r)
        return ProjectView.model_validate(r.json())

    async def archive_project(self, project_id: str) -> ProjectView:
        """``DELETE /api/v1/projects/{id}`` — soft-delete (archive)."""
        r = await self._client.delete(f"/api/v1/projects/{project_id}")
        self._raise_for_status(r)
        return ProjectView.model_validate(r.json())

    async def list_project_members(self, project_id: str) -> ProjectMemberListView:
        """``GET /api/v1/projects/{id}/members``."""
        r = await self._client.get(f"/api/v1/projects/{project_id}/members")
        self._raise_for_status(r)
        return ProjectMemberListView.model_validate(r.json())

    async def add_project_member(
        self,
        project_id: str,
        *,
        principal_id: str,
        role: ProjectMemberRole,
    ) -> ProjectMemberView:
        """``POST /api/v1/projects/{id}/members``."""
        r = await self._client.post(
            f"/api/v1/projects/{project_id}/members",
            json={"principal_id": principal_id, "role": role.value},
        )
        self._raise_for_status(r)
        return ProjectMemberView.model_validate(r.json())

    async def remove_project_member(self, project_id: str, principal_id: str) -> None:
        """``DELETE /api/v1/projects/{id}/members/{principal_id}``."""
        r = await self._client.delete(f"/api/v1/projects/{project_id}/members/{principal_id}")
        self._raise_for_status(r)

    # ------------------------------------------------------------------
    # Managed skills (ADR 060 D2/D3)
    # ------------------------------------------------------------------

    async def list_skills(self, *, limit: int = 100) -> SkillListResponse:
        """``GET /api/v1/skills`` — latest-per-name, newest-first."""
        r = await self._client.get("/api/v1/skills", params={"limit": limit})
        self._raise_for_status(r)
        return SkillListResponse.model_validate(r.json())

    async def get_skill(self, name: str, *, version: str | None = None) -> SkillView:
        """``GET /api/v1/skills/{name}`` (optionally ``?version=``)."""
        params = {"version": version} if version is not None else None
        r = await self._client.get(f"/api/v1/skills/{name}", params=params)
        self._raise_for_status(r)
        return SkillView.model_validate(r.json())

    async def list_skill_versions(self, name: str, *, limit: int = 50) -> SkillVersionsResponse:
        """``GET /api/v1/skills/{name}/versions`` — newest-first."""
        r = await self._client.get(f"/api/v1/skills/{name}/versions", params={"limit": limit})
        self._raise_for_status(r)
        return SkillVersionsResponse.model_validate(r.json())

    async def upsert_skill(
        self,
        name: str,
        *,
        version: str,
        files: dict[str, str],
        description: str | None = None,
    ) -> SkillView:
        """``PUT /api/v1/skills/{name}`` — publish a new version.

        Used for both create-as-update and update of a managed skill (rows
        are immutable; each call is a new ``(name, version)``). ``files`` MUST
        include ``skill.yaml``.
        """
        body: dict[str, Any] = {"version": version, "files": files}
        if description is not None:
            body["description"] = description
        r = await self._client.put(f"/api/v1/skills/{name}", json=body)
        self._raise_for_status(r)
        return SkillView.model_validate(r.json())

    async def delete_skill(self, name: str, *, version: str | None = None) -> SkillView:
        """``DELETE /api/v1/skills/{name}`` (optionally ``?version=``)."""
        params = {"version": version} if version is not None else None
        r = await self._client.delete(f"/api/v1/skills/{name}", params=params)
        self._raise_for_status(r)
        return SkillView.model_validate(r.json())

    async def attach_skill_to_agent(
        self, agent: str, *, ref: str, version: str | None = None
    ) -> ResourceAttachView:
        """``POST /api/v1/agents/{name}/skills`` — attach a registry skill."""
        body: dict[str, Any] = {"ref": ref}
        if version is not None:
            body["version"] = version
        r = await self._client.post(f"/api/v1/agents/{agent}/skills", json=body)
        self._raise_for_status(r)
        return ResourceAttachView.model_validate(r.json())

    # ------------------------------------------------------------------
    # Managed contexts (ADR 060 D2/D3)
    # ------------------------------------------------------------------

    async def list_contexts(self, *, limit: int = 100) -> ContextListResponse:
        """``GET /api/v1/contexts`` — latest-per-name, newest-first."""
        r = await self._client.get("/api/v1/contexts", params={"limit": limit})
        self._raise_for_status(r)
        return ContextListResponse.model_validate(r.json())

    async def create_context(
        self,
        *,
        name: str,
        body: str,
        description: str | None = None,
        version: str = "v1",
    ) -> ContextView:
        """``POST /api/v1/contexts`` — create the first version."""
        payload: dict[str, Any] = {"name": name, "body": body, "version": version}
        if description is not None:
            payload["description"] = description
        r = await self._client.post("/api/v1/contexts", json=payload)
        self._raise_for_status(r)
        return ContextView.model_validate(r.json())

    async def get_context(self, name: str, *, version: str | None = None) -> ContextView:
        """``GET /api/v1/contexts/{name}`` (optionally ``?version=``)."""
        params = {"version": version} if version is not None else None
        r = await self._client.get(f"/api/v1/contexts/{name}", params=params)
        self._raise_for_status(r)
        return ContextView.model_validate(r.json())

    async def list_context_versions(self, name: str, *, limit: int = 50) -> ContextVersionsResponse:
        """``GET /api/v1/contexts/{name}/versions`` — newest-first."""
        r = await self._client.get(f"/api/v1/contexts/{name}/versions", params={"limit": limit})
        self._raise_for_status(r)
        return ContextVersionsResponse.model_validate(r.json())

    async def upsert_context(
        self,
        name: str,
        *,
        version: str,
        body: str,
        description: str | None = None,
    ) -> ContextView:
        """``PUT /api/v1/contexts/{name}`` — publish a new version."""
        payload: dict[str, Any] = {"version": version, "body": body}
        if description is not None:
            payload["description"] = description
        r = await self._client.put(f"/api/v1/contexts/{name}", json=payload)
        self._raise_for_status(r)
        return ContextView.model_validate(r.json())

    async def delete_context(self, name: str, *, version: str | None = None) -> ContextView:
        """``DELETE /api/v1/contexts/{name}`` (optionally ``?version=``)."""
        params = {"version": version} if version is not None else None
        r = await self._client.delete(f"/api/v1/contexts/{name}", params=params)
        self._raise_for_status(r)
        return ContextView.model_validate(r.json())

    async def attach_context_to_agent(
        self, agent: str, *, ref: str, version: str | None = None
    ) -> ResourceAttachView:
        """``POST /api/v1/agents/{name}/contexts`` — attach a registry context."""
        body: dict[str, Any] = {"ref": ref}
        if version is not None:
            body["version"] = version
        r = await self._client.post(f"/api/v1/agents/{agent}/contexts", json=body)
        self._raise_for_status(r)
        return ResourceAttachView.model_validate(r.json())

    # ------------------------------------------------------------------
    # Workflow definitions (ADR 037 D1 — workflow API parity)
    # ------------------------------------------------------------------

    async def list_workflows(
        self,
        *,
        published_only: bool = False,
        limit: int = 100,
    ) -> WorkflowListResponse:
        """``GET /api/v1/workflows`` — workflow definitions for this tenant."""
        params: dict[str, str | int | bool] = {"limit": limit}
        if published_only:
            params["published_only"] = "true"
        r = await self._client.get("/api/v1/workflows", params=params)
        self._raise_for_status(r)
        return WorkflowListResponse.model_validate(r.json())

    async def get_workflow(
        self,
        name: str,
        *,
        version: str | None = None,
    ) -> WorkflowDetailView:
        """``GET /api/v1/workflows/{name}`` — spec + bundle metadata."""
        params: dict[str, str] = {}
        if version is not None:
            params["version"] = version
        r = await self._client.get(f"/api/v1/workflows/{name}", params=params)
        self._raise_for_status(r)
        return WorkflowDetailView.model_validate(r.json())

    async def list_workflow_versions(
        self,
        name: str,
        *,
        limit: int = 50,
    ) -> WorkflowVersionsView:
        """``GET /api/v1/workflows/{name}/versions`` — registry version
        history, newest-first."""
        r = await self._client.get(f"/api/v1/workflows/{name}/versions", params={"limit": limit})
        self._raise_for_status(r)
        return WorkflowVersionsView.model_validate(r.json())

    async def publish_workflow(
        self,
        name: str,
        *,
        version: str | None = None,
    ) -> WorkflowPublishedView:
        """``POST /api/v1/workflows/{name}/publish`` — promote to published.

        ``version=None`` promotes the current latest."""
        params: dict[str, str] = {}
        if version is not None:
            params["version"] = version
        r = await self._client.post(f"/api/v1/workflows/{name}/publish", params=params)
        self._raise_for_status(r)
        return WorkflowPublishedView.model_validate(r.json())

    async def revert_workflow(
        self,
        name: str,
        *,
        to_version: str,
    ) -> WorkflowRevertedView:
        """``POST /api/v1/workflows/{name}/revert`` — non-destructive rollback."""
        body = WorkflowRevertSubmission(to_version=to_version)
        r = await self._client.post(
            f"/api/v1/workflows/{name}/revert", json=body.model_dump(mode="json")
        )
        self._raise_for_status(r)
        return WorkflowRevertedView.model_validate(r.json())

    async def validate_workflow_spec(
        self,
        name: str,
        *,
        workflow_yaml: str,
        files: dict[str, str] | None = None,
    ) -> WorkflowValidationView:
        """``POST /api/v1/workflows/{name}/validate/from-spec`` — validate a
        JSON-body workflow without persisting. Drives ``mdk workflow validate``."""
        body = WorkflowCreateRequest(workflow_yaml=workflow_yaml, files=files or {})
        r = await self._client.post(
            f"/api/v1/workflows/{name}/validate/from-spec",
            json=body.model_dump(mode="json"),
        )
        self._raise_for_status(r)
        return WorkflowValidationView.model_validate(r.json())

    # ------------------------------------------------------------------
    # Judge Engineer — author + commit evals/judge.yaml
    # ------------------------------------------------------------------

    async def generate_judge(
        self,
        agent: str,
        *,
        rubric_dimensions: list[str] | None = None,
        include_examples: bool = True,
        model: str | None = None,
        budget_usd: float = 0.10,
        mock: bool = False,
    ) -> JudgeGenerateResponse:
        """``POST /api/v1/agents/{name}/judge/generate``.

        Synchronous (~few seconds). Returns the full generated YAML +
        the inferred / supplied dimensions + a rationale. Pass
        ``mock=True`` for the deterministic MockProvider (no API key
        needed — used by hermetic tests and ``mdk judge generate
        --mock``).
        """
        body = JudgeGenerateRequest(
            rubric_dimensions=rubric_dimensions,
            include_examples=include_examples,
            model=model,
            budget_usd=budget_usd,
        )
        headers = {"X-MDK-Judge-Engineer-Mock": "1"} if mock else None
        r = await self._client.post(
            f"/api/v1/agents/{agent}/judge/generate",
            json=body.model_dump(mode="json", exclude_none=False),
            headers=headers,
        )
        self._raise_for_status(r)
        return JudgeGenerateResponse.model_validate(r.json())

    async def commit_judge(self, agent: str, *, judge_yaml: str) -> JudgeCommitResponse:
        """``POST /api/v1/agents/{name}/judge/commit``.

        Persists the supplied ``judge_yaml`` body at
        ``<agent_dir>/evals/judge.yaml``. Server re-validates the YAML
        before writing — a malformed body returns 422 without touching
        disk.
        """
        body = JudgeCommitRequest(judge_yaml=judge_yaml)
        r = await self._client.post(
            f"/api/v1/agents/{agent}/judge/commit",
            json=body.model_dump(mode="json"),
        )
        self._raise_for_status(r)
        return JudgeCommitResponse.model_validate(r.json())

    # ------------------------------------------------------------------
    # Convenience: poll until terminal
    # ------------------------------------------------------------------

    async def wait_for_terminal(
        self,
        job_id: str,
        *,
        poll_interval_seconds: float = 1.0,
        max_wait_seconds: float | None = None,
    ) -> JobView:
        """Poll ``GET /jobs/{id}`` until the job reaches a terminal status.

        Terminal = SUCCESS / ERROR / SAFETY_BLOCKED. QUEUED + RUNNING
        keep the loop going. ``max_wait_seconds=None`` waits forever
        (the worker should be progressing; if it isn't, the operator
        should diagnose, not the client).
        """
        import asyncio  # noqa: PLC0415

        terminal = {JobStatus.SUCCESS, JobStatus.ERROR, JobStatus.SAFETY_BLOCKED}
        elapsed = 0.0
        while True:
            job = await self.get_job(job_id)
            if job.status in terminal:
                return job
            if max_wait_seconds is not None and elapsed >= max_wait_seconds:
                # Surface as a regular timeout — caller decides what
                # to do (often: keep the job_id, exit, come back later).
                raise TimeoutError(
                    f"job {job_id} still {job.status.value} after "
                    f"{elapsed:.0f}s — abandoning poll, server will keep working"
                )
            await asyncio.sleep(poll_interval_seconds)
            elapsed += poll_interval_seconds

    # ------------------------------------------------------------------
    # Webhooks (ADR 035 D2 — outbound delivery management)
    # ------------------------------------------------------------------

    async def create_webhook(
        self,
        *,
        url: str,
        kind_filter: list[str],
        enabled: bool = True,
    ) -> WebhookCreatedView:
        """``POST /api/v1/webhooks`` — subscribe to lifecycle events.

        Returns the subscription view plus the plaintext signing
        ``secret`` — surfaced ONCE on creation. Capture it now; it is
        irrecoverable from any later call. Requires ``admin``.
        """
        body = {"url": url, "kind_filter": kind_filter, "enabled": enabled}
        r = await self._client.post("/api/v1/webhooks", json=body)
        self._raise_for_status(r)
        return WebhookCreatedView.model_validate(r.json())

    async def list_webhooks(self, *, include_disabled: bool = True) -> WebhookListView:
        """``GET /api/v1/webhooks`` — this tenant's webhook subscriptions.

        Carries ``secret_hint`` (last 4 chars) only; never the full
        secret. Requires ``read``.
        """
        params = {"include_disabled": "true" if include_disabled else "false"}
        r = await self._client.get("/api/v1/webhooks", params=params)
        self._raise_for_status(r)
        return WebhookListView.model_validate(r.json())

    async def get_webhook(self, webhook_id: str) -> WebhookView:
        """``GET /api/v1/webhooks/{id}`` — single subscription view.

        Tenant-scoped: cross-tenant id 404s. Requires ``read``.
        """
        r = await self._client.get(f"/api/v1/webhooks/{webhook_id}")
        self._raise_for_status(r)
        return WebhookView.model_validate(r.json())

    async def delete_webhook(self, webhook_id: str) -> None:
        """``DELETE /api/v1/webhooks/{id}`` — remove a subscription.

        Idempotent (204 even on unknown id). Requires ``admin``.
        """
        r = await self._client.delete(f"/api/v1/webhooks/{webhook_id}")
        self._raise_for_status(r)

    async def set_webhook_enabled(self, webhook_id: str, *, enabled: bool) -> WebhookView:
        """``PATCH /api/v1/webhooks/{id}`` — toggle the enabled flag.

        Returns the post-update view. Requires ``admin``.
        """
        r = await self._client.patch(
            f"/api/v1/webhooks/{webhook_id}",
            json={"enabled": enabled},
        )
        self._raise_for_status(r)
        return WebhookView.model_validate(r.json())

    async def list_webhook_attempts(
        self, webhook_id: str, *, limit: int = 100
    ) -> WebhookAttemptListView:
        """``GET /api/v1/webhooks/{id}/attempts`` — recent delivery log.

        Newest-first. Each row has ``status_code`` / ``error_kind`` /
        ``response_excerpt`` (truncated to ~512 chars). Requires
        ``read``.
        """
        r = await self._client.get(
            f"/api/v1/webhooks/{webhook_id}/attempts",
            params={"limit": limit},
        )
        self._raise_for_status(r)
        return WebhookAttemptListView.model_validate(r.json())

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _raise_for_status(self, r: httpx.Response) -> None:
        """Translate non-2xx into ``MovateClientError`` with structured detail."""
        if r.is_success:
            return
        # The runtime returns errors as
        #   {"detail": {"error": {"code": "...", "message": "..."}}}
        # via runtime/errors.py. Fall back gracefully for non-movate
        # 5xx (e.g. ingress timeouts that don't go through our envelope).
        code = "unknown"
        message = f"HTTP {r.status_code}"
        try:
            payload = r.json()
            err = payload.get("detail", {}).get("error", {}) if isinstance(payload, dict) else {}
            code = err.get("code", code)
            message = err.get("message", message)
        except ValueError:
            # Non-JSON body (e.g. an Azure ingress 502 HTML page).
            pass
        raise MovateClientError(status_code=r.status_code, code=code, message=message)


__all__ = ["MovateClient", "MovateClientError"]
