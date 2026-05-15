"""``movate config`` — manage the user-level CLI config (~/.movate/config.yaml).

Subcommands:

* ``movate config add-target`` — register a deployment + bearer-token env var
* ``movate config list-targets`` — show what's registered
* ``movate config use`` — pick the default target
* ``movate config show`` — dump the current config (for debugging)
* ``movate config remove-target`` — delete a target

Bearer tokens are NEVER stored in the config file — only the name of
an env var that holds them. See :mod:`movate.core.user_config` for the
file layout.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._console import confirm_destructive, error, hint, success
from movate.core.user_config import (
    TargetConfig,
    UserConfigError,
    config_path,
    load_user_config,
    save_user_config,
)

stdout = Console()
err = Console(stderr=True)

config_app = typer.Typer(
    name="config",
    help="Manage user-level movate config (deployment targets, active target).",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@config_app.command("add-target")
def add_target(
    name: str = typer.Argument(..., help="Friendly target name, e.g. 'prod', 'staging'."),
    url: str = typer.Option(..., "--url", help="Base URL of the runtime."),
    key_env: str = typer.Option(
        ...,
        "--key-env",
        help="Name of the env var that holds the bearer token (e.g. MDK_PROD_KEY).",
    ),
    set_active: bool = typer.Option(
        False,
        "--set-active",
        help="Make this the default target (saves you a `config use` step).",
    ),
    # --- Optional Azure deploy config (consumed by `movate deploy`) -------
    azure_subscription: str = typer.Option(
        None,
        "--azure-subscription",
        help="Azure subscription id. Required to enable `movate deploy --target <name>`.",
    ),
    azure_resource_group: str = typer.Option(
        None,
        "--azure-resource-group",
        help="Resource group containing the ACA env + ACR (e.g. movate-dev-rg).",
    ),
    azure_acr_name: str = typer.Option(
        None,
        "--azure-acr",
        help="ACR registry name without the .azurecr.io suffix (e.g. movatedevacr).",
    ),
    azure_env: str = typer.Option(
        None,
        "--azure-env",
        help=(
            "Environment label (dev / staging / prod). Used to derive Container "
            "App names: movate-{env}-api, movate-{env}-worker. Should match the "
            "`env` param used when running the Bicep deployment."
        ),
    ),
) -> None:
    """Register a deployment target.

    [bold]Examples:[/bold]

      [dim]# Local dev runtime (no deploy config)[/dim]
      $ mdk config add-target local --url http://127.0.0.1:8000 --key-env MDK_LOCAL_KEY

      [dim]# Prod, with deploy enabled, and make it the default[/dim]
      $ mdk config add-target prod \\
            --url https://movate-prod-api.eastus2.azurecontainerapps.io \\
            --key-env MDK_PROD_KEY \\
            --azure-subscription "$SUBSCRIPTION_ID" \\
            --azure-resource-group movate-prod-rg \\
            --azure-acr movateprodacr \\
            --azure-env prod \\
            --set-active
    """
    cfg = load_user_config()
    cfg.targets[name] = TargetConfig(
        url=url,
        key_env=key_env,
        azure_subscription=azure_subscription,
        azure_resource_group=azure_resource_group,
        azure_acr_name=azure_acr_name,
        azure_env=azure_env,
    )
    if set_active or cfg.active is None:
        cfg.active = name
    path = save_user_config(cfg)
    success(f"added target {name!r} → {url}")
    if cfg.active == name:
        hint(f"[dim]  (active target is now {name!r})[/dim]")
    # Surface which capabilities are configured so the operator sees
    # at registration time whether `movate deploy` will work.
    deploy_ready = all((azure_subscription, azure_resource_group, azure_acr_name, azure_env))
    if deploy_ready:
        hint(f"[dim]  (deploy enabled: env={azure_env}, rg={azure_resource_group})[/dim]")
    else:
        hint(
            "[dim]  (deploy NOT enabled — pass --azure-subscription / "
            "--azure-resource-group / --azure-acr / --azure-env to enable)[/dim]"
        )
    hint(f"[dim]config: {path}[/dim]")


@config_app.command("list-targets")
def list_targets() -> None:
    """Show all registered targets, highlighting the active one."""
    cfg = load_user_config()
    if not cfg.targets:
        hint("[dim]no targets registered — run `mdk config add-target` first[/dim]")
        return

    table = Table(title="movate targets")
    table.add_column("name", style="bold")
    table.add_column("url")
    table.add_column("key_env", style="dim")
    table.add_column("active")
    for name, t in sorted(cfg.targets.items()):
        active = "[green]●[/green]" if name == cfg.active else ""
        table.add_row(name, t.url, t.key_env, active)
    stdout.print(table)


@config_app.command("current")
def current() -> None:
    """Print the active target as a single line (script-friendly).

    Use in shell prompts ("which env am I pointing at?") or as a
    sanity check before a destructive op:

      $ mdk config current
      prod  https://movate-prod.azurecontainerapps.io  MDK_PROD_KEY

      $ if [ "$(mdk config current | awk '{print $1}')" = prod ]; then
            echo "refusing to run dev tooling against prod"
        fi

    No target configured / no active pointer → exit 1 with a short
    stderr message (distinct from "no config exists" since we don't
    want to surface implementation detail to scripts)."""
    cfg = load_user_config()
    if not cfg.active or cfg.active not in cfg.targets:
        error("no active target — run `movate config add-target` then `config use <name>`")
        raise typer.Exit(code=1)
    t = cfg.targets[cfg.active]
    # Tab-separated for easy `awk` / `cut` consumption; the columns
    # are name, url, key_env — same order as `config list-targets`.
    # Write through sys.stdout directly because Rich's Console.print
    # silently expands tabs to spaces (it's a renderer; tabs are a
    # layout primitive for it). For machine-readable output we want
    # the literal byte sequence.
    import sys  # noqa: PLC0415

    sys.stdout.write(f"{cfg.active}\t{t.url}\t{t.key_env}\n")


@config_app.command("use")
def use_target(
    name: str = typer.Argument(..., help="Name of an already-registered target."),
) -> None:
    """Set the active target — CLI commands default to it when --target is omitted."""
    cfg = load_user_config()
    if name not in cfg.targets:
        available = ", ".join(sorted(cfg.targets)) or "(none)"
        error(f"target {name!r} not found. Available: {available}")
        raise typer.Exit(code=2)
    cfg.active = name
    save_user_config(cfg)
    success(f"active target → {name!r}")


@config_app.command("show")
def show() -> None:
    """Dump the resolved config (useful for debugging)."""
    cfg = load_user_config()
    stdout.print(cfg.model_dump_json(indent=2))
    hint(f"[dim]config path: {config_path()}[/dim]")


@config_app.command("remove-target")
def remove_target(
    name: str = typer.Argument(..., help="Name of a registered target."),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirm prompt (use in scripts / CI).",
    ),
) -> None:
    """Delete a target. If it was active, the active pointer is cleared
    (next CLI call will require --target until you `config use` again).

    Prompts before deleting; pass ``-y`` to bypass for scripts."""
    cfg = load_user_config()
    if name not in cfg.targets:
        error(f"target {name!r} not found")
        raise typer.Exit(code=2)
    confirm_destructive(f"Remove target {name!r} from config?", yes=yes)
    del cfg.targets[name]
    if cfg.active == name:
        cfg.active = None
    save_user_config(cfg)
    success(f"removed target {name!r}")


# Re-export the error type so callers don't have to reach into core.
__all__ = ["UserConfigError", "config_app"]
