"""``movate doctor`` — environment + configuration sanity check."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from movate import __version__
from movate.providers.pricing import load_pricing

console = Console()

_REQUIRED_DEPS = ("typer", "rich", "pydantic", "yaml", "jinja2", "litellm", "aiosqlite")
_OPTIONAL_DEPS = ("langfuse", "opentelemetry", "asyncpg", "fastapi")
_PROVIDER_KEYS = (
    ("OPENAI_API_KEY", "OpenAI"),
    ("ANTHROPIC_API_KEY", "Anthropic"),
    ("AZURE_OPENAI_API_KEY", "Azure OpenAI"),
    ("GEMINI_API_KEY", "Gemini"),
)


def _ok(label: str) -> str:
    return f"[green]ok[/green] [dim]{label}[/dim]" if label else "[green]ok[/green]"


def _missing(label: str) -> str:
    return f"[yellow]missing[/yellow] [dim]{label}[/dim]" if label else "[yellow]missing[/yellow]"


def doctor() -> None:
    """Report on the local environment, deps, API keys, and movate state."""
    table = Table(title="movate doctor", show_header=True, header_style="bold")
    table.add_column("Check")
    table.add_column("Result")

    table.add_row("Python", sys.version.split()[0])
    table.add_row("movate", __version__)
    table.add_row("", "")

    # Required deps
    for mod in _REQUIRED_DEPS:
        spec = importlib.util.find_spec(mod)
        table.add_row(f"dep: {mod}", _ok("") if spec else "[red]missing (install fail)[/red]")

    table.add_row("", "")

    # Optional deps
    for mod in _OPTIONAL_DEPS:
        spec = importlib.util.find_spec(mod)
        table.add_row(f"opt: {mod}", _ok("") if spec else _missing("not installed"))

    table.add_row("", "")

    # Provider API keys
    any_key = False
    for env_var, label in _PROVIDER_KEYS:
        present = bool(os.environ.get(env_var, "").strip())
        any_key = any_key or present
        table.add_row(env_var, _ok(label) if present else _missing(label))

    if not any_key:
        table.add_row(
            "[yellow]hint[/yellow]",
            "[dim]no provider keys set; use --mock for offline runs[/dim]",
        )

    table.add_row("", "")

    # Storage
    sqlite_path = Path("~/.movate/local.db").expanduser()
    state = "exists" if sqlite_path.exists() else "will be created on first run"
    table.add_row("storage (sqlite)", f"{sqlite_path} [dim]({state})[/dim]")

    # Pricing
    try:
        pricing = load_pricing()
        models = len(pricing.models)
        table.add_row(
            "pricing",
            f"v{pricing.version} ({models} models, last_verified {pricing.last_verified})",
        )
    except Exception as exc:
        table.add_row("pricing", f"[red]load failed: {exc}[/red]")

    # Project config
    project_yaml = Path("movate.yaml")
    table.add_row(
        "movate.yaml",
        f"[green]found[/green] [dim]({project_yaml.resolve()})[/dim]"
        if project_yaml.exists()
        else _missing("not in cwd; defaults will be used"),
    )

    console.print(table)
