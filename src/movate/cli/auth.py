"""``movate auth`` — manage tenant API keys."""

from __future__ import annotations

import typer

from movate.cli._stub import not_yet_implemented

auth_app = typer.Typer(
    name="auth",
    help="Manage API keys for the movate runtime.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@auth_app.command("create-key")
def create_key(
    tenant: str = typer.Argument(..., help="Tenant id."),
) -> None:
    """Create a new API key for a tenant."""
    _ = tenant
    not_yet_implemented("auth create-key", "v0.5")


@auth_app.command("list-keys")
def list_keys() -> None:
    """List active API keys."""
    not_yet_implemented("auth list-keys", "v0.5")


@auth_app.command("revoke-key")
def revoke_key(
    key_id: str = typer.Argument(..., help="Key id to revoke."),
) -> None:
    """Revoke an API key."""
    _ = key_id
    not_yet_implemented("auth revoke-key", "v0.5")
