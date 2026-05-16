"""``mdk schema`` — author + compile + inspect schema files.

Surfaces the three nested DSLs (canonical / shorthand / JSON Schema)
as one toolchain. Today only ``compile`` ships; future subcommands
(``validate``, ``diff``, ``codegen``) will land here too.

Use cases:

* Business author edits ``schema/input.yaml`` (canonical), runs
  ``mdk schema compile`` to preview the generated JSON Schema
  before committing.
* Engineer needs the equivalent shorthand for a quick test:
  ``mdk schema compile schema/input.yaml --format shorthand``.
* CI wants to assert the canonical file compiles cleanly:
  ``mdk schema compile schema/input.yaml --check``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
import yaml
from rich.console import Console

from movate.core.canonical_schema import (
    CanonicalSchemaError,
    compile_canonical,
    is_canonical_format,
)
from movate.core.schema_shorthand import (
    SchemaShorthandError,
    compile_shorthand,
)

schema_app = typer.Typer(
    name="schema",
    help="Author + compile schema files (canonical / shorthand / JSON Schema).",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

stdout = Console()
err = Console(stderr=True)


@schema_app.command("compile")
def compile_cmd(  # noqa: PLR0912 — flag dispatch; branch count is inherent
    source: Path = typer.Argument(
        ...,
        help="Path to a .yaml schema file (canonical, shorthand, or JSON Schema-in-YAML).",
    ),
    fmt: str = typer.Option(
        "json-schema",
        "--format",
        "-f",
        help=(
            "Output format: [bold]json-schema[/bold] (default; compact) "
            "or [bold]json-schema-pretty[/bold] (indented)."
        ),
    ),
    output: Path = typer.Option(
        None,
        "--output",
        "-o",
        help="Write output to this file (default: stdout).",
    ),
    check: bool = typer.Option(
        False,
        "--check",
        help="Validate the source compiles cleanly but don't emit output. CI-friendly.",
    ),
) -> None:
    """Compile a schema file to JSON Schema.

    Detects the source DSL (canonical / shorthand / hand-written
    JSON Schema) by inspecting the top-level YAML keys, then
    dispatches to the appropriate compiler.

    [bold]Examples:[/bold]

      [dim]# Compile + write to stdout[/dim]
      $ mdk schema compile schema/input.yaml

      [dim]# Compile + write to a file (pretty-printed)[/dim]
      $ mdk schema compile schema/input.yaml -o schema/input.json --format json-schema-pretty

      [dim]# CI smoke: did this file compile cleanly?[/dim]
      $ mdk schema compile schema/input.yaml --check
    """
    if not source.is_file():
        err.print(f"[red]✗[/red] schema file not found: [bold]{source}[/bold]")
        raise typer.Exit(code=2)

    if fmt not in ("json-schema", "json-schema-pretty"):
        err.print(
            f"[red]✗[/red] --format must be one of 'json-schema' or "
            f"'json-schema-pretty'; got {fmt!r}"
        )
        raise typer.Exit(code=2)

    # Parse the source YAML.
    try:
        raw_text = source.read_text()
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        err.print(f"[red]✗[/red] invalid YAML in [bold]{source}[/bold]: {exc}")
        raise typer.Exit(code=2) from None
    if not isinstance(data, dict):
        err.print(f"[red]✗[/red] schema file must be a top-level object, got {type(data).__name__}")
        raise typer.Exit(code=2)

    # Dispatch on shape — same three-way sniff the loader uses.
    if is_canonical_format(data):
        source_form = "canonical"
        try:
            compiled = compile_canonical(data)
        except CanonicalSchemaError as exc:
            err.print(f"[red]✗[/red] canonical schema error in [bold]{source}[/bold]: {exc}")
            raise typer.Exit(code=2) from None
    elif "$schema" in data or (data.get("type") == "object" and "properties" in data):
        # Already JSON Schema — passthrough (still useful for the
        # `--check` flow to confirm it parses).
        source_form = "json-schema"
        compiled = data
    else:
        source_form = "shorthand"
        try:
            compiled = compile_shorthand(data, root_label=source.name)
        except SchemaShorthandError as exc:
            err.print(f"[red]✗[/red] shorthand error in [bold]{source}[/bold]: {exc}")
            raise typer.Exit(code=2) from None

    if check:
        err.print(
            f"[green]✓[/green] [bold]{source}[/bold] compiles cleanly "
            f"(detected: [cyan]{source_form}[/cyan])."
        )
        # Greppable line so CI workflows can branch on `ok=true|false`
        # without parsing the human-facing line above.
        err.print(
            f"[dim]mdk_schema_compile_summary: source={source} form={source_form} ok=true[/dim]"
        )
        return

    # Serialize JSON. The 'pretty' form prints with indent=2; the
    # default form is compact (no extra whitespace) — operators
    # piping to disk or to jq prefer compact.
    if fmt == "json-schema-pretty":
        serialized = json.dumps(compiled, indent=2) + "\n"
    else:
        serialized = json.dumps(compiled, separators=(",", ":")) + "\n"

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized)
        err.print(
            f"[green]✓[/green] wrote {len(serialized)} bytes to [bold]{output}[/bold] "
            f"(from {source_form} source)"
        )
        err.print(
            f"[dim]mdk_schema_compile_summary: source={source} "
            f"form={source_form} output={output} ok=true[/dim]"
        )
    else:
        sys.stdout.write(serialized)
