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
import signal
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console

from movate.cli._console import hint
from movate.cli._runtime import build_local_runtime, shutdown_runtime
from movate.core.models import JobRecord, JobStatus
from movate.core.notify import build_dispatcher
from movate.runtime.dispatch import DispatchOutcome, WorkerDispatch
from movate.runtime.registry import scan_agents, scan_workflows
from movate.runtime.worker import Worker, WorkerConfig

err = Console(stderr=True)


def worker(
    tenant_id: str = typer.Option(
        None,
        "--tenant-id",
        help="Drain only this tenant's queue. Omit to drain all tenants (operator/dev mode).",
    ),
    agents_path: Path = typer.Option(
        Path("./agents"),
        "--agents-path",
        envvar="MOVATE_AGENTS_PATH",
        help="Directory to scan for agent.yaml files.",
    ),
    workflows_path: Path = typer.Option(
        Path("./workflows"),
        "--workflows-path",
        envvar="MOVATE_WORKFLOWS_PATH",
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
    rt = await build_local_runtime(mock=mock)

    agents = scan_agents(agents_path)
    workflows = scan_workflows(workflows_path)

    if not agents and not workflows:
        err.print(
            f"[yellow]⚠[/yellow] no agents at {agents_path} and no workflows at "
            f"{workflows_path} — every job will land in ERROR (unknown_target)"
        )
    else:
        if agents:
            err.print(f"[green]✓[/green] {len(agents)} agent(s) loaded:")
            for b in agents:
                err.print(f"  - {b.spec.name} v{b.spec.version}")
        if workflows:
            err.print(f"[green]✓[/green] {len(workflows)} workflow(s) loaded:")
            for name in sorted(workflows):
                err.print(f"  - {name}")

    dispatch = WorkerDispatch(
        storage=rt.storage,
        executor=rt.executor,
        agents=agents,
        workflows=workflows,
    )
    config = WorkerConfig(
        poll_interval_seconds=poll_interval,
        tenant_id=tenant_id,
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

    notifier = build_dispatcher()
    hint(f"[dim]notifications: {notifier.name} backend[/dim]")

    worker_obj = Worker(
        storage=rt.storage,
        dispatch=dispatch,
        config=config,
        on_job_complete=on_job_complete,
        notifier=notifier,
    )

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
        f"poll={poll_interval}s — waiting for jobs"
    )
    try:
        await worker_obj.run_forever(stop_event)
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)
        err.print("[green]✓[/green] worker stopped cleanly")
