"""``mdk export`` — emit MDK artifacts in standard / interop formats.

First subcommand is ``json-schema`` (item 147 in Group K). Future
subcommands land here as separate functions: ``langgraph`` (item
146), ``oci-bundle`` (item 148).

The strategic framing (see BACKLOG Group K North Star): MDK is the
IDE; we compile DOWN to other ecosystems. The JSON Schema export is
the trivial case — we already maintain validated schemas; emitting
them in standalone form lets downstream code-generation tools
(Pydantic, TypeScript, Go) consume MDK agents as type sources.

Why this is a Tier 1 polish item (not a foundational one):
* Cheap (validators already have `.schema` attributes)
* No new dependencies
* Cross-ecosystem credibility — "MDK schemas → quicktype → any
  language" is a real one-liner for users not on the Python path
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Literal

import typer
from rich.console import Console

from movate.cli._completion import complete_agent_path
from movate.core.loader import AgentLoadError, load_agent

console = Console()
err_console = Console(stderr=True)


export_app = typer.Typer(
    name="export",
    help=(
        "Export MDK artifacts in standard / interop formats. Lets "
        "MDK agents serve as type sources for downstream "
        "code-generation tools (Pydantic, TypeScript, Go, ...)."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


# ---------------------------------------------------------------------------
# Subcommand: json-schema
# ---------------------------------------------------------------------------


_Direction = Literal["input", "output", "both"]


@export_app.command("json-schema")
def json_schema(
    agent_path: Path = typer.Argument(
        ...,
        help="Path to an agent directory.",
        shell_complete=complete_agent_path,
    ),
    direction: str = typer.Option(
        "both",
        "--direction",
        "-d",
        help=(
            "Which schema to export: ``input`` | ``output`` | ``both`` "
            "(default). ``both`` returns a JSON object with both keys."
        ),
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help=(
            "Write to this path instead of stdout. Useful for "
            "scripting: ``mdk export json-schema my-agent -o schema.json``."
        ),
    ),
    pretty: bool = typer.Option(
        True,
        "--pretty/--compact",
        help=(
            "Pretty-print with 2-space indent (default) or emit "
            "compact one-line JSON for piping to other tools."
        ),
    ),
) -> None:
    """Emit an agent's input / output JSON Schema as standalone JSON.

    [bold]Examples:[/bold]

      [dim]# Both schemas to stdout[/dim]
      $ mdk export json-schema my-agent

      [dim]# Just the output schema, to a file[/dim]
      $ mdk export json-schema my-agent --direction output -o out.schema.json

      [dim]# Pipe to quicktype for TypeScript types[/dim]
      $ mdk export json-schema my-agent --direction input --compact | \\
          quicktype -s schema -l ts -o agent_input.ts

      [dim]# Generate Pydantic models[/dim]
      $ mdk export json-schema my-agent --direction output --compact | \\
          datamodel-codegen --input-file-type jsonschema --input -
    """
    if direction not in {"input", "output", "both"}:
        err_console.print(
            f"[red]✗[/red] --direction must be one of input | output | both (got {direction!r})"
        )
        raise typer.Exit(code=2)

    try:
        bundle = load_agent(agent_path)
    except AgentLoadError as exc:
        err_console.print(f"[red]✗ load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    payload = _build_payload(
        direction=direction,  # type: ignore[arg-type]  -- guarded above
        input_schema=bundle.input_schema,
        output_schema=bundle.output_schema,
        agent_name=bundle.spec.name,
    )

    text = json.dumps(payload, indent=2 if pretty else None, sort_keys=False)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + ("\n" if pretty else ""))
        console.print(f"[green]✓[/green] wrote {direction} schema to [bold]{output}[/bold]")
    else:
        # Print via stdout (not rich.console) so piping works
        # cleanly — Rich would inject ANSI escape codes that break
        # downstream parsers like quicktype / datamodel-codegen.
        sys.stdout.write(text)
        if pretty:
            sys.stdout.write("\n")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_payload(
    *,
    direction: _Direction,
    input_schema: dict,
    output_schema: dict,
    agent_name: str,
) -> dict:
    """Construct the JSON payload for the requested direction.

    For ``both``, returns a wrapper object with explicit ``input`` and
    ``output`` keys + the agent name + a ``$schema`` declaration so
    the result is itself a valid JSON Schema document (which makes
    the file self-describing when committed to a repo).

    For ``input`` / ``output`` alone, returns the schema as-is so the
    output is a clean JSON Schema that any consumer can parse
    directly without unwrapping.
    """
    if direction == "input":
        return input_schema
    if direction == "output":
        return output_schema
    # both — wrap.
    return {
        "$comment": (
            f"Exported by mdk export json-schema from agent {agent_name!r}. "
            f"input/output keys each contain a standalone JSON Schema."
        ),
        "agent": agent_name,
        "input": input_schema,
        "output": output_schema,
    }
