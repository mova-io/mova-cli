"""``mdk env {list, check, diff}`` — env-var management (Sprint O Day 8-9).

Distinct from `mdk secrets` (values). This module owns **names +
presence**: what env vars does the project need? Are they all set?

  $ mdk env list            # show every env var the project references
  $ mdk env check            # validate they're all set; exit 1 if missing
  $ mdk env check --strict   # treat unset optionals as failures too
  $ mdk env diff             # show what's set vs missing in current shell

Pairs with `mdk profiles` (active context) and `mdk secrets` (value
management, Sprint O Day 4-7). Together: profiles names the
context, secrets manages the values, env validates the names are
all present.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from movate.env_mgmt import (
    discover_env_vars,
)
from movate.env_mgmt.discovery import check_presence

console = Console()
err_console = Console(stderr=True)


env_app = typer.Typer(
    name="env",
    help=(
        "Manage env-var [bold]names + presence[/bold] for the project. "
        "Distinct from [bold]mdk secrets[/bold] (values). Discovers "
        "every env var referenced in .env.example, agent.yaml, and "
        "skill impl.py files."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _resolve_project_root(explicit: Path | None) -> Path:
    """Walk-up resolution — same convention as snapshot_cmd / diff_cmd."""
    if explicit is not None:
        if not explicit.is_dir():
            err_console.print(f"[red]✗[/red] --project path is not a directory: {explicit}")
            raise typer.Exit(code=2)
        return explicit.resolve()
    current = Path.cwd().resolve()
    while True:
        if (current / "movate.yaml").is_file():
            return current
        if current.parent == current:
            break
        current = current.parent
    return Path.cwd().resolve()


# ---------------------------------------------------------------------------
# Subcommand: list
# ---------------------------------------------------------------------------


@env_app.command("list")
def list_(
    project: Path | None = typer.Option(
        None,
        "--project",
        "-p",
        help="Project root. Defaults to walking up from cwd for movate.yaml.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON instead of a Rich table — pipe-friendly.",
    ),
) -> None:
    """List every env var the project references.

    Sources merged in priority order:
    1. ``.env.example`` (canonical — operator-curated)
    2. ``${VAR}`` / ``$VAR`` references in every agent.yaml
    3. ``os.environ[...]`` / ``os.environ.get(...)`` in skill impl.py

    Each variable lists its sources so operators can chase "why does
    my project need FOO?" back to the originating file.
    """
    project_root = _resolve_project_root(project)
    refs = discover_env_vars(project_root)

    if json_output:
        payload = [
            {
                "name": r.name,
                "required": r.required,
                "default": r.default,
                "sources": [s.value for s in r.sources],
            }
            for r in refs
        ]
        # Direct stdout write — Rich injects ANSI codes that break jq.
        import sys  # noqa: PLC0415

        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return

    if not refs:
        console.print(
            "[yellow]⚠[/yellow] no env-var references found. "
            "Add a [bold].env.example[/bold] file at the project root, "
            "or reference env vars in agent.yaml / skill impl.py."
        )
        return

    table = Table(
        title=f"Env vars ({len(refs)})",
        title_style="bold",
    )
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Required", no_wrap=True)
    table.add_column("Default", style="dim", no_wrap=True)
    table.add_column("Sources", style="dim")

    for ref in refs:
        req = "[red]yes[/red]" if ref.required else "[dim]no[/dim]"
        default = ref.default or "[dim]—[/dim]"
        sources = ", ".join(s.value for s in ref.sources)
        table.add_row(ref.name, req, default, sources)

    console.print(table)


# ---------------------------------------------------------------------------
# Subcommand: check
# ---------------------------------------------------------------------------


@env_app.command("check")
def check(
    project: Path | None = typer.Option(
        None,
        "--project",
        "-p",
        help="Project root.",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help=(
            "Treat unset optionals as failures too. CI-friendly: "
            "require every declared var to be set even when it has "
            "a default in .env.example."
        ),
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON instead of a Rich panel.",
    ),
) -> None:
    """Validate that every required env var is set in the current shell.

    Exit 0 = all required vars set. Exit 1 = at least one missing.
    With ``--strict``, optional vars (those with defaults in
    ``.env.example``) also fail when unset.

    Designed for CI gating: ``mdk env check --strict || exit`` at the
    top of a deploy script makes "set every env var" a hard
    precondition.
    """
    project_root = _resolve_project_root(project)
    refs = discover_env_vars(project_root)
    missing, present = check_presence(refs, dict(os.environ), strict=strict)

    if json_output:
        payload = {
            "missing": [{"name": r.name, "required": r.required} for r in missing],
            "present": [{"name": r.name} for r in present],
            "all_set": not missing,
            "strict": strict,
        }
        import sys  # noqa: PLC0415

        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        if missing:
            raise typer.Exit(code=1)
        return

    if not refs:
        console.print(
            "[yellow]⚠[/yellow] no env-var references found in project. Nothing to check."
        )
        return

    if not missing:
        mode = "strict" if strict else "default"
        console.print(f"[green]✓[/green] all {len(present)} env var(s) set ({mode} mode)")
        return

    console.print(f"[red]✗[/red] {len(missing)} env var(s) missing in current shell:")
    for ref in missing:
        req_label = "required" if ref.required else "optional"
        default_str = f"  [dim](default: {ref.default!r})[/dim]" if ref.default else ""
        sources = ", ".join(s.value for s in ref.sources)
        console.print(
            f"  [red]✗[/red] [bold]{ref.name}[/bold]  "
            f"[dim]({req_label}, from {sources})[/dim]{default_str}"
        )
    console.print()
    console.print(
        "[dim]Set the missing vars in your shell (or via [bold]mdk secrets[/bold] "
        "once that ships in Sprint O Day 4-7).[/dim]"
    )
    raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Subcommand: diff
# ---------------------------------------------------------------------------


@env_app.command("diff")
def diff(
    project: Path | None = typer.Option(
        None,
        "--project",
        "-p",
        help="Project root.",
    ),
) -> None:
    """Compare what's declared in the project vs what's set in the shell.

    Three categories rendered:

    * **declared + set** — green, correct state
    * **declared + unset** — red, action needed
    * **set but not declared** — yellow, possibly an unused var (or
      one the operator should add to ``.env.example``)

    The third category is informational: it catches env vars the
    project doesn't formally declare but the operator has set
    anyway. Doesn't fail the check — just surfaces signal.

    Future (Sprint O Day 12-13 — ``mdk promote``): cross-profile
    diff comparing local env vs deploy-target's resolved env. Needs
    profile target metadata to land first.
    """
    project_root = _resolve_project_root(project)
    refs = discover_env_vars(project_root)
    env = dict(os.environ)

    declared_names = {r.name for r in refs}
    declared_set = [r for r in refs if env.get(r.name)]
    declared_unset = [r for r in refs if r.name not in env or not env[r.name]]

    # Env vars set in the shell that look MDK-related but aren't
    # declared. Heuristic: starts with MDK_ / MOVATE_ / one of common
    # AI-provider prefixes (OPENAI_, ANTHROPIC_, AZURE_, AWS_, etc).
    # Avoids surfacing the user's entire environment.
    mdk_prefixes = (
        "MDK_",
        "MOVATE_",
        "OPENAI_",
        "ANTHROPIC_",
        "AZURE_",
        "AWS_",
        "LANGFUSE_",
    )
    extra_in_shell = sorted(
        name
        for name in env
        if any(name.startswith(p) for p in mdk_prefixes) and name not in declared_names
    )

    if not refs and not extra_in_shell:
        console.print(
            "[yellow]⚠[/yellow] no env-var references found in project "
            "and no MDK-related vars set in shell."
        )
        return

    table = Table(title="Env diff", title_style="bold")
    table.add_column("Status", no_wrap=True)
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Detail", style="dim")

    for ref in declared_set:
        table.add_row("[green]✓ set[/green]", ref.name, "declared + set")
    for ref in declared_unset:
        req_label = "required" if ref.required else "optional"
        table.add_row("[red]✗ unset[/red]", ref.name, f"declared {req_label}")
    for name in extra_in_shell:
        table.add_row(
            "[yellow]? extra[/yellow]",
            name,
            "set in shell, not declared in project",
        )

    console.print(table)
