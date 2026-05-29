"""``mdk workflow`` — inspect + resume workflow runs AND manage workflow
definitions on a deployed runtime.

Two surfaces under one command group:

**Runs** (ADR 017 D5 PR 2 — HITL resume-on-signal):

* ``mdk workflow runs [--paused]`` — list workflow runs (newest first);
  ``--paused`` narrows to the HITL queue and prints each gate's prompt.
* ``mdk workflow signal <workflow_run_id> --decision <JSON|file|->`` — POST
  the human's decision; the runtime validates + enqueues a continuation job
  and this command prints the continuation job id.

**Definitions** (ADR 037 D1 — workflow API parity):

* ``mdk workflow list [--published-only]`` — list workflow definitions in
  the target tenant.
* ``mdk workflow show <name> [--version V]`` — full spec + bundle metadata.
* ``mdk workflow publish <name> [--version V]`` — promote a version to
  published (the "blessed" pointer the catalog filters on).
* ``mdk workflow revert <name> --to-version V`` — non-destructive rollback.
* ``mdk workflow validate <path>`` — parse + structurally validate a local
  workflow.yaml against the runtime's compiler.

Talks to the runtime's HTTP API via :class:`MovateClient` — same transport +
target resolution as ``mdk jobs``. The signal endpoint does NOT run the
workflow inline; the worker resumes it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._console import echo_remote_context, error, get_global_target, hint
from movate.cli._output import TableJson
from movate.cli._progress import spinner
from movate.core.client import MovateClient, MovateClientError
from movate.core.models import WorkflowStatus
from movate.core.user_config import (
    UserConfigError,
    resolve_bearer_token,
    resolve_target,
)
from movate.runtime.schemas import (
    RunAccepted,
    WorkflowDetailView,
    WorkflowListResponse,
    WorkflowPublishedView,
    WorkflowRevertedView,
    WorkflowRunListView,
    WorkflowValidationView,
)

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
    listing = asyncio.run(
        _fetch_runs(
            target=target, status=status, limit=limit, suppress=output_format == TableJson.JSON
        )
    )
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
        _signal(
            target=target,
            workflow_run_id=workflow_run_id,
            decision=payload,
            suppress=output_format == TableJson.JSON,
        )
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
    *, target: str | None, status: WorkflowStatus | None, limit: int, suppress: bool = False
) -> WorkflowRunListView:
    client = _build_client(target, suppress=suppress)
    try:
        async with client:
            with spinner("fetching workflow runs..."):
                return await client.list_workflow_runs(status=status, limit=limit)
    except MovateClientError as exc:
        error(str(exc), context="runs")
        raise typer.Exit(code=exc.status_code // 100) from None


async def _signal(
    *, target: str | None, workflow_run_id: str, decision: dict[str, Any], suppress: bool = False
) -> RunAccepted:
    client = _build_client(target, suppress=suppress)
    try:
        async with client:
            with spinner("signalling..."):
                return await client.signal_workflow_run(workflow_run_id, decision=decision)
    except MovateClientError as exc:
        error(str(exc), context="signal")
        raise typer.Exit(code=exc.status_code // 100) from None


def _build_client(target: str | None, *, suppress: bool = False) -> MovateClient:
    """Resolve target name → MovateClient. Exits cleanly on config errors.

    Mirrors ``mdk jobs`` target precedence: per-command ``--target`` >
    top-level ``-t`` / ``MOVATE_TARGET`` > active config target.

    Echoes the resolved target + URL + credential source (masked) on
    stderr before returning, so a 401/403 is self-diagnosing.
    ``suppress`` (passed by ``-o json`` callers) silences the echo.
    """
    try:
        target_name, target_cfg = resolve_target(target or get_global_target())
        token = resolve_bearer_token(target_cfg)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None
    echo_remote_context(target_name, target_cfg, suppress=suppress)
    return MovateClient(base_url=target_cfg.url, api_key=token)


# ---------------------------------------------------------------------------
# Workflow definitions (ADR 037 D1 — workflow API parity)
# ---------------------------------------------------------------------------


@workflow_app.command("list")
def list_workflows(
    published_only: bool = typer.Option(
        False,
        "--published-only",
        help="Filter to workflows that have at least one published version.",
    ),
    limit: int = typer.Option(100, "--limit", "-n", help="Max rows to return."),
    target: str = typer.Option(None, "--target", "-t", help="Deployment target name."),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """List workflow definitions on the target runtime, newest-first.

    [bold]Examples:[/bold]

      [dim]# Every workflow in the tenant[/dim]
      $ mdk workflow list

      [dim]# Only workflows with a blessed version[/dim]
      $ mdk workflow list --published-only
    """
    listing = asyncio.run(
        _fetch_workflows(
            target=target,
            published_only=published_only,
            limit=limit,
            suppress=output_format == TableJson.JSON,
        )
    )
    if output_format == TableJson.JSON:
        stdout.print(listing.model_dump_json(indent=2), soft_wrap=True, highlight=False)
        return
    if listing.count == 0:
        hint("[dim]no workflow definitions found[/dim]")
        return
    table = Table(title=f"{listing.count} workflow definition(s)")
    table.add_column("name", style="bold")
    table.add_column("latest", style="dim")
    table.add_column("published", style="dim")
    table.add_column("description", overflow="fold")
    for w in listing.workflows:
        table.add_row(
            w.name,
            w.version,
            w.published_version or "[dim]—[/dim]",
            w.description,
        )
    stdout.print(table)


@workflow_app.command("show")
def show_workflow(
    name: str = typer.Argument(..., help="Workflow name (workflow.yaml ``name``)."),
    version: str = typer.Option(
        None, "--version", help="Specific version; omit for the current latest."
    ),
    target: str = typer.Option(None, "--target", "-t", help="Deployment target name."),
    as_json: bool = typer.Option(
        False, "--json", help="Emit the detail view as JSON instead of a table."
    ),
) -> None:
    """Show the full spec + bundle metadata for one workflow."""
    detail = asyncio.run(
        _fetch_workflow(
            target=target,
            name=name,
            version=version,
            suppress=as_json,
        )
    )
    if as_json:
        stdout.print(detail.model_dump_json(indent=2), soft_wrap=True, highlight=False)
        return
    table = Table(title=f"workflow {detail.name}@{detail.version}")
    table.add_column("field", style="bold")
    table.add_column("value", overflow="fold")
    table.add_row("description", detail.description)
    table.add_row("owner", detail.owner)
    table.add_row("tags", ", ".join(detail.tags) or "[dim]—[/dim]")
    table.add_row("entrypoint", detail.entrypoint)
    table.add_row("state_schema", detail.state_schema_path)
    table.add_row("content_hash", detail.content_hash[:16] + "…")
    table.add_row("created_by", detail.created_by or "[dim]<system>[/dim]")
    table.add_row("created_at", detail.created_at.isoformat())
    table.add_row("published_version", detail.published_version or "[dim]—[/dim]")
    table.add_row("nodes", str(len(detail.nodes)))
    table.add_row("edges", str(len(detail.edges)))
    table.add_row("files", ", ".join(detail.files))
    stdout.print(table)


@workflow_app.command("publish")
def publish_workflow(
    name: str = typer.Argument(..., help="Workflow name."),
    version: str = typer.Option(
        None,
        "--version",
        help="Version to promote; omit to promote the current latest.",
    ),
    target: str = typer.Option(None, "--target", "-t", help="Deployment target name."),
    as_json: bool = typer.Option(False, "--json", help="Emit the response as JSON."),
) -> None:
    """Promote a workflow version to "published"."""
    result = asyncio.run(
        _publish_workflow(
            target=target,
            name=name,
            version=version,
            suppress=as_json,
        )
    )
    if as_json:
        stdout.print(result.model_dump_json(indent=2), soft_wrap=True, highlight=False)
        return
    prior = result.previous_published_version or "[dim]—[/dim]"
    stdout.print(
        f"[green]✓[/green] published [bold]{result.name}[/bold]@"
        f"[bold]{result.published_version}[/bold] (prev: {prior})"
    )


@workflow_app.command("revert")
def revert_workflow(
    name: str = typer.Argument(..., help="Workflow name."),
    to_version: str = typer.Option(
        ...,
        "--to-version",
        help="Existing version to roll back to (re-published forward).",
    ),
    target: str = typer.Option(None, "--target", "-t", help="Deployment target name."),
    as_json: bool = typer.Option(False, "--json", help="Emit the response as JSON."),
) -> None:
    """Roll a workflow back to a prior version (non-destructive).

    The targeted version's bundle is re-published as a NEW latest version
    with a ``+revert.N`` suffix; the prior history stays intact so a
    follow-up revert is always possible.
    """
    result = asyncio.run(
        _revert_workflow(
            target=target,
            name=name,
            to_version=to_version,
            suppress=as_json,
        )
    )
    if as_json:
        stdout.print(result.model_dump_json(indent=2), soft_wrap=True, highlight=False)
        return
    stdout.print(
        f"[green]✓[/green] reverted [bold]{result.name}[/bold] to "
        f"[bold]{result.reverted_from}[/bold] (new latest: {result.version})"
    )


@workflow_app.command("validate")
def validate_workflow(
    path: str = typer.Argument(
        ...,
        help=(
            "Path to a workflow.yaml or its containing directory. The CLI "
            "uploads the spec + sibling state schema to the target runtime."
        ),
    ),
    workflow_name: str = typer.Option(
        None,
        "--name",
        help=(
            "Workflow name to validate against on the remote (the validate "
            "endpoint is per-name but doesn't require the workflow to exist "
            "yet). Defaults to the path's basename."
        ),
    ),
    target: str = typer.Option(None, "--target", "-t", help="Deployment target name."),
    as_json: bool = typer.Option(False, "--json", help="Emit the response as JSON."),
) -> None:
    """Validate a local workflow.yaml against the target runtime."""
    from pathlib import Path  # noqa: PLC0415

    p = Path(path).resolve()
    wf_yaml = p / "workflow.yaml" if p.is_dir() else p
    if not wf_yaml.is_file():
        error(f"workflow.yaml not found at {wf_yaml}", context="validate")
        raise typer.Exit(code=2)
    workflow_yaml_text = wf_yaml.read_text(encoding="utf-8")
    extra_files: dict[str, str] = {}
    # Bundle the sibling state schema if present (the spec usually references
    # ``./schema/state.json`` or ``./state.json``). The remote loader needs it
    # to compile.
    workflow_dir = wf_yaml.parent
    for candidate in ("schema/state.json", "state.json"):
        sibling = workflow_dir / candidate
        if sibling.is_file():
            extra_files[candidate] = sibling.read_text(encoding="utf-8")
    name = workflow_name or workflow_dir.name
    result = asyncio.run(
        _validate_workflow(
            target=target,
            name=name,
            workflow_yaml=workflow_yaml_text,
            files=extra_files,
            suppress=as_json,
        )
    )
    if as_json:
        stdout.print(result.model_dump_json(indent=2), soft_wrap=True, highlight=False)
        return
    if result.passed:
        stdout.print(f"[green]✓[/green] {name}: validated")
        return
    stdout.print(f"[red]✗[/red] {name}: validation failed")
    for issue in result.errors:
        stdout.print(f"  [red]error[/red] {issue.code}: {issue.message}")
    raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Async glue for the definition commands
# ---------------------------------------------------------------------------


async def _fetch_workflows(
    *, target: str | None, published_only: bool, limit: int, suppress: bool = False
) -> WorkflowListResponse:
    client = _build_client(target, suppress=suppress)
    try:
        async with client:
            with spinner("fetching workflows..."):
                return await client.list_workflows(published_only=published_only, limit=limit)
    except MovateClientError as exc:
        error(str(exc), context="list")
        raise typer.Exit(code=exc.status_code // 100) from None


async def _fetch_workflow(
    *, target: str | None, name: str, version: str | None, suppress: bool = False
) -> WorkflowDetailView:
    client = _build_client(target, suppress=suppress)
    try:
        async with client:
            with spinner("fetching workflow..."):
                return await client.get_workflow(name, version=version)
    except MovateClientError as exc:
        error(str(exc), context="show")
        raise typer.Exit(code=exc.status_code // 100) from None


async def _publish_workflow(
    *, target: str | None, name: str, version: str | None, suppress: bool = False
) -> WorkflowPublishedView:
    client = _build_client(target, suppress=suppress)
    try:
        async with client:
            with spinner("publishing..."):
                return await client.publish_workflow(name, version=version)
    except MovateClientError as exc:
        error(str(exc), context="publish")
        raise typer.Exit(code=exc.status_code // 100) from None


async def _revert_workflow(
    *, target: str | None, name: str, to_version: str, suppress: bool = False
) -> WorkflowRevertedView:
    client = _build_client(target, suppress=suppress)
    try:
        async with client:
            with spinner("reverting..."):
                return await client.revert_workflow(name, to_version=to_version)
    except MovateClientError as exc:
        error(str(exc), context="revert")
        raise typer.Exit(code=exc.status_code // 100) from None


async def _validate_workflow(
    *,
    target: str | None,
    name: str,
    workflow_yaml: str,
    files: dict[str, str],
    suppress: bool = False,
) -> WorkflowValidationView:
    client = _build_client(target, suppress=suppress)
    try:
        async with client:
            with spinner("validating..."):
                return await client.validate_workflow_spec(
                    name, workflow_yaml=workflow_yaml, files=files
                )
    except MovateClientError as exc:
        error(str(exc), context="validate")
        raise typer.Exit(code=exc.status_code // 100) from None


__all__ = ["workflow_app"]
