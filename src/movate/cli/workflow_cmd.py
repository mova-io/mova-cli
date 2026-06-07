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
import json
from pathlib import Path
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

# Length of "YYYY-MM-DDTHH:MM:SS" for timestamp display truncation.
_TS_DISPLAY_LEN = 19

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
    table.add_column("approvers", overflow="fold")
    table.add_column("gate prompt", overflow="fold")
    icon = {
        WorkflowStatus.SUCCESS: "[green]✓ success[/green]",
        WorkflowStatus.ERROR: "[red]✗ error[/red]",
        WorkflowStatus.PAUSED: "[yellow]⏸ paused[/yellow]",
    }
    for w in listing.workflow_runs:
        prompt = ""
        approvers_cell = ""
        if w.human_task:
            prompt = str(w.human_task.get("prompt", ""))
            contract = w.human_task.get("output_contract") or []
            if contract:
                prompt += f"  [dim](needs: {', '.join(str(k) for k in contract)})[/dim]"
            approvers = w.human_task.get("approvers") or []
            # Who can clear this gate (ADR 062/083). "anyone" when unrestricted,
            # so an operator scanning the HITL queue knows whom to chase.
            approvers_cell = (
                ", ".join(str(a) for a in approvers) if approvers else "[dim]anyone[/dim]"
            )
        table.add_row(
            w.workflow_run_id[:8] + "…",
            f"{w.workflow}@{w.workflow_version}",
            icon.get(w.status, w.status.value),
            approvers_cell,
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
# Replay — deterministic time-travel against a Temporal run's history
# (ADR 054 D6 / Phase 3). Lives under `mdk workflow` because the run it
# targets is a workflow_run_id (== the Temporal workflow id, D6).
# ---------------------------------------------------------------------------


@workflow_app.command("replay")
def replay(
    run_id: str = typer.Argument(
        ...,
        help=(
            "The workflow_run_id to replay (== the Temporal workflow id, ADR 054 "
            "D6). From [bold]mdk workflow runs[/bold] or [bold]mdk runs show[/bold]."
        ),
        metavar="RUN_ID",
    ),
    workflows_path: Path = typer.Option(
        Path("./workflows"),
        "--workflows-path",
        help=(
            "Directory holding the workflow bundles (the same layout "
            "[bold]mdk worker[/bold] scans). The run's workflow is recompiled "
            "from its on-disk definition here."
        ),
    ),
    tenant_id: str = typer.Option(
        "local",
        "--tenant-id",
        help="Tenant scope for the storage lookup (default: 'local' for CLI use).",
    ),
    target: str = typer.Option(
        None,
        "--target",
        "-t",
        help="Temporal namespace / deployment target to connect to.",
    ),
    from_file: Path = typer.Option(
        None,
        "--from-file",
        help=(
            "Replay from an exported event-history JSON file instead of fetching "
            "from a live Temporal server. Use [bold]mdk workflow history <run_id> "
            "--output history.json[/bold] to export."
        ),
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Print each activity replay step as it is re-executed.",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit the replay result as JSON instead of a table."
    ),
) -> None:
    """Replay a Temporal-backed workflow run against its recorded history.

    Time-travel for a past ``runtime: temporal`` run: fetch its full event
    history from Temporal (the run's id == the mdk workflow_run_id, ADR 054
    D6), recompile the workflow from its on-disk definition (the SAME Track-B
    compile path the worker uses), and re-feed history through the workflow
    code via :class:`temporalio.worker.Replayer`. If the code reproduces every
    decision the history recorded, the run is [bold]deterministic[/bold]; any
    drift surfaces as a non-determinism error rather than a silent pass.

    Replay does NOT re-execute activities (no LLM calls, no side effects, no
    cost) — it replays the workflow's *decisions* against the durable history.

    This is a verification/diagnosis tool: use it after editing a workflow to
    confirm an in-flight run's trajectory still type-checks against the new
    code, or to reproduce exactly what a past run decided.

    [bold]Offline mode:[/bold] pass ``--from-file history.json`` to replay from
    an exported event-history file (no live Temporal connection needed). Export
    with ``mdk workflow history <run_id> --output history.json``.

    [bold]Examples:[/bold]

      [dim]# Verify a past run replays deterministically[/dim]
      $ mdk workflow replay 1f3c...

      [dim]# Replay from an exported history file (offline)[/dim]
      $ mdk workflow replay 1f3c... --from-file history.json

      [dim]# Verbose output showing each activity replay step[/dim]
      $ mdk workflow replay 1f3c... --verbose

      [dim]# Machine-readable result[/dim]
      $ mdk workflow replay 1f3c... --json
    """
    # Normalize typer OptionInfo → None when called directly (e.g. tests).
    _from_file = from_file if isinstance(from_file, Path) else None
    result = asyncio.run(
        _replay_workflow(
            run_id=run_id,
            workflows_path=Path(workflows_path),
            tenant_id=tenant_id,
            target=target,
            from_file=_from_file,
            verbose=verbose,
            suppress=as_json,
        )
    )
    if as_json:
        stdout.print(json.dumps(result, indent=2), soft_wrap=True, highlight=False)
        raise typer.Exit(code=0 if result["deterministic"] else 1)
    if result["deterministic"]:
        stdout.print(
            "[green]✓ Replay matches — deterministic execution confirmed[/green] — "
            f"[bold]{result['workflow']}[/bold] run "
            f"[dim]{run_id}[/dim] reproduces identical decisions against its "
            f"recorded history."
        )
        return
    stdout.print(
        f"[red]✗ non-deterministic[/red] — [bold]{result['workflow']}[/bold] run "
        f"[dim]{run_id}[/dim] diverged from its recorded history:"
    )
    stdout.print(f"  [red]{result['divergence']}[/red]")
    hint(
        "[dim]the current on-disk workflow no longer reproduces this run's "
        "decisions — a code change broke determinism for this history.[/dim]"
    )
    raise typer.Exit(code=1)


async def _replay_workflow(
    *,
    run_id: str,
    workflows_path: Path,
    tenant_id: str,
    target: str | None = None,
    from_file: Path | None = None,
    verbose: bool = False,
    suppress: bool,
) -> dict[str, Any]:
    """Resolve -> recompile -> fetch history -> replay. Returns a result dict.

    Exits cleanly (``typer.Exit``) on every operator-facing failure: an
    unknown run, a native/non-temporal run, a missing on-disk definition, or
    a missing ``[temporal]`` extra / connection. Never leaks a traceback.

    ``from_file`` enables offline mode: replay from an exported event-history
    JSON file instead of fetching from a live Temporal server. The [temporal]
    extra is still required (the Replayer lives there), but no Temporal
    connection is needed.

    ``target`` selects the Temporal namespace when fetching history live.

    ``verbose`` prints each activity replay step to stderr.
    """
    from movate.runtime.workflow_backend import (  # noqa: PLC0415
        WorkflowBackendError,
        load_compiled_workflow_class,
        require_backend_available,
    )

    # 1. Resolve the run from storage -> recover the workflow name (D6: the
    #    workflow_run_id is both the storage key AND the Temporal workflow id).
    record = await _fetch_workflow_run(run_id=run_id, tenant_id=tenant_id)
    if record is None:
        error(
            f"workflow run {run_id!r} not found under tenant {tenant_id!r}. "
            "Run `mdk workflow runs` to browse recent runs.",
            context="replay",
        )
        raise typer.Exit(code=1)

    # 2. Recover the WorkflowGraph from the on-disk definition and confirm it
    #    is a runtime: temporal workflow. A native/langgraph run has no
    #    Temporal history to replay -- reject cleanly, never crash.
    graph = _load_workflow_graph(record.workflow, workflows_path)
    if graph is None:
        error(
            f"workflow {record.workflow!r} (for run {run_id}) not found under "
            f"{workflows_path}. Point --workflows-path at the directory holding "
            "its workflow.yaml.",
            context="replay",
        )
        raise typer.Exit(code=2)
    runtime = getattr(graph, "runtime", "native") or "native"
    if runtime != "temporal":
        error(
            f"replay requires a Temporal-backed run (runtime: temporal); this run used {runtime}.",
            context="replay",
        )
        raise typer.Exit(code=2)

    # 3. Recompile to the @workflow.defn class (the SAME path the worker uses)
    #    and ensure the [temporal] extra is available (fail loud,
    #    ADR 054 D6 / ADR 055 D6). In offline mode (--from-file) we only need
    #    the extra, not a live Temporal connection.
    if from_file is not None:
        # Offline mode: just need the [temporal] extra for the Replayer.
        try:
            import temporalio  # noqa: F401, PLC0415
        except ImportError:
            error(
                "The [temporal] extra is not installed. "
                "Install with: uv tool install --editable '.[temporal]' --force",
                context="replay",
            )
            raise typer.Exit(code=2) from None
    else:
        try:
            require_backend_available("temporal")
        except WorkflowBackendError as exc:
            error(str(exc), context="replay")
            raise typer.Exit(code=2) from None

    from movate.core.workflow.compilers.temporal import TemporalCompiler  # noqa: PLC0415

    try:
        compiled = TemporalCompiler().compile(graph)
        workflow_cls = load_compiled_workflow_class(
            compiled.module_source, compiled.workflow_class_name
        )
    except WorkflowBackendError as exc:
        error(str(exc), context="replay")
        raise typer.Exit(code=2) from None
    except Exception as exc:  # a compile failure is operator-facing, not a crash.
        error(f"could not recompile workflow {record.workflow!r}: {exc}", context="replay")
        raise typer.Exit(code=2) from None

    # 4. Fetch history (from file or Temporal) and 5. replay it.
    try:
        outcome = await _fetch_history_and_replay(
            run_id=run_id,
            workflow_cls=workflow_cls,
            suppress=suppress,
            from_file=from_file,
            target=target,
            verbose=verbose,
        )
    except WorkflowBackendError as exc:
        error(str(exc), context="replay")
        raise typer.Exit(code=2) from None

    deterministic, divergence = outcome
    return {
        "run_id": run_id,
        "workflow": record.workflow,
        "workflow_version": record.workflow_version,
        "runtime": "temporal",
        "deterministic": deterministic,
        "divergence": divergence,
    }


async def _fetch_workflow_run(*, run_id: str, tenant_id: str) -> Any:
    """Pull the WorkflowRunRecord from local storage. ``None`` on miss."""
    from movate.storage import build_storage  # noqa: PLC0415

    storage = build_storage()
    await storage.init()
    try:
        return await storage.get_workflow_run(run_id, tenant_id=tenant_id)
    finally:
        await storage.close()


def _load_workflow_graph(name: str, workflows_path: Path) -> Any:
    """Recover the :class:`WorkflowGraph` for ``name`` from the on-disk bundles.

    Reuses :func:`movate.runtime.registry.scan_workflows` (the SAME scan the
    worker uses) so the graph — including its ``runtime`` — is recovered
    identically. ``None`` when no bundle by that name is present.
    """
    from movate.runtime.registry import scan_workflows  # noqa: PLC0415

    return scan_workflows(workflows_path).get(name)


async def _fetch_history_and_replay(
    *,
    run_id: str,
    workflow_cls: Any,
    suppress: bool,
    from_file: Path | None = None,
    target: str | None = None,
    verbose: bool = False,
) -> tuple[bool, str | None]:
    """Connect to Temporal (or read a file), fetch the run's history, and replay it.

    Returns ``(deterministic, divergence_message)``. ``temporalio`` is
    imported lazily HERE (import isolation -- ADR 054 D7 / ADR 055 Boundaries):
    nothing at this module's scope touches Temporal.

    Replay runs the workflow code against the durable event history WITHOUT
    re-executing activities; a :class:`temporalio.worker.Replayer` surfaces a
    non-determinism error if the current code can't reproduce the recorded
    decisions. We pass ``raise_on_replay_failure=False`` and inspect
    ``WorkflowReplayResult.replay_failure`` so divergence is reported, not
    raised as a traceback.

    ``from_file`` enables offline mode: load history from a JSON file exported
    by ``mdk workflow history <run_id> --output <file>``.

    ``target`` overrides the Temporal namespace for live connections.

    ``verbose`` prints each event type in the history to stderr.
    """
    import contextlib  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415

    from temporalio.client import WorkflowHistory  # noqa: PLC0415
    from temporalio.worker import Replayer, UnsandboxedWorkflowRunner  # noqa: PLC0415

    def _spin(message: str) -> Any:
        # Suppress the stderr spinner under --json so the channel stays clean.
        return contextlib.nullcontext() if suppress else spinner(message)

    history: WorkflowHistory
    if from_file is not None:
        # Offline mode: load history from an exported JSON file.
        if not from_file.is_file():
            from movate.runtime.workflow_backend import WorkflowBackendError  # noqa: PLC0415

            raise WorkflowBackendError(
                f"history file not found: {from_file}. "
                "Export with: mdk workflow history <run_id> --output history.json"
            )
        with _spin("loading history from file..."):
            raw = json.loads(from_file.read_text(encoding="utf-8"))
            history = WorkflowHistory.from_json(run_id, raw)
    else:
        from temporalio.client import Client  # noqa: PLC0415
        from temporalio.service import TLSConfig  # noqa: PLC0415

        from movate.runtime.workflow_backend import _resolve_temporal_connection  # noqa: PLC0415

        conn = _resolve_temporal_connection()  # fail-loud if no TEMPORAL_HOST.
        # --target overrides the namespace for this connection.
        namespace = target if target else conn.namespace

        tls: Any = False
        if conn.tls_cert_path:
            tls = TLSConfig(server_root_ca_cert=_Path(conn.tls_cert_path).read_bytes())

        with _spin("connecting to Temporal..."):
            client = await Client.connect(conn.host, namespace=namespace, tls=tls)

        with _spin("fetching workflow history..."):
            handle = client.get_workflow_handle(run_id)
            history = await handle.fetch_history()

    # Verbose: print each event type in the history to stderr.
    if verbose and not suppress:
        event_count = len(history.events)
        err.print(f"[dim]history contains {event_count} event(s):[/dim]")
        for evt in history.events:
            evt_type = evt.event_type
            # The proto enum value name is human-readable (e.g.
            # EVENT_TYPE_ACTIVITY_TASK_COMPLETED).
            evt_cls = type(evt_type)
            evt_name = evt_cls.Name(evt_type) if hasattr(evt_cls, "Name") else str(evt_type)
            err.print(f"  [dim]{evt.event_id:>4}[/dim] {evt_name}")

    # Replay unsandboxed: the compiled workflow module is generated at runtime
    # (a source string, not an importable file the sandbox can re-load), and
    # determinism is enforced at COMPILE time -- the SAME reasoning the worker
    # applies (workflow_backend._execute_on_temporal).
    replayer = Replayer(
        workflows=[workflow_cls],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    with _spin("replaying against history..."):
        result = await replayer.replay_workflow(history, raise_on_replay_failure=False)

    failure = result.replay_failure
    if failure is None:
        return True, None
    return False, str(failure)


# ---------------------------------------------------------------------------
# History — fetch + export a Temporal run's full event history
# (ADR 054 D6 / Phase 3). Companion to `mdk workflow replay --from-file`.
# ---------------------------------------------------------------------------


@workflow_app.command("history")
def history(
    run_id: str = typer.Argument(
        ...,
        help=(
            "The workflow_run_id whose event history to fetch (== the Temporal "
            "workflow id, ADR 054 D6)."
        ),
        metavar="RUN_ID",
    ),
    output: Path = typer.Option(
        None,
        "--output",
        "-o",
        help=(
            "Write the history as JSON to this file path. When omitted the "
            "history is printed to stdout."
        ),
    ),
    fmt: str = typer.Option(
        None,
        "--format",
        "-f",
        help=(
            "Output format: 'json' (machine-readable) or 'table' (human-readable, "
            "default for terminal). Defaults to 'json' when piped or --output is set."
        ),
    ),
    target: str = typer.Option(
        None,
        "--target",
        "-t",
        help="Temporal namespace / deployment target to connect to.",
    ),
) -> None:
    """Fetch and display the full event history for a Temporal workflow run.

    The event history is Temporal's durable record of every decision the
    workflow made -- it is the input ``mdk workflow replay`` verifies against.

    Use ``--output history.json`` to export the history to a file for offline
    replay (``mdk workflow replay <run_id> --from-file history.json``).

    [bold]Examples:[/bold]

      [dim]# Print a human-readable table of events[/dim]
      $ mdk workflow history 1f3c...

      [dim]# Export to a JSON file for offline replay[/dim]
      $ mdk workflow history 1f3c... --output history.json

      [dim]# Pipe JSON to jq[/dim]
      $ mdk workflow history 1f3c... --format json | jq '.events | length'
    """
    import sys  # noqa: PLC0415

    # Resolve format: explicit flag > auto-detect based on context.
    if fmt is not None:
        effective_fmt = fmt.lower()
    elif output is not None or not sys.stdout.isatty():
        effective_fmt = "json"
    else:
        effective_fmt = "table"

    if effective_fmt not in ("json", "table"):
        error(f"unknown --format {fmt!r}; expected 'json' or 'table'.", context="history")
        raise typer.Exit(code=2)

    suppress = effective_fmt == "json" and output is None
    result = asyncio.run(
        _fetch_history(
            run_id=run_id,
            target=target,
            suppress=suppress,
        )
    )

    if effective_fmt == "json":
        json_str = json.dumps(result, indent=2)
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json_str, encoding="utf-8")
            stdout.print(
                f"[green]✓[/green] exported {len(result.get('events', []))} event(s) "
                f"to [bold]{output}[/bold]"
            )
        else:
            stdout.print(json_str, soft_wrap=True, highlight=False)
        return

    # Table format: one row per event.
    events = result.get("events", [])
    if not events:
        hint("[dim]no events in this workflow's history[/dim]")
        return
    table = Table(title=f"workflow history for {run_id[:12]}... ({len(events)} events)")
    table.add_column("id", style="dim", justify="right")
    table.add_column("event_type")
    table.add_column("timestamp", style="dim")
    for evt in events:
        evt_type = evt.get("eventType", evt.get("event_type", ""))
        evt_id = str(evt.get("eventId", evt.get("event_id", "")))
        timestamp = evt.get("eventTime", evt.get("event_time", ""))
        # Truncate to YYYY-MM-DDTHH:MM:SS for table readability.
        if isinstance(timestamp, str) and len(timestamp) > _TS_DISPLAY_LEN:
            timestamp = timestamp[:_TS_DISPLAY_LEN]
        table.add_row(evt_id, evt_type, timestamp)
    stdout.print(table)
    if output is not None:
        json_str = json.dumps(result, indent=2)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json_str, encoding="utf-8")
        stdout.print(f"[green]✓[/green] also exported to [bold]{output}[/bold]")


