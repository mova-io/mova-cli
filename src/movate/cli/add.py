"""``movate add <name> --template <role>`` — add a role-based agent to a project.

Companion to :mod:`movate.cli.init`. Where ``mdk init`` scaffolds a
single agent directory at the chosen target path, ``mdk add`` is
project-aware: it finds the surrounding ``movate.yaml`` (or accepts
``--project <path>``) and lands the new agent under that project's
``agents/<name>/`` subdirectory.

Templates resolve via :func:`movate.templates.get_template_path`,
which checks the role registry (:data:`movate.templates.ROLE_TEMPLATES`)
first and falls back to the shape registry. So ``mdk add foo
--template support-triage`` picks the role; ``mdk add foo --template
faq`` still works against the legacy shape.

Three discovery modes:

* ``mdk add --list-roles`` — pretty-print the role catalog (table)
* ``mdk add --list-roles --json`` — machine-readable; powers the
  Mova iO wizard's "Choose a template" dropdown
* ``mdk add --describe <role>`` — preview a role before scaffolding
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table

from movate.templates import (
    get_template_path,
    list_roles,
    list_templates,
)

console = Console()


def _find_project_root(start: Path) -> Path | None:
    """Walk up from ``start`` looking for a ``movate.yaml``.

    Returns the directory containing it, or ``None`` if no
    ``movate.yaml`` exists in the path from ``start`` up to the
    filesystem root. ``mdk add`` uses this to figure out which
    project the new agent belongs to when ``--project`` isn't
    explicitly given.
    """
    current = start.resolve()
    while True:
        if (current / "movate.yaml").is_file():
            return current
        if current.parent == current:
            # Hit the filesystem root without finding a config.
            return None
        current = current.parent


def _read_role_metadata(template_path: Path) -> dict[str, str]:
    """Pull display metadata from a role template's ``agent.yaml``.

    Used by ``--list-roles`` + ``--describe`` to render the catalog
    without forcing a full ``load_agent`` (which would also need
    the schemas + prompt to validate). Cheap dict lookup.

    Returns ``{}`` on any failure so the catalog command stays
    permissive — if one role's YAML is malformed we still list the
    others.
    """
    yaml_path = template_path / "agent.yaml"
    if not yaml_path.is_file():
        return {}
    try:
        raw = yaml.safe_load(yaml_path.read_text())
    except yaml.YAMLError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        "description": str(raw.get("description") or ""),
        "role": str(raw.get("role") or ""),
        "persona": str(raw.get("persona") or "").strip(),
        "tags": ", ".join(raw.get("tags") or []) if isinstance(raw.get("tags"), list) else "",
    }


def _resolve_project_root(project: Path | None) -> Path:
    """Resolve the project root from ``--project`` or walk-up discovery.

    Pulled out of the ``add`` function so the main command stays under
    Ruff's branch-count limit (PLR0912) and so the resolution policy
    (explicit flag wins; otherwise walk-up; otherwise cwd with a
    warning) reads as one cohesive unit instead of inline branches.

    Prints user-facing status lines as a side effect — same console
    output the inline version produced. Exits via :class:`typer.Exit`
    on a non-existent ``--project`` path.
    """
    if project is not None:
        project_root = project.resolve()
        if not project_root.is_dir():
            console.print(f"[red]error:[/red] --project path is not a directory: {project_root}")
            raise typer.Exit(code=2)
        return project_root

    found = _find_project_root(Path.cwd())
    if found is not None:
        console.print(f"[dim]using project root [bold]{found}[/bold] (found movate.yaml)[/dim]")
        return found

    project_root = Path.cwd().resolve()
    console.print(
        f"[yellow]⚠[/yellow] no [bold]movate.yaml[/bold] found in this "
        f"directory or any parent; using cwd: [bold]{project_root}[/bold]"
    )
    return project_root


def _prepare_destination(agents_dir: Path, name: str, *, force: bool) -> Path:
    """Compute the agent's target dir under ``agents/``, honoring ``--force``.

    Returns the (now-empty) destination path. Exits via
    :class:`typer.Exit` if the destination exists and ``--force`` is
    not set — the user would otherwise lose edits in place.
    """
    agents_dir.mkdir(parents=True, exist_ok=True)
    dest = agents_dir / name
    if dest.exists() and not force:
        console.print(f"[red]error:[/red] {dest} already exists (use --force to overwrite)")
        raise typer.Exit(code=2)
    if dest.exists() and force:
        shutil.rmtree(dest)
    return dest


def _list_roles_table() -> None:
    """Render the role catalog as a Rich table."""
    table = Table(
        title="MDK role templates",
        title_style="bold",
        show_lines=True,
    )
    table.add_column("Role", style="cyan", no_wrap=True)
    table.add_column("Description", style="white")
    table.add_column("Tags", style="dim")
    for name in list_roles():
        try:
            meta = _read_role_metadata(get_template_path(name))
        except (ValueError, FileNotFoundError):
            meta = {}
        table.add_row(
            name,
            meta.get("description", ""),
            meta.get("tags", ""),
        )
    console.print(table)


def _list_roles_json() -> None:
    """Emit the role catalog as JSON. Used by the Mova iO wizard to
    populate the "Choose a template" dropdown — same fields as the
    Rich table but machine-parseable."""
    payload = []
    for name in list_roles():
        try:
            meta = _read_role_metadata(get_template_path(name))
        except (ValueError, FileNotFoundError):
            meta = {}
        payload.append(
            {
                "name": name,
                "description": meta.get("description", ""),
                "role": meta.get("role", ""),
                "tags": [t.strip() for t in (meta.get("tags") or "").split(",") if t.strip()],
            }
        )
    console.print_json(json.dumps(payload))


def _describe_role(name: str) -> None:
    """Preview a role's metadata + schemas before scaffolding."""
    try:
        template_path = get_template_path(name)
    except ValueError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2) from None

    meta = _read_role_metadata(template_path)
    console.print(f"[bold cyan]{name}[/bold cyan]")
    if meta.get("description"):
        console.print(f"  {meta['description']}")
    console.print()

    if meta.get("persona"):
        console.print("[bold]Persona[/bold]")
        # Indent multi-line persona for readability.
        for line in meta["persona"].splitlines():
            console.print(f"  {line}")
        console.print()

    if meta.get("tags"):
        console.print(f"[bold]Tags:[/bold] {meta['tags']}")
        console.print()

    role_md = template_path / "ROLE.md"
    if role_md.is_file():
        console.print("[bold]When to use this template[/bold]")
        console.print(f"  see [cyan]{role_md}[/cyan] for full guidance")
        console.print()

    console.print("[bold]Files this role scaffolds:[/bold]")
    for entry in sorted(template_path.rglob("*")):
        if entry.is_file():
            rel = entry.relative_to(template_path)
            console.print(f"  • {rel}")


