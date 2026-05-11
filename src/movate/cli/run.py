"""``movate run <path>`` — execute an agent or a workflow locally.

Auto-detects which: a path containing ``workflow.yaml`` runs as a workflow
(input parsed as JSON for ``initial_state``); otherwise runs as an agent
with the existing string/JSON/file/stdin input coercion.

v0.3 supports local execution only. Remote (`--remote`) hits the FastAPI
runtime and lands in v0.5 alongside `movate serve`.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._completion import complete_agent_path
from movate.cli._output import Run
from movate.cli._runtime import build_local_runtime, shutdown_runtime
from movate.cli._workflow_path import is_workflow_path
from movate.core.loader import AgentBundle, AgentLoadError, load_agent
from movate.core.models import RunRequest, WorkflowStatus
from movate.core.run_replay import (
    AgentReplayDiff,
    ReplayMismatchError,
    render_replay_json,
    replay_agent_run,
)
from movate.core.workflow import (
    WorkflowCompileError,
    WorkflowGraph,
    WorkflowResult,
    WorkflowRunError,
    WorkflowRunner,
    compile_workflow,
    load_workflow_spec,
    validate_linear,
)
from movate.core.workflow.spec import WorkflowSpecLoadError

console = Console(stderr=True)


def run(
    path: Path = typer.Argument(
        ...,
        help="Path to an agent or workflow directory.",
        shell_complete=complete_agent_path,
    ),
    input_arg: str = typer.Argument(
        None,
        metavar="INPUT",
        help=(
            "Input: agent mode accepts a plain string (auto-wraps), JSON, file, "
            "or '-' for stdin. Workflow mode requires JSON, a file, or '-'."
        ),
    ),
    input_flag: str = typer.Option(
        None, "--input", "-i", help="Alternative way to pass input (preferred for explicit JSON)."
    ),
    replay_id: str = typer.Option(
        None,
        "--replay",
        help=(
            "Re-run a recorded RunRecord by id against the current agent code. "
            "Pins the original input; everything else (prompt, model, schemas) "
            "comes from disk. Mutually exclusive with positional INPUT/--input."
        ),
    ),
    mock: bool = typer.Option(
        False, "--mock", help="Use the deterministic MockProvider (no API keys; for smoke tests)."
    ),
    stream: bool = typer.Option(
        False,
        "--stream",
        help=(
            "Render model tokens to stderr as they arrive (preview). "
            "Final output is still schema-validated + persisted normally; "
            "this flag only adds a live preview. Workflow + replay modes ignore it."
        ),
    ),
    output_format: Run = typer.Option(Run.JSON, "--output", "-o", case_sensitive=False),
) -> None:
    """Run an agent or workflow against the given input.

    [bold]Agent examples:[/bold]

      [dim]# Plain string — auto-wraps to the agent's single required string field[/dim]
      $ movate run ./faq-agent "What is movate?"

      [dim]# See tokens as they arrive (dev-loop preview)[/dim]
      $ movate run ./faq-agent "hi" --stream

      [dim]# Mock mode (no API calls)[/dim]
      $ movate run ./faq-agent "hello" --mock

      [dim]# Replay a stored run against current code (regression debug)[/dim]
      $ movate run ./faq-agent --replay 4f8a-...

    [bold]Workflow examples:[/bold]

      [dim]# Initial state as JSON[/dim]
      $ movate run ./returns-workflow '{"order_id": "ord-123"}' --mock

      [dim]# Initial state from a file[/dim]
      $ movate run ./returns-workflow --input initial_state.json
    """
    if replay_id is not None:
        if input_arg is not None or input_flag is not None:
            console.print(
                "[red]✗[/red] --replay is mutually exclusive with positional INPUT / --input"
            )
            raise typer.Exit(code=2)
        if is_workflow_path(path):
            console.print(
                "[red]✗[/red] --replay supports agents only in v0.4; "
                "workflow replay lands in a follow-up"
            )
            raise typer.Exit(code=2)
        _dispatch_replay(path, replay_id, mock=mock, output_format=output_format)
        return

    if is_workflow_path(path):
        if stream:
            # Workflows are multi-step; streaming one node's tokens
            # interleaved with another's would be confusing. Out of scope
            # for v0.5 — surface the limitation explicitly rather than
            # silently ignoring the flag.
            console.print("[red]✗[/red] --stream supports agents only; workflow streaming is TBD")
            raise typer.Exit(code=2)
        _dispatch_workflow(path, input_flag or input_arg, mock=mock, output_format=output_format)
    else:
        _dispatch_agent(
            path, input_flag or input_arg, mock=mock, stream=stream, output_format=output_format
        )


# ---------------------------------------------------------------------------
# Agent dispatch (unchanged behaviour)
# ---------------------------------------------------------------------------


def _dispatch_agent(
    path: Path,
    raw: str | None,
    *,
    mock: bool,
    stream: bool,
    output_format: Run,
) -> None:
    try:
        bundle = load_agent(path)
    except AgentLoadError as exc:
        console.print(f"[red]✗ load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    if raw is None:
        console.print("[red]✗ provide input as a positional arg or via --input[/red]")
        raise typer.Exit(code=2)
    payload = _coerce_agent_input(raw, bundle)

    asyncio.run(
        _run_local_agent(bundle, payload, output_format=output_format, mock=mock, stream=stream)
    )


def _coerce_agent_input(arg: str, bundle: AgentBundle) -> dict[str, Any]:
    """Best-effort interpretation of an agent's positional input.

    Order:
      1. ``-`` → read JSON from stdin
      2. existing file path → read JSON from file
      3. parses as a JSON object → use as-is
      4. plain string AND agent has exactly one required string field →
         wrap as ``{<field>: arg}``
      5. otherwise → raise with a helpful message
    """
    if arg == "-":
        return _ensure_dict(json.loads(sys.stdin.read()))

    p = Path(arg)
    if p.is_file():
        return _ensure_dict(json.loads(p.read_text()))

    try:
        parsed = json.loads(arg)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    schema = bundle.input_schema
    required = list(schema.get("required", []))
    properties = schema.get("properties", {}) or {}
    string_required = [
        name for name in required if properties.get(name, {}).get("type") == "string"
    ]
    if len(string_required) == 1 and len(required) == 1:
        return {string_required[0]: arg}

    raise typer.BadParameter(
        f"input is not valid JSON and cannot be auto-wrapped — agent "
        f"{bundle.spec.name!r} requires {required}. Pass JSON via --input or "
        f"as a JSON-formatted positional arg."
    )


async def _run_local_agent(
    bundle: AgentBundle,
    payload: dict[str, Any],
    *,
    output_format: Run,
    mock: bool,
    stream: bool = False,
) -> None:
    rt = await build_local_runtime(mock=mock)
    try:
        request = RunRequest(agent=bundle.spec.name, input=payload)
        # Streaming preview goes to stderr so it doesn't poison stdout
        # (which carries the schema-validated final JSON). The MockProvider
        # has no real stream path, so silently skip streaming under --mock.
        on_token = _streaming_callback() if stream and not mock else None
        response = await rt.executor.execute(bundle, request, on_token=on_token)
        if on_token is not None:
            # End the streamed preview with a newline so the JSON output
            # below starts on its own line.
            sys.stderr.write("\n")
            sys.stderr.flush()
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)

    if output_format == Run.TEXT:
        sys.stdout.write(response.human_readable + "\n")
    else:
        sys.stdout.write(response.model_dump_json(indent=2) + "\n")

    if response.status == "error":
        raise typer.Exit(code=1)


def _streaming_callback() -> Callable[[str], None]:
    """Return a token callback that writes each chunk to stderr,
    unbuffered, so the preview reads as a live stream.

    Lives here (not in _console.py) because it touches sys.stderr
    directly rather than going through Rich — Rich would buffer and
    apply markup, which is wrong for incremental tokens."""

    def _emit(text: str) -> None:
        sys.stderr.write(text)
        sys.stderr.flush()

    return _emit


# ---------------------------------------------------------------------------
# Workflow dispatch
# ---------------------------------------------------------------------------


def _dispatch_workflow(path: Path, raw: str | None, *, mock: bool, output_format: Run) -> None:
    try:
        spec, parent = load_workflow_spec(path)
    except WorkflowSpecLoadError as exc:
        console.print(f"[red]✗ workflow.yaml load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None
    try:
        graph = compile_workflow(spec, parent)
        validate_linear(graph)
    except WorkflowCompileError as exc:
        console.print(f"[red]✗ workflow validation failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    if raw is None:
        # Default to empty initial state — convenient when state_schema has
        # no `required` fields.
        initial_state: dict[str, Any] = {}
    else:
        initial_state = _coerce_workflow_input(raw)

    asyncio.run(_run_local_workflow(graph, initial_state, output_format=output_format, mock=mock))


def _coerce_workflow_input(arg: str) -> dict[str, Any]:
    """Workflows take a JSON object for ``initial_state`` — no auto-wrap.

    Accepts ``-`` (stdin), a file path, or a JSON string literal.
    """
    if arg == "-":
        return _ensure_dict(json.loads(sys.stdin.read()))
    p = Path(arg)
    if p.is_file():
        return _ensure_dict(json.loads(p.read_text()))
    try:
        parsed = json.loads(arg)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(
            f"workflow input must be JSON (object), file path, or '-': {exc}"
        ) from exc
    return _ensure_dict(parsed)


async def _run_local_workflow(
    graph: WorkflowGraph,
    initial_state: dict[str, Any],
    *,
    output_format: Run,
    mock: bool,
) -> None:
    rt = await build_local_runtime(mock=mock)
    runner = WorkflowRunner(executor=rt.executor, storage=rt.storage)
    try:
        try:
            result = await runner.run(graph, initial_state=initial_state)
        except WorkflowRunError as exc:
            console.print(f"[red]✗ workflow failed:[/red] {exc}")
            raise typer.Exit(code=2) from None
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)

    if output_format == Run.JSON:
        _emit_workflow_json(result)
    else:
        _emit_workflow_text(result)

    if result.status is WorkflowStatus.ERROR:
        raise typer.Exit(code=1)


def _emit_workflow_json(result: WorkflowResult) -> None:
    payload = {
        "workflow_run_id": result.workflow_run_id,
        "status": result.status.value,
        "initial_state": result.initial_state,
        "final_state": result.final_state,
        "duration_ms": result.duration_ms,
        "error_node_id": result.error_node_id,
        "error": result.error.model_dump() if result.error else None,
        "nodes": [
            {
                "node_id": r.node_id,
                "agent": r.agent,
                "status": r.status.value,
                "cost_usd": r.metrics.cost_usd,
                "latency_ms": r.metrics.latency_ms,
                "tokens": r.metrics.tokens.model_dump(),
                "output": r.output,
                "error": r.error.model_dump() if r.error else None,
            }
            for r in result.runs
        ],
    }
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")


def _emit_workflow_text(result: WorkflowResult) -> None:
    """Pretty Rich summary on stderr; final state JSON on stdout for piping."""
    head = Table(title=f"workflow run {result.workflow_run_id[:8]}…", show_header=False)
    head.add_column("field", style="dim")
    head.add_column("value")
    head.add_row("status", _status_badge(result))
    head.add_row("duration", f"{result.duration_ms} ms")
    if result.error_node_id:
        head.add_row("error_node", result.error_node_id)
        if result.error:
            head.add_row("error", f"{result.error.type}: {result.error.message}")
    head.add_row("workflow_run_id", result.workflow_run_id)
    console.print(head)

    if result.runs:
        rows = Table(title="Nodes", show_header=True, header_style="bold")
        rows.add_column("#", style="dim", width=3)
        rows.add_column("node")
        rows.add_column("agent")
        rows.add_column("status")
        rows.add_column("ms")
        rows.add_column("cost")
        for i, r in enumerate(result.runs, start=1):
            rows.add_row(
                str(i),
                r.node_id or "?",
                r.agent,
                r.status.value,
                str(r.metrics.latency_ms),
                f"${r.metrics.cost_usd:.6f}",
            )
        console.print(rows)

    sys.stdout.write(json.dumps(result.final_state, indent=2) + "\n")


def _status_badge(result: WorkflowResult) -> str:
    if result.status is WorkflowStatus.SUCCESS:
        return "[green]SUCCESS[/green]"
    return "[red]ERROR[/red]"


# ---------------------------------------------------------------------------
# Replay dispatch
# ---------------------------------------------------------------------------


def _dispatch_replay(path: Path, run_id: str, *, mock: bool, output_format: Run) -> None:
    try:
        bundle = load_agent(path)
    except AgentLoadError as exc:
        console.print(f"[red]✗ load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    asyncio.run(_run_replay(bundle, run_id, output_format=output_format, mock=mock))


async def _run_replay(
    bundle: AgentBundle,
    run_id: str,
    *,
    output_format: Run,
    mock: bool,
) -> None:
    rt = await build_local_runtime(mock=mock)
    try:
        try:
            diff = await replay_agent_run(
                storage=rt.storage,
                executor=rt.executor,
                bundle=bundle,
                run_id=run_id,
            )
        except ReplayMismatchError as exc:
            console.print(f"[red]✗ replay failed:[/red] {exc}")
            raise typer.Exit(code=2) from None
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)

    if output_format == Run.TEXT:
        _emit_replay_text(diff)
    else:
        sys.stdout.write(json.dumps(render_replay_json(diff), indent=2, default=str) + "\n")

    # Output changes are normal — surfacing them IS the goal. Only fail
    # the gate when the replay itself errored (regression in the agent).
    if diff.current.status == "error":
        raise typer.Exit(code=1)


def _emit_replay_text(diff: AgentReplayDiff) -> None:
    head = Table(
        title=f"agent replay vs run {diff.original.run_id[:8]}…",
        show_header=False,
    )
    head.add_column("field", style="dim")
    head.add_column("value")
    head.add_row("agent", diff.original.agent)
    head.add_row(
        "agent_version",
        f"{diff.original.agent_version} (recorded)",
    )
    head.add_row("recorded_at", diff.original.created_at.isoformat())
    head.add_row(
        "status",
        f"{diff.original.status.value} → {diff.current.status}"
        + ("  [yellow](changed)[/yellow]" if diff.status_changed else ""),
    )
    head.add_row(
        "output_changed",
        "[yellow]YES[/yellow]" if diff.output_changed else "[green]no[/green]",
    )
    if diff.changed_keys:
        head.add_row("changed_keys", ", ".join(diff.changed_keys))
    head.add_row(
        "cost_delta",
        f"${diff.original.metrics.cost_usd:.6f} → ${diff.current.metrics.cost_usd:.6f}  "
        f"({diff.cost_delta_usd:+.6f})",
    )
    head.add_row(
        "latency_delta",
        f"{diff.original.metrics.latency_ms} → {diff.current.metrics.latency_ms} ms  "
        f"({diff.latency_delta_ms:+d})",
    )
    console.print(head)

    sys.stdout.write(
        json.dumps(
            {
                "input": diff.original.input,
                "recorded_output": diff.original.output,
                "current_output": diff.current.data,
            },
            indent=2,
            default=str,
        )
        + "\n"
    )


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


def _ensure_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise typer.BadParameter(f"input must be a JSON object, got {type(value).__name__}")
    return value
