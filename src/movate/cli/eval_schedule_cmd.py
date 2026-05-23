"""``mdk eval-schedule`` + ``mdk eval-scheduler-tick`` — continuous eval (ADR 016 D2).

Continuous eval runs the eval suite *on a cadence* against the live agent and
alerts on drift vs. a baseline. There is **no in-process timer daemon**: an
operator sets a per-agent cadence (``mdk eval-schedule set``), and an external
cron (a Container Apps **Job** on Azure, or any cron locally) periodically runs
``mdk eval-scheduler-tick``. The tick finds schedules whose cadence has elapsed
and enqueues a ``JobKind.EVAL`` job for each — reusing the existing eval-as-job
path. The worker executes them and, on completion, diffs against the baseline
and fires a drift alert on regression.

Schedules are additive + default-off: nothing runs until a cadence is set, and
existing eval/job behaviour is unchanged for agents without a schedule.

Subcommands:

* ``set <agent> --cadence <dur>`` — create / update an agent's cadence.
* ``list`` — show this tenant's schedules.
* ``clear <agent>`` — remove an agent's schedule.

The sibling ``mdk eval-scheduler-tick`` is the cron entrypoint.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._completion import complete_agent_path
from movate.cli._output import Report
from movate.core.models import EvalSchedule
from movate.core.scheduler import TickResult
from movate.storage.base import StorageProvider

console = Console()
err_console = Console(stderr=True)

# Local CLI storage scopes records under the "local" tenant — matches
# build_local_runtime's Executor tenant_id and the harvest command.
_LOCAL_TENANT = "local"

eval_schedule_app = typer.Typer(
    name="eval-schedule",
    help="Manage continuous-eval cadences per agent (ADR 016 D2).",
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


@eval_schedule_app.command("set")
def set_schedule(
    agent: str = typer.Argument(
        ...,
        help="Agent directory OR bare name (resolved to ./agents/<name>).",
        shell_complete=complete_agent_path,
    ),
    cadence: str = typer.Option(
        ...,
        "--cadence",
        help="How often to eval: int seconds or a duration (30m, 6h, 1d).",
    ),
    mock: bool = typer.Option(
        False,
        "--mock",
        help="Use the deterministic MockProvider (cheap smoke cadence, no tokens).",
    ),
    runs: int = typer.Option(1, "--runs", min=1, max=10, help="Runs per case."),
    gate_mode: str = typer.Option("mean", "--gate-mode", help="mean | min | p10."),
    gate: float = typer.Option(0.7, "--gate", min=0.0, max=1.0, help="Per-case pass score."),
    objective: str | None = typer.Option(
        None, "--objective", help="Only eval cases for this objective id (sampling)."
    ),
    tolerance: float = typer.Option(
        0.05,
        "--tolerance",
        min=0.0,
        max=1.0,
        help="Allowable mean_score/pass_rate drop vs baseline before drift fires.",
    ),
    baseline_id: str | None = typer.Option(
        None,
        "--baseline-id",
        help="Pin a baseline eval_id. Default: diff against the prior eval.",
    ),
    notify_email: str | None = typer.Option(
        None, "--notify-email", help="Email to alert on drift (needs SMTP configured)."
    ),
    disabled: bool = typer.Option(
        False, "--disabled", help="Create the schedule but leave it dormant (no enqueue)."
    ),
    output_format: Report = typer.Option(Report.TABLE, "--format", case_sensitive=False),
) -> None:
    """Set (or update) an agent's continuous-eval cadence.

    [bold]Examples:[/bold]

      [dim]# Cheap mock smoke eval every 30 minutes[/dim]
      $ mdk eval-schedule set rag-qa --cadence 30m --mock

      [dim]# Real eval every 6h, alert on a >3% drop[/dim]
      $ mdk eval-schedule set rag-qa --cadence 6h --tolerance 0.03 --notify-email me@co.com

    Drive the tick from cron: [bold]mdk eval-scheduler-tick[/bold] every few
    minutes (Azure: a Container Apps Job). The tick only enqueues schedules
    that are actually due.
    """
    try:
        cadence_seconds = _parse_cadence(cadence)
    except ValueError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    if gate_mode not in ("mean", "min", "p10"):
        err_console.print(f"[red]✗[/red] --gate-mode must be mean|min|p10, got {gate_mode!r}")
        raise typer.Exit(code=2)

    agent_name = _resolve_agent_name(agent)

    schedule = EvalSchedule(
        tenant_id=_LOCAL_TENANT,
        agent=agent_name,
        cadence_seconds=cadence_seconds,
        enabled=not disabled,
        mock=mock,
        runs=runs,
        gate_mode=gate_mode,
        gate=gate,
        objective=objective,
        regression_tolerance=tolerance,
        baseline_id=baseline_id,
        notify_email=notify_email,
    )
    asyncio.run(_save(schedule))

    if output_format == Report.JSON:
        console.print_json(schedule.model_dump_json())
        return
    state = "enabled" if schedule.enabled else "disabled (dormant)"
    console.print(
        f"[green]✓[/green] schedule for [bold]{agent_name}[/bold] set: "
        f"every {_humanize(cadence_seconds)} ({state}"
        f"{', mock' if mock else ''}, tolerance ±{tolerance:.2f})"
    )
    console.print(
        "[dim]drive it from cron:[/dim] [bold]mdk eval-scheduler-tick[/bold] "
        "[dim](Azure: a Container Apps Job)[/dim]"
    )


@eval_schedule_app.command("list")
def list_schedules(
    output_format: Report = typer.Option(Report.TABLE, "--format", case_sensitive=False),
) -> None:
    """List this project's continuous-eval schedules."""
    schedules = asyncio.run(_list())
    if output_format == Report.JSON:
        console.print_json(data=[s.model_dump(mode="json") for s in schedules])
        return
    if not schedules:
        console.print("[dim]no eval schedules — set one with[/dim] mdk eval-schedule set <agent>")
        return
    table = Table(title="Continuous-eval schedules")
    table.add_column("agent", style="bold")
    table.add_column("cadence")
    table.add_column("enabled")
    table.add_column("mock")
    table.add_column("tolerance")
    table.add_column("last enqueued")
    for s in schedules:
        table.add_row(
            s.agent,
            _humanize(s.cadence_seconds),
            "yes" if s.enabled else "no",
            "yes" if s.mock else "no",
            f"±{s.regression_tolerance:.2f}",
            s.last_enqueued_at.isoformat(timespec="seconds") if s.last_enqueued_at else "never",
        )
    console.print(table)


