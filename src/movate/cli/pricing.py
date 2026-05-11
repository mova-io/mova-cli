"""``movate pricing`` — print the packaged model pricing table.

The table is the canonical source for cost calculation in the executor;
``movate pricing`` lets a developer audit it without spelunking the YAML
or duplicating the data in a doc.
"""

from __future__ import annotations

import json
import sys

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._output import TableJson
from movate.providers.pricing import load_pricing

console = Console()
err_console = Console(stderr=True)


def pricing(
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
    provider_prefix: str = typer.Option(
        None,
        "--provider",
        "-p",
        help="Only show entries whose provider string starts with this prefix.",
    ),
) -> None:
    """Show the price-per-1k-tokens table for all configured models.

    [bold]Examples:[/bold]

      [dim]# Full table (default)[/dim]
      $ movate pricing

      [dim]# Just OpenAI / Azure entries[/dim]
      $ movate pricing -p openai/

      [dim]# Machine-readable for diffing in CI[/dim]
      $ movate pricing -o json | jq .
    """
    try:
        table_data = load_pricing()
    except Exception as exc:
        err_console.print(f"[red]✗ failed to load pricing table:[/red] {exc}")
        raise typer.Exit(code=2) from None

    rows = sorted(
        (k, v)
        for k, v in table_data.models.items()
        if not provider_prefix or k.startswith(provider_prefix)
    )

    if output_format == "json":
        payload = {
            "version": table_data.version,
            "last_verified": table_data.last_verified,
            "models": {
                k: {
                    "input_per_1k": v.input_per_1k,
                    "output_per_1k": v.output_per_1k,
                    "cached_input_per_1k": v.cached_input_per_1k,
                }
                for k, v in rows
            },
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return

    title = f"movate pricing — v{table_data.version} (last verified {table_data.last_verified})"
    table = Table(title=title, show_header=True, header_style="bold")
    table.add_column("Provider")
    table.add_column("In / 1k", justify="right")
    table.add_column("Out / 1k", justify="right")
    table.add_column("Cached / 1k", justify="right")

    for name, price in rows:
        cached = (
            f"${price.cached_input_per_1k:.6f}"
            if price.cached_input_per_1k is not None
            else "[dim]—[/dim]"
        )
        table.add_row(
            name,
            f"${price.input_per_1k:.6f}",
            f"${price.output_per_1k:.6f}",
            cached,
        )

    console.print(table)
    if not rows:
        err_console.print("[yellow]no entries matched the provider filter[/yellow]")
        raise typer.Exit(code=1)
    console.print(f"[dim]{len(rows)} model(s) priced.[/dim]")
