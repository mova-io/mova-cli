"""Prefect adapter — drive a movate agent/workflow as a Prefect task (ADR 017 D3).

OPT-IN. Install the extra to use it::

    uv pip install 'movate-cli[prefect]'

A team that already runs Prefect calls a movate agent as one step of a
flow. This is a *thin* wrapper over the existing async API
(:func:`movate.integrations.orchestration.run_target_async`) — it adds
**no** runtime logic; Prefect just gets retries/observability/state for
free around a normal movate ``submit → poll → fetch``.

ADR 017's posture is binding: **movate stays the *callable*, never the
*dependent*.** ``prefect`` is therefore imported *lazily* inside the
function — importing :mod:`movate` (or ``movate.cli.main``) without
Prefect installed never breaks, and Prefect's API churn touches only this
thin file.

Usage (inside a Prefect flow)::

    from prefect import flow
    from movate.integrations.prefect import run_agent

    @flow
    def triage_flow(ticket: dict):
        # Connection from MOVATE_RUNTIME_URL + MOVATE_API_KEY env, or pass
        # base_url=/api_key= explicitly.
        result = run_agent("triage-bot", ticket)
        return result  # the agent's typed `output` dict

``run_agent`` IS a Prefect task (decorated with ``@task`` at definition
time), so calling it inside a ``@flow`` records a task run. To drive a
movate *workflow* (DAG) instead of a single agent, use ``run_workflow``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from movate.core.models import JobKind
from movate.integrations.orchestration import (
    MovateConnection,
    OrchestrationError,
    run_target_async,
)

# Cache the decorated tasks per JobKind so we decorate (and import Prefect)
# exactly once per process, not once per call.
_tasks: dict[JobKind, Callable[..., dict[str, Any]]] = {}


def _require_prefect() -> Any:
    """Import ``prefect.task`` lazily, with a clear install hint on failure.

    Importing here (not at module top) is the lazy-import contract: a base
    ``import movate.integrations.prefect`` must NOT require Prefect — only
    *using* the adapter does.
    """
    try:
        from prefect import task  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised via the no-dep test
        raise OrchestrationError(
            "the Prefect adapter requires the `prefect` extra "
            "(install with `uv pip install 'movate-cli[prefect]'`)"
        ) from exc
    return task


def _run_sync(
    *,
    target: str,
    payload: dict[str, Any],
    kind: JobKind,
    base_url: str | None,
    api_key: str | None,
    poll_interval: float,
    poll_timeout: float | None,
    notify_email: str | None,
) -> dict[str, Any]:
    """Synchronous bridge to the async core.

    Prefect task bodies are ordinarily sync; the movate client is async.
    ``asyncio.run`` opens a fresh loop per call — fine for a task body
    (one submit→poll→fetch per invocation, no shared loop state). If a
    caller is already inside a running loop (rare for a Prefect task), we
    surface a clear error rather than crash with the opaque
    "asyncio.run() cannot be called from a running event loop".
    """
    connection = (
        MovateConnection(base_url=base_url, api_key=api_key)
        if base_url is not None and api_key is not None
        else MovateConnection.from_env()
    )
    coro = run_target_async(
        connection=connection,
        target=target,
        payload=payload,
        kind=kind,
        poll_interval=poll_interval,
        poll_timeout=poll_timeout,
        notify_email=notify_email,
    )
    try:
        return asyncio.run(coro)
    except RuntimeError as exc:
        if "running event loop" in str(exc):
            coro.close()
            raise OrchestrationError(
                "run_agent/run_workflow run a fresh event loop and can't be "
                "called from inside an already-running loop; call "
                "run_target_async(...) directly in async contexts"
            ) from exc
        raise


def _make_task(kind: JobKind, name: str) -> Callable[..., dict[str, Any]]:
    """Build (and cache) a Prefect-``@task``-decorated callable for one kind.

    Decoration + the Prefect import happen on first access per JobKind (then
    the result is cached); a base install that never touches these symbols
    stays Prefect-free.
    """
    cached = _tasks.get(kind)
    if cached is not None:
        return cached

    task = _require_prefect()

    @task(name=name)  # type: ignore[untyped-decorator]  # prefect.task is untyped to mypy
    def _task(
        target: str,
        payload: dict[str, Any],
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        poll_interval: float = 1.0,
        poll_timeout: float | None = 300.0,
        notify_email: str | None = None,
    ) -> dict[str, Any]:
        return _run_sync(
            target=target,
            payload=payload,
            kind=kind,
            base_url=base_url,
            api_key=api_key,
            poll_interval=poll_interval,
            poll_timeout=poll_timeout,
            notify_email=notify_email,
        )

    result: Callable[..., dict[str, Any]] = _task
    _tasks[kind] = result
    return result


def run_agent(
    target: str,
    payload: dict[str, Any],
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    poll_interval: float = 1.0,
    poll_timeout: float | None = 300.0,
    notify_email: str | None = None,
) -> dict[str, Any]:
    """Run a movate **agent** as a Prefect task; return its ``output`` dict.

    Decorated as a Prefect ``@task`` on first call (Prefect is imported
    lazily). Inside a ``@flow`` this records a task run with Prefect's
    state/retry/observability around the movate ``submit → poll → fetch``.

    Args:
        target: agent name registered on the runtime.
        payload: the run input (the JSON object the agent expects).
        base_url / api_key: runtime URL + bearer token. Omit BOTH to read
            ``MOVATE_RUNTIME_URL`` + ``MOVATE_API_KEY`` from env (the same
            wiring ``mdk submit`` uses). Pass BOTH to override.
        poll_interval: seconds between job-status polls.
        poll_timeout: max seconds to wait; ``None`` waits indefinitely.
        notify_email: optional address the worker emails on terminal status.

    Raises:
        OrchestrationError: on submit/poll failure, a non-success terminal
            status, a timeout, or if the ``prefect`` extra isn't installed.
    """
    task_fn = _make_task(JobKind.AGENT, name="movate-run-agent")
    return task_fn(
        target,
        payload,
        base_url=base_url,
        api_key=api_key,
        poll_interval=poll_interval,
        poll_timeout=poll_timeout,
        notify_email=notify_email,
    )


def run_workflow(
    target: str,
    payload: dict[str, Any],
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    poll_interval: float = 1.0,
    poll_timeout: float | None = 300.0,
    notify_email: str | None = None,
) -> dict[str, Any]:
    """Run a movate **workflow** (DAG) as a Prefect task; return its output.

    Identical wiring to :func:`run_agent` but submits ``JobKind.WORKFLOW``
    so the runtime drives the ``WorkflowRunner`` instead of a single agent.
    ``payload`` is the workflow's initial state.
    """
    task_fn = _make_task(JobKind.WORKFLOW, name="movate-run-workflow")
    return task_fn(
        target,
        payload,
        base_url=base_url,
        api_key=api_key,
        poll_interval=poll_interval,
        poll_timeout=poll_timeout,
        notify_email=notify_email,
    )


__all__ = ["run_agent", "run_workflow"]
