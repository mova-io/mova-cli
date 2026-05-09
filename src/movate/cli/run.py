"""``movate run <agent>`` — execute an agent locally.

v0.1 supports local execution only. Remote (`--remote`) hits the FastAPI
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

from movate.cli._runtime import build_local_runtime, shutdown_runtime
from movate.core.loader import AgentBundle, AgentLoadError, load_agent
from movate.core.models import RunRequest

console = Console(stderr=True)


def run(
    path: Path = typer.Argument(..., help="Path to agent directory."),
    input_arg: str = typer.Argument(
        None,
        metavar="INPUT",
        help=(
            "Input: a plain string (auto-wraps to the agent's single required string field), "
            "JSON object, file path, or '-' for stdin."
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
    """Execute an agent. Local mode only in v0.1.

    [bold]Examples:[/bold]

      [dim]# Plain string — auto-wraps to the agent's single required string field[/dim]
      $ movate run ./faq-agent "What is movate?"

      [dim]# Explicit JSON[/dim]
      $ movate run ./faq-agent '{"text": "What is movate?"}'

      [dim]# Read input from a file[/dim]
      $ movate run ./faq-agent --input data.json

      [dim]# Read input from stdin[/dim]
      $ echo '{"text":"hi"}' | movate run ./faq-agent -

      [dim]# Mock mode (no API calls)[/dim]
      $ movate run ./faq-agent "hello" --mock
    """
    try:
        bundle = load_agent(path)
    except AgentLoadError as exc:
        console.print(f"[red]✗ load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    raw = input_flag or input_arg
    if raw is None:
        console.print("[red]✗ provide input as a positional arg or via --input[/red]")
        raise typer.Exit(code=2)
    payload = _coerce_input(raw, bundle)

    asyncio.run(_run_local(bundle, payload, output_format=output_format, mock=mock))


def _coerce_input(arg: str, bundle: AgentBundle) -> dict[str, Any]:
    """Best-effort interpretation of the positional input arg.

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


def _ensure_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise typer.BadParameter(f"input must be a JSON object, got {type(value).__name__}")
    return value


async def _run_local(
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
