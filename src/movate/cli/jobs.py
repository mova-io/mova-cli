"""``movate jobs`` — inspect job state on a deployed runtime.

Subcommands:

* ``movate jobs show <id>`` — single job's current state
* ``movate jobs wait <id>`` — block until terminal (without re-submitting)
* ``movate jobs list`` — paginate this tenant's recent jobs (--status filter)
* ``movate jobs list-agents`` — what the runtime can run

Distinct from ``movate logs`` (queries the LOCAL sqlite for replay /
post-mortem). This module hits the runtime's HTTP API instead.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._console import error, get_global_target, hint, warn
from movate.cli._output import TableJson
from movate.cli._progress import spinner
from movate.core.client import MovateClient, MovateClientError
from movate.core.models import JobStatus
from movate.core.user_config import (
    UserConfigError,
    resolve_bearer_token,
    resolve_target,
)
from movate.runtime.schemas import AgentListView, JobListView, JobView

stdout = Console()
err = Console(stderr=True)

jobs_app = typer.Typer(
    name="jobs",
    help="Inspect jobs on a deployed movate runtime.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@jobs_app.command("show")
def show(
    job_id: str = typer.Argument(..., help="Job id from `movate submit`."),
    target: str = typer.Option(None, "--target", "-t", help="Deployment target name."),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """Show the current state of one job."""
    view = asyncio.run(_fetch_one(job_id=job_id, target=target))
    _emit(view, output_format=output_format)
    # Exit 1 for terminal-but-failed; 0 for queued (still in-flight) and
    # for success. Lets bash branches distinguish in-flight vs failed.
    terminal_non_success = (JobStatus.ERROR, JobStatus.SAFETY_BLOCKED)
    if view.status in terminal_non_success:
        raise typer.Exit(code=1)


@jobs_app.command("wait")
def wait(
    job_id: str = typer.Argument(..., help="Job id from `movate submit`."),
    target: str = typer.Option(None, "--target", "-t", help="Deployment target name."),
    timeout: float = typer.Option(300.0, "--timeout", help="Max seconds; exits 124 if exceeded."),
    poll_interval: float = typer.Option(1.0, "--poll-interval", help="Seconds between polls."),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """Block on a job until it reaches a terminal state.

    Use when you submitted with fire-and-forget and now want to wait:

      $ JOB_ID=$(movate submit faq-agent '{"text": "..."}' | jq -r .job_id)
      $ movate jobs wait "$JOB_ID" --timeout 600
    """
    view = asyncio.run(
        _wait_terminal(job_id=job_id, target=target, timeout=timeout, poll_interval=poll_interval)
    )
    _emit(view, output_format=output_format)
    if view.status != JobStatus.SUCCESS:
        raise typer.Exit(code=1)


@jobs_app.command("list")
def list_jobs(
    status: JobStatus = typer.Option(
        None,
        "--status",
        "-s",
        case_sensitive=False,
        help="Only show jobs in this state (queued, running, success, error, safety_blocked).",
    ),
    limit: int = typer.Option(20, "--limit", "-n", help="Max rows to return (server caps at 100)."),
    target: str = typer.Option(None, "--target", "-t", help="Deployment target name."),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """List this tenant's recent jobs on the target runtime, newest first.

    [bold]Examples:[/bold]

      [dim]# 20 most recent jobs on the active target[/dim]
      $ movate jobs list

      [dim]# Just failures, last 50[/dim]
      $ movate jobs list -s error -n 50

      [dim]# In-flight jobs only, pipe-friendly[/dim]
      $ movate jobs list -s running -o json | jq '.jobs[].job_id'
    """
    listing = asyncio.run(_fetch_list(target=target, status=status, limit=limit))
    if output_format == TableJson.JSON:
        stdout.print(listing.model_dump_json(indent=2), soft_wrap=True, highlight=False)
        return
    if listing.count == 0:
        # Distinct from "no agents" — operators reading this scan stderr
        # for the dim hint before assuming the call succeeded with no rows.
        filter_desc = f" with status={status.value}" if status else ""
        hint(f"[dim]no jobs found{filter_desc}[/dim]")
        return
    table = Table(title=f"{listing.count} job(s) on {target or '<active>'}")
    table.add_column("job_id", style="dim")
    table.add_column("kind/target", overflow="fold")
    table.add_column("status")
    table.add_column("created", style="dim")
    icon = {
        JobStatus.SUCCESS: "[green]✓ success[/green]",
        JobStatus.ERROR: "[red]✗ error[/red]",
        JobStatus.SAFETY_BLOCKED: "[yellow]⊘ safety_blocked[/yellow]",
        JobStatus.QUEUED: "[dim]● queued[/dim]",
        JobStatus.RUNNING: "[blue]● running[/blue]",
    }
    for j in listing.jobs:
        table.add_row(
            j.job_id[:8] + "…",
            f"{j.kind.value}/{j.target}",
            icon.get(j.status, j.status.value),
            j.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        )
    stdout.print(table)


@jobs_app.command("list-agents")
def list_agents(
    target: str = typer.Option(None, "--target", "-t", help="Deployment target name."),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """List agents registered on the target runtime."""
    listing = asyncio.run(_fetch_agents(target=target))
    if output_format == TableJson.JSON:
        stdout.print(listing.model_dump_json(indent=2), soft_wrap=True, highlight=False)
        return
    if not listing.agents:
        hint("[dim]no agents registered[/dim]")
        return
    table = Table(title=f"agents on {target or '<active>'}")
    table.add_column("name", style="bold")
    table.add_column("version")
    table.add_column("description", overflow="fold")
    for a in listing.agents:
        table.add_row(a.name, a.version, a.description or "")
    stdout.print(table)


@jobs_app.command("reap")
def reap(
    visibility_timeout: float = typer.Option(
        None,
        "--visibility-timeout",
        help=(
            "Seconds a job may sit in RUNNING before it's treated as orphaned "
            "and reclaimed. Defaults to the 15-min reaper default. Must be "
            "generously larger than the longest expected job."
        ),
    ),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """Run the stale-job reaper once against the LOCAL runtime.

    Crash-recovery one-shot: finds jobs orphaned in RUNNING past the
    visibility timeout and requeues them (or dead-letters once the retry
    budget is exhausted). Mirrors how [bold]mdk scheduler-tick[/bold] runs
    locally — it builds the local runtime and calls the storage reaper
    directly; it does NOT hit a deployed runtime's HTTP API.

    In production the worker loop + scheduler tick run this automatically;
    this command is an operator escape hatch for the local queue.
    """
    requeued, dead_lettered = asyncio.run(_reap(visibility_timeout=visibility_timeout))
    if output_format == TableJson.JSON:
        stdout.print_json(data={"requeued": requeued, "dead_lettered": dead_lettered})
        return
    if requeued == 0 and dead_lettered == 0:
        hint("[dim]no stale jobs — reclaimed 0[/dim]")
        return
    stdout.print(
        f"[green]✓[/green] reclaimed [bold]{requeued}[/bold] requeued, "
        f"[bold]{dead_lettered}[/bold] dead-lettered"
    )


# ---------------------------------------------------------------------------
# Async glue — kept narrow because each command has a slightly different
# shape (one-shot vs poll vs list).
# ---------------------------------------------------------------------------


async def _fetch_one(*, job_id: str, target: str | None) -> JobView:
    client = _build_client(target)
    try:
        async with client:
            with spinner("fetching job state..."):
                return await client.get_job(job_id)
    except MovateClientError as exc:
        error(str(exc), context="fetch")
        raise typer.Exit(code=exc.status_code // 100) from None


async def _wait_terminal(
    *, job_id: str, target: str | None, timeout: float, poll_interval: float
) -> JobView:
    client = _build_client(target)
    try:
        async with client:
            with spinner(f"waiting on {job_id[:8]}..."):
                return await client.wait_for_terminal(
                    job_id,
                    poll_interval_seconds=poll_interval,
                    max_wait_seconds=timeout,
                )
    except TimeoutError as exc:
        warn(str(exc), icon="⏱")
        raise typer.Exit(code=124) from None
    except MovateClientError as exc:
        error(str(exc), context="poll")
        raise typer.Exit(code=exc.status_code // 100) from None


async def _fetch_agents(*, target: str | None) -> AgentListView:
    client = _build_client(target)
    try:
        async with client:
            return await client.list_agents()
    except MovateClientError as exc:
        error(str(exc), context="list-agents")
        raise typer.Exit(code=exc.status_code // 100) from None


async def _fetch_list(*, target: str | None, status: JobStatus | None, limit: int) -> JobListView:
    client = _build_client(target)
    try:
        async with client:
            with spinner("fetching jobs..."):
                return await client.list_jobs(status=status, limit=limit)
    except MovateClientError as exc:
        error(str(exc), context="list")
        raise typer.Exit(code=exc.status_code // 100) from None


async def _reap(*, visibility_timeout: float | None) -> tuple[int, int]:
    """Build the local runtime and run the stale-job reaper once.

    Local-only (mirrors ``mdk scheduler-tick``'s local path) — does NOT
    require an API route or auth scope, so it stays in the CLI plane.
    Returns ``(requeued, dead_lettered)``.
    """
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from movate.cli._runtime import build_local_runtime, shutdown_runtime  # noqa: PLC0415
    from movate.core.job_retry import (  # noqa: PLC0415
        DEFAULT_POLICY,
        DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
    )

    timeout = visibility_timeout if visibility_timeout else DEFAULT_VISIBILITY_TIMEOUT_SECONDS
    runtime = await build_local_runtime(mock=True)
    try:
        now = datetime.now(UTC)
        result = await runtime.storage.reclaim_stale_jobs(
            older_than=now - timedelta(seconds=timeout),
            max_attempts=DEFAULT_POLICY.max_attempts,
            now=now,
        )
        return result.requeued, result.dead_lettered
    finally:
        await shutdown_runtime(runtime.storage, runtime.tracer)


def _build_client(target: str | None) -> MovateClient:
    """Resolve target name → MovateClient. Exits cleanly on config errors.

    Precedence (highest wins):
      1. Per-command ``--target`` flag (the ``target`` arg here).
      2. Top-level ``movate -t <name>`` / ``MOVATE_TARGET`` env var
         (via :func:`get_global_target`).
      3. Active config target (``resolve_target(None)`` default)."""
    try:
        _, target_cfg = resolve_target(target or get_global_target())
        token = resolve_bearer_token(target_cfg)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None
    return MovateClient(base_url=target_cfg.url, api_key=token)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _emit(view: JobView, *, output_format: TableJson) -> None:
    if output_format == TableJson.JSON:
        stdout.print(view.model_dump_json(indent=2), soft_wrap=True, highlight=False)
        return

    icon = {
        JobStatus.SUCCESS: "[green]✓[/green]",
        JobStatus.ERROR: "[red]✗[/red]",
        JobStatus.SAFETY_BLOCKED: "[yellow]⊘[/yellow]",
        JobStatus.QUEUED: "[dim]●[/dim]",
        JobStatus.RUNNING: "[blue]●[/blue]",
    }.get(view.status, "?")
    table = Table(title=f"{icon} job {view.job_id[:8]}…", show_header=False)
    table.add_column("field", style="dim")
    table.add_column("value")
    table.add_row("job_id", view.job_id)
    table.add_row("kind/target", f"{view.kind.value}/{view.target}")
    table.add_row("status", view.status.value)
    if view.result_run_id:
        table.add_row("run_id", view.result_run_id)
    if view.error:
        table.add_row("error", f"{view.error.type}: {view.error.message}")
    table.add_row("created_at", view.created_at.isoformat())
    if view.claimed_at:
        table.add_row("claimed_at", view.claimed_at.isoformat())
    if view.completed_at:
        table.add_row("completed_at", view.completed_at.isoformat())
    stdout.print(table)


__all__ = ["jobs_app"]
