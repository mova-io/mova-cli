"""``mdk secrets {set, get, list, delete, export-shell}`` — Sprint O Day 4-7.

Per-profile secret storage. Distinct from ``mdk env`` (names +
presence): this owns **values**.

  $ mdk profiles use dev                              # pick the namespace
  $ mdk secrets set OPENAI_API_KEY                    # interactive prompt (hidden)
  $ mdk secrets set OPENAI_API_KEY --value sk-...     # non-interactive
  $ mdk secrets list                                  # names + descriptions, NO values
  $ mdk secrets get OPENAI_API_KEY                    # echo the value
  $ mdk secrets delete OPENAI_API_KEY --force         # remove
  $ eval $(mdk secrets export-shell)                  # source into current shell

MVP storage: ``~/.movate/secrets/<profile>.yaml``, chmod 0600.
Plaintext-at-rest with file permissions as the line of defense.
Operator-passphrase encryption + cloud sync (Key Vault, AWS Secrets
Manager) land as follow-ups behind the same CLI.
"""

from __future__ import annotations

import sys

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from movate.profiles import get_active_profile, load_registry
from movate.profiles.store import ProfileNotFoundError, ProfileStoreError
from movate.secrets import (
    SecretNotFoundError,
    SecretsStoreError,
    load_store,
)
from movate.secrets.store import save_store

console = Console()
err_console = Console(stderr=True)