async def _fetch_history(
    *,
    run_id: str,
    target: str | None = None,
    suppress: bool = False,
) -> dict[str, Any]:
    """Fetch the full event history for ``run_id`` from Temporal.

    Returns the history as a JSON-serializable dict (the proto's JSON form).
    """
    import contextlib  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415

    from movate.runtime.workflow_backend import (  # noqa: PLC0415
        WorkflowBackendError,
        _resolve_temporal_connection,
    )

    try:
        import temporalio  # noqa: F401, PLC0415
    except ImportError:
        error(
            "The [temporal] extra is not installed. "
            "Install with: uv tool install --editable '.[temporal]' --force",
            context="history",
        )
        raise typer.Exit(code=2) from None

    from temporalio.client import Client  # noqa: PLC0415
    from temporalio.service import TLSConfig  # noqa: PLC0415

    try:
        conn = _resolve_temporal_connection()
    except WorkflowBackendError as exc:
        error(str(exc), context="history")
        raise typer.Exit(code=2) from None

    namespace = target if target else conn.namespace

    tls: Any = False
    if conn.tls_cert_path:
        tls = TLSConfig(server_root_ca_cert=_Path(conn.tls_cert_path).read_bytes())

    def _spin(message: str) -> Any:
        return contextlib.nullcontext() if suppress else spinner(message)

    with _spin("connecting to Temporal..."):
        client = await Client.connect(conn.host, namespace=namespace, tls=tls)

    with _spin("fetching workflow history..."):
        handle = client.get_workflow_handle(run_id)
        history = await handle.fetch_history()

    # Serialize the WorkflowHistory to a JSON-compatible dict.
    # WorkflowHistory.to_json() returns a JSON string; we parse it back
    # to a dict so the caller can re-serialize with indent/output control.
    # We wrap with the workflow_id so offline replay can reconstruct.
    history_json_str = history.to_json()
    history_dict: dict[str, Any] = json.loads(history_json_str)
    history_dict["workflowId"] = run_id
    return history_dict


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
