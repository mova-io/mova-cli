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
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._runtime import build_local_runtime, shutdown_runtime
from movate.cli._workflow_path import is_workflow_path
from movate.core.loader import AgentBundle, AgentLoadError, load_agent
from movate.core.models import RunRequest, WorkflowStatus
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
    path: Path = typer.Argument(..., help="Path to an agent or workflow directory."),
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
    mock: bool = typer.Option(
        False, "--mock", help="Use the deterministic MockProvider (no API keys; for smoke tests)."
    ),
    output_format: str = typer.Option("json", "--output", "-o", help="json | text"),
) -> None:
    """Run an agent or workflow against the given input.

    [bold]Agent examples:[/bold]

      [dim]# Plain string — auto-wraps to the agent's single required string field[/dim]
      $ movate run ./faq-agent "What is movate?"

      [dim]# Mock mode (no API calls)[/dim]
      $ movate run ./faq-agent "hello" --mock

    [bold]Workflow examples:[/bold]

      [dim]# Initial state as JSON[/dim]
      $ movate run ./returns-workflow '{"order_id": "ord-123"}' --mock

      [dim]# Initial state from a file[/dim]
      $ movate run ./returns-workflow --input initial_state.json
    """
    if is_workflow_path(path):
        _dispatch_workflow(path, input_flag or input_arg, mock=mock, output_format=output_format)
    else:
        _dispatch_agent(path, input_flag or input_arg, mock=mock, output_format=output_format)


# ---------------------------------------------------------------------------
# Agent dispatch (unchanged behaviour)
# ---------------------------------------------------------------------------


def _dispatch_agent(path: Path, raw: str | None, *, mock: bool, output_format: str) -> None:
    try:
        bundle = load_agent(path)
    except AgentLoadError as exc:
        console.print(f"[red]✗ load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    if raw is None:
        console.print("[red]✗ provide input as a positional arg or via --input[/red]")
        raise typer.Exit(code=2)
    payload = _coerce_agent_input(raw, bundle)

    asyncio.run(_run_local_agent(bundle, payload, output_format=output_format, mock=mock))


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
    bundle: AgentBundle, payload: dict[str, Any], *, output_format: str, mock: bool
) -> None:
    rt = await build_local_runtime(mock=mock)
    try:
        request = RunRequest(agent=bundle.spec.name, input=payload)
        response = await rt.executor.execute(bundle, request)
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)

    if output_format == "text":
        sys.stdout.write(response.human_readable + "\n")
    else:
        sys.stdout.write(response.model_dump_json(indent=2) + "\n")

    if response.status == "error":
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Workflow dispatch
# ---------------------------------------------------------------------------


def _dispatch_workflow(path: Path, raw: str | None, *, mock: bool, output_format: str) -> None:
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
    output_format: str,
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

    if output_format == "json":
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
# Shared helper
# ---------------------------------------------------------------------------


def _ensure_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise typer.BadParameter(f"input must be a JSON object, got {type(value).__name__}")
    return value
