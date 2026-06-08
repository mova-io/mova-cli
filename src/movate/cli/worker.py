"""``movate worker`` — drain the queue and execute jobs.

Pairs with ``movate serve`` (queues jobs via ``POST /run``). Run both
in sibling processes for a complete service: the API enqueues, the
worker drains.

The worker process is intentionally simple — one async event loop,
one queue claim at a time. Horizontal scale is "run more worker
processes": each one independently calls ``claim_next_job`` (atomic
via sqlite ``BEGIN IMMEDIATE`` or Postgres ``SELECT ... FOR UPDATE
SKIP LOCKED``) so two workers never dispatch the same job.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console

from movate.cli._console import hint, success
from movate.cli._runtime import build_local_runtime, shutdown_runtime
from movate.core.job_retry import DEFAULT_VISIBILITY_TIMEOUT_SECONDS
from movate.core.models import JobRecord, JobStatus
from movate.core.notify import build_dispatcher
from movate.runtime.alert_worker import build_alert_worker
from movate.runtime.dispatch import DispatchOutcome, WorkerDispatch
from movate.runtime.registry import scan_agents, scan_workflows
from movate.runtime.webhook_worker import WebhookWorker, WebhookWorkerConfig
from movate.runtime.worker import Worker, WorkerConfig

err = Console(stderr=True)
logger = logging.getLogger(__name__)

# Operators override the reaper's visibility timeout via this env var.
# The MDK_*↔MOVATE_* alias shim (sync_env_aliases, run at CLI startup)
# already bridges the legacy MOVATE_ prefix, so we only read MDK_ here.
_VISIBILITY_TIMEOUT_ENV = "MDK_JOB_VISIBILITY_TIMEOUT_SECONDS"

# Operators override the per-job execution timeout (item 34) via this env
# var. Same MDK_*↔MOVATE_* shim applies; we only read MDK_ here. Keep this
# strictly below the visibility timeout (see WorkerConfig.job_timeout_seconds).
_JOB_TIMEOUT_ENV = "MDK_JOB_TIMEOUT_SECONDS"

# The per-job execution timeout (item 34) defaults to 600s. Mirror the
# WorkerConfig default rather than reaching into runtime for it — this is
# the operator-facing default surfaced in the CLI/env contract.
_DEFAULT_JOB_TIMEOUT_SECONDS = 600.0


def _resolve_visibility_timeout() -> float:
    """Read the visibility timeout from env, defaulting on missing/bad input.

    A malformed or non-positive value falls back to the default rather
    than failing the worker — a misconfigured env var shouldn't take a
    queue worker down, and the default is a safe (generous) timeout.
    """
    raw = os.environ.get(_VISIBILITY_TIMEOUT_ENV)
    if not raw:
        return DEFAULT_VISIBILITY_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a number — using default %.0fs",
            _VISIBILITY_TIMEOUT_ENV,
            raw,
            DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
        )
        return DEFAULT_VISIBILITY_TIMEOUT_SECONDS
    if value <= 0:
        logger.warning(
            "%s=%r must be positive — using default %.0fs",
            _VISIBILITY_TIMEOUT_ENV,
            raw,
            DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
        )
        return DEFAULT_VISIBILITY_TIMEOUT_SECONDS
    return value


def _resolve_job_timeout() -> float:
    """Read the per-job execution timeout from env, defaulting on missing/bad input.

    Mirrors :func:`_resolve_visibility_timeout`, with ONE difference: a
    non-positive value is a VALID operator opt-out (disable the per-job
    bound — see ``WorkerConfig.job_timeout_seconds``), so it's passed
    through verbatim rather than coerced to the default. Only a missing
    or unparseable value falls back to the default; a misconfigured env
    var shouldn't take a worker down.
    """
    raw = os.environ.get(_JOB_TIMEOUT_ENV)
    if not raw:
        return _DEFAULT_JOB_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a number — using default %.0fs",
            _JOB_TIMEOUT_ENV,
            raw,
            _DEFAULT_JOB_TIMEOUT_SECONDS,
        )
        return _DEFAULT_JOB_TIMEOUT_SECONDS
    return value


def worker(
    tenant_id: str = typer.Option(
        None,
        "--tenant-id",
        help="Drain only this tenant's queue. Omit to drain all tenants (operator/dev mode).",
    ),
    agents_path: Path = typer.Option(
        Path("./agents"),
        "--agents-path",
        envvar=["MDK_AGENTS_PATH", "MOVATE_AGENTS_PATH"],
        help="Directory to scan for agent.yaml files.",
    ),
    workflows_path: Path = typer.Option(
        Path("./workflows"),
        "--workflows-path",
        envvar=["MDK_WORKFLOWS_PATH", "MOVATE_WORKFLOWS_PATH"],
        help=(
            "Directory to scan for workflow.yaml files. Optional; "
            "JobKind.WORKFLOW jobs ERROR if no workflows are registered."
        ),
    ),
    poll_interval: float = typer.Option(
        0.5,
        "--poll-interval",
        help="Seconds to sleep when the queue is empty.",
    ),
    mock: bool = typer.Option(
        False,
        "--mock",
        help="Use the deterministic MockProvider (no API keys; for smoke tests).",
    ),
    backend: str = typer.Option(
        "queue",
        "--backend",
        help=(
            "Worker backend (ADR 055 D4): [bold]queue[/bold] (default) drains the "
            "job queue and serves native + langgraph (in-process) workflows — "
            "unchanged. [bold]temporal[/bold] connects to the Temporal service "
            "(TEMPORAL_HOST/NAMESPACE/TLS_CERT), registers every 'runtime: temporal' "
            "workflow + the activities, and runs a Temporal worker (needs mdk[temporal])."
        ),
    ),
    from_storage: bool = typer.Option(
        False,
        "--from-storage",
        envvar="MDK_TEMPORAL_WORKFLOWS_FROM_STORAGE",
        help=(
            "(temporal backend, ADR 088) ALSO load published 'runtime: temporal' "
            "workflows from storage for --tenant-id, not just the filesystem — so "
            "`mdk workflow publish <wf>` makes it hostable without writing to the "
            "agents volume. Default off; the filesystem scan wins on a name clash."
        ),
    ),
) -> None:
    """Drain the queue, dispatch each job, persist the result.

    [bold]Examples:[/bold]

      [dim]# Default: drain all tenants, scan ./agents and ./workflows[/dim]
      $ movate worker

      [dim]# Tenant-scoped (production: one worker pool per tenant)[/dim]
      $ movate worker --tenant-id <tenant-uuid>

      [dim]# Hermetic smoke (no API keys)[/dim]
      $ movate worker --mock
    """
    if backend not in ("queue", "temporal"):
        err.print(f"[red]✗[/red] --backend must be 'queue' or 'temporal' (got {backend!r})")
        raise typer.Exit(code=2)

    if backend == "temporal":
        asyncio.run(
            _run_temporal_worker(
                tenant_id=tenant_id,
                workflows_path=workflows_path,
                from_storage=from_storage,
                mock=mock,
            )
        )
        return

    asyncio.run(
        _run_worker(
            tenant_id=tenant_id,
            agents_path=agents_path,
            workflows_path=workflows_path,
            poll_interval=poll_interval,
            mock=mock,
        )
    )


async def _run_worker(
    *,
    tenant_id: str | None,
    agents_path: Path,
    workflows_path: Path,
    poll_interval: float,
    mock: bool,
) -> None:
    from movate.cli._runtime import register_pool_observability  # noqa: PLC0415
    from movate.tracing import init_metrics, install_log_correlation  # noqa: PLC0415

    # Initialize OTel metrics once at worker startup (R3 / item 33), after
    # dotenv + MDK_*→MOVATE_* alias sync (both run at CLI import in main.py).
    # Mirrors the tracer wiring; a complete no-op when the otel extra is absent
    # or the OTLP sink/endpoint isn't configured. Never raises.
    init_metrics()
    # Stamp the active distributed-trace trace_id/span_id (ADR 019/024) onto
    # every log line the worker emits while executing a job, so App Insights /
    # Log Analytics can pivot from a trace to its correlated logs (item 38).
    # Wired here at the execution edge — not only in the CLI callback — so jobs
    # drained by this worker are correlated even when the process is launched
    # outside the top-level callback. Idempotent; a complete no-op when the otel
    # extra is absent; never raises.
    install_log_correlation()

    rt = await build_local_runtime(mock=mock)
    # ADR 034 D3 — wire the asyncpg pool's saturation gauges. build_local_runtime
    # already ran storage.init(), so the pool exists. No-op on SQLite / metrics
    # off. Never raises.
    register_pool_observability(rt.storage)

    agents = scan_agents(agents_path)
    workflows = scan_workflows(workflows_path)

    if not agents and not workflows:
        err.print(
            f"[yellow]⚠[/yellow] no agents at {agents_path} and no workflows at "
            f"{workflows_path} — every job will land in ERROR (unknown_target)"
        )
    else:
        if agents:
            success(f"{len(agents)} agent(s) loaded:")
            for b in agents:
                err.print(f"  - {b.spec.name} v{b.spec.version}")
        if workflows:
            success(f"{len(workflows)} workflow(s) loaded:")
            for name in sorted(workflows):
                err.print(f"  - {name}")

    # Build the notifier first so it can be shared between the dispatch
    # (drift alerts on completed evals — ADR 016 D2) and the worker's
    # terminal-job notification hook.
    notifier = build_dispatcher()

    dispatch = WorkerDispatch(
        storage=rt.storage,
        executor=rt.executor,
        agents=agents,
        workflows=workflows,
        use_mock_for_eval=mock,
        notifier=notifier,
    )
    config = WorkerConfig(
        poll_interval_seconds=poll_interval,
        tenant_id=tenant_id,
        visibility_timeout_seconds=_resolve_visibility_timeout(),
        job_timeout_seconds=_resolve_job_timeout(),
    )

    def on_job_complete(job: JobRecord, outcome: DispatchOutcome, duration_ms: int) -> None:
        """Print one line per finished job — a streaming feed beats a
        progress bar here because the worker runs indefinitely with
        unknown total. Status icon + color + duration so the operator
        can eyeball throughput and failures at a glance."""
        ts = datetime.now().strftime("%H:%M:%S")
        if outcome.status == JobStatus.SUCCESS:
            icon, color = "✓", "green"
        elif outcome.status == JobStatus.SAFETY_BLOCKED:
            icon, color = "⊘", "yellow"
        else:  # ERROR
            icon, color = "✗", "red"
        err.print(
            f"[dim]{ts}[/dim] [{color}]{icon}[/{color}] "
            f"{job.kind.value}/{job.target} "
            f"[dim]({duration_ms}ms · {job.job_id[:8]})[/dim]"
        )

    hint(f"[dim]notifications: {notifier.name} backend[/dim]")

    worker_obj = Worker(
        storage=rt.storage,
        dispatch=dispatch,
        config=config,
        on_job_complete=on_job_complete,
        notifier=notifier,
    )

    # ADR 035 D2 — webhook delivery worker runs in the SAME process /
    # event loop as the job worker. Wiring it here keeps "mdk worker"
    # as the single drain entry-point; deployments don't need a
    # second container. The webhook worker is fully independent: a
    # subscriber that hangs or crashes the loop has no effect on job
    # dispatch (separate tasks, separate clients).
    webhook_worker = WebhookWorker(
        storage=rt.storage,
        config=WebhookWorkerConfig(tenant_id=tenant_id),
    )

    # ADR 057 step 2 — alert-router consumer runs in the SAME process / loop.
    # It drains ``alert.raised`` events (raised by the drift / dead-letter /
    # budget sources) and routes them to the configured sinks. Opt-in: with no
    # ``alerts:`` routes + no sink env vars the router is inactive and this is a
    # pure no-op (zero behavior change). Fully independent of the job/webhook
    # workers (separate task; a sink that hangs can't sink dispatch).
    alert_worker = build_alert_worker(storage=rt.storage, tenant_id=tenant_id)
    if alert_worker.is_active:
        hint("[dim]alert routing: active (ADR 057)[/dim]")

    stop_event = asyncio.Event()

    def _handle_signal(*_: object) -> None:
        err.print()  # newline after ^C
        hint("[dim]received shutdown signal — finishing current job...[/dim]")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    err.print(
        f"[bold]movate worker[/bold] — tenant={tenant_id or '<all>'} "
        f"poll={poll_interval}s — waiting for jobs + webhook deliveries"
    )
    try:
        # Run both workers concurrently. ``return_exceptions=True``
        # ensures one worker's bug doesn't sink the other; failures
        # surface in the logs (each worker logs its own crashes).
        await asyncio.gather(
            worker_obj.run_forever(stop_event),
            webhook_worker.run_forever(stop_event),
            alert_worker.run_forever(stop_event),
            return_exceptions=True,
        )
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)
        success("worker stopped cleanly")


async def _run_temporal_worker(
    *,
    tenant_id: str | None,
    workflows_path: Path,
    from_storage: bool = False,
    mock: bool,
) -> None:
    """Run a Temporal worker (``mdk worker --backend temporal``, ADR 055 D4).

    Connects to the Temporal service (TEMPORAL_HOST/NAMESPACE/TLS_CERT, D5),
    scans + compiles every ``runtime: temporal`` workflow, installs the Track-C
    activity context with this process's storage/pricing/tracer/provider (the
    SAME Executor wiring the queue worker uses — ADR 054 D3), registers the
    compiled workflows + the four activities on the shared task queue, and
    polls until SIGINT/SIGTERM.

    Kept thin: the heavy lifting (compile + register + run) lives in
    :func:`movate.runtime.workflow_backend.run_temporal_worker`; this is the
    CLI shell (runtime build, env hint, signal handling, banner).
    """
    from movate.providers.pricing import load_pricing  # noqa: PLC0415
    from movate.runtime.registry import scan_workflows  # noqa: PLC0415
    from movate.runtime.workflow_backend import (  # noqa: PLC0415
        DEFAULT_TASK_QUEUE,
        WorkflowBackendError,
        _resolve_temporal_connection,
        require_backend_available,
        run_temporal_worker,
    )

    # Fail loud BEFORE building anything if the extra/connection isn't ready.
    try:
        require_backend_available("temporal")
    except WorkflowBackendError as exc:
        err.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    rt = await build_local_runtime(mock=mock)
    # Initialize OTel metrics + log correlation + pool gauges at startup, exactly
    # like the native worker above (_run_worker) — without this the Temporal
    # worker emits NO metrics, so mdk.workflow.completed (ADR 082) and the
    # asyncpg pool gauges would silently never export. All three are complete
    # no-ops when the otel extra is absent / the OTLP sink is off; none raise.
    from movate.cli._runtime import register_pool_observability  # noqa: PLC0415
    from movate.tracing import init_metrics, install_log_correlation  # noqa: PLC0415

    init_metrics()
    install_log_correlation()
    register_pool_observability(rt.storage)

    workflows = scan_workflows(workflows_path)
    # ADR 091 — resolve each workflow's effective runtime (auto → temporal when
    # available + compilable, else native) so the worker hosts auto-default
    # workflows, not only those with an explicit `runtime: temporal`.
    from movate.runtime.workflow_backend import resolve_effective_runtime  # noqa: PLC0415

    temporal_wfs = {
        name: g for name, g in workflows.items() if resolve_effective_runtime(g, None) == "temporal"
    }
    # ADR 088 — opt-in second source: published runtime:temporal workflows from
    # storage. Merged with the filesystem scan; filesystem wins on a name clash
    # (local-dev override). Default off → behavior unchanged.
    if from_storage:
        from movate.runtime.registry import load_published_temporal_workflows  # noqa: PLC0415

        published = await load_published_temporal_workflows(
            rt.storage, tenant_id=tenant_id or "local"
        )
        for name, graph in published.items():
            temporal_wfs.setdefault(name, graph)  # filesystem entry wins
        if published:
            hint(f"[dim]loaded {len(published)} published temporal workflow(s) from storage[/dim]")
    if not temporal_wfs:
        err.print(
            f"[yellow]⚠[/yellow] no [bold]runtime: temporal[/bold] workflows at "
            f"{workflows_path} — the worker will start but host nothing. Add "
            f"[bold]runtime: temporal[/bold] to a workflow.yaml to register it."
        )

    stop_event = asyncio.Event()

    def _handle_signal(*_: object) -> None:
        err.print()  # newline after ^C
        hint("[dim]received shutdown signal — stopping Temporal worker...[/dim]")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    # Resolve connection details for the startup banner (ADR 054 D8/D12).
    # require_backend_available already validated; this is a cheap re-resolve.
    conn = _resolve_temporal_connection()
    # Mirror the activities registered by run_temporal_worker() in
    # workflow_backend.py — keep this banner list in sync with that worker's
    # activities=[...] so the startup banner doesn't under-report what's wired.
    activity_names = [
        "call_agent_activity",
        "call_skill_activity",
        "call_gate_activity",
        "call_judge_activity",
        "call_human_activity",
        "persist_workflow_result_activity",
    ]

    if temporal_wfs:
        success(f"{len(temporal_wfs)} temporal workflow(s) registering:")
        for name in sorted(temporal_wfs):
            err.print(f"  - {name}")
    hint(f"[dim]host: {conn.host}[/dim]")
    hint(f"[dim]namespace: {conn.namespace}[/dim]")
    hint(f"[dim]task queue: {DEFAULT_TASK_QUEUE}[/dim]")
    hint(f"[dim]tls: {'yes (' + conn.tls_cert_path + ')' if conn.tls_cert_path else 'no'}[/dim]")
    hint(f"[dim]activities: {', '.join(activity_names)}[/dim]")
    err.print(
        f"[bold]movate worker[/bold] (temporal) — tenant={tenant_id or '<all>'} "
        f"host={conn.host} ns={conn.namespace} queue={DEFAULT_TASK_QUEUE} "
        f"workflows={len(temporal_wfs)} — waiting for Temporal workflow tasks"
    )
    try:
        await run_temporal_worker(
            temporal_wfs,
            storage=rt.storage,
            pricing=load_pricing(),
            tracer=rt.tracer,
            provider=rt.provider,
            tenant_id=tenant_id or "local",
            stop_event=stop_event,
        )
    except WorkflowBackendError as exc:
        err.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)
        success("temporal worker stopped cleanly")