def add(
    name: str | None = typer.Argument(None, help="Agent name to create (lowercase, hyphenated)."),
    template: str = typer.Option(
        "",
        "--template",
        "-t",
        help=(
            f"Role template to scaffold from. Roles: {', '.join(list_roles())}. "
            f"Shapes (legacy): {', '.join(list_templates())}."
        ),
    ),
    project: Path | None = typer.Option(
        None,
        "--project",
        help=(
            "Project root (the directory containing ``movate.yaml``). "
            "Defaults to walking up from the current directory until a "
            "``movate.yaml`` is found; falls back to cwd if none is found."
        ),
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing agent directory."),
    list_roles_flag: bool = typer.Option(
        False, "--list-roles", help="Print the role catalog and exit."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="With --list-roles: emit JSON instead of a table."
    ),
    describe: str | None = typer.Option(
        None,
        "--describe",
        help="Print a role's metadata + scaffolded files; don't create anything.",
    ),
) -> None:
    """Add a role-based agent to an existing project.

    Examples:

      [bold]mdk add[/bold] --list-roles              # see what's available
      [bold]mdk add[/bold] --describe sql-writer     # preview before adding
      [bold]mdk add[/bold] invoice-ocr --template support-triage
      [bold]mdk add[/bold] my-sql --template sql-writer --project ~/dev/sandisk
    """
    # Discovery modes — no scaffolding, just print.
    if list_roles_flag:
        if json_output:
            _list_roles_json()
        else:
            _list_roles_table()
        raise typer.Exit(code=0)

    if describe is not None:
        _describe_role(describe)
        raise typer.Exit(code=0)

    # Scaffolding requires both name + template.
    if not name:
        console.print("[red]error:[/red] agent name is required (or use --list-roles / --describe)")
        raise typer.Exit(code=2)
    if not template:
        console.print(f"[red]error:[/red] --template is required. Roles: {', '.join(list_roles())}")
        raise typer.Exit(code=2)

    # Resolve the template directory.
    try:
        template_dir = get_template_path(template)
    except ValueError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2) from None

    # Resolve the project root + prepare the destination dir.
    project_root = _resolve_project_root(project)
    dest = _prepare_destination(project_root / "agents", name, force=force)

    shutil.copytree(template_dir, dest)

    # Substitute the agent name placeholder in agent.yaml.
    yaml_path = dest / "agent.yaml"
    contents = yaml_path.read_text().replace("__AGENT_NAME__", name)
    yaml_path.write_text(contents)

    # Print success + next steps.
    console.print()
    console.print(
        f"[green]✓[/green] added [bold]{name}[/bold] "
        f"(template: [bold]{template}[/bold]) at "
        f"[bold]{dest.relative_to(project_root)}/[/bold]"
    )
    console.print(f"  project root: [dim]{project_root}[/dim]")
    console.print()
    console.print("[bold]Files scaffolded:[/bold]")
    for entry in sorted(dest.rglob("*")):
        if entry.is_file():
            console.print(f"  • {entry.relative_to(dest)}")

    role_md = dest / "ROLE.md"
    if role_md.is_file():
        console.print()
        console.print(
            f"[dim]see [cyan]{role_md.relative_to(project_root)}[/cyan] "
            f"for when-to-use guidance + customization tips[/dim]"
        )

    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print(f"  movate validate {dest}")
    console.print(f"  movate run {dest} --mock '{{}}'")
