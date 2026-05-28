"""``mdk project`` — CLI parity layer for the project API endpoints.

Per-endpoint PRs landing tonight add the matching server routes (and
the per-endpoint command files they each carry); this module is the
COMPREHENSIVE polish layer over the top:

* Every read command supports ``--json`` for machine output — same
  shape conventions ``mdk fleet`` / ``mdk runs list`` already use.
* Every write command prints a "next-step hint" so the operator
  always knows what to type next — same shape ``mdk init`` /
  ``mdk add`` already use.
* Every command respects ``--target`` (falling back to the
  top-level ``-t`` / ``MDK_TARGET`` env var, then to the active
  config target via :func:`movate.core.user_config.resolve_target`).
* Before any remote call, :func:`echo_remote_context` prints the
  resolved target + credential source to stderr so 401/403 is
  self-diagnosing (suppressed under ``--json``).

The actual per-endpoint CLI commands ship inside each endpoint PR;
this file is the front-door subapp that holds them together and
guarantees the cross-command UX is consistent. Each command here is
a thin wrapper over the JSON over HTTP API — no business logic
lives in the CLI layer (ADR 026: control plane ⊥ execution plane).

Cross-references (rendered in ``--help``):

* ``mdk project add-agent --help`` → see ``mdk catalog list`` to
  browse reusable agents.
* The catalog-side cross-reference lives in :mod:`movate.cli.catalog_cmd`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._console import (
    echo_remote_context,
    error,
    get_global_target,
    hint,
    success,
)
from movate.cli._output import TableJson
from movate.core.user_config import (
    TargetConfig,
    UserConfigError,
    resolve_bearer_token,
    resolve_target,
)

stdout = Console()


project_app = typer.Typer(
    name="project",
    help=(
        "Manage projects on a deployed movate runtime.\n\n"
        "All subcommands talk to a runtime over HTTP — pick which one with "
        "[bold]--target <env>[/bold] (or set an active target via "
        "[bold]mdk config use-target <env>[/bold]).\n\n"
        "[bold]Read[/bold] commands support [bold]--json[/bold] for scripting "
        "([dim]mdk project list --json | jq ...[/dim]). [bold]Write[/bold] "
        "commands print a next-step hint so you always know what to type next."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


# ---------------------------------------------------------------------------
# Shared HTTP plumbing (parity with submit / runs / batch / jobs)
# ---------------------------------------------------------------------------


def _resolve(target: str | None) -> tuple[str, TargetConfig, str]:
    """Same target-resolution shape every other remote subcommand uses.

    Returns ``(target_name, target_cfg, bearer_token)``. Exits cleanly on
    a config / unset-env error (code 2) rather than letting an opaque
    KeyError bubble up.
    """
    try:
        name, cfg = resolve_target(target or get_global_target())
        token = resolve_bearer_token(cfg)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None
    return name, cfg, token


async def _request(
    *,
    target_name: str,
    target_cfg: TargetConfig,
    token: str,
    method: str,
    path: str,
    json_body: dict[str, Any] | None = None,
    suppress_echo: bool = False,
    action_label: str | None = None,
) -> dict[str, Any]:
    """Make ONE authenticated request to the runtime; return the JSON body.

    Routes through ``httpx.AsyncClient`` directly (rather than
    :class:`movate.core.client.MovateClient`) because the project +
    catalog endpoints land in tonight's PRs and aren't on
    :class:`MovateClient` yet. The next PR can swap this for typed
    methods once the per-endpoint PRs ship their client additions —
    behavior here is intentionally narrow + Protocol-friendly.

    Echoes the resolved remote target + credential source via
    :func:`echo_remote_context` BEFORE the call so a 401/403 explains
    itself. ``suppress_echo=True`` for ``--json`` callers keeps stderr
    clean for scripts.
    """
    import httpx  # noqa: PLC0415 — lazy keeps CLI import time down

    echo_remote_context(target_name, target_cfg, action=action_label, suppress=suppress_echo)
    base_url = target_cfg.url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.request(
            method,
            f"{base_url}{path}",
            json=json_body,
            headers=headers,
        )
    if not r.is_success:
        # Match MovateClient's error envelope shape; fall back gracefully
        # for non-movate 5xx (e.g. ingress timeouts).
        msg = f"HTTP {r.status_code}"
        try:
            payload = r.json()
            if isinstance(payload, dict):
                detail = payload.get("detail")
                if isinstance(detail, dict):
                    err = detail.get("error", {})
                    if isinstance(err, dict):
                        msg = err.get("message", msg)
        except ValueError:
            pass
        error(msg, context=f"{method.lower()} {path}")
        raise typer.Exit(code=r.status_code // 100)
    if not r.content:
        return {}
    try:
        body = r.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {"items": body}


# ---------------------------------------------------------------------------
# Read commands
# ---------------------------------------------------------------------------


@project_app.command("list")
def list_projects(
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
        help="Output shape: 'table' for humans, 'json' for piping into jq.",
    ),
    json_flag: bool = typer.Option(
        False,
        "--json",
        help="Shortcut for [bold]-o json[/bold] — emit a machine-readable JSON array.",
    ),
) -> None:
    """List projects registered on the target runtime.

    [bold]Examples:[/bold]

      [dim]# Human-readable table against the active target[/dim]
      $ mdk project list

      [dim]# Pipe to jq for scripted use[/dim]
      $ mdk project list --target prod --json | jq '.[] | .name'

      [dim]# Look up a single project with `show` once you've got the name[/dim]
      $ mdk project show my-project --target prod

    [bold]See also:[/bold]
      [bold]mdk project show <name>[/bold] — drill into one project.
      [bold]mdk catalog list[/bold] — browse reusable agents you can add.
    """
    as_json = json_flag or output_format == TableJson.JSON
    target_name, target_cfg, token = _resolve(target)
    body = asyncio.run(
        _request(
            target_name=target_name,
            target_cfg=target_cfg,
            token=token,
            method="GET",
            path="/api/v1/projects",
            suppress_echo=as_json,
            action_label="list projects",
        )
    )
    items = body.get("projects", body.get("items", []))
    if not isinstance(items, list):
        items = []

    if as_json:
        stdout.print_json(json.dumps(items))
        return

    if not items:
        hint(f"[dim]no projects on {target_name}.[/dim]")
        hint(
            f"[dim]Tip: [bold]mdk project create <name> --target {target_name}[/bold] "
            f"to register one.[/dim]"
        )
        return

    table = Table(title=f"projects on {target_name}")
    table.add_column("Name", style="bold", no_wrap=True)
    table.add_column("Agents", no_wrap=True)
    table.add_column("Updated", overflow="fold")
    for row in items:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name", "?"))
        agent_count = row.get("agent_count")
        agents_cell = str(agent_count) if isinstance(agent_count, int) else "[dim]—[/dim]"
        updated = str(row.get("updated_at", row.get("created_at", "")))
        table.add_row(name, agents_cell, updated or "[dim]—[/dim]")
    stdout.print(table)


@project_app.command("show")
def show_project(
    name: str = typer.Argument(..., help="Project name (as listed by `mdk project list`)."),
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
    """Show one project's full record.

    [bold]Example:[/bold]

      $ mdk project show my-faq-bot --target dev
    """
    as_json = json_flag or output_format == TableJson.JSON
    target_name, target_cfg, token = _resolve(target)
    body = asyncio.run(
        _request(
            target_name=target_name,
            target_cfg=target_cfg,
            token=token,
            method="GET",
            path=f"/api/v1/projects/{name}",
            suppress_echo=as_json,
            action_label="show project",
        )
    )

    if as_json:
        stdout.print_json(json.dumps(body))
        return

    table = Table(title=f"project {body.get('name', name)}", show_header=False)
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


@project_app.command("add-agent")
def add_agent(
    project: str = typer.Argument(..., help="Project name to add the agent into."),
    name: str = typer.Argument(..., help="Name for the new agent."),
    from_catalog: str | None = typer.Option(
        None,
        "--from-catalog",
        help=(
            "Clone an agent from the catalog by [bold]<slug>[/bold] "
            "(see [bold]mdk catalog list[/bold] to browse reusable agents)."
        ),
    ),
    from_llm: str | None = typer.Option(
        None,
        "--from-llm",
        help=(
            "Natural-language description — the runtime composes an "
            "agent bundle from it (cloud-side scaffold). Pair with "
            "[bold]mdk init <name> '<desc>' --target <env>[/bold] when "
            "you want the bundle written back locally too."
        ),
    ),
    target: str | None = typer.Option(
        None,
        "--target",
        "-t",
        help="Deployment target name. Defaults to top-level -t / MDK_TARGET / active target.",
    ),
) -> None:
    """Add a new agent to a project on the target runtime.

    Two source modes (mutually exclusive):

    * [bold]--from-catalog <slug>[/bold] — clone an existing reusable
      agent from the catalog. See [bold]mdk catalog list[/bold] to
      browse what's available.
    * [bold]--from-llm "<description>"[/bold] — describe the agent in
      natural language; the cloud-side bundle composer generates it.

    [bold]Examples:[/bold]

      [dim]# Clone a reusable FAQ bot from the catalog[/dim]
      $ mdk project add-agent my-proj billing-faq \\
          --from-catalog faq-bot --target dev

      [dim]# LLM-generate an agent into the project[/dim]
      $ mdk project add-agent my-proj triager \\
          --from-llm "ticket triage by priority" --target dev

    [bold]See also:[/bold]
      [bold]mdk catalog list[/bold] — browse reusable agents to clone.
      [bold]mdk init <name> '<desc>' --target <env>[/bold] — same
        cloud-side LLM scaffold but ALSO writes the bundle locally.
    """
    if from_catalog and from_llm:
        error("pick one of [bold]--from-catalog[/bold] or [bold]--from-llm[/bold], not both.")
        raise typer.Exit(code=2)
    if not from_catalog and not from_llm:
        error(
            "one of [bold]--from-catalog <slug>[/bold] or "
            "[bold]--from-llm '<description>'[/bold] is required."
        )
        raise typer.Exit(code=2)

    target_name, target_cfg, token = _resolve(target)
    body: dict[str, Any] = {"name": name}
    if from_catalog:
        body["source"] = "catalog"
        body["catalog_slug"] = from_catalog
    else:
        body["source"] = "llm"
        body["description"] = from_llm

    result = asyncio.run(
        _request(
            target_name=target_name,
            target_cfg=target_cfg,
            token=token,
            method="POST",
            path=f"/api/v1/projects/{project}/agents",
            json_body=body,
            action_label="add agent",
        )
    )
    success(
        f"added [bold]{name}[/bold] to project [bold]{project}[/bold] on [bold]{target_name}[/bold]"
    )
    agent_id = result.get("agent_id", result.get("id"))
    # Next-step hint: what to type now. Mirrors the panel shape in
    # `mdk add` / `mdk init` — one or two copy-pasteable commands the
    # operator most likely wants next.
    hint("\n[bold]Next steps:[/bold]")
    hint(
        f"  [dim]$[/dim] [bold]mdk project show {project} --target {target_name}[/bold]"
        "   [dim]# confirm the agent landed[/dim]"
    )
    hint(
        f"  [dim]$[/dim] [bold]mdk run {name} --target {target_name} '{{...}}'[/bold]"
        "   [dim]# try one live[/dim]"
    )
    if agent_id:
        hint(f"  [dim]agent_id: {agent_id}[/dim]")


@project_app.command("delete")
def delete_project(
    name: str = typer.Argument(..., help="Project name to delete."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the interactive confirm (CI-friendly)."
    ),
    target: str | None = typer.Option(
        None,
        "--target",
        "-t",
        help="Deployment target name. Defaults to top-level -t / MDK_TARGET / active target.",
    ),
) -> None:
    """Delete a project on the target runtime (DESTRUCTIVE).

    Requires [bold]--yes[/bold] in non-interactive (CI) contexts.

    [bold]Example:[/bold]

      $ mdk project delete stale-experiment --target dev --yes
    """
    from movate.cli._console import confirm_destructive  # noqa: PLC0415

    confirm_destructive(
        f"Delete project {name!r} on the runtime? This cannot be undone.",
        yes=yes,
    )
    target_name, target_cfg, token = _resolve(target)
    asyncio.run(
        _request(
            target_name=target_name,
            target_cfg=target_cfg,
            token=token,
            method="DELETE",
            path=f"/api/v1/projects/{name}",
            action_label="delete project",
        )
    )
    success(f"deleted project [bold]{name}[/bold] on [bold]{target_name}[/bold]")
    hint(
        f"\n[dim]Next: [bold]mdk project list --target {target_name}[/bold] "
        f"to confirm the remaining projects.[/dim]"
    )


__all__ = ["project_app"]
