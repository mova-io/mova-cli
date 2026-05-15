"""``mdk profiles {list, show, use, create, delete}`` — Sprint O Day 1-3.

Kubectl-context-style named environment contexts. First member of
the Sprint O env-management cluster (profiles → secrets → env →
migrate → promote, in dependency order).

  $ mdk profiles create dev --target dev-runtime --tenant movate-dev
  $ mdk profiles use dev
  $ mdk profiles list                    # current marked with *
  $ mdk profiles show prod
  $ mdk profiles delete legacy --force

The active profile is the *operational* context. Downstream Sprint
O commands (`mdk secrets`, `mdk env`, `mdk promote`) read it to
resolve their per-environment behavior.

Layered on top of `mdk config` (technical target registry); not a
replacement. Profile.target references an mdk-config target name.
"""

from __future__ import annotations

import re

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from movate.profiles import (
    Profile,
    ProfileNotFoundError,
    ProfileStoreError,
    get_active_profile,
    load_registry,
    set_active_profile,
)
from movate.profiles.store import save_registry

console = Console()
err_console = Console(stderr=True)


profiles_app = typer.Typer(
    name="profiles",
    help=(
        "Manage named environment contexts (dev / staging / prod / "
        "customer). Each profile bundles a target + tenant_id + "
        "description. The active profile drives `mdk secrets`, "
        "`mdk env`, `mdk promote` (Sprint O)."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


# Profile name format mirrors agent + skill names: lowercase
# alphanumeric with hyphens. Keeps profile names usable as URL
# fragments / filenames / env-var-namespace suffixes downstream.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$")


def _validate_name(name: str) -> None:
    """Reject names that don't match the standard kebab-case pattern.

    Raised early (before any disk write) so a bad name doesn't half-
    persist. The same convention runs through `mdk add` / `mdk
    scaffold tool` — keeping it consistent helps operators muscle-
    memory the rule.
    """
    if not _NAME_RE.match(name):
        err_console.print(
            f"[red]✗[/red] profile name {name!r} invalid; "
            f"must be lowercase alphanumeric with hyphens "
            f"(e.g. 'dev', 'staging', 'us-east-prod')"
        )
        raise typer.Exit(code=2)


# ---------------------------------------------------------------------------
# Subcommand: list
# ---------------------------------------------------------------------------


@profiles_app.command("current")
def current() -> None:
    """Print the active profile name (or exit 1 if none set).

    Plain stdout — no Rich. Designed for shell substitution:

      [dim]$ MY_PROFILE=$(mdk profiles current)[/dim]
      [dim]$ mdk secrets list --profile "$MY_PROFILE"[/dim]

    Faster typing than [bold]mdk profiles show --active-only[/bold] for
    the single most-asked profile question. Same vibe as
    [bold]git branch --show-current[/bold].
    """
    import sys  # noqa: PLC0415

    from movate.profiles import get_active_profile  # noqa: PLC0415

    active = get_active_profile()
    if not active:
        err_console.print(
            "[red]✗[/red] no active profile. "
            "[dim]Run [bold]mdk profiles use <name>[/bold] to set one.[/dim]"
        )
        raise typer.Exit(code=1)
    sys.stdout.write(active + "\n")


@profiles_app.command("list")
def list_() -> None:
    """List every registered profile; mark the active one with *.

    Empty registry prints a friendly hint pointing at `create` rather
    than rendering an empty table.
    """
    try:
        registry = load_registry()
    except ProfileStoreError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    if not registry.profiles:
        console.print(
            "[yellow]⚠[/yellow] no profiles yet. Create one with "
            "[bold]mdk profiles create <name>[/bold]."
        )
        return

    active = get_active_profile()
    table = Table(
        title=f"Profiles ({len(registry.profiles)})",
        title_style="bold",
    )
    table.add_column("Active", style="cyan", no_wrap=True)
    table.add_column("Name", style="bold", no_wrap=True)
    table.add_column("Target", no_wrap=True)
    table.add_column("Tenant ID", style="dim", no_wrap=True)
    table.add_column("Description", style="dim")

    for name in registry.list_names():
        profile = registry.get(name)
        marker = "[green]*[/green]" if name == active else " "
        table.add_row(
            marker,
            profile.name,
            profile.target or "[dim]—[/dim]",
            profile.effective_tenant_id,
            profile.description or "[dim]—[/dim]",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Subcommand: show
# ---------------------------------------------------------------------------


@profiles_app.command("show")
def show(
    name: str = typer.Argument(
        ...,
        help=(
            "Profile name to inspect. Pass [bold]@active[/bold] to "
            "show the currently-active profile."
        ),
    ),
) -> None:
    """Render one profile's details + active status."""
    try:
        registry = load_registry()
    except ProfileStoreError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    # `@active` sugar — resolve to current-active-profile name.
    if name == "@active":
        active = get_active_profile()
        if active is None:
            err_console.print(
                "[red]✗[/red] no active profile set. "
                "Run [bold]mdk profiles use <name>[/bold] first."
            )
            raise typer.Exit(code=1)
        name = active

    try:
        profile = registry.get(name)
    except ProfileNotFoundError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from None

    active = get_active_profile()
    is_active = profile.name == active

    body = (
        f"[bold]name:[/bold]        [cyan]{profile.name}[/cyan]"
        + ("  [green]* active[/green]" if is_active else "")
        + "\n"
        f"[bold]target:[/bold]      {profile.target or '[dim]—[/dim]'}\n"
        f"[bold]tenant id:[/bold]   {profile.effective_tenant_id}"
        + (" [dim](derived from name)[/dim]" if not profile.tenant_id else "")
        + "\n"
        f"[bold]description:[/bold] {profile.description or '[dim]—[/dim]'}"
    )
    console.print(
        Panel(
            body,
            title=f"profile [cyan]{profile.name}[/cyan]",
            title_align="left",
            border_style="cyan",
        )
    )


# ---------------------------------------------------------------------------
# Subcommand: use (activate)
# ---------------------------------------------------------------------------


@profiles_app.command("use")
def use(
    name: str = typer.Argument(..., help="Profile name to activate."),
) -> None:
    """Activate a profile.

    Writes the marker file at ``~/.movate/active-profile``. Downstream
    Sprint O commands (`mdk secrets`, `mdk env`, `mdk promote`) read
    this on each invocation.

    The active profile is process-independent — switching in one
    shell affects every shell. That's the kubectl convention and
    matches operator mental model ("I'm working in prod" is a global
    state, not a per-process one). Override per-command with
    profile-specific flags when needed.
    """
    try:
        registry = load_registry()
    except ProfileStoreError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    try:
        profile = registry.get(name)
    except ProfileNotFoundError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from None

    # Capture the prior profile BEFORE setting the new one so we can
    # echo the transition. Operators currently switch then run
    # `profiles show` to confirm; this removes that round-trip.
    from movate.profiles import get_active_profile  # noqa: PLC0415

    prior = get_active_profile()
    set_active_profile(profile.name)

    if prior == profile.name:
        # No-op switch (operator re-activating the active profile);
        # don't pretend there was a transition.
        console.print(f"[dim]✓ already active:[/dim] [bold cyan]{profile.name}[/bold cyan]")
    elif prior:
        console.print(
            f"[green]✓[/green] switched: "
            f"[dim]{prior}[/dim] → [bold cyan]{profile.name}[/bold cyan]"
            + (f"  [dim]({profile.description})[/dim]" if profile.description else "")
        )
    else:
        # First-time activation — no prior to show.
        console.print(
            f"[green]✓[/green] active profile: [bold cyan]{profile.name}[/bold cyan]"
            + (f"  [dim]({profile.description})[/dim]" if profile.description else "")
        )


# ---------------------------------------------------------------------------
# Subcommand: create
# ---------------------------------------------------------------------------


@profiles_app.command("create")
def create(
    name: str = typer.Argument(
        ...,
        help="Profile name (lowercase + hyphens, e.g. 'dev', 'us-east-prod').",
    ),
    target: str = typer.Option(
        "",
        "--target",
        "-t",
        help=(
            "Reference to an [bold]mdk config[/bold] target name. "
            "Empty for local-only profiles (no deploy target). "
            "Required for profiles used by [bold]mdk promote[/bold]."
        ),
    ),
    tenant_id: str = typer.Option(
        "",
        "--tenant",
        help=(
            "Multi-tenant scoping id. Defaults to the profile name "
            "(common case: `dev` profile = tenant_id `dev`). "
            "Override when names diverge."
        ),
    ),
    description: str = typer.Option(
        "",
        "--description",
        "-d",
        help="One-line human-readable note. Surfaces in `mdk profiles list`.",
    ),
    activate: bool = typer.Option(
        False,
        "--use",
        help="Activate the new profile immediately after creating it.",
    ),
) -> None:
    """Register a new profile (or update an existing one).

    Idempotent: re-running with the same name overwrites the prior
    entry. Treating create + update as one verb keeps the surface
    small; operators get one mental model.

    [bold]Examples:[/bold]

      [dim]# Minimal — local-only profile[/dim]
      $ mdk profiles create local

      [dim]# Full — production profile pointing at a deployed runtime[/dim]
      $ mdk profiles create prod \\
          --target prod-runtime \\
          --tenant acme-prod \\
          --description "Production for acme.com" \\
          --use
    """
    _validate_name(name)
    if tenant_id:
        _validate_name(tenant_id)

    try:
        registry = load_registry()
    except ProfileStoreError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    was_existing = name in registry.profiles
    registry.add(
        Profile(
            name=name,
            target=target.strip(),
            tenant_id=tenant_id.strip(),
            description=description.strip(),
        )
    )
    save_registry(registry)

    if activate:
        set_active_profile(name)

    verb = "updated" if was_existing else "created"
    suffix = "  [green]* now active[/green]" if activate else ""
    console.print(f"[green]✓[/green] {verb} profile [bold cyan]{name}[/bold cyan]{suffix}")


# ---------------------------------------------------------------------------
# Subcommand: delete
# ---------------------------------------------------------------------------


@profiles_app.command("delete")
def delete(
    name: str = typer.Argument(..., help="Profile name to remove."),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Required to actually delete. The default behavior is a "
            "dry-run preview (exit 1) — matches `mdk snapshot delete`'s "
            "safety gate. Profiles can carry meaningful operational "
            "state once secrets ship in Sprint O; deletion should be "
            "a deliberate operator action."
        ),
    ),
) -> None:
    """Remove a profile from the registry.

    If the deleted profile was active, the active marker is also
    cleared — no dangling "active profile X doesn't exist" state.
    Dry-run mode (no ``--force``) just confirms which profile would
    be deleted, useful for verifying name selection before commit.
    """
    try:
        registry = load_registry()
    except ProfileStoreError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    try:
        profile = registry.get(name)
    except ProfileNotFoundError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from None

    if not force:
        console.print(
            f"[yellow]⚠ dry-run:[/yellow] would delete profile "
            f"[bold]{profile.name}[/bold]"
            + (f" ({profile.description})" if profile.description else "")
        )
        console.print("[dim]Re-run with [bold]--force[/bold] to delete.[/dim]")
        raise typer.Exit(code=1)

    registry.remove(name)
    save_registry(registry)
    if get_active_profile() == name:
        set_active_profile(None)
    console.print(f"[green]✓[/green] deleted profile [bold]{profile.name}[/bold]")
