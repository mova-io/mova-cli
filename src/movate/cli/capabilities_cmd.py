"""``mdk capabilities`` — show what a deployed runtime version supports.

Hits ``GET /api/v1/capabilities`` on the target runtime and renders the
self-description: reachable models, feature flags (derived from the deployed
route table / importable modules — not a static promise), the scope
vocabulary, this tenant's effective limits, and installed extras.

Useful for "what does THIS deployment support?" when the same Mova iO control
plane talks to many heterogeneous customer runtimes on different ``mdk``
versions. Read-only; never mutates the target.

``--json`` emits the raw ``CapabilitiesView`` for scripting / Mova iO; the
default renders a human table.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._console import error, get_global_target, hint
from movate.cli._output import TableJson
from movate.cli._progress import spinner
from movate.core.client import MovateClient, MovateClientError
from movate.core.user_config import (
    UserConfigError,
    resolve_bearer_token,
    resolve_target,
)
from movate.runtime.schemas import CapabilitiesView

stdout = Console()
err = Console(stderr=True)


def capabilities(
    target: str = typer.Option(
        None,
        "--target",
        "-t",
        help=(
            "Deployment target name (from `mdk config list-targets`). "
            "Omit to use the active target."
        ),
    ),
    output_format: TableJson = typer.Option(
        TableJson.TABLE,
        "--output",
        "-o",
        case_sensitive=False,
        help="`table` (default, human) or `json` (raw CapabilitiesView).",
    ),
) -> None:
    """Show the capability matrix of a deployed runtime.

    [bold]Examples:[/bold]

      [dim]# Against the active target[/dim]
      $ mdk capabilities

      [dim]# A specific deployment, as JSON for scripting[/dim]
      $ mdk capabilities --target prod --output json
    """
    try:
        target_name, target_cfg = resolve_target(target or get_global_target())
        token = resolve_bearer_token(target_cfg)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None

    asyncio.run(
        _show(
            target_name=target_name,
            base_url=target_cfg.url,
            token=token,
            output_format=output_format,
        )
    )


async def _show(
    *,
    target_name: str,
    base_url: str,
    token: str,
    output_format: TableJson,
) -> None:
    async with MovateClient(base_url=base_url, api_key=token) as client:
        try:
            with spinner(f"fetching capabilities from {target_name}..."):
                view = await client.capabilities()
        except MovateClientError as exc:
            error(str(exc), context="capabilities")
            raise typer.Exit(code=1) from None

    if output_format == TableJson.JSON:
        stdout.print(view.model_dump_json(), soft_wrap=True, highlight=False)
        return

    _render_tables(view, target_name=target_name)


def _render_tables(view: CapabilitiesView, *, target_name: str) -> None:
    """Render the capability matrix as Rich tables."""
    stdout.print(
        f"[bold]{target_name}[/bold] — mdk [cyan]{view.mdk_version}[/cyan] "
        f"(API [cyan]{view.api_version}[/cyan])"
    )

    if view.minimal:
        # The target accepted us unauthenticated / under-scoped — only the
        # version fingerprint came back. Tell the operator why the rest is
        # missing instead of rendering empty tables.
        hint(
            "[dim]minimal view — the target returned only its version "
            "fingerprint. Use a key with the `read` scope (set a target token "
            "via `mdk auth ...`) for the full capability matrix.[/dim]"
        )
        return

    # Features.
    if view.features is not None:
        feat_table = Table(title="features", title_style="bold", show_lines=False)
        feat_table.add_column("feature")
        feat_table.add_column("supported", justify="center")
        for name, supported in view.features.items():
            mark = "[green]yes[/green]" if supported else "[dim]no[/dim]"
            feat_table.add_row(name, mark)
        stdout.print(feat_table)

    # Models.
    if view.models is not None:
        stdout.print(
            f"[bold]models[/bold]: {len(view.models.available)} available"
            + (f", default [cyan]{view.models.default}[/cyan]" if view.models.default else "")
        )
        if view.models.byok_configured:
            stdout.print(
                "  [bold]BYOK configured[/bold] (provider names): "
                + ", ".join(view.models.byok_configured)
            )

    # Limits.
    if view.limits is not None:
        lim_table = Table(title="limits", title_style="bold", show_lines=False)
        lim_table.add_column("limit")
        lim_table.add_column("value", justify="right")
        lim = view.limits
        lim_table.add_row(
            "rate_limit_per_min",
            str(lim.rate_limit_per_min) if lim.rate_limit_per_min is not None else "off",
        )
        lim_table.add_row(
            "tenant_rate_limit_per_min",
            str(lim.tenant_rate_limit_per_min)
            if lim.tenant_rate_limit_per_min is not None
            else "off",
        )
        lim_table.add_row("max_batch_size", str(lim.max_batch_size))
        stdout.print(lim_table)

    # Scopes + extras (compact one-liners).
    if view.scopes_supported is not None:
        stdout.print("[bold]scopes[/bold]: " + ", ".join(view.scopes_supported))
    if view.extras_installed is not None:
        extras = ", ".join(view.extras_installed) if view.extras_installed else "(none)"
        stdout.print("[bold]extras installed[/bold]: " + extras)
