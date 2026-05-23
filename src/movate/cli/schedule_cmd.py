"""``mdk schedule`` + ``mdk scheduler-tick`` — generic cron schedules (ADR 017 D2).

Generalizes the continuous-eval scheduler (ADR 016 D2) from eval-only to
enqueuing arbitrary ``JobKind.AGENT`` / ``JobKind.WORKFLOW`` jobs on a
cadence. There is **no in-process timer daemon**: an operator records a
schedule (``mdk schedule set``), and an external cron (a Container Apps
**Job** on Azure, or any cron locally) periodically runs ``mdk
scheduler-tick``. The tick finds schedules whose cadence has elapsed and
enqueues a job for each — reusing the existing ``mdk submit`` / ``POST
/run`` job shape, so the worker executes them with no new dispatch branch.

``mdk scheduler-tick`` is the **unified** cron entrypoint: it drains BOTH
the eval schedules (ADR 016) and these generic schedules (ADR 017). The
older ``mdk eval-scheduler-tick`` still exists and ticks eval-only, for
back-compat.

Schedules are additive + default-off: nothing runs until one is set, and
existing agent/workflow/job behaviour is unchanged otherwise.

Subcommands:

* ``set <target> --kind agent|workflow --cadence <dur>`` — create / update.
* ``list`` — show this project's schedules.
* ``clear <name>`` — remove a schedule by its handle.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._completion import complete_agent_path
from movate.cli._output import Report
from movate.core.models import JobKind, JobSchedule
from movate.core.scheduler import TickResult
from movate.storage.base import StorageProvider

console = Console()
err_console = Console(stderr=True)

# Local CLI storage scopes records under the "local" tenant — matches
# build_local_runtime's Executor tenant_id and the eval-schedule command.
_LOCAL_TENANT = "local"

schedule_app = typer.Typer(
    name="schedule",
    help="Manage cron schedules that enqueue agent/workflow jobs (ADR 017 D2).",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _parse_cadence(value: str) -> int:
    """Parse a cadence string into seconds.

    Accepts a bare integer (seconds) or a duration with a unit suffix:
    ``s`` (seconds), ``m`` (minutes), ``h`` (hours), ``d`` (days). E.g.
    ``"6h"`` → 21600, ``"30m"`` → 1800, ``"3600"`` → 3600.
    """
    s = value.strip().lower()
    m = re.fullmatch(r"(\d+)\s*([smhd]?)", s)
    if not m:
        raise ValueError(
            f"invalid --cadence {value!r}; use an int (seconds) or a duration like 30m, 6h, 1d."
        )
    n = int(m.group(1))
    if n <= 0:
        raise ValueError("--cadence must be positive")
    unit = m.group(2) or "s"
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _humanize(seconds: int) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0 and seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


@schedule_app.command("set")
def set_schedule(
    target: str = typer.Argument(
        ...,
        help="Agent or workflow name to run on the cadence.",
        shell_complete=complete_agent_path,
    ),
    cadence: str = typer.Option(
        ...,
        "--cadence",
        help="How often to run: int seconds or a duration (30m, 6h, 1d).",
    ),
    kind: JobKind = typer.Option(
        JobKind.AGENT,
        "--kind",
        "-k",
        case_sensitive=False,
        help="Job kind to enqueue: agent | workflow.",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        help="Schedule handle (unique per tenant). Defaults to the target name.",
    ),
    input_arg: str | None = typer.Option(
        None,
        "--input",
        "-i",
        help="Job payload as JSON object, file path, or '-' for stdin. Default: {}.",
    ),
    disabled: bool = typer.Option(
        False, "--disabled", help="Create the schedule but leave it dormant (no enqueue)."
    ),
    notify_email: str | None = typer.Option(
        None, "--notify-email", help="Email to notify when an enqueued job finishes."
    ),
    output_format: Report = typer.Option(Report.TABLE, "--format", case_sensitive=False),
) -> None:
    """Set (or update) a cron schedule that enqueues agent/workflow jobs.

    [bold]Examples:[/bold]

      [dim]# Run an agent every 6h with a fixed input[/dim]
      $ mdk schedule set faq-agent --cadence 6h --input '{"text": "daily digest"}'

      [dim]# Run a workflow nightly, named, notify on finish[/dim]
      $ mdk schedule set returns-pipeline -k workflow --cadence 1d \\
          --name nightly-returns --notify-email me@co.com

    Drive the tick from cron: [bold]mdk scheduler-tick[/bold] every few
    minutes (Azure: a Container Apps Job). The tick only enqueues schedules
    that are actually due, and drains eval schedules too.
    """
    if kind not in (JobKind.AGENT, JobKind.WORKFLOW):
        err_console.print(
            f"[red]✗[/red] --kind must be agent|workflow, got {kind.value!r} "
            "(eval has its own scheduler: mdk eval-schedule)"
        )
        raise typer.Exit(code=2)

    try:
        cadence_seconds = _parse_cadence(cadence)
    except ValueError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    try:
        payload = _coerce_input(input_arg) if input_arg is not None else {}
    except ValueError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    target_name = _resolve_target_name(target)
    schedule_name = name or target_name

    schedule = JobSchedule(
        tenant_id=_LOCAL_TENANT,
        name=schedule_name,
        kind=kind,
        target=target_name,
        cadence_seconds=cadence_seconds,
        enabled=not disabled,
        input=payload,
        notify_email=notify_email,
    )
    asyncio.run(_save(schedule))

    if output_format == Report.JSON:
        console.print_json(schedule.model_dump_json())
        return
    state = "enabled" if schedule.enabled else "disabled (dormant)"
    console.print(
        f"[green]✓[/green] schedule [bold]{schedule_name}[/bold] set: "
        f"{kind.value} [bold]{target_name}[/bold] every {_humanize(cadence_seconds)} ({state})"
    )
    console.print(
        "[dim]drive it from cron:[/dim] [bold]mdk scheduler-tick[/bold] "
        "[dim](Azure: a Container Apps Job)[/dim]"
    )


@schedule_app.command("list")
def list_schedules(
    output_format: Report = typer.Option(Report.TABLE, "--format", case_sensitive=False),
) -> None:
    """List this project's cron schedules."""
    schedules = asyncio.run(_list())
    if output_format == Report.JSON:
        console.print_json(data=[s.model_dump(mode="json") for s in schedules])
        return
    if not schedules:
        console.print(
            "[dim]no schedules — set one with[/dim] mdk schedule set <target> --cadence <dur>"
        )
        return
    table = Table(title="Cron schedules")
    table.add_column("name", style="bold")
    table.add_column("kind")
    table.add_column("target")
    table.add_column("cadence")
    table.add_column("enabled")
    table.add_column("last enqueued")
    for s in schedules:
        table.add_row(
            s.name,
            s.kind.value,
            s.target,
            _humanize(s.cadence_seconds),
            "yes" if s.enabled else "no",
            s.last_enqueued_at.isoformat(timespec="seconds") if s.last_enqueued_at else "never",
        )
    console.print(table)


