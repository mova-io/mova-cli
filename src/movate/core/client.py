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

from movate.core.models import JobKind, JobStatus
from movate.runtime.schemas import (
    AgentListView,
    HealthView,
    JobListView,
    JobView,
    RunAccepted,
    RunSubmission,
    RunView,
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
