"""``mdk batch`` — bulk async inference over a dataset (item 17).

Submit a whole JSONL dataset for an agent and movate enqueues ONE ordinary
``JobKind.AGENT`` job per row at the target runtime, all sharing a
``batch_id``. Because each row is a normal queue job, it inherits retry /
dead-letter / canary / observability for free — there is no new execution
path. A parent ``BatchRecord`` lets the status command aggregate progress.

Pairs with ``mdk config add-target`` (the CLI knows which runtime to talk to
and which env var holds the bearer token), exactly like ``mdk submit``.

Subcommands:

* ``submit <agent> <dataset.jsonl> [--target] [--wait]`` — POST the dataset;
  print ``batch_id`` + total. ``--wait`` polls the status endpoint to
  completion (like ``mdk submit --wait``).
* ``status <batch_id>`` — render the per-status aggregate + derived state.
* ``list`` — this tenant's recent batches at the target.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._completion import complete_agent_name
from movate.cli._console import error, get_global_target, hint
from movate.cli._output import TableJson
from movate.cli._progress import spinner
from movate.core.client import MovateClient, MovateClientError
from movate.core.user_config import (
    UserConfigError,
    resolve_bearer_token,
    resolve_target,
)
from movate.runtime.schemas import BatchListView, BatchStatusView

stdout = Console()
err = Console(stderr=True)

batch_app = typer.Typer(
    name="batch",
    help="Bulk async inference: submit a dataset, run one job per row (item 17).",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------


@batch_app.command("submit")
def submit_batch(
    agent: str = typer.Argument(
        ...,
        help="Agent name registered on the target runtime.",
        shell_complete=complete_agent_name,
    ),
    dataset: Path = typer.Argument(
        ...,
        metavar="DATASET.JSONL",
        help="Path to a JSONL dataset — one JSON object per line = one run's input.",
    ),
    target: str = typer.Option(
        None,
        "--target",
        "-t",
        help=(
            "Deployment target name (from `mdk config list-targets`). "
            "Omit to use the active target."
        ),
    ),
    wait: bool = typer.Option(
        False,
        "--wait",
        "-w",
        help="Block until every row reaches a terminal state, then print the aggregate.",
    ),
    timeout: float = typer.Option(
        600.0,
        "--timeout",
        help=(
            "Max seconds to wait when --wait is set. After this the batch "
            "continues server-side; CLI exits 124."
        ),
    ),
    poll_interval: float = typer.Option(
        2.0, "--poll-interval", help="Seconds between batch-status polls (--wait only)."
    ),
    notify_email: str = typer.Option(
        None,
        "--notify-email",
        help="Email the worker notifies as EACH row reaches a terminal status.",
    ),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """Submit a JSONL dataset as a batch of async runs.

    [bold]Examples:[/bold]

      [dim]# Fire-and-forget against the active target[/dim]
      $ mdk batch submit faq-agent prompts.jsonl
      → prints {"batch_id": "...", "total": 42, "status": "queued"}

      [dim]# Wait for the whole batch to finish[/dim]
      $ mdk batch submit faq-agent prompts.jsonl --wait
    """
    try:
        rows = _read_jsonl(dataset)
    except (OSError, ValueError) as exc:
        error(str(exc), context="batch")
        raise typer.Exit(code=2) from None
    if not rows:
        error(f"dataset {dataset} has no rows — nothing to submit")
        raise typer.Exit(code=2)

    try:
        target_name, target_cfg = resolve_target(target or get_global_target())
        token = resolve_bearer_token(target_cfg)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None

    asyncio.run(
        _submit(
            target_name=target_name,
            base_url=target_cfg.url,
            token=token,
            agent=agent,
            rows=rows,
            notify_email=notify_email,
            wait=wait,
            timeout=timeout,
            poll_interval=poll_interval,
            output_format=output_format,
        )
    )


async def _submit(
    *,
    target_name: str,
    base_url: str,
    token: str,
    agent: str,
    rows: list[dict[str, Any]],
    notify_email: str | None,
    wait: bool,
    timeout: float,
    poll_interval: float,
    output_format: TableJson,
) -> None:
    async with MovateClient(base_url=base_url, api_key=token) as client:
        try:
            with spinner(f"submitting {len(rows)} rows to {target_name}..."):
                accepted = await client.submit_batch(
                    agent=agent, rows=rows, notify_email=notify_email
                )
        except MovateClientError as exc:
            error(str(exc), context="batch")
            raise typer.Exit(code=1) from None

        if not wait:
            if output_format == TableJson.JSON:
                stdout.print(accepted.model_dump_json(), soft_wrap=True, highlight=False)
            else:
                stdout.print(accepted.model_dump_json(), soft_wrap=True, highlight=False)
                hint(
                    f"[dim]queued batch {accepted.batch_id} ({accepted.total} rows) on "
                    f"{target_name}. Poll with: mdk batch status {accepted.batch_id}"
                    + (f" -t {target_name}" if target_name != "local" else "")
                    + "[/dim]"
                )
            return

        try:
            with spinner("waiting for batch to complete..."):
                final = await client.wait_for_batch(
                    accepted.batch_id,
                    poll_interval_seconds=poll_interval,
                    max_wait_seconds=timeout,
                )
        except TimeoutError as exc:
            err.print(f"[yellow]⏱[/yellow] {exc}")
            raise typer.Exit(code=124) from None
        except MovateClientError as exc:
            error(str(exc), context="poll")
            raise typer.Exit(code=1) from None

        _emit_status(final, output_format=output_format)
        # Exit 1 if any row failed so CI scripts can branch.
        if final.counts.error or final.counts.safety_blocked or final.counts.dead_letter:
            raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@batch_app.command("status")
def batch_status(
    batch_id: str = typer.Argument(..., help="Batch id returned by `mdk batch submit`."),
    target: str = typer.Option(
        None,
        "--target",
        "-t",
        help="Deployment target name. Omit to use the active target.",
    ),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """Show the per-status aggregate + derived state of a batch."""
    try:
        _target_name, target_cfg = resolve_target(target or get_global_target())
        token = resolve_bearer_token(target_cfg)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None

    asyncio.run(
        _status(
            base_url=target_cfg.url,
            token=token,
            batch_id=batch_id,
            output_format=output_format,
        )
    )


async def _status(
    *,
    base_url: str,
    token: str,
    batch_id: str,
    output_format: TableJson,
) -> None:
    async with MovateClient(base_url=base_url, api_key=token) as client:
        try:
            view = await client.get_batch(batch_id)
        except MovateClientError as exc:
            error(str(exc), context="batch")
            raise typer.Exit(code=1) from None
    _emit_status(view, output_format=output_format)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@batch_app.command("list")
def list_batches(
    target: str = typer.Option(
        None,
        "--target",
        "-t",
        help="Deployment target name. Omit to use the active target.",
    ),
    limit: int = typer.Option(20, "--limit", help="Max batches to show (server-capped at 100)."),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """List this tenant's recent batches at the target."""
    try:
        _target_name, target_cfg = resolve_target(target or get_global_target())
        token = resolve_bearer_token(target_cfg)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None

    asyncio.run(
        _list(
            base_url=target_cfg.url,
            token=token,
            limit=limit,
            output_format=output_format,
        )
    )