@schedule_app.command("clear")
def clear_schedule(
    name: str = typer.Argument(..., help="Schedule handle to remove."),
) -> None:
    """Remove a cron schedule by its handle."""
    deleted = asyncio.run(_delete(name))
    if deleted:
        console.print(f"[green]✓[/green] cleared schedule [bold]{name}[/bold]")
    else:
        console.print(f"[dim]no schedule[/dim] {name} [dim]— nothing to clear[/dim]")


def scheduler_tick(
    output_format: Report = typer.Option(Report.TABLE, "--format", case_sensitive=False),
) -> None:
    """Run one unified scheduler tick — enqueue ALL due jobs (ADR 017 D2).

    The unified cron entrypoint. Drains BOTH the eval schedules (ADR 016)
    and the generic agent/workflow schedules (ADR 017) in one pass, so a
    single Container Apps Job (or any cron) keeps every cadence current.
    Enqueues one job per due schedule and stamps each so it won't
    re-enqueue inside its cadence window. A running worker then executes
    the jobs.

    [bold]Examples:[/bold]

      [dim]# Run the unified tick (cron calls this on an interval)[/dim]
      $ mdk scheduler-tick
    """
    result = asyncio.run(_tick())
    if output_format == Report.JSON:
        console.print_json(data={"enqueued": result.enqueued, "skipped": result.skipped})
        return
    if result.enqueued_count == 0:
        console.print(f"[dim]no schedules due — enqueued 0, skipped {len(result.skipped)}[/dim]")
        return
    console.print(
        f"[green]✓[/green] enqueued [bold]{result.enqueued_count}[/bold] job(s); "
        f"skipped {len(result.skipped)} not-yet-due"
    )


