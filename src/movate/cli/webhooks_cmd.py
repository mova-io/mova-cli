"""``mdk webhooks`` — manage outbound webhook subscriptions (ADR 035 D2).

Sibling to ``mdk trigger`` (the *inbound* webhook surface). Where a
trigger lets an external system POST events INTO the runtime, a
webhook lets the runtime POST events OUT to a subscriber when
lifecycle events happen (a run finishes, an eval fails, a canary
promotes, ...).

Subcommands:

* ``mdk webhooks list`` — show this tenant's subscriptions (table or JSON).
* ``mdk webhooks create --url <https://...> --kind <kind>[,kind]
  [--disabled]`` — register a subscription; prints the HMAC secret
  ONCE on creation (irrecoverable after).
* ``mdk webhooks show <id>`` — single subscription's current state.
* ``mdk webhooks delete <id> [--yes]`` — remove a subscription
  (confirm prompt unless ``--yes``).
* ``mdk webhooks enable/disable <id>`` — toggle without recreating.
* ``mdk webhooks attempts <id> [--limit N]`` — recent delivery log.

All read commands accept ``--output json`` for scripting, matching the
existing CLI convention (``mdk jobs``, ``mdk runs``, ``mdk batch``).

This module talks to a deployed runtime via :class:`MovateClient`;
``cli ⊥ runtime`` is preserved by going through the HTTP API rather
than reaching into storage directly.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._console import echo_remote_context, error, get_global_target, hint
from movate.cli._output import TableJson
from movate.cli._progress import spinner
from movate.core.client import MovateClient, MovateClientError
from movate.core.user_config import (
    UserConfigError,
    resolve_bearer_token,
    resolve_target,
)
from movate.core.webhooks import (
    WILDCARD_KIND,
    WebhookAttemptListView,
    WebhookCreatedView,
    WebhookListView,
    WebhookView,
)

stdout = Console()
err = Console(stderr=True)


webhooks_app = typer.Typer(
    name="webhooks",
    help="Manage outbound webhook subscriptions for lifecycle events (ADR 035 D2).",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@webhooks_app.command("list")
def list_webhooks(
    target: str = typer.Option(None, "--target", "-t", help="Deployment target name."),
    include_disabled: bool = typer.Option(
        True,
        "--include-disabled/--enabled-only",
        help="Show disabled subscriptions. Default: include disabled.",
    ),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """List this tenant's webhook subscriptions.

    Each row carries ``secret_hint`` (last 4 chars) only; the full
    HMAC secret is irrecoverable from this surface — it is shown ONCE
    on creation. Use ``mdk webhooks create`` again if you've lost the
    secret.
    """
    suppress = output_format == TableJson.JSON
    listing = asyncio.run(
        _list(target=target, include_disabled=include_disabled, suppress=suppress)
    )
    if output_format == TableJson.JSON:
        stdout.print(listing.model_dump_json(indent=2), soft_wrap=True, highlight=False)
        return
    if not listing.webhooks:
        hint("[dim]no webhooks — create one with[/dim] mdk webhooks create --url https://...")
        return
    table = Table(title="webhook subscriptions")
    table.add_column("id", style="bold")
    table.add_column("url", overflow="fold")
    table.add_column("kinds")
    table.add_column("enabled")
    table.add_column("failures")
    table.add_column("secret hint")
    for w in listing.webhooks:
        table.add_row(
            w.id[:12],
            w.url,
            ",".join(w.kind_filter),
            "yes" if w.enabled else "[red]no[/red]",
            str(w.failure_count),
            w.secret_hint,
        )
    stdout.print(table)


@webhooks_app.command("create")
def create_webhook(
    url: str = typer.Option(
        ...,
        "--url",
        "-u",
        help="HTTPS URL to POST events to. http:// is rejected at create time.",
    ),
    kinds: str = typer.Option(
        WILDCARD_KIND,
        "--kind",
        "-k",
        help=(
            "Comma-separated event kinds (e.g. 'run.completed,eval.failed'). "
            "Use '*' to receive every kind. Default: '*'."
        ),
    ),
    disabled: bool = typer.Option(
        False,
        "--disabled",
        help="Create the subscription dormant (won't receive deliveries until enabled).",
    ),
    target: str = typer.Option(None, "--target", "-t", help="Deployment target name."),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """Subscribe a URL to lifecycle events.

    Prints the HMAC signing secret ONCE on stderr (with a save-now
    warning) — copy it immediately, it is never retransmitted. Each
    delivery carries an ``X-MDK-Signature: t=<ts>,v1=<hex>`` header
    (HMAC-SHA256 of ``"<ts>.<raw_body>"`` under the secret); the
    subscriber verifies by recomputing under the captured secret.

    [bold]Examples:[/bold]

      [dim]# Subscribe to every event[/dim]
      $ mdk webhooks create --url https://example.com/hook

      [dim]# Just run terminals + eval failures[/dim]
      $ mdk webhooks create --url https://example.com/hook \\
          -k run.completed,run.failed,eval.failed
    """
    kind_list = [k.strip() for k in kinds.split(",") if k.strip()]
    if not kind_list:
        error("--kind must be a non-empty comma-separated list of event kinds (or '*')")
        raise typer.Exit(code=2)
    suppress = output_format == TableJson.JSON
    created = asyncio.run(
        _create(
            target=target,
            url=url,
            kind_filter=kind_list,
            enabled=not disabled,
            suppress=suppress,
        )
    )
    _emit_created(created, output_format=output_format)


@webhooks_app.command("show")
def show(
    webhook_id: str = typer.Argument(..., help="Webhook id from `mdk webhooks list`."),
    target: str = typer.Option(None, "--target", "-t", help="Deployment target name."),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """Show one subscription's current state (no full secret)."""
    suppress = output_format == TableJson.JSON
    view = asyncio.run(_show(target=target, webhook_id=webhook_id, suppress=suppress))
    _emit_one(view, output_format=output_format)


