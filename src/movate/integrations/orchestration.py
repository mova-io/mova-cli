"""Shared core for the external-orchestrator adapters (ADR 017 D3).

The Prefect task and the Airflow ``MovateAgentOperator`` are both *thin*
wrappers over the **existing** async API — they don't reimplement any
runtime logic, they just drive :class:`movate.core.client.MovateClient`
through the canonical ``submit → poll → fetch`` sequence the CLI's
``mdk submit --wait`` uses:

1. ``POST /run`` (``MovateClient.submit_job``) enqueues a job and returns
   ``{job_id, status: queued}`` (202).
2. ``GET /jobs/{id}`` (``MovateClient.wait_for_terminal``) is polled until
   the job reaches a terminal status (SUCCESS / ERROR / SAFETY_BLOCKED /
   DEAD_LETTER / CANCELLED).
3. ``GET /runs/{result_run_id}`` (``MovateClient.get_run``) fetches the
   actual agent ``output`` — ``JobView`` carries only pointer state.

ADR 017's posture is binding: **movate stays the *callable*, never the
*dependent*.** This module therefore takes NO orchestrator dependency. It
lives next to ``github.py`` and follows the same conventions: a frozen
config dataclass read from env, lazy imports of the heavyweight optional
lib (Prefect / Airflow) inside the adapter modules, and a clear
"install the extra" error if the lib is missing.

Why ``submit → poll → fetch`` rather than the inline
``POST /api/v1/agents/{name}/runs?wait=true``? The async path is the
production-grade route: it goes through the durable Postgres queue + the
KEDA worker pool (so it scales, retries, and dead-letters), it works for
BOTH ``AGENT`` and ``WORKFLOW`` targets, and it holds no HTTP request open
for the full agent duration. Inline ``?wait=true`` exists for the wizard
demo path (single in-process LLM call); orchestrated pipeline steps want
the queue. The whole point of D3 is to drive that existing async API.

The agent's typed ``output`` dict is what flows back to the orchestrator
as the task's return value — so a downstream Prefect task / Airflow task
can consume it via XComs or the flow's return wiring.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from movate.core.client import MovateClient, MovateClientError
from movate.core.models import JobKind, JobStatus

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OrchestrationError(RuntimeError):
    """Raised when a movate run driven from an external orchestrator fails.

    Covers both transport-level failures (the runtime returned a non-2xx —
    wrapping :class:`MovateClientError`) and *terminal-but-failed* runs (the
    job reached ``ERROR`` / ``SAFETY_BLOCKED`` / ``DEAD_LETTER`` / ``CANCELLED``).
    Orchestrators (Prefect/Airflow) treat a raised exception as a failed
    task — exactly the signal a pipeline step wants on a bad run.
    """


# ---------------------------------------------------------------------------
# Connection config
# ---------------------------------------------------------------------------


# Env vars the adapters read when an explicit base_url / api_key isn't passed.
# Mirrors the CLI's target-resolution convention (MOVATE_*), so an operator
# who already exports these for `mdk submit` gets the same wiring for free.
# (These are env var NAMES, not secret values.)
_ENV_BASE_URL = "MOVATE_RUNTIME_URL"
_ENV_API_KEY_NAME = "MOVATE_API_KEY"


@dataclass(frozen=True)
class MovateConnection:
    """Where to reach a movate runtime + the bearer token to use.

    Both adapters accept these explicitly (Airflow operators wire them from
    the DAG; a Prefect task takes kwargs) OR fall back to env so the no-code
    operator path (``MOVATE_RUNTIME_URL`` + ``MOVATE_API_KEY`` already set
    for ``mdk submit``) works without per-task config.
    """

    base_url: str
    """Runtime base URL, e.g. ``https://agents.acme.example`` — the same
    value ``mdk config add-target`` stores. No trailing slash required;
    :class:`MovateClient` strips it."""

    api_key: str
    """Bearer token (an ``mvt_*`` runtime key, or a federated OIDC JWT if
    the runtime accepts those per ADR 012 D3). Needs the ``run`` + ``read``
    scopes to submit and poll."""

    timeout: float = 30.0
    """Per-request HTTP timeout (seconds). The overall wait is governed by
    ``poll_timeout`` on the run call, not this."""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> MovateConnection:
        """Build a connection from process env. Raises a clear
        :class:`OrchestrationError` naming the missing var rather than a
        bare ``KeyError`` — the orchestrator's task log then reads as
        actionable config guidance."""
        e = env if env is not None else os.environ
        base_url = e.get(_ENV_BASE_URL)
        api_key = e.get(_ENV_API_KEY_NAME)
        missing = [
            name
            for name, value in ((_ENV_BASE_URL, base_url), (_ENV_API_KEY_NAME, api_key))
            if not value
        ]
        if missing or base_url is None or api_key is None:
            raise OrchestrationError(
                "movate orchestrator adapter needs a runtime URL + API key; "
                f"set {' and '.join(missing)} (or pass base_url/api_key "
                "explicitly to the task/operator)"
            )
        return cls(
            base_url=base_url,
            api_key=api_key,
            timeout=float(e.get("MOVATE_RUNTIME_TIMEOUT", "30.0")),
        )


# ---------------------------------------------------------------------------
# The one call both adapters share
# ---------------------------------------------------------------------------


async def run_target_async(
    *,
    connection: MovateConnection,
    target: str,
    payload: dict[str, Any],
    kind: JobKind = JobKind.AGENT,
    poll_interval: float = 1.0,
    poll_timeout: float | None = 300.0,
    notify_email: str | None = None,
) -> dict[str, Any]:
    """Submit a movate agent/workflow run, wait for it, return its ``output``.

    This is the shared engine for both adapters. It performs the canonical
    ``submit → poll → fetch`` sequence over :class:`MovateClient` and returns
    the run's typed ``output`` dict (what a downstream step consumes).

    Args:
        connection: runtime URL + bearer token (see :class:`MovateConnection`).
        target: the agent OR workflow name registered on the runtime.
        payload: the run input — the JSON object the agent/workflow expects.
        kind: ``JobKind.AGENT`` (default) or ``JobKind.WORKFLOW``.
        poll_interval: seconds between ``GET /jobs/{id}`` polls.
        poll_timeout: max seconds to wait for a terminal status; ``None``
            waits indefinitely. On timeout the run keeps going server-side —
            we raise :class:`OrchestrationError` so the orchestrator's own
            retry/alerting kicks in (the job_id is in the message for triage).
        notify_email: optional address the worker emails on terminal status.

    Returns:
        The run's ``output`` dict (``{}`` if the run produced no output —
        e.g. a workflow whose final node returns nothing).

    Raises:
        OrchestrationError: on a transport failure, a non-success terminal
            status, or a poll timeout. The message carries the job_id and
            (where available) the typed runtime error so the failing task
            log is actionable.
    """
    async with MovateClient(
        base_url=connection.base_url,
        api_key=connection.api_key,
        timeout=connection.timeout,
    ) as client:
        try:
            accepted = await client.submit_job(
                kind=kind,
                target=target,
                input=payload,
                notify_email=notify_email,
            )
        except MovateClientError as exc:
            raise OrchestrationError(
                f"movate {kind.value} '{target}' submit failed: {exc}"
            ) from exc

        try:
            job = await client.wait_for_terminal(
                accepted.job_id,
                poll_interval_seconds=poll_interval,
                max_wait_seconds=poll_timeout,
            )
        except TimeoutError as exc:
            # The job is NOT cancelled — it continues server-side. Surface
            # the id so an operator (or an orchestrator retry) can resume
            # tracking it via `mdk jobs show <id>`.
            raise OrchestrationError(
                f"movate {kind.value} '{target}' (job {accepted.job_id}) did "
                f"not finish within {poll_timeout}s; it continues server-side"
            ) from exc
        except MovateClientError as exc:
            raise OrchestrationError(
                f"movate {kind.value} '{target}' (job {accepted.job_id}) poll failed: {exc}"
            ) from exc

        if job.status != JobStatus.SUCCESS:
            detail = ""
            if job.error is not None:
                detail = f" — {job.error.type}: {job.error.message}"
            raise OrchestrationError(
                f"movate {kind.value} '{target}' (job {job.job_id}) finished "
                f"{job.status.value}{detail}"
            )

        # SUCCESS but no run pointer is anomalous (the worker always writes a
        # RunRecord on a successful AGENT run). Be defensive: return {} rather
        # than crashing the orchestrator on a missing id.
        if not job.result_run_id:
            return {}

        try:
            run = await client.get_run(job.result_run_id)
        except MovateClientError as exc:
            raise OrchestrationError(
                f"movate {kind.value} '{target}' (job {job.job_id}) succeeded "
                f"but fetching run {job.result_run_id} failed: {exc}"
            ) from exc

        return run.output or {}


__all__ = [
    "MovateConnection",
    "OrchestrationError",
    "run_target_async",
]