# ---------------------------------------------------------------------------
# Input coercion — same rules as `mdk submit` (JSON object / file / stdin).
# ---------------------------------------------------------------------------


def _coerce_input(arg: str) -> dict[str, Any]:
    """Parse a job payload from a JSON object string, a file path, or '-' (stdin)."""
    import json  # noqa: PLC0415
    import sys  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    if arg == "-":
        return _ensure_dict(json.loads(sys.stdin.read()))
    stripped = arg.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            return _ensure_dict(json.loads(arg))
        except json.JSONDecodeError as exc:
            raise ValueError(f"--input looks like JSON but failed to parse: {exc}") from exc
    try:
        is_file = Path(arg).is_file()
    except OSError:
        is_file = False
    if is_file:
        return _ensure_dict(json.loads(Path(arg).read_text()))
    try:
        parsed = json.loads(arg)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--input must be a JSON object, file path, or '-': {exc}") from exc
    return _ensure_dict(parsed)


def _ensure_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"--input must be a JSON object, got {type(value).__name__}")
    return value


# ---------------------------------------------------------------------------
# Storage helpers — build the local runtime, run one op, tear down.
# ---------------------------------------------------------------------------


def _resolve_target_name(target: str) -> str:
    """Resolve a directory-or-name argument to the declared agent/workflow name.

    Jobs key off the ``agent.yaml`` name, which can differ from the
    directory name. When the argument doesn't resolve to a bundle on disk
    (e.g. operating against a remote-published target), fall back to the
    bare argument so an operator can still schedule by name.
    """
    from pathlib import Path  # noqa: PLC0415

    from movate.cli._resolve import resolve_agent_or_workflow_arg  # noqa: PLC0415
    from movate.core.loader import load_agent  # noqa: PLC0415

    try:
        resolved = Path(resolve_agent_or_workflow_arg(target))
        if (resolved / "agent.yaml").is_file():
            return load_agent(resolved).spec.name
    except Exception:
        pass
    return target


@asynccontextmanager
async def _local_storage() -> AsyncIterator[StorageProvider]:
    """Build the local runtime, yield its storage, tear down cleanly."""
    from movate.cli._runtime import build_local_runtime, shutdown_runtime  # noqa: PLC0415

    runtime = await build_local_runtime(mock=True)
    try:
        yield runtime.storage
    finally:
        await shutdown_runtime(runtime.storage, runtime.tracer)


async def _save(schedule: JobSchedule) -> None:
    async with _local_storage() as storage:
        await storage.save_job_schedule(schedule)


async def _list() -> list[JobSchedule]:
    async with _local_storage() as storage:
        return await storage.list_job_schedules(tenant_id=_LOCAL_TENANT)


async def _delete(name: str) -> bool:
    async with _local_storage() as storage:
        return await storage.delete_job_schedule(name, tenant_id=_LOCAL_TENANT)


async def _tick() -> TickResult:
    from movate.core.scheduler import run_all_scheduler_ticks  # noqa: PLC0415

    async with _local_storage() as storage:
        return await run_all_scheduler_ticks(storage, tenant_id=_LOCAL_TENANT)


__all__ = ["schedule_app", "scheduler_tick"]