@webhooks_app.command("delete")
def delete(
    webhook_id: str = typer.Argument(..., help="Webhook id from `mdk webhooks list`."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    target: str = typer.Option(None, "--target", "-t", help="Deployment target name."),
) -> None:
    """Remove a webhook subscription (revokes its signing secret).

    Idempotent: deleting a non-existent subscription is a no-op
    success. Confirms first unless ``--yes`` is set.
    """
    if not yes:
        confirmed = typer.confirm(f"Delete webhook {webhook_id}? This revokes its signing secret.")
        if not confirmed:
            hint("[dim]aborted[/dim]")
            raise typer.Exit(code=1)
    asyncio.run(_delete(target=target, webhook_id=webhook_id))
    stdout.print(f"[green]✓[/green] deleted webhook [bold]{webhook_id}[/bold]")


@webhooks_app.command("enable")
def enable(
    webhook_id: str = typer.Argument(..., help="Webhook id from `mdk webhooks list`."),
    target: str = typer.Option(None, "--target", "-t", help="Deployment target name."),
) -> None:
    """Re-enable a disabled subscription."""
    view = asyncio.run(_set_enabled(target=target, webhook_id=webhook_id, enabled=True))
    stdout.print(
        f"[green]✓[/green] webhook [bold]{view.id}[/bold] enabled "
        f"(kinds={','.join(view.kind_filter)})"
    )


@webhooks_app.command("disable")
def disable(
    webhook_id: str = typer.Argument(..., help="Webhook id from `mdk webhooks list`."),
    target: str = typer.Option(None, "--target", "-t", help="Deployment target name."),
) -> None:
    """Disable a subscription (the worker stops delivering until re-enabled)."""
    view = asyncio.run(_set_enabled(target=target, webhook_id=webhook_id, enabled=False))
    stdout.print(
        f"[yellow]⊘[/yellow] webhook [bold]{view.id}[/bold] disabled "
        f"(kinds={','.join(view.kind_filter)})"
    )


@webhooks_app.command("attempts")
def attempts(
    webhook_id: str = typer.Argument(..., help="Webhook id from `mdk webhooks list`."),
    limit: int = typer.Option(50, "--limit", "-n", help="Max attempts to fetch (1..500)."),
    target: str = typer.Option(None, "--target", "-t", help="Deployment target name."),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """List recent delivery attempts for a subscription (newest-first).

    Each row carries the HTTP status, the truncated response excerpt,
    and the ``error_kind`` (``ok`` / ``http_error`` / ``timeout`` /
    ``connection`` / ``max_retries``). Useful for triaging a flaky
    subscriber.
    """
    suppress = output_format == TableJson.JSON
    listing = asyncio.run(
        _attempts(
            target=target,
            webhook_id=webhook_id,
            limit=limit,
            suppress=suppress,
        )
    )
    if output_format == TableJson.JSON:
        stdout.print(listing.model_dump_json(indent=2), soft_wrap=True, highlight=False)
        return
    if not listing.attempts:
        hint("[dim]no attempts recorded yet[/dim]")
        return
    table = Table(title=f"recent attempts for {webhook_id[:12]}")
    table.add_column("attempted_at")
    table.add_column("status")
    table.add_column("error")
    table.add_column("attempt")
    table.add_column("event", overflow="fold")
    for a in listing.attempts:
        table.add_row(
            a.attempted_at.isoformat(timespec="seconds"),
            str(a.status_code) if a.status_code is not None else "—",
            a.error_kind,
            str(a.attempt_n),
            a.event_id,
        )
    stdout.print(table)


# ---------------------------------------------------------------------------
# Async glue
# ---------------------------------------------------------------------------


async def _list(
    *, target: str | None, include_disabled: bool, suppress: bool = False
) -> WebhookListView:
    client = _build_client(target, suppress=suppress)
    try:
        async with client:
            with spinner("fetching webhooks..."):
                return await client.list_webhooks(include_disabled=include_disabled)
    except MovateClientError as exc:
        error(str(exc), context="list")
        raise typer.Exit(code=exc.status_code // 100) from None


async def _create(
    *,
    target: str | None,
    url: str,
    kind_filter: list[str],
    enabled: bool,
    suppress: bool = False,
) -> WebhookCreatedView:
    client = _build_client(target, suppress=suppress)
    try:
        async with client:
            with spinner("creating webhook..."):
                return await client.create_webhook(
                    url=url, kind_filter=kind_filter, enabled=enabled
                )
    except MovateClientError as exc:
        error(str(exc), context="create")
        raise typer.Exit(code=exc.status_code // 100) from None


async def _show(*, target: str | None, webhook_id: str, suppress: bool = False) -> WebhookView:
    client = _build_client(target, suppress=suppress)
    try:
        async with client:
            with spinner("fetching webhook..."):
                return await client.get_webhook(webhook_id)
    except MovateClientError as exc:
        error(str(exc), context="show")
        raise typer.Exit(code=exc.status_code // 100) from None


async def _delete(*, target: str | None, webhook_id: str) -> None:
    client = _build_client(target, suppress=False)
    try:
        async with client:
            with spinner("deleting webhook..."):
                await client.delete_webhook(webhook_id)
    except MovateClientError as exc:
        error(str(exc), context="delete")
        raise typer.Exit(code=exc.status_code // 100) from None


async def _set_enabled(*, target: str | None, webhook_id: str, enabled: bool) -> WebhookView:
    client = _build_client(target, suppress=False)
    try:
        async with client:
            with spinner("updating webhook..."):
                return await client.set_webhook_enabled(webhook_id, enabled=enabled)
    except MovateClientError as exc:
        error(str(exc), context="update")
        raise typer.Exit(code=exc.status_code // 100) from None


async def _attempts(
    *, target: str | None, webhook_id: str, limit: int, suppress: bool = False
) -> WebhookAttemptListView:
    client = _build_client(target, suppress=suppress)
    try:
        async with client:
            with spinner("fetching attempts..."):
                return await client.list_webhook_attempts(webhook_id, limit=limit)
    except MovateClientError as exc:
        error(str(exc), context="attempts")
        raise typer.Exit(code=exc.status_code // 100) from None


def _build_client(target: str | None, *, suppress: bool = False) -> MovateClient:
    """Resolve target → MovateClient, echoing context unless ``-o json``."""
    try:
        target_name, target_cfg = resolve_target(target or get_global_target())
        token = resolve_bearer_token(target_cfg)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None
    echo_remote_context(target_name, target_cfg, suppress=suppress)
    return MovateClient(base_url=target_cfg.url, api_key=token)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _emit_one(view: WebhookView, *, output_format: TableJson) -> None:
    if output_format == TableJson.JSON:
        stdout.print(view.model_dump_json(indent=2), soft_wrap=True, highlight=False)
        return
    stdout.print(f"[bold]{view.id}[/bold]")
    stdout.print(f"  url:          {view.url}")
    stdout.print(f"  kinds:        {','.join(view.kind_filter)}")
    stdout.print(f"  enabled:      {'yes' if view.enabled else 'no'}")
    stdout.print(f"  failures:     {view.failure_count}")
    stdout.print(f"  secret hint:  {view.secret_hint}")
    stdout.print(f"  created_at:   {view.created_at.isoformat(timespec='seconds')}")


def _emit_created(view: WebhookCreatedView, *, output_format: TableJson) -> None:
    """Render the create response. Secret goes to stderr with a save-now
    warning so a scripted ``> file`` redirect of stdout doesn't lose it."""
    if output_format == TableJson.JSON:
        stdout.print(view.model_dump_json(indent=2), soft_wrap=True, highlight=False)
        return
    stdout.print(
        f"[green]✓[/green] webhook [bold]{view.id}[/bold] created "
        f"(kinds={','.join(view.kind_filter)})"
    )
    stdout.print(f"[dim]url:[/dim] {view.url}")
    err.print()
    err.print(
        "[bold yellow]save the webhook secret now — never shown again[/bold yellow]\n"
        f"  secret: {view.secret}"
    )
    err.print(
        "\n[dim]Each delivery POST includes header X-MDK-Signature: t=<ts>,v1=<hmac_hex>[/dim]"
    )
    err.print(
        '[dim]where <hmac_hex> is HMAC-SHA256 of f"{ts}.{raw_body}" keyed by the secret.[/dim]'
    )


__all__ = ["webhooks_app"]
