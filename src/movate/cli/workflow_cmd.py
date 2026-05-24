"""``mdk workflow`` — inspect + resume workflow runs on a deployed runtime.

HITL resume-on-signal (ADR 017 D5, PR 2). A multi-agent workflow can pause
at a ``HUMAN`` gate node; the runner persists a durable PAUSED checkpoint
(prompt + the state keys the human must supply via ``output_contract``).
This command group lets an operator find those paused runs and signal a
decision to resume them:

* ``mdk workflow runs [--paused]`` — list workflow runs (newest first);
  ``--paused`` narrows to the HITL queue and prints each gate's prompt.
* ``mdk workflow signal <workflow_run_id> --decision <JSON|file|->`` — POST
  the human's decision; the runtime validates + enqueues a continuation job
  and this command prints the continuation job id.

Talks to the runtime's HTTP API (``/api/v1/workflow-runs``) via
:class:`MovateClient` — same transport + target resolution as ``mdk jobs``.
The signal endpoint does NOT run the workflow inline; the worker resumes it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._console import error, get_global_target, hint
from movate.cli._output import TableJson
from movate.cli._progress import spinner
from movate.core.client import MovateClient, MovateClientError
from movate.core.models import WorkflowStatus
from movate.core.user_config import (
    UserConfigError,
    resolve_bearer_token,
    resolve_target,
)
from movate.runtime.schemas import RunAccepted, WorkflowRunListView

stdout = Console()
err = Console(stderr=True)

workflow_app = typer.Typer(
    name="workflow",
    help="Inspect + resume (HITL) workflow runs on a deployed movate runtime.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@workflow_app.command("runs")
def runs(
    paused: bool = typer.Option(
        False,
        "--paused",
        help="Only show PAUSED runs awaiting a human signal (the HITL queue).",
    ),
    limit: int = typer.Option(20, "--limit", "-n", help="Max rows to return (server caps at 100)."),
    target: str = typer.Option(None, "--target", "-t", help="Deployment target name."),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """List this tenant's workflow runs on the target runtime, newest first.

    [bold]Examples:[/bold]

      [dim]# Paused runs waiting on a human decision[/dim]
      $ mdk workflow runs --paused

      [dim]# All workflow runs, pipe-friendly[/dim]
      $ mdk workflow runs -o json | jq '.workflow_runs[].workflow_run_id'
    """
    status = WorkflowStatus.PAUSED if paused else None
    listing = asyncio.run(_fetch_runs(target=target, status=status, limit=limit))
    if output_format == TableJson.JSON:
        stdout.print(listing.model_dump_json(indent=2), soft_wrap=True, highlight=False)
        return
    if listing.count == 0:
        filter_desc = " paused" if paused else ""
        hint(f"[dim]no{filter_desc} workflow runs found[/dim]")
        return
    paused_label = " paused" if paused else ""
    title = f"{listing.count}{paused_label} workflow run(s) on {target or '<active>'}"
    table = Table(title=title)
    table.add_column("workflow_run_id", style="dim")
    table.add_column("workflow", overflow="fold")
    table.add_column("status")
    table.add_column("gate prompt", overflow="fold")
    icon = {
        WorkflowStatus.SUCCESS: "[green]✓ success[/green]",
        WorkflowStatus.ERROR: "[red]✗ error[/red]",
        WorkflowStatus.PAUSED: "[yellow]⏸ paused[/yellow]",
    }
    for w in listing.workflow_runs:
        prompt = ""
        if w.human_task:
            prompt = str(w.human_task.get("prompt", ""))
            contract = w.human_task.get("output_contract") or []
            if contract:
                prompt += f"  [dim](needs: {', '.join(str(k) for k in contract)})[/dim]"
        table.add_row(
            w.workflow_run_id[:8] + "…",
            f"{w.workflow}@{w.workflow_version}",
            icon.get(w.status, w.status.value),
            prompt,
        )
    stdout.print(table)


@workflow_app.command("signal")
def signal(
    workflow_run_id: str = typer.Argument(..., help="Paused workflow_run_id to resume."),
    decision: str = typer.Option(
        ...,
        "--decision",
        "-d",
        help=(
            "The human decision as a JSON object, a file path, or '-' for "
            "stdin. Must include every key the gate's output_contract names."
        ),
    ),
    target: str = typer.Option(None, "--target", "-t", help="Deployment target name."),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """Signal a human decision to resume a paused workflow run.

    The runtime validates the decision against the gate's
    ``output_contract``, merges it into the checkpoint, and enqueues a
    continuation job that the worker resumes from the gate's successor. This
    prints the continuation job id (poll it with ``mdk jobs wait <id>``).

    [bold]Examples:[/bold]

      [dim]# Approve and continue[/dim]
      $ mdk workflow signal 1f3c... --decision '{"decision": "approve"}'

      [dim]# Decision from a file[/dim]
      $ mdk workflow signal 1f3c... -d ./approval.json
    """
    try:
        payload = _coerce_decision(decision)
    except ValueError as exc:
        error(str(exc), context="decision")
        raise typer.Exit(code=2) from None

    accepted = asyncio.run(
        _signal(target=target, workflow_run_id=workflow_run_id, decision=payload)
    )
    if output_format == TableJson.JSON:
        stdout.print(accepted.model_dump_json(indent=2), soft_wrap=True, highlight=False)
        return
    stdout.print(
        f"[green]✓[/green] signalled — continuation job "
        f"[bold]{accepted.job_id}[/bold] ({accepted.status.value})"
    )
    hint(f"[dim]poll it:[/dim] mdk jobs wait {accepted.job_id}")


# ---------------------------------------------------------------------------
# Decision coercion — same rules as `mdk submit --input` (JSON / file / '-').
# ---------------------------------------------------------------------------


def _coerce_decision(arg: str) -> dict[str, Any]:
    """Parse the decision from a JSON object string, a file path, or '-' (stdin)."""
    import json  # noqa: PLC0415
    import sys  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    if arg == "-":
        return _ensure_dict(json.loads(sys.stdin.read()))
    stripped = arg.lstrip()
    if stripped.startswith("{"):
        try:
            return _ensure_dict(json.loads(arg))
        except json.JSONDecodeError as exc:
            raise ValueError(f"--decision looks like JSON but failed to parse: {exc}") from exc
    try:
        is_file = Path(arg).is_file()
    except OSError:
        is_file = False
    if is_file:
        return _ensure_dict(json.loads(Path(arg).read_text()))
    try:
        parsed = json.loads(arg)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--decision must be a JSON object, file path, or '-': {exc}") from exc
    return _ensure_dict(parsed)


def _ensure_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"--decision must be a JSON object, got {type(value).__name__}")
    return value


# ---------------------------------------------------------------------------
# Async glue
# ---------------------------------------------------------------------------


async def _fetch_runs(
    *, target: str | None, status: WorkflowStatus | None, limit: int
) -> WorkflowRunListView:
    client = _build_client(target)
    try:
        async with client:
            with spinner("fetching workflow runs..."):
                return await client.list_workflow_runs(status=status, limit=limit)
    except MovateClientError as exc:
        error(str(exc), context="runs")
        raise typer.Exit(code=exc.status_code // 100) from None


async def _signal(
    *, target: str | None, workflow_run_id: str, decision: dict[str, Any]
) -> RunAccepted:
    client = _build_client(target)
    try:
        async with client:
            with spinner("signalling..."):
                return await client.signal_workflow_run(workflow_run_id, decision=decision)
    except MovateClientError as exc:
        error(str(exc), context="signal")
        raise typer.Exit(code=exc.status_code // 100) from None


def _build_client(target: str | None) -> MovateClient:
    """Resolve target name → MovateClient. Exits cleanly on config errors.

    Mirrors ``mdk jobs`` target precedence: per-command ``--target`` >
    top-level ``-t`` / ``MOVATE_TARGET`` > active config target.
    """
    try:
        _, target_cfg = resolve_target(target or get_global_target())
        token = resolve_bearer_token(target_cfg)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None
    return MovateClient(base_url=target_cfg.url, api_key=token)


__all__ = ["workflow_app"]
