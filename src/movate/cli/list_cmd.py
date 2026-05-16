"""``mdk list`` — list recent runs (and optionally in-flight jobs).

Companion to ``mdk explain <run-id>`` (Phase J-2) and ``mdk run``. If
you've just run a few agents and want to know which run IDs are
available to pass to ``explain`` (or which workflows are mid-flight),
this is the command.

Two views, switched by ``--jobs``:

* **Default — runs view**: most-recent :class:`RunRecord` rows from
  local storage, newest first. Each row's ``run_id`` is the value the
  user pastes into ``mdk explain``. Filterable by ``--agent`` and
  ``--status``.
* **``--jobs`` view — in-flight + recent jobs**: :class:`JobRecord`
  rows. Useful in worker / async deploys (``mdk submit``, ``mdk serve``)
  where a job is queued and the operator wants to know what's pending.

Local-CLI runs are synchronous — by the time ``mdk run`` returns, the
RunRecord is already persisted. So locally, ``mdk list`` is "what did
I run recently?" rather than "what's still running?". The ``--jobs``
view becomes interesting when workers are draining the queue.

Output is a Rich table by default. ``--json`` emits the records as
JSON for piping (e.g. ``mdk list --json | jq '.[].run_id'``).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._runtime import build_local_runtime, shutdown_runtime
from movate.core.models import JobRecord, JobStatus, RunRecord

console = Console()
err_console = Console(stderr=True)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# In-flight = jobs that haven't reached a terminal state yet. Matches
# the worker's "claimable" definition plus RUNNING (claimed but not
# yet finished). Used by ``--in-flight`` to filter the jobs view.
_IN_FLIGHT_STATUSES: frozenset[JobStatus] = frozenset({JobStatus.QUEUED, JobStatus.RUNNING})

# Status → Rich color. Lets the operator scan a long list and spot
# failures / blocks at a glance without reading the column.
_STATUS_STYLE: dict[str, str] = {
    "success": "green",
    "running": "cyan",
    "queued": "yellow",
    "error": "red",
    "safety_blocked": "magenta",
    "dead_letter": "bright_red",
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def list_(
    agent: str | None = typer.Option(
        None,
        "--agent",
        "-a",
        help="Filter to one agent name (e.g. ``--agent faq-agent``).",
    ),
    status: str | None = typer.Option(
        None,
        "--status",
        "-s",
        help=(
            "Filter by status: ``success`` | ``running`` | ``queued`` | "
            "``error`` | ``safety_blocked`` | ``dead_letter``."
        ),
    ),
    in_flight: bool = typer.Option(
        False,
        "--in-flight",
        help=(
            "Shortcut: with --jobs, show only QUEUED + RUNNING (active "
            "work). Without --jobs, this flag is ignored (local runs "
            "are synchronous; nothing is ever in flight)."
        ),
    ),
    jobs_view: bool = typer.Option(
        False,
        "--jobs",
        help=(
            "Switch from the runs view (completed RunRecords) to the "
            "jobs view (JobRecords — queued, running, terminal)."
        ),
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        "-n",
        min=1,
        max=200,
        help="Maximum rows to display (newest first). Default 20.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON instead of a Rich table. Pipe-friendly.",
    ),
    full_id: bool = typer.Option(
        False,
        "--full-id",
        help=(
            "Show full UUIDs instead of the 8-char prefix. Useful "
            "when copy-pasting into ``mdk explain <run-id>``."
        ),
    ),
) -> None:
    """List recent runs (or in-flight jobs) for run-ID discovery.

    [bold]Examples:[/bold]

      [dim]# Last 20 runs newest-first (the IDs you'd pass to ``mdk explain``)[/dim]
      $ mdk list

      [dim]# Filter to one agent[/dim]
      $ mdk list --agent faq-agent

      [dim]# Find failed runs[/dim]
      $ mdk list --status error

      [dim]# In-flight jobs (queued + running)[/dim]
      $ mdk list --jobs --in-flight

      [dim]# Copy-paste a full run id for ``mdk explain``[/dim]
      $ mdk list --full-id --limit 5

      [dim]# Pipe to jq[/dim]
      $ mdk list --json --status success | jq '.[].run_id'
    """
    asyncio.run(
        _run_list(
            agent=agent,
            status=status,
            in_flight=in_flight,
            jobs_view=jobs_view,
            limit=limit,
            json_output=json_output,
            full_id=full_id,
        )
    )


async def _run_list(
    *,
    agent: str | None,
    status: str | None,
    in_flight: bool,
    jobs_view: bool,
    limit: int,
    json_output: bool,
    full_id: bool,
) -> None:
    """Async core of the ``list`` command. Pulled out so the Typer
    entry point stays thin and the asyncio.run boundary is a single
    explicit call.
    """
    # Validate --status against the JobStatus enum upfront so a typo
    # surfaces as a clean error rather than an empty result set.
    if status is not None:
        try:
            JobStatus(status)
        except ValueError as exc:
            err_console.print(
                f"[red]✗[/red] invalid status {status!r}; expected one of "
                f"{[s.value for s in JobStatus]}"
            )
            raise typer.Exit(code=2) from exc

    # Build local runtime to get a storage handle. We don't need
    # the executor or provider, but ``build_local_runtime`` is the
    # canonical way to bootstrap SQLite at ``~/.movate/local.db``.
    # Use ``mock=True`` to avoid burning startup time on optional
    # native-SDK adapter probing — we're not running anything.
    runtime = await build_local_runtime(mock=True)
    try:
        if jobs_view:
            records = await _fetch_jobs(
                runtime.storage,
                status=status,
                in_flight=in_flight,
                agent=agent,
                limit=limit,
            )
            if json_output:
                _emit_jobs_json(records)
            else:
                _emit_jobs_table(records, full_id=full_id)
        else:
            records = await _fetch_runs(  # type: ignore[assignment]
                runtime.storage,
                agent=agent,
                status=status,
                limit=limit,
            )
            if json_output:
                _emit_runs_json(records)  # type: ignore[arg-type]
            else:
                _emit_runs_table(records, full_id=full_id)  # type: ignore[arg-type]
    finally:
        await shutdown_runtime(runtime.storage, runtime.tracer)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


async def _fetch_runs(
    storage: object,
    *,
    agent: str | None,
    status: str | None,
    limit: int,
) -> list[RunRecord]:
    """List recent RunRecords filtered by the CLI flags.

    ``storage`` is duck-typed to :class:`StorageProvider` so we don't
    have to thread a Protocol-typed reference through; mypy still
    catches misuse via the method signatures.
    """
    return await storage.list_runs(  # type: ignore[attr-defined, no-any-return]
        agent=agent,
        status=status,
        limit=limit,
        tenant_id="local",  # local CLI is always tenant_id="local"
    )


async def _fetch_jobs(
    storage: object,
    *,
    status: str | None,
    in_flight: bool,
    agent: str | None,
    limit: int,
) -> list[JobRecord]:
    """List JobRecords filtered by the CLI flags.

    ``--in-flight`` is a shortcut that overrides ``--status``: it
    fetches QUEUED and RUNNING separately and merges the result.
    The storage layer doesn't support ``status IN (...)`` directly,
    so we paginate per status and merge in Python.
    """
    if in_flight:
        results: list[JobRecord] = []
        for st in _IN_FLIGHT_STATUSES:
            chunk = await storage.list_jobs(  # type: ignore[attr-defined]
                status=st,
                target=agent,
                limit=limit,
                tenant_id="local",
            )
            results.extend(chunk)
        # Sort by created_at desc + trim to limit. Newest first
        # mirrors the single-status default.
        results.sort(key=lambda j: j.created_at, reverse=True)
        return results[:limit]
    status_filter = JobStatus(status) if status else None
    return await storage.list_jobs(  # type: ignore[attr-defined, no-any-return]
        status=status_filter,
        target=agent,
        limit=limit,
        tenant_id="local",
    )


# ---------------------------------------------------------------------------
# Render: runs view
# ---------------------------------------------------------------------------


def _emit_runs_table(records: list[RunRecord], *, full_id: bool) -> None:
    """Render runs as a Rich table. Newest first."""
    if not records:
        console.print(
            "[yellow]⚠[/yellow] no runs found. "
            "Run an agent first: [bold]mdk run <agent> '<input>'[/bold]"
        )
        return

    table = Table(title=f"Recent runs ({len(records)})", title_style="bold")
    table.add_column("run_id", style="cyan", no_wrap=True)
    table.add_column("agent", style="bold")
    table.add_column("status", no_wrap=True)
    table.add_column("when", style="dim", no_wrap=True)
    table.add_column("latency", style="dim", justify="right", no_wrap=True)
    table.add_column("cost", style="dim", justify="right", no_wrap=True)

    for r in records:
        status_color = _STATUS_STYLE.get(r.status.value, "white")
        latency = f"{r.metrics.latency_ms} ms" if r.metrics.latency_ms is not None else "—"
        cost = f"${r.metrics.cost_usd:.4f}" if r.metrics.cost_usd is not None else "—"
        table.add_row(
            r.run_id if full_id else r.run_id[:8],
            r.agent,
            f"[{status_color}]{r.status.value}[/{status_color}]",
            _format_when(r.created_at),
            latency,
            cost,
        )
    console.print(table)

    if not full_id:
        console.print(
            "[dim]Run IDs truncated to 8 chars. Pass [bold]--full-id[/bold] "
            "to copy into [bold]mdk explain[/bold].[/dim]"
        )


def _emit_runs_json(records: list[RunRecord]) -> None:
    """Emit runs as a JSON array. Stable key order for diff-friendly piping."""
    payload = [
        {
            "run_id": r.run_id,
            "agent": r.agent,
            "status": r.status.value,
            "created_at": r.created_at.isoformat(),
            "latency_ms": r.metrics.latency_ms,
            "cost_usd": r.metrics.cost_usd,
            "provider": r.provider,
            "workflow_run_id": r.workflow_run_id,
        }
        for r in records
    ]
    console.print_json(json.dumps(payload))


# ---------------------------------------------------------------------------
# Render: jobs view
# ---------------------------------------------------------------------------


def _emit_jobs_table(records: list[JobRecord], *, full_id: bool) -> None:
    if not records:
        console.print(
            "[yellow]⚠[/yellow] no jobs found. "
            "Use [bold]mdk submit[/bold] to enqueue one, or remove "
            "[bold]--jobs[/bold] to see completed local runs."
        )
        return

    table = Table(title=f"Jobs ({len(records)})", title_style="bold")
    table.add_column("job_id", style="cyan", no_wrap=True)
    table.add_column("target", style="bold")
    table.add_column("status", no_wrap=True)
    table.add_column("created", style="dim", no_wrap=True)
    table.add_column("attempts", style="dim", justify="right", no_wrap=True)

    for j in records:
        status_color = _STATUS_STYLE.get(j.status.value, "white")
        table.add_row(
            j.job_id if full_id else j.job_id[:8],
            j.target,
            f"[{status_color}]{j.status.value}[/{status_color}]",
            _format_when(j.created_at),
            str(j.attempt_count),
        )
    console.print(table)


def _emit_jobs_json(records: list[JobRecord]) -> None:
    payload = [
        {
            "job_id": j.job_id,
            "target": j.target,
            "status": j.status.value,
            "created_at": j.created_at.isoformat(),
            "attempt_count": j.attempt_count,
        }
        for j in records
    ]
    console.print_json(json.dumps(payload))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Thresholds for the relative-time renderer (in seconds). Named so
# the if-ladder reads as "less than a minute ago" instead of the
# operator having to mentally translate `60`.
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86400


def _format_when(when: datetime) -> str:
    """Human-friendly timestamp.

    For runs/jobs older than 24h, show the date + HH:MM. For more
    recent ones, show a relative form (``2m ago`` / ``45s ago``).
    The relative form is more useful when an operator just ran
    something — the absolute date carries less signal than "a few
    seconds ago."
    """
    # Compute delta in UTC to avoid local-tz weirdness when the user's
    # storage and shell disagree.
    from datetime import UTC  # noqa: PLC0415  -- stdlib, lazy is fine

    now = datetime.now(UTC)
    delta = (now - when).total_seconds()
    if delta < _SECONDS_PER_MINUTE:
        return f"{int(delta)}s ago"
    if delta < _SECONDS_PER_HOUR:
        return f"{int(delta // _SECONDS_PER_MINUTE)}m ago"
    if delta < _SECONDS_PER_DAY:
        return f"{int(delta // _SECONDS_PER_HOUR)}h ago"
    return when.strftime("%Y-%m-%d %H:%M")
