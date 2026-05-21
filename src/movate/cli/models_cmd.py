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
from dataclasses import dataclass, field

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from movate.cli._output import TableJson
from movate.providers.pricing import ModelPrice, PricingTable, load_pricing

console = Console()
err_console = Console(stderr=True)

models_app = typer.Typer(
    name="models",
    help="Browse the MDK model catalog — pricing, context windows, and capabilities.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


# ---------------------------------------------------------------------------
# Capability catalogue
#
# The pricing table (pricing.yaml) tracks cost data only. Capability metadata
# — context window, tool-use support, vision support — is maintained here.
# Keys match the LiteLLM provider strings in pricing.yaml exactly.
# ---------------------------------------------------------------------------


@dataclass
class _ModelCaps:
    """Static capability metadata for one model."""

    context_window: int
    supports_tools: bool = True
    supports_vision: bool = False
    notes: str = ""


# Default capabilities by provider prefix (used as fallback if a model ID
# isn't listed explicitly below).
_PROVIDER_DEFAULTS: dict[str, _ModelCaps] = {
    "openai": _ModelCaps(context_window=128_000, supports_tools=True, supports_vision=False),
    "azure": _ModelCaps(context_window=128_000, supports_tools=True, supports_vision=False),
    "anthropic": _ModelCaps(context_window=200_000, supports_tools=True, supports_vision=False),
}

_CAPABILITY_CATALOGUE: dict[str, _ModelCaps] = {
    # OpenAI
    "openai/gpt-4o-2024-08-06": _ModelCaps(
        context_window=128_000,
        supports_tools=True,
        supports_vision=True,
    ),
    "openai/gpt-4o-mini-2024-07-18": _ModelCaps(
        context_window=128_000,
        supports_tools=True,
        supports_vision=True,
    ),
    "openai/o1-2024-12-17": _ModelCaps(
        context_window=200_000,
        supports_tools=True,
        supports_vision=True,
        notes="Reasoning model; extended thinking built in.",
    ),
    # Azure OpenAI
    "azure/gpt-4o-2024-08-06": _ModelCaps(
        context_window=128_000,
        supports_tools=True,
        supports_vision=True,
        notes="Azure-hosted GPT-4o; slight markup vs first-party.",
    ),
    # Anthropic
    "anthropic/claude-opus-4-6": _ModelCaps(
        context_window=200_000,
        supports_tools=True,
        supports_vision=True,
    ),
    "anthropic/claude-sonnet-4-6": _ModelCaps(
        context_window=200_000,
        supports_tools=True,
        supports_vision=True,
    ),
    "anthropic/claude-haiku-4-5-20251001": _ModelCaps(
        context_window=200_000,
        supports_tools=True,
        supports_vision=True,
    ),
}


def _caps_for(model_id: str) -> _ModelCaps:
    """Return capability metadata for *model_id*, falling back to provider defaults."""
    if model_id in _CAPABILITY_CATALOGUE:
        return _CAPABILITY_CATALOGUE[model_id]
    provider = model_id.split("/", maxsplit=1)[0] if "/" in model_id else ""
    return _PROVIDER_DEFAULTS.get(
        provider,
        _ModelCaps(context_window=0, supports_tools=False, supports_vision=False),
    )


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
# ---------------------------------------------------------------------------


@dataclass
class _ModelRow:
    model_id: str
    provider: str
    context_window: int
    input_per_1m: float
    output_per_1m: float
    cached_input_per_1m: float | None
    supports_tools: bool
    supports_vision: bool
    notes: str = field(default="")
    in_pricing_table: bool = field(default=True)

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "provider": self.provider,
            "context_window": self.context_window,
            "input_per_1m": self.input_per_1m,
            "output_per_1m": self.output_per_1m,
            "cached_input_per_1m": self.cached_input_per_1m,
            "supports_tools": self.supports_tools,
            "supports_vision": self.supports_vision,
            "notes": self.notes,
            "in_pricing_table": self.in_pricing_table,
        }


def _build_rows(
    table_data: PricingTable,
    *,
    provider_filter: str | None = None,
    has_tools: bool = False,
    has_vision: bool = False,
    search_filter: str | None = None,
) -> list[_ModelRow]:
    needle = search_filter.lower() if search_filter else None
    rows: list[_ModelRow] = []
    for model_id, price in sorted(table_data.models.items()):
        provider = model_id.split("/")[0] if "/" in model_id else model_id
        if provider_filter and provider != provider_filter:
            continue
        if needle and needle not in model_id.lower():
            continue
        caps = _caps_for(model_id)
        if has_tools and not caps.supports_tools:
            continue
        if has_vision and not caps.supports_vision:
            continue
        rows.append(
            _ModelRow(
                model_id=model_id,
                provider=provider,
                context_window=caps.context_window,
                input_per_1m=price.input_per_1k * 1000,
                output_per_1m=price.output_per_1k * 1000,
                cached_input_per_1m=(
                    price.cached_input_per_1k * 1000
                    if price.cached_input_per_1k is not None
                    else None
                ),
                supports_tools=caps.supports_tools,
                supports_vision=caps.supports_vision,
                notes=caps.notes,
                in_pricing_table=True,
            )
        )
    # Sort: provider ascending, then model_id ascending.
    rows.sort(key=lambda r: (r.provider, r.model_id))
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
        table_data,
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

    price: ModelPrice | None = table_data.models.get(model_id)
    in_pricing = price is not None
    caps = _caps_for(model_id)

    if not in_pricing:
        err_console.print(f"[red]✗ model not found:[/red] {model_id!r}")
        err_console.print(
            "[dim]hint: run [bold]mdk models list[/bold] to see all models in the catalog.[/dim]"
        )
        raise typer.Exit(code=1)

    provider = model_id.split("/", maxsplit=1)[0] if "/" in model_id else model_id
    model_name = model_id.split("/", 1)[1] if "/" in model_id else model_id

    row = _ModelRow(
        model_id=model_id,
        provider=provider,
        context_window=caps.context_window,
        input_per_1m=price.input_per_1k * 1000,
        output_per_1m=price.output_per_1k * 1000,
        cached_input_per_1m=(
            price.cached_input_per_1k * 1000 if price.cached_input_per_1k is not None else None
        ),
        supports_tools=caps.supports_tools,
        supports_vision=caps.supports_vision,
        notes=caps.notes,
        in_pricing_table=in_pricing,
    )

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
