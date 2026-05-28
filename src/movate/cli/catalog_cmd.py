"""``mdk catalog`` — CLI parity layer for the catalog API endpoints.

The catalog is the deployed runtime's library of REUSABLE agents an
operator can clone into a project (cf. ``mdk project add-agent
--from-catalog <slug>``). Per-endpoint PRs landing tonight add the
matching server routes; this module is the comprehensive polish
layer over the top — same shape as :mod:`movate.cli.project_cmd`:

* ``--json`` on every read command (table → JSON parity with
  ``mdk fleet`` / ``mdk runs list``).
* Next-step hints on every write.
* ``--target`` plumbed through to the resolved runtime, falling
  back to the top-level ``-t`` / ``MDK_TARGET`` env var, then the
  active config target.
* Pre-call :func:`echo_remote_context` echo so 401/403 explains
  itself (suppressed under ``--json``).

Cross-references (rendered in ``--help``):

* ``mdk catalog list --help`` → use ``mdk project add-agent
  --from-catalog <slug>`` to clone.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._console import (
    hint,
    success,
)
from movate.cli._output import TableJson
from movate.cli.project_cmd import _request, _resolve

stdout = Console()


catalog_app = typer.Typer(
    name="catalog",
    help=(
        "Browse + publish reusable agents on a deployed movate runtime.\n\n"
        "The catalog is the runtime's library of agents you can CLONE into a "
        "project (see [bold]mdk project add-agent --from-catalog <slug>[/bold]). "
        "Pick the runtime with [bold]--target <env>[/bold].\n\n"
        "[bold]Read[/bold] commands support [bold]--json[/bold] for scripting; "
        "[bold]write[/bold] commands print a next-step hint."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


# ---------------------------------------------------------------------------
# Read commands
# ---------------------------------------------------------------------------


@catalog_app.command("list")
def list_catalog(
    target: str | None = typer.Option(
        None,
        "--target",
        "-t",
        help="Deployment target name. Defaults to top-level -t / MDK_TARGET / active target.",
    ),
    tag: str | None = typer.Option(
        None,
        "--tag",
        help="Filter to entries carrying this tag (e.g. [bold]--tag rag[/bold]).",
    ),
    output_format: TableJson = typer.Option(
        TableJson.TABLE,
        "--output",
        "-o",
        case_sensitive=False,
    ),
    json_flag: bool = typer.Option(
        False,
        "--json",
        help="Shortcut for [bold]-o json[/bold] — emit a machine-readable array.",
    ),
) -> None:
    """List reusable agents in the runtime's catalog.

    Use [bold]mdk project add-agent --from-catalog <slug>[/bold] to
    clone one of these into a project.

    [bold]Examples:[/bold]

      [dim]# Human-readable table[/dim]
      $ mdk catalog list --target dev

      [dim]# Filter by tag + pipe to jq for scripted use[/dim]
      $ mdk catalog list --target dev --tag rag --json | jq '.[] | .slug'

      [dim]# Drill into one entry[/dim]
      $ mdk catalog show faq-bot --target dev

    [bold]See also:[/bold]
      [bold]mdk project add-agent --from-catalog <slug>[/bold] — clone a
        catalog entry into a project.
      [bold]mdk catalog publish <agent>[/bold] — push your own agent
        into the catalog so others can clone it.
    """
    as_json = json_flag or output_format == TableJson.JSON
    target_name, target_cfg, token = _resolve(target)
    path = "/api/v1/catalog"
    if tag:
        path += f"?tag={tag}"
    body = asyncio.run(
        _request(
            target_name=target_name,
            target_cfg=target_cfg,
            token=token,
            method="GET",
            path=path,
            suppress_echo=as_json,
            action_label="list catalog",
        )
    )
    items = body.get("entries", body.get("items", body.get("catalog", [])))
    if not isinstance(items, list):
        items = []

    if as_json:
        stdout.print_json(json.dumps(items))
        return

    if not items:
        hint(f"[dim]no catalog entries on {target_name}.[/dim]")
        hint(
            f"[dim]Tip: [bold]mdk catalog publish <agent> --target "
            f"{target_name}[/bold] to add one.[/dim]"
        )
        return

    table = Table(title=f"catalog on {target_name}")
    table.add_column("Slug", style="bold", no_wrap=True)
    table.add_column("Description", overflow="fold")
    table.add_column("Tags", no_wrap=True)
    table.add_column("Version", no_wrap=True)
    for row in items:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug", row.get("name", "?")))
        desc = str(row.get("description", "") or "[dim]—[/dim]")
        tags = row.get("tags") or []
        tags_cell = ", ".join(str(t) for t in tags) if isinstance(tags, list) else ""
        version = str(row.get("version", "") or "[dim]—[/dim]")
        table.add_row(slug, desc, tags_cell or "[dim]—[/dim]", version)
    stdout.print(table)
    # Cross-reference hint — the next obvious action once they've browsed.
    hint(
        f"\n[dim]Clone one into a project: "
        f"[bold]mdk project add-agent <project> <name> --from-catalog <slug> "
        f"--target {target_name}[/bold].[/dim]"
    )


@catalog_app.command("show")
def show_catalog_entry(
    slug: str = typer.Argument(..., help="Catalog entry slug (from `mdk catalog list`)."),
    target: str | None = typer.Option(
        None,
        "--target",
        "-t",
        help="Deployment target name. Defaults to top-level -t / MDK_TARGET / active target.",
    ),
    output_format: TableJson = typer.Option(
        TableJson.TABLE,
        "--output",
        "-o",
        case_sensitive=False,
    ),
    json_flag: bool = typer.Option(
        False,
        "--json",
        help="Shortcut for [bold]-o json[/bold].",
    ),
) -> None:
    """Show one catalog entry's full record.

    [bold]Example:[/bold]

      $ mdk catalog show faq-bot --target dev
    """
    as_json = json_flag or output_format == TableJson.JSON
    target_name, target_cfg, token = _resolve(target)
    body = asyncio.run(
        _request(
            target_name=target_name,
            target_cfg=target_cfg,
            token=token,
            method="GET",
            path=f"/api/v1/catalog/{slug}",
            suppress_echo=as_json,
            action_label="show catalog",
        )
    )

    if as_json:
        stdout.print_json(json.dumps(body))
        return

    table = Table(title=f"catalog/{body.get('slug', slug)}", show_header=False)
    table.add_column("field", style="dim")
    table.add_column("value")
    for key, value in body.items():
        if isinstance(value, list | dict):
            table.add_row(str(key), json.dumps(value, indent=2))
        else:
            table.add_row(str(key), "" if value is None else str(value))
    stdout.print(table)


# ---------------------------------------------------------------------------
# Write commands
# ---------------------------------------------------------------------------


@catalog_app.command("publish")
def publish_to_catalog(
    agent: str = typer.Argument(..., help="Local agent name to publish."),
    slug: str | None = typer.Option(
        None,
        "--slug",
        help="Catalog slug (defaults to the agent name).",
    ),
    description: str | None = typer.Option(
        None, "--description", help="One-line description shown in `catalog list`."
    ),
    tag: list[str] = typer.Option(
        [],
        "--tag",
        help="Repeatable: add a tag to the catalog entry (e.g. [bold]--tag rag --tag faq[/bold]).",
    ),
    target: str | None = typer.Option(
        None,
        "--target",
        "-t",
        help="Deployment target name. Defaults to top-level -t / MDK_TARGET / active target.",
    ),
) -> None:
    """Publish a local agent to the runtime's catalog.

    Once published, [bold]mdk catalog list[/bold] surfaces the new
    entry and other operators can clone it with [bold]mdk project
    add-agent --from-catalog <slug>[/bold].

    [bold]Example:[/bold]

      $ mdk catalog publish my-faq-bot --slug faq-bot \\
          --description "FAQ over a docs site" --tag rag --target dev
    """
    target_name, target_cfg, token = _resolve(target)
    body: dict[str, Any] = {"agent": agent, "slug": slug or agent}
    if description:
        body["description"] = description
    if tag:
        body["tags"] = list(tag)

    result = asyncio.run(
        _request(
            target_name=target_name,
            target_cfg=target_cfg,
            token=token,
            method="POST",
            path="/api/v1/catalog",
            json_body=body,
            action_label="publish to catalog",
        )
    )
    published_slug = result.get("slug", body["slug"])
    success(
        f"published [bold]{agent}[/bold] as catalog slug "
        f"[bold]{published_slug}[/bold] on [bold]{target_name}[/bold]"
    )
    hint("\n[bold]Next steps:[/bold]")
    hint(
        f"  [dim]$[/dim] [bold]mdk catalog show {published_slug} "
        f"--target {target_name}[/bold]   [dim]# confirm the entry[/dim]"
    )
    hint(
        f"  [dim]$[/dim] [bold]mdk project add-agent <project> <name> "
        f"--from-catalog {published_slug} --target {target_name}[/bold]"
        f"   [dim]# clone into a project[/dim]"
    )


@catalog_app.command("delete")
def delete_catalog_entry(
    slug: str = typer.Argument(..., help="Catalog entry slug to delete."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the interactive confirm (CI-friendly)."
    ),
    target: str | None = typer.Option(
        None,
        "--target",
        "-t",
        help="Deployment target name.",
    ),
) -> None:
    """Delete a catalog entry on the target runtime (DESTRUCTIVE).

    Already-cloned projects are NOT affected — this just removes the
    catalog source so it can't be cloned again.

    [bold]Example:[/bold]

      $ mdk catalog delete stale-bot --target dev --yes
    """
    from movate.cli._console import confirm_destructive  # noqa: PLC0415

    confirm_destructive(
        f"Delete catalog entry {slug!r}? Existing clones in projects are not affected.",
        yes=yes,
    )
    target_name, target_cfg, token = _resolve(target)
    asyncio.run(
        _request(
            target_name=target_name,
            target_cfg=target_cfg,
            token=token,
            method="DELETE",
            path=f"/api/v1/catalog/{slug}",
            action_label="delete catalog",
        )
    )
    success(f"deleted catalog entry [bold]{slug}[/bold] on [bold]{target_name}[/bold]")
    hint(
        f"\n[dim]Next: [bold]mdk catalog list --target {target_name}[/bold] "
        f"to confirm the remaining entries.[/dim]"
    )


__all__ = ["catalog_app"]
