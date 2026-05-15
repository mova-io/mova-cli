"""``mdk import openapi <spec>`` — scaffold one skill per OpenAPI operation.

Companion to ``mdk import lyzr`` / ``mdk import json``. Where those
two import a single agent definition, ``import openapi`` lifts an
entire API surface (one skill per operation) so an agent can reach
into the API by referencing the generated skills.

  $ mdk import openapi petstore.yaml
  $ mdk import openapi stripe.json --target ./skills/stripe/ --prefix stripe-
  $ mdk import openapi spec.json --dry-run            # preview, no writes
  $ mdk import openapi spec.json --only getPetById    # one operation

What gets generated per operation:

* ``skills/<operation-id>/skill.yaml`` — HTTP-kind skill with the
  full URL, method, and auth placeholder (``bearer-from-env:OPENAPI_TOKEN``
  — operators rename the env var after import).
* Path / query / header / body parameters collapse into the skill's
  inline input schema. Path params get ``{{ input.<name> }}``
  interpolation in the URL.
* Response 2xx schema (or ``object`` fallback) → output schema.
* ``side_effects: read-only`` for GET/HEAD/OPTIONS; ``mutates-state``
  otherwise.

What we don't import (left for operator follow-up):

* OAuth flows / scopes — edit the ``auth`` block after import.
* Recursive schema refs — collapses to ``object``; expand manually
  if you need strict typing.
* Server selection beyond ``servers[0].url`` — pass ``--server`` to
  override.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from movate.cli.import_lyzr import import_app
from movate.importers import OpenAPIParseError, parse_openapi, skill_yaml_for

console = Console()
err_console = Console(stderr=True)

# Cap the conflict list shown in error messages — operators don't need
# all 500 names dumped to stderr when an entire import collides.
_MAX_CONFLICTS_SHOWN = 3


@import_app.command("openapi")
def import_openapi(
    spec: Path = typer.Argument(
        ...,
        help="Path to the OpenAPI 3.x spec (JSON or YAML).",
        metavar="SPEC",
    ),
    target: Path = typer.Option(
        Path("./skills"),
        "--target",
        "-t",
        help=(
            "Parent directory for the generated skill dirs "
            "(default: ./skills/). One skill subdirectory per operation."
        ),
    ),
    prefix: str = typer.Option(
        "",
        "--prefix",
        help=(
            "Prefix prepended to every generated skill name. "
            "Useful for multi-spec imports (e.g. [dim]--prefix stripe-[/dim] so "
            "operations from stripe.json don't collide with shopify.json)."
        ),
    ),
    server: str = typer.Option(
        "",
        "--server",
        help=(
            "Override the server URL. Default uses [bold]spec.servers[0].url[/bold]; "
            "pass [bold]--server[/bold] to point at a different environment "
            "(staging / prod) without editing the spec."
        ),
    ),
    only: list[str] = typer.Option(
        None,
        "--only",
        help=(
            "Generate only the named operation(s). Repeatable. "
            "Useful when you want one specific skill from a 500-op spec."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite existing skill directories under --target.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print what would be generated without writing anything.",
    ),
) -> None:
    """Import an OpenAPI spec as one skill per operation.

    [bold]Examples:[/bold]

      [dim]$ mdk import openapi petstore.yaml[/dim]
      [dim]$ mdk import openapi stripe.json --prefix stripe- --target ./skills/stripe[/dim]
      [dim]$ mdk import openapi spec.json --only getPetById --only listPets[/dim]
      [dim]$ mdk import openapi spec.json --dry-run[/dim]

    After import, edit each ``skill.yaml`` to:
      • Replace the ``auth`` placeholder with your real env var name
      • Tighten the input schema if you have stricter types than the spec
      • Add a useful ``description`` if the operation's summary was vague
    """
    if not spec.is_file():
        err_console.print(f"[red]✗[/red] spec file not found: {spec}")
        raise typer.Exit(code=2)

    try:
        text = spec.read_text()
    except OSError as exc:
        err_console.print(f"[red]✗[/red] could not read {spec}: {exc}")
        raise typer.Exit(code=2) from None

    # Parse the spec.
    try:
        operations = parse_openapi(text)
    except OpenAPIParseError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    # Pull server URL from the spec unless --server was passed.
    server_url = server or _extract_server_url(text)

    # Apply --only filter.
    if only:
        wanted = set(only)
        # Match against unprefixed operation_id (operators don't pre-prefix
        # when typing --only) so the filter is intuitive.
        operations = [op for op in operations if op.operation_id in wanted]
        if not operations:
            err_console.print(
                f"[red]✗[/red] none of {sorted(wanted)} matched. "
                "Run [bold]mdk import openapi <spec> --dry-run[/bold] to see all operations."
            )
            raise typer.Exit(code=2)

    # Build the plan: list of (skill_name, skill_dir, skill.yaml-dict).
    target_root = target.resolve()
    plan: list[tuple[str, Path, dict]] = []
    for op in operations:
        skill_name = f"{prefix}{op.operation_id}" if prefix else op.operation_id
        skill_dir = target_root / skill_name
        skill_doc = skill_yaml_for(op, server_url=server_url)
        # Honor the prefix in the embedded name field too.
        skill_doc["name"] = skill_name
        plan.append((skill_name, skill_dir, skill_doc))

    if dry_run:
        _render_dry_run(plan, server_url)
        return

    # Refuse overwrite without --force.
    if not force:
        conflicts = [name for name, d, _ in plan if d.exists()]
        if conflicts:
            shown = conflicts[:_MAX_CONFLICTS_SHOWN]
            extra = len(conflicts) - _MAX_CONFLICTS_SHOWN
            overflow = f" (+{extra} more)" if extra > 0 else ""
            err_console.print(
                f"[red]✗[/red] {len(conflicts)} skill dir(s) already exist "
                f"under {target_root}: {', '.join(shown)}{overflow}. "
                "Re-run with [bold]--force[/bold] to overwrite."
            )
            raise typer.Exit(code=2)

    written: list[tuple[str, Path]] = []
    for name, skill_dir, doc in plan:
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_yaml = skill_dir / "skill.yaml"
        skill_yaml.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100))
        written.append((name, skill_dir))

    _render_summary(written, target_root, server_url)


# ---------------------------------------------------------------------------
# Helpers (formatting + small extraction)
# ---------------------------------------------------------------------------


def _extract_server_url(text: str) -> str:
    """Pull ``servers[0].url`` out of the spec text without re-parsing semantically.

    We already validated the spec parses via :func:`parse_openapi`; this
    second lightweight load is just to fish out the server. Cheap and
    keeps :func:`parse_openapi` free of CLI-only concerns (the operation
    list shouldn't carry CLI-only context like "which server URL did we
    pick").
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            return ""
    if not isinstance(data, dict):
        return ""
    servers = data.get("servers") or []
    if not isinstance(servers, list) or not servers:
        return ""
    first = servers[0]
    if not isinstance(first, dict):
        return ""
    return str(first.get("url") or "")


def _render_dry_run(plan: list[tuple[str, Path, dict]], server_url: str) -> None:
    """Render the would-be skills as a Rich table."""
    table = Table(
        title=(
            f"mdk import openapi — dry-run ({len(plan)} operation(s))"
            + (f"  [dim]→ {server_url}[/dim]" if server_url else "")
        ),
        title_style="bold",
        show_lines=False,
    )
    table.add_column("Skill", style="cyan", no_wrap=True)
    table.add_column("Method", no_wrap=True)
    table.add_column("Path", style="dim", no_wrap=True)
    table.add_column("Inputs", justify="right", style="dim")
    table.add_column("Side effects", style="dim", no_wrap=True)

    for name, _dir, doc in plan:
        method = doc["implementation"]["method"]
        url = doc["implementation"]["entry"]
        # Trim the server URL for display so the path stays readable.
        display_path = url[len(server_url) :] if server_url and url.startswith(server_url) else url
        n_inputs = len(doc["schema"]["input"])
        side = doc["side_effects"]
        method_color = "green" if side == "read-only" else "yellow"
        table.add_row(
            name,
            f"[{method_color}]{method}[/{method_color}]",
            display_path,
            str(n_inputs),
            side,
        )
    console.print(table)
    console.print(
        "\n[yellow]⚠ dry-run — no files written.[/yellow] "
        "Re-run without [bold]--dry-run[/bold] to scaffold."
    )


def _render_summary(written: list[tuple[str, Path]], target_root: Path, server_url: str) -> None:
    """Render the success panel + edit-this-next hint."""
    body_lines = [
        f"[bold]Scaffolded:[/bold] {len(written)} skill(s)",
        f"[bold]Target:[/bold]     [cyan]{target_root}[/cyan]",
    ]
    if server_url:
        body_lines.append(f"[bold]Server:[/bold]     [cyan]{server_url}[/cyan]")
    body_lines.append("")
    body_lines.append("[bold]Next steps:[/bold]")
    body_lines.append(
        "  • Edit each [cyan]skill.yaml[/cyan] [dim]auth[/dim] "
        "block — replace [cyan]OPENAPI_TOKEN[/cyan] with your real env var."
    )
    body_lines.append(
        "  • [cyan]mdk secrets set OPENAPI_TOKEN[/cyan]   [dim]# or your real env var[/dim]"
    )
    body_lines.append(
        "  • [cyan]mdk skills list[/cyan]                 [dim]# verify all registered[/dim]"
    )
    body_lines.append("  • Reference the skills in your agent.yaml's [cyan]skills[/cyan] block.")
    console.print(
        Panel(
            "\n".join(body_lines),
            title="[green]✓[/green] OpenAPI import complete",
            title_align="left",
            border_style="green",
        )
    )