secrets_app = typer.Typer(
    name="secrets",
    help=(
        "Per-profile secret storage (values; distinct from [bold]mdk env[/bold]). "
        "Operates on the [bold]active profile[/bold] by default — switch with "
        "[bold]mdk profiles use <name>[/bold] or override per-command via "
        "[bold]--profile[/bold]."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _resolve_profile(explicit: str) -> str:
    """Resolve the secrets profile from --profile or the active marker.

    Hard error (exit 2) if neither path resolves. Refuse to default
    to a magic ``"local"`` — operators shouldn't accidentally write
    secrets into a profile they didn't intend.
    """
    if explicit:
        # Validate the named profile exists in the registry — typo
        # prevention. Profile registry errors bubble up.
        try:
            registry = load_registry()
            registry.get(explicit)
        except ProfileNotFoundError as exc:
            err_console.print(f"[red]✗[/red] {exc}")
            raise typer.Exit(code=2) from None
        except ProfileStoreError as exc:
            err_console.print(f"[red]✗[/red] {exc}")
            raise typer.Exit(code=2) from None
        return explicit

    active = get_active_profile()
    if not active:
        err_console.print(
            "[red]✗[/red] no active profile and no --profile passed. "
            "Run [bold]mdk profiles use <name>[/bold] or "
            "[bold]mdk secrets <cmd> --profile <name>[/bold]."
        )
        raise typer.Exit(code=2)
    return active


# ---------------------------------------------------------------------------
# Subcommand: set
# ---------------------------------------------------------------------------


@secrets_app.command("set")
def set_(
    name: str = typer.Argument(..., help="Secret name (e.g. OPENAI_API_KEY)."),
    value: str | None = typer.Option(
        None,
        "--value",
        help=(
            "The secret value. Omit to be prompted interactively "
            "(input hidden — preferred for security)."
        ),
    ),
    description: str = typer.Option(
        "",
        "--description",
        "-d",
        help="Operator-supplied note about this secret.",
    ),
    profile: str = typer.Option(
        "",
        "--profile",
        "-p",
        help="Override the active profile for this operation only.",
    ),
) -> None:
    """Store a secret in the active profile.

    Re-running on an existing name updates the value + bumps the
    last_rotated timestamp. The original created_at is preserved
    so rotation history is auditable later.

    [bold red]⚠ MVP: secrets are stored UNENCRYPTED on disk[/bold red]
    (file permission 0600 is the only line of defense). For
    production secrets use Key Vault directly today; native
    encryption + cloud sync land as Sprint O follow-ups.
    """
    profile_name = _resolve_profile(profile)

    # If --value not given (None), prompt with hidden input.
    # If --value "" (explicitly empty), skip the prompt and fail below.
    if value is None:
        value = typer.prompt(f"Value for {name}", hide_input=True, confirmation_prompt=False)
    if not value:
        err_console.print("[red]✗[/red] secret value cannot be empty")
        raise typer.Exit(code=2)

    try:
        store = load_store(profile_name)
    except SecretsStoreError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    was_existing = name in store.secrets
    store.set(name, value, description=description)
    save_store(store)

    verb = "rotated" if was_existing else "stored"
    console.print(
        f"[green]✓[/green] {verb} [bold]{name}[/bold] in profile [cyan]{profile_name}[/cyan]"
    )
    # One-time loud warning on every set — operators need this signal
    # repeatedly until production-grade encryption ships.
    console.print(
        "[yellow]⚠ MVP storage is unencrypted on disk[/yellow] "
        "[dim](chmod 0600). For production secrets use Key Vault "
        "directly. See BACKLOG K-state for the encryption follow-up.[/dim]"
    )


# ---------------------------------------------------------------------------
# Subcommand: get
# ---------------------------------------------------------------------------


@secrets_app.command("get")
def get(
    name: str = typer.Argument(..., help="Secret name to retrieve."),
    profile: str = typer.Option(
        "",
        "--profile",
        "-p",
        help="Override the active profile.",
    ),
) -> None:
    """Print a secret's value to stdout.

    [bold]Plain stdout write, no Rich formatting[/bold] — designed
    for command substitution: ``MY_KEY=$(mdk secrets get MY_KEY)``.

    Use sparingly: any command that echoes a secret risks leaking it
    via shell history or terminal scrollback. For shell sourcing,
    prefer [bold]mdk secrets export-shell[/bold] which never echoes
    the value to a terminal that's likely to be screenshared.
    """
    profile_name = _resolve_profile(profile)
    try:
        store = load_store(profile_name)
        secret = store.get(name)
    except SecretNotFoundError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from None
    except SecretsStoreError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    # Plain stdout — no Rich ANSI codes. Trailing newline so shell
    # substitution captures cleanly.
    sys.stdout.write(secret.value + "\n")


# ---------------------------------------------------------------------------
# Subcommand: list
# ---------------------------------------------------------------------------


@secrets_app.command("list")
def list_(
    profile: str = typer.Option(
        "",
        "--profile",
        "-p",
        help="Override the active profile.",
    ),
) -> None:
    """List secret names in the active profile. NEVER displays values.

    Values stay opaque in this view — operators check them via
    explicit `get`. Reduces accidental-shoulder-surfing risk.
    """
    profile_name = _resolve_profile(profile)
    try:
        store = load_store(profile_name)
    except SecretsStoreError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    if not store.secrets:
        console.print(
            f"[yellow]⚠[/yellow] no secrets in profile [cyan]{profile_name}[/cyan]. "
            f"Add one with [bold]mdk secrets set <NAME>[/bold]."
        )
        return

    table = Table(
        title=f"Secrets ({len(store.secrets)}) in profile [cyan]{profile_name}[/cyan]",
        title_style="bold",
    )
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Description", style="dim")
    table.add_column("Created", style="dim", no_wrap=True)
    table.add_column("Last rotated", style="dim", no_wrap=True)

    for name in store.names():
        secret = store.get(name)
        table.add_row(
            secret.name,
            secret.description or "[dim]—[/dim]",
            secret.created_at or "[dim]—[/dim]",
            secret.last_rotated or "[dim](initial)[/dim]",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Subcommand: delete
# ---------------------------------------------------------------------------


@secrets_app.command("delete")
def delete(
    name: str = typer.Argument(..., help="Secret name to remove."),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Required to actually delete. Default behavior is a "
            "dry-run preview — matches `mdk snapshot delete` / "
            "`mdk profiles delete` safety convention."
        ),
    ),
    profile: str = typer.Option(
        "",
        "--profile",
        "-p",
        help="Override the active profile.",
    ),
) -> None:
    """Remove a secret from the active profile."""
    profile_name = _resolve_profile(profile)
    try:
        store = load_store(profile_name)
        secret = store.get(name)
    except SecretNotFoundError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from None
    except SecretsStoreError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    if not force:
        console.print(
            f"[yellow]⚠ dry-run:[/yellow] would delete [bold]{secret.name}[/bold] "
            f"from profile [cyan]{profile_name}[/cyan]"
        )
        console.print("[dim]Re-run with [bold]--force[/bold] to delete.[/dim]")
        raise typer.Exit(code=1)

    store.delete(name)
    save_store(store)
    console.print(
        f"[green]✓[/green] deleted [bold]{secret.name}[/bold] "
        f"from profile [cyan]{profile_name}[/cyan]"
    )


# ---------------------------------------------------------------------------
# Subcommand: export-shell
# ---------------------------------------------------------------------------


@secrets_app.command("export-shell")
def export_shell(
    profile: str = typer.Option(
        "",
        "--profile",
        "-p",
        help="Override the active profile.",
    ),
) -> None:
    """Emit shell ``export`` statements for every secret in the profile.

    Designed for command substitution into the current shell:

      $ eval $(mdk secrets export-shell)

    Plain stdout, no Rich. Each secret becomes ``export NAME='value'``
    with single-quote-escaped value (handles embedded ``$`` / quotes
    safely). The secrets remain in the parent shell's process env
    for child commands to read — same pattern `dotenv` uses.

    [bold red]⚠ never run this on a screenshared terminal[/bold red]
    — the export lines render the values in plain text. Use the
    explicit ``mdk secrets get`` for safer one-off retrieval.
    """
    profile_name = _resolve_profile(profile)
    try:
        store = load_store(profile_name)
    except SecretsStoreError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    if not store.secrets:
        err_console.print(f"[yellow]⚠[/yellow] no secrets in profile [cyan]{profile_name}[/cyan]")
        return

    for name in store.names():
        secret = store.get(name)
        # Single-quote-escape: replace any embedded single quote with
        # the standard shell-safe trick: '"'"' (close, quoted-quote, reopen).
        escaped = secret.value.replace("'", "'\"'\"'")
        sys.stdout.write(f"export {name}='{escaped}'\n")


# ---------------------------------------------------------------------------
# Subcommand: where (paths debug)
# ---------------------------------------------------------------------------


@secrets_app.command("where")
def where(
    profile: str = typer.Option(
        "",
        "--profile",
        "-p",
        help="Override the active profile.",
    ),
) -> None:
    """Print the on-disk path for the active profile's secrets file.

    Operator-friendly when chasing "where exactly are my secrets
    stored?" or for `chmod`-debugging.
    """
    from movate.secrets.store import _store_path  # noqa: PLC0415

    profile_name = _resolve_profile(profile)
    path = _store_path(profile_name)
    exists = path.is_file()

    body = (
        f"[bold]profile:[/bold]   [cyan]{profile_name}[/cyan]\n"
        f"[bold]path:[/bold]      {path}\n"
        f"[bold]exists:[/bold]    {'yes' if exists else 'no'}"
    )
    if exists:
        mode = oct(path.stat().st_mode & 0o777)
        body += f"\n[bold]mode:[/bold]      {mode}"
        if mode != "0o600":
            body += "  [red](should be 0o600)[/red]"
    console.print(
        Panel(body, title="Secrets file location", title_align="left", border_style="cyan")
    )
