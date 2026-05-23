"""``mdk models`` — model catalog: list and inspect all known models.

Two subcommands:

* ``mdk models list`` — Rich table of every model in the pricing table.
  Columns: Provider, Model ID, Context (tokens), Input $/1M, Output $/1M,
  Capabilities (tools / vision checkmarks). Supports ``--provider``,
  ``--has-tools``, ``--has-vision`` filter flags.

* ``mdk models show <model-id>`` — detail panel for one model: full ID,
  provider, pricing (input / output / cached), capabilities, and whether
  the model is tracked in MDK's pricing table.

The canonical *pricing* data comes from ``movate.providers.pricing``.
Capability metadata (context window, tool-use support, vision support) is
maintained in the ``_CAPABILITY_CATALOGUE`` dict below — keyed by the full
LiteLLM provider string so it stays in sync with the pricing table.
"""

from __future__ import annotations

import json
import sys

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from movate.cli._output import TableJson
from movate.providers.model_catalog import ModelInfo, caps_for, model_catalog, model_info
from movate.providers.pricing import ModelPrice, load_pricing

console = Console()
err_console = Console(stderr=True)

models_app = typer.Typer(
    name="models",
    help="Browse the MDK model catalog — pricing, context windows, and capabilities.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


# ---------------------------------------------------------------------------
# Capability + pricing data
#
# The catalogue (pricing + context window / tool-use / vision capability) is
# maintained in the shared :mod:`movate.providers.model_catalog` module so the
# runtime's read-only ``GET /api/v1/models`` endpoint and this CLI command
# share one source of truth (the runtime never imports from ``cli``).
# ---------------------------------------------------------------------------


_ONE_MILLION = 1_000_000
_ONE_THOUSAND = 1_000


def _fmt_context(tokens: int) -> str:
    if tokens == 0:
        return "—"
    if tokens >= _ONE_MILLION:
        return f"{tokens // _ONE_MILLION}M"
    if tokens >= _ONE_THOUSAND:
        return f"{tokens // _ONE_THOUSAND}k"
    return str(tokens)


def _fmt_price_per_million(per_1m: float) -> str:
    """Format a price-per-1M-tokens value for display."""
    return f"${per_1m:.4f}"


def _check(value: bool) -> str:
    return "[green]✓[/green]" if value else "[dim]—[/dim]"


# ---------------------------------------------------------------------------
# Build the rows list (shared between list and JSON output)
#
# The catalogue itself (pricing + caps, sorted by provider then model_id)
# comes from the shared ``model_catalog()``; the filter flags here are a
# pure CLI presentation concern, so they stay in the control plane.
# ---------------------------------------------------------------------------


def _build_rows(
    *,
    provider_filter: str | None = None,
    has_tools: bool = False,
    has_vision: bool = False,
    search_filter: str | None = None,
) -> list[ModelInfo]:
    needle = search_filter.lower() if search_filter else None
    rows: list[ModelInfo] = []
    for info in model_catalog():
        if provider_filter and info.provider != provider_filter:
            continue
        if needle and needle not in info.model_id.lower():
            continue
        if has_tools and not info.supports_tools:
            continue
        if has_vision and not info.supports_vision:
            continue
        rows.append(info)
    return rows


# ---------------------------------------------------------------------------
# ``mdk models list``
# ---------------------------------------------------------------------------


@models_app.command("list")
def models_list(
    provider: str = typer.Option(
        None,
        "--provider",
        "-p",
        help=("Only show models from this provider (e.g. ``anthropic``, ``openai``, ``azure``)."),
    ),
    search: str = typer.Option(
        None,
        "--search",
        "-s",
        help="Case-insensitive substring filter on model ID (e.g. ``gpt-4o``, ``sonnet``).",
    ),
    has_tools: bool = typer.Option(
        False,
        "--has-tools",
        help="Only show models that support tool / function calling.",
    ),
    has_vision: bool = typer.Option(
        False,
        "--has-vision",
        help="Only show models that accept image inputs.",
    ),
    output_format: TableJson = typer.Option(
        TableJson.TABLE,
        "--output",
        "-o",
        case_sensitive=False,
        help="Output format: ``table`` (default) or ``json``.",
    ),
) -> None:
    """List all models in the MDK catalog with pricing and capabilities.

    [bold]Examples:[/bold]

      [dim]# Full catalog[/dim]
      $ mdk models list

      [dim]# Anthropic models only[/dim]
      $ mdk models list --provider anthropic

      [dim]# Search for GPT-4o variants[/dim]
      $ mdk models list --search gpt-4o

      [dim]# Models that support tool-use and vision[/dim]
      $ mdk models list --has-tools --has-vision

      [dim]# Machine-readable output[/dim]
      $ mdk models list -o json | jq .
    """
    try:
        table_data = load_pricing()
    except Exception as exc:
        err_console.print(f"[red]✗ failed to load pricing table:[/red] {exc}")
        raise typer.Exit(code=2) from None

    rows = _build_rows(
        provider_filter=provider,
        search_filter=search,
        has_tools=has_tools,
        has_vision=has_vision,
    )

    if output_format == TableJson.JSON:
        payload = {
            "version": table_data.version,
            "last_verified": table_data.last_verified,
            "models": [r.to_dict() for r in rows],
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        if not rows:
            raise typer.Exit(code=1)
        return

    title = f"MDK model catalog — v{table_data.version} (last verified {table_data.last_verified})"
    tbl = Table(title=title, show_header=True, header_style="bold")
    tbl.add_column("Provider", style="cyan")
    tbl.add_column("Model ID")
    tbl.add_column("Context", justify="right")
    tbl.add_column("Input $/1M", justify="right")
    tbl.add_column("Output $/1M", justify="right")
    tbl.add_column("Tools", justify="center")
    tbl.add_column("Vision", justify="center")

    for row in rows:
        tbl.add_row(
            row.provider,
            row.model_id,
            _fmt_context(row.context_window),
            _fmt_price_per_million(row.input_per_1m),
            _fmt_price_per_million(row.output_per_1m),
            _check(row.supports_tools),
            _check(row.supports_vision),
        )

    console.print(tbl)
    if not rows:
        err_console.print("[yellow]no models matched the filter[/yellow]")
        raise typer.Exit(code=1)
    console.print(f"[dim]{len(rows)} model(s) in catalog.[/dim]")


# ---------------------------------------------------------------------------
# ``mdk models show <model-id>``
# ---------------------------------------------------------------------------


@models_app.command("show")
def models_show(
    model_id: str = typer.Argument(
        ...,
        help="Full LiteLLM model ID, e.g. ``anthropic/claude-sonnet-4-6``.",
    ),
    output_format: TableJson = typer.Option(
        TableJson.TABLE,
        "--output",
        "-o",
        case_sensitive=False,
        help="Output format: ``table`` (default) or ``json``.",
    ),
) -> None:
    """Show pricing and capability details for a specific model.

    [bold]Examples:[/bold]

      [dim]# Detail panel for Claude Sonnet[/dim]
      $ mdk models show anthropic/claude-sonnet-4-6

      [dim]# Machine-readable[/dim]
      $ mdk models show openai/gpt-4o-2024-08-06 -o json
    """
    try:
        table_data = load_pricing()
    except Exception as exc:
        err_console.print(f"[red]✗ failed to load pricing table:[/red] {exc}")
        raise typer.Exit(code=2) from None

    row = model_info(model_id, table_data)
    if row is None:
        err_console.print(f"[red]✗ model not found:[/red] {model_id!r}")
        err_console.print(
            "[dim]hint: run [bold]mdk models list[/bold] to see all models in the catalog.[/dim]"
        )
        raise typer.Exit(code=1)

    in_pricing = row.in_pricing_table
    price: ModelPrice = table_data.models[model_id]
    caps = caps_for(model_id)
    provider = row.provider
    model_name = model_id.split("/", 1)[1] if "/" in model_id else model_id

    if output_format == TableJson.JSON:
        sys.stdout.write(json.dumps(row.to_dict(), indent=2) + "\n")
        return

    # Rich detail panel
    lines: list[str] = [
        f"[bold]Model ID[/bold]      {model_id}",
        f"[bold]Provider[/bold]      {provider}",
        f"[bold]Name[/bold]          {model_name}",
        "",
        f"[bold]Context window[/bold]  {_fmt_context(caps.context_window)} tokens",
        "",
        "[bold]Pricing[/bold]",
        f"  Input       {_fmt_price_per_million(price.input_per_1k * 1000)} / 1M tokens",
        f"  Output      {_fmt_price_per_million(price.output_per_1k * 1000)} / 1M tokens",
    ]
    if price.cached_input_per_1k is not None:
        lines.append(
            f"  Cached in   {_fmt_price_per_million(price.cached_input_per_1k * 1000)} / 1M tokens"
        )
    lines += [
        "",
        "[bold]Capabilities[/bold]",
        f"  Tool / function calling   {_check(caps.supports_tools)}",
        f"  Vision (image input)      {_check(caps.supports_vision)}",
    ]
    if caps.notes:
        lines += ["", f"[dim]{caps.notes}[/dim]"]
    lines += [
        "",
        "[green]✓ Available in MDK[/green]"
        if in_pricing
        else "[yellow]⚠ Not in MDK pricing table[/yellow]",
    ]

    panel_content = "\n".join(lines)
    console.print(
        Panel(
            Text.from_markup(panel_content),
            title=f"[bold]{model_id}[/bold]",
            expand=False,
        )
    )