async def _list(
    *,
    base_url: str,
    token: str,
    limit: int,
    output_format: TableJson,
) -> None:
    async with MovateClient(base_url=base_url, api_key=token) as client:
        try:
            view = await client.list_batches(limit=limit)
        except MovateClientError as exc:
            error(str(exc), context="batch")
            raise typer.Exit(code=1) from None
    _emit_list(view, output_format=output_format)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _emit_status(view: BatchStatusView, *, output_format: TableJson) -> None:
    if output_format == TableJson.JSON:
        stdout.print(view.model_dump_json(), soft_wrap=True, highlight=False)
        return
    icon = "[green]✓[/green]" if view.state == "complete" else "[yellow]…[/yellow]"
    table = Table(title=f"{icon} batch {view.batch_id} ({view.agent})", show_header=False)
    table.add_column("field", style="dim")
    table.add_column("value")
    table.add_row("state", view.state)
    table.add_row("total", str(view.total))
    c = view.counts
    table.add_row("queued", str(c.queued))
    table.add_row("running", str(c.running))
    table.add_row("success", str(c.success))
    table.add_row("error", str(c.error))
    table.add_row("safety_blocked", str(c.safety_blocked))
    table.add_row("dead_letter", str(c.dead_letter))
    stdout.print(table)


def _emit_list(view: BatchListView, *, output_format: TableJson) -> None:
    if output_format == TableJson.JSON:
        stdout.print(view.model_dump_json(), soft_wrap=True, highlight=False)
        return
    if not view.batches:
        stdout.print("[dim]no batches — submit one with[/dim] mdk batch submit <agent> <dataset>")
        return
    table = Table(title="Batches")
    table.add_column("batch_id", style="bold")
    table.add_column("agent")
    table.add_column("total")
    table.add_column("created")
    for b in view.batches:
        table.add_row(
            b.batch_id,
            b.agent,
            str(b.total),
            b.created_at.isoformat(timespec="seconds"),
        )
    stdout.print(table)


# ---------------------------------------------------------------------------
# Dataset reading — JSONL, one JSON object per line. '-' reads stdin.
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file (or stdin when ``path`` is ``-``) into input rows.

    Every non-empty line must be a JSON object — same contract the server
    enforces. Raises ``ValueError`` (named line) on a malformed / non-object
    line so the operator fixes it before the request leaves the machine.
    """
    raw = sys.stdin.read() if str(path) == "-" else path.read_text()
    rows: list[dict[str, Any]] = []
    for lineno, raw_line in enumerate(raw.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"dataset line {lineno} is not valid JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise ValueError(
                f"dataset line {lineno} must be a JSON object, got {type(obj).__name__}"
            )
        rows.append(obj)
    return rows


__all__ = ["batch_app"]
