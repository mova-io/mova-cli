"""``movate doctor`` — environment + configuration sanity check.

Default mode reports on the local environment (Python, deps, provider
keys, tracer, storage). Pass ``--target <name>`` to add an Azure-side
preflight that walks the deploy path (``az`` login → subscription
→ resource group → ACR → Container Apps → ``/healthz``) — the
first thing to run when ``movate deploy`` is acting up.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from movate import __version__
from movate.providers.pricing import load_pricing
from movate.tracing import build_tracer

console = Console()

_REQUIRED_DEPS = ("typer", "rich", "pydantic", "yaml", "jinja2", "litellm", "aiosqlite")
_OPTIONAL_DEPS = ("langfuse", "opentelemetry", "asyncpg", "fastapi")
_PROVIDER_KEYS = (
    ("OPENAI_API_KEY", "OpenAI"),
    ("ANTHROPIC_API_KEY", "Anthropic"),
    ("AZURE_OPENAI_API_KEY", "Azure OpenAI"),
    ("GEMINI_API_KEY", "Gemini"),
)
_TRACING_KEYS = (
    ("MOVATE_TRACER", "explicit override"),
    ("LANGFUSE_SECRET_KEY", "Langfuse secret"),
    ("LANGFUSE_PUBLIC_KEY", "Langfuse public"),
    ("LANGFUSE_HOST", "Langfuse host"),
    ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTel endpoint"),
    ("OTEL_SERVICE_NAME", "OTel service.name"),
)


def _ok(label: str) -> str:
    return f"[green]ok[/green] [dim]{label}[/dim]" if label else "[green]ok[/green]"


def _missing(label: str) -> str:
    return f"[yellow]missing[/yellow] [dim]{label}[/dim]" if label else "[yellow]missing[/yellow]"


def doctor(
    target: str = typer.Option(
        None,
        "--target",
        "-t",
        help=(
            "Also run the Azure preflight for a registered target "
            "(az login → subscription → RG → ACR → Container Apps → /healthz). "
            "Use this when `movate deploy` is failing."
        ),
    ),
) -> None:
    """Report on the local environment, deps, API keys, and movate state.

    With ``--target <name>``, adds a second table walking the Azure
    deploy path so you see the earliest broken link, not a stack trace
    from ``movate deploy``.
    """
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

    # Tracing keys (separate from agent provider keys — easier to scan)
    for env_var, label in _TRACING_KEYS:
        present = bool(os.environ.get(env_var, "").strip())
        table.add_row(env_var, _ok(label) if present else _missing(label))

    # Resolved tracer — what `movate run` would actually use right now.
    try:
        tracer = build_tracer()
        table.add_row("resolved tracer", f"[green]{tracer.name}[/green]")
    except Exception as exc:  # pragma: no cover - diagnostic only
        table.add_row("resolved tracer", f"[red]error: {exc}[/red]")

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

    # ------------------------------------------------------------------
    # Optional: Azure preflight when --target is set
    # ------------------------------------------------------------------
    if target is not None:
        _render_azure_preflight(target)


def _render_azure_preflight(target_name: str) -> None:
    """Print a second table with the Azure-side checks. Resolves the
    target first; missing target is itself a finding (operator pointer
    in the error tells them to run `movate config add-target`)."""
    # Local imports — keep the doctor command's hot-path tight; these
    # are only needed when --target is set.
    from movate.cli._azure_doctor import run_azure_preflight  # noqa: PLC0415
    from movate.core.user_config import UserConfigError, resolve_target  # noqa: PLC0415

    try:
        target_name_resolved, target_cfg = resolve_target(target_name)
    except UserConfigError as exc:
        console.print(f"\n[red]✗ azure preflight skipped:[/red] {exc}")
        return

    table = Table(
        title=f"azure preflight → {target_name_resolved}",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Check")
    table.add_column("Result")

    for check in run_azure_preflight(target_name_resolved, target_cfg):
        if check.status == "ok":
            badge = _ok(check.detail)
        elif check.status == "missing":
            badge = _missing(check.detail)
        else:
            badge = (
                f"[red]error[/red] [dim]{check.detail}[/dim]"
                if check.detail
                else "[red]error[/red]"
            )
        table.add_row(check.name, badge)

    console.print()
    console.print(table)