@eval_schedule_app.command("clear")
def clear_schedule(
    agent: str = typer.Argument(
        ...,
        help="Agent directory OR bare name whose schedule to remove.",
        shell_complete=complete_agent_path,
    ),
) -> None:
    """Remove an agent's continuous-eval schedule."""
    agent_name = _resolve_agent_name(agent)
    deleted = asyncio.run(_delete(agent_name))
    if deleted:
        console.print(f"[green]✓[/green] cleared schedule for [bold]{agent_name}[/bold]")
    else:
        console.print(f"[dim]no schedule for[/dim] {agent_name} [dim]— nothing to clear[/dim]")


def scheduler_tick(
    output_format: Report = typer.Option(Report.TABLE, "--format", case_sensitive=False),
) -> None:
    """Run one scheduler tick: enqueue eval jobs for due schedules (ADR 016 D2).

    The cron entrypoint. Drive it from an external scheduler — on Azure a
    Container Apps Job with a cron trigger; locally any cron or a manual run.
    Enqueues a [bold]JobKind.EVAL[/bold] job per due schedule (reusing the
    existing eval-job path) and stamps each so it won't re-enqueue inside its
    cadence window. A running worker then executes the jobs + checks drift.

    [bold]Examples:[/bold]

      [dim]# Run the tick (cron calls this on an interval)[/dim]
      $ mdk eval-scheduler-tick
    """
    result = asyncio.run(_tick())
    if output_format == Report.JSON:
        console.print_json(data={"enqueued": result.enqueued, "skipped": result.skipped})
        return
    if result.enqueued_count == 0:
        console.print(f"[dim]no schedules due — enqueued 0, skipped {len(result.skipped)}[/dim]")
        return
    console.print(
        f"[green]✓[/green] enqueued [bold]{result.enqueued_count}[/bold] eval job(s); "
        f"skipped {len(result.skipped)} not-yet-due"
    )


# ---------------------------------------------------------------------------
# Storage helpers — build the local runtime, run one op, tear down.
# ---------------------------------------------------------------------------


def _resolve_agent_name(agent: str) -> str:
    """Resolve a directory-or-name argument to the agent's declared name.

    Runs/schedules key off the ``agent.yaml`` name, which can differ from the
    directory name. When the argument doesn't resolve to a bundle on disk
    (e.g. operating against a remote-published agent), fall back to the bare
    argument so an operator can still schedule by name.
    """
    from pathlib import Path  # noqa: PLC0415

    from movate.cli._resolve import resolve_agent_or_workflow_arg  # noqa: PLC0415
    from movate.core.loader import load_agent  # noqa: PLC0415

    try:
        resolved = Path(resolve_agent_or_workflow_arg(agent))
        if (resolved / "agent.yaml").is_file():
            return load_agent(resolved).spec.name
    except Exception:
        pass
    return agent


@asynccontextmanager
async def _local_storage() -> AsyncIterator[StorageProvider]:
    """Build the local runtime, yield its storage, tear down cleanly."""
    from movate.cli._runtime import build_local_runtime, shutdown_runtime  # noqa: PLC0415

    runtime = await build_local_runtime(mock=True)
    try:
        yield runtime.storage
    finally:
        await shutdown_runtime(runtime.storage, runtime.tracer)


async def _save(schedule: EvalSchedule) -> None:
    async with _local_storage() as storage:
        await storage.save_eval_schedule(schedule)


async def _list() -> list[EvalSchedule]:
    async with _local_storage() as storage:
        return await storage.list_eval_schedules(tenant_id=_LOCAL_TENANT)


async def _delete(agent: str) -> bool:
    async with _local_storage() as storage:
        return await storage.delete_eval_schedule(agent, tenant_id=_LOCAL_TENANT)


async def _tick() -> TickResult:
    from movate.core.scheduler import run_scheduler_tick  # noqa: PLC0415

    async with _local_storage() as storage:
        return await run_scheduler_tick(storage, tenant_id=_LOCAL_TENANT)


__all__ = ["eval_schedule_app", "scheduler_tick"]
