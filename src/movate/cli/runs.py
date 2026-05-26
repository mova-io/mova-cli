"""``movate runs`` — look up a past run's result on a deployed runtime.

Subcommands:

* ``movate runs show <run_id>`` — fetch a single RunRecord (including its
  ``output``) by id

A synchronous ``mdk run --target <env>`` executes inline and persists a
RunRecord, but it does NOT enqueue a JobRecord — so ``mdk jobs list`` shows
"no jobs found" and there is no way to look up that *past* run afterwards
except by re-reading the inline ``mdk run`` output. ``runs show`` closes
that gap: it fetches the persisted RunRecord by the ``run_id`` the inline
run already printed.

Read-only control-plane command — it hits the runtime's existing
``GET /runs/{run_id}`` endpoint (the same one ``mdk submit --wait`` and
``mdk explain`` use). No new runtime/API surface.

Distinct from ``movate jobs`` (job-queue state for *asynchronous*
submissions) and ``movate logs`` (queries the LOCAL sqlite for replay /
post-mortem). This module hits the runtime's HTTP API for the actual run
result.
"""

from __future__ import annotations

import asyncio
import json

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._console import error, get_global_target
from movate.cli._output import TableJson
from movate.cli._progress import spinner
from movate.core.client import MovateClient, MovateClientError
from movate.core.models import JobStatus
from movate.core.user_config import (
    UserConfigError,
    resolve_bearer_token,
    resolve_target,
)
from movate.runtime.schemas import RunView

stdout = Console()
err = Console(stderr=True)

runs_app = typer.Typer(
    name="runs",
    help="Inspect run results on a deployed movate runtime.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@runs_app.command("show")
def show(
    run_id: str = typer.Argument(
        ...,
        help="Run id (printed by `mdk run --target`, or a job's result_run_id).",
    ),
    target: str = typer.Option(None, "--target", "-t", help="Deployment target name."),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """Show a single run including the agent's ``output``.

    Use this to look up the result of a *past* deployed run by its id —
    e.g. the ``run_id`` a synchronous ``mdk run --target dev`` printed,
    or a job's ``result_run_id`` from ``mdk jobs show``.

    [bold]Examples:[/bold]

      [dim]# Look up a run printed by an earlier `mdk run --target dev`[/dim]
      $ mdk runs show 4f8a1c2e --target dev

      [dim]# Pipe-friendly — grab just the output[/dim]
      $ mdk runs show 4f8a1c2e -t dev -o json | jq '.output'
    """
    view = asyncio.run(_fetch_run(run_id=run_id, target=target))
    _emit(view, output_format=output_format)
    # Exit 1 for terminal-but-failed; 0 for success. Mirrors `jobs show`
    # so bash branches can distinguish a failed run from a clean one.
    terminal_non_success = (JobStatus.ERROR, JobStatus.SAFETY_BLOCKED)
    if view.status in terminal_non_success:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Async glue
# ---------------------------------------------------------------------------


async def _fetch_run(*, run_id: str, target: str | None) -> RunView:
    client = _build_client(target)
    try:
        async with client:
            with spinner("fetching run..."):
                return await client.get_run(run_id)
    except MovateClientError as exc:
        error(str(exc), context="fetch")
        raise typer.Exit(code=exc.status_code // 100) from None


def _build_client(target: str | None) -> MovateClient:
    """Resolve target name → MovateClient. Exits cleanly on config errors.

    Same precedence as ``movate jobs`` (per-command ``--target`` →
    top-level ``-t`` / ``MOVATE_TARGET`` → active config target)."""
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


def _emit(view: RunView, *, output_format: TableJson) -> None:
    if output_format == TableJson.JSON:
        stdout.print(view.model_dump_json(indent=2), soft_wrap=True, highlight=False)
        return

    icon = {
        JobStatus.SUCCESS: "[green]✓[/green]",
        JobStatus.ERROR: "[red]✗[/red]",
        JobStatus.SAFETY_BLOCKED: "[yellow]⊘[/yellow]",
        JobStatus.QUEUED: "[dim]●[/dim]",
        JobStatus.RUNNING: "[blue]●[/blue]",
        JobStatus.CANCELLED: "[magenta]⊗[/magenta]",
    }.get(view.status, "?")
    table = Table(title=f"{icon} run {view.run_id[:8]}…", show_header=False)
    table.add_column("field", style="dim")
    table.add_column("value")
    table.add_row("run_id", view.run_id)
    table.add_row("job_id", view.job_id)
    table.add_row("agent", f"{view.agent} @ {view.agent_version}")
    table.add_row("status", view.status.value)
    table.add_row("provider", view.provider)
    table.add_row(
        "cost",
        f"${view.metrics.cost_usd:.4f} "
        f"({view.metrics.tokens.input}+{view.metrics.tokens.output} tok)",
    )
    table.add_row("latency", f"{view.metrics.latency_ms}ms")
    if view.thread_id:
        table.add_row("thread_id", view.thread_id)
    if view.error:
        table.add_row("error", f"{view.error.type}: {view.error.message}")
    table.add_row("created_at", view.created_at.isoformat())
    stdout.print(table)

    # Output panel under the table — mirrors `submit --wait` so operators
    # see the agent's actual response without a second command. Skipped
    # silently when there's no output (e.g. an errored run).
    if view.output is not None:
        stdout.print()
        stdout.print("[bold]output[/bold]")
        stdout.print(json.dumps(view.output, indent=2), soft_wrap=True, highlight=False)


__all__ = ["runs_app"]
