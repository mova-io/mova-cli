"""``mdk templates`` — discover scaffolds that ``mdk init -t`` / ``mdk add`` accept.

The template name → packaged-dir mapping lives in
:mod:`movate.templates`. This command surfaces it interactively so an
operator who's forgotten the exact name doesn't have to read
``mdk init --help`` or grep the source.

ADR 028 extends the surface from a thin name+description listing to a
richer ``TemplateInfo`` view that each template owns via a sibling
``template.yaml`` file. Subcommands:

* ``list``  — table of all templates (agents + workflows), with optional
  ``--json`` for scripts.
* ``show``  — full metadata + file tree for one template.

The legacy ``list`` rendering (name / directory / one-line description
pulled from ``agent.yaml``) is preserved as a fallback for any template
that ships without a ``template.yaml`` (back-compat per CLAUDE.md rule 5
— a partial template never crashes the discovery command).
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

from movate.templates import (
    TEMPLATES,
    TEMPLATES_DIR,
    TemplateInfo,
    TemplateInfoLoadError,
    list_template_infos,
    load_template_info,
)

# Table-column cap so long descriptions don't blow out the layout.
# Module-level constant satisfies N806 + lets future tests assert on it.
_MAX_DESC_LEN = 70

# Max number of files we list inline for ``mdk templates show <name>``.
# Beyond this the tree is truncated with a count of the remainder, so
# huge KB-bundled templates (hr-policy ships dozens of fixtures) don't
# blow out a single screen.
_MAX_TREE_ENTRIES = 80

app = typer.Typer(
    name="templates",
    help="Discover the agent + workflow templates `mdk init -t` and `mdk add` accept.",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)


def _read_description_from_agent_yaml(template_dir: Path) -> str:
    """Legacy fallback: pull the one-liner from the template's agent.yaml.

    Used only when a template ships without ``template.yaml`` (ADR 028
    metadata). New templates should never hit this path; kept for
    back-compat so a half-migrated tree still renders rows.
    """
    agent_yaml = template_dir / "agent.yaml"
    if not agent_yaml.is_file():
        return "—"
    try:
        data = yaml.safe_load(agent_yaml.read_text()) or {}
    except yaml.YAMLError:
        return "—"
    desc = data.get("description") or data.get("summary") or "—"
    desc = " ".join(str(desc).split())
    return desc if len(desc) <= _MAX_DESC_LEN else desc[: _MAX_DESC_LEN - 3] + "…"


def _truncate(s: str, n: int = _MAX_DESC_LEN) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 3] + "…"


def _info_or_legacy_row(name: str, dir_name: str) -> tuple[str, str, list[str], str]:
    """Resolve (shape, description, tags, recommended_for) for one template.

    Prefers the rich ``template.yaml`` (ADR 028) but falls back to a
    description scraped from ``agent.yaml`` so a maintainer who hasn't
    yet added metadata still sees their template in the list rather than
    a confusing absence.
    """
    try:
        info = load_template_info(name)
        return info.shape, info.description, list(info.tags), info.recommended_for
    except TemplateInfoLoadError:
        return "agent", _read_description_from_agent_yaml(TEMPLATES_DIR / dir_name), [], ""


def _format_tags(tags: list[str]) -> str:
    """Comma-separated tag string for the list/show views."""
    if not tags:
        return "[dim]—[/dim]"
    return ", ".join(tags)


@app.command("list")
def list_cmd(
    json_output: bool = typer.Option(
        False,
        "--json",
        help=(
            "Emit a JSON array of template records to stdout. The shape "
            "follows :meth:`TemplateInfo.to_dict` and is a stable public "
            "contract (CLAUDE.md rule 5). Templates missing a "
            "``template.yaml`` are omitted from the JSON view — use the "
            "rendered table for the full legacy listing."
        ),
    ),
    shape: str | None = typer.Option(
        None,
        "--shape",
        help=(
            "Filter rows by shape — [bold]agent[/bold] or "
            "[bold]workflow[/bold]. Omit to list everything."
        ),
    ),
) -> None:
    """List every template `mdk init -t <name>` / `mdk add <name>` accepts.

    Renders a Rich table by default; pass ``--json`` for machine output
    consumed by scripts and external editors. The table view includes a
    legacy-fallback row for any template missing the new ADR 028
    ``template.yaml`` so partial migrations don't drop rows.
    """
    if shape is not None and shape not in {"agent", "workflow"}:
        err_console.print(f"[red]✗[/red] --shape must be 'agent' or 'workflow', got {shape!r}")
        raise typer.Exit(code=2)

    infos = list_template_infos(include_workflows=True)
    if shape is not None:
        infos = [i for i in infos if i.shape == shape]

    if json_output:
        # Stable JSON contract: ordered by name, only fully-described
        # templates (those with a valid template.yaml) appear. Stdout-only
        # — no Rich logs — so callers can pipe directly to ``jq``.
        payload = [info.to_dict() for info in infos]
        typer.echo(json.dumps(payload, indent=2, sort_keys=False))
        return

    # Rich table — includes a legacy fallback row for every agent template
    # that has no template.yaml yet. This is the operator-facing surface;
    # JSON is the contract surface.
    described_names = {i.name for i in infos}

    table = Table(
        title=_table_title(shape=shape, n_described=len(infos)),
        title_style="bold",
        header_style="bold cyan",
    )
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Shape", no_wrap=True)
    table.add_column("Description")
    table.add_column("Tags", style="dim")

    # First: every described template (sorted by name).
    for info in infos:
        table.add_row(
            info.name,
            _shape_cell(info.shape),
            _truncate(info.description),
            _format_tags(list(info.tags)),
        )

    # Then: agent templates without template.yaml — legacy view. Skipped
    # when --shape=workflow is in play (only agents lack metadata today).
    if shape != "workflow":
        for legacy_name in sorted(TEMPLATES.keys()):
            if legacy_name in described_names:
                continue
            dir_name = TEMPLATES[legacy_name]
            legacy_desc = _read_description_from_agent_yaml(TEMPLATES_DIR / dir_name)
            table.add_row(
                legacy_name,
                _shape_cell("agent"),
                _truncate(legacy_desc),
                "[dim]—[/dim]",
            )

    console.print(table)
    console.print(
        "\n[dim]Show one: [bold]mdk templates show <name>[/bold]. "
        "Scaffold: [bold]mdk init <project-name> -t <template>[/bold] "
        "or [bold]mdk add <template>[/bold] inside a project.[/dim]"
    )


def _table_title(*, shape: str | None, n_described: int) -> str:
    """Pretty title for the list table — kept out of the body for readability."""
    if shape == "agent":
        return f"Available templates · agent ({n_described} described)"
    if shape == "workflow":
        return f"Available templates · workflow ({n_described} described)"
    return f"Available templates ({n_described} described)"


def _shape_cell(shape: str) -> str:
    """Color-code the shape cell so workflow rows stand out in the table."""
    if shape == "workflow":
        return "[magenta]workflow[/magenta]"
    return "[green]agent[/green]"


@app.command("show")
def show_cmd(
    name: str = typer.Argument(..., help="Template name to inspect."),
    json_output: bool = typer.Option(
        False,
        "--json",
        help=(
            "Emit one template record as JSON. Same shape as the records "
            "from ``mdk templates list --json`` so scripts can use either "
            "command interchangeably. The file tree is omitted from JSON "
            "— callers wanting it can walk ``directory`` themselves."
        ),
    ),
) -> None:
    """Show full metadata + on-disk file tree for one template.

    Fails with exit code 1 when the template name is unknown so scripts
    can detect missing templates cleanly. Output is Rich by default;
    pass ``--json`` for a stable machine-readable record.
    """
    try:
        info = load_template_info(name)
    except TemplateInfoLoadError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from None
    except ValueError as exc:
        # Raised by get_template_path for unknown names — message already
        # lists the available templates. Re-emit on stderr + exit 1.
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from None

    if json_output:
        typer.echo(json.dumps(info.to_dict(), indent=2, sort_keys=False))
        return

    _render_show_panel(info)


def _render_show_panel(info: TemplateInfo) -> None:
    """Render the human-facing Panel + file tree for ``templates show``.

    Split out of :func:`show_cmd` so the rendering logic is easy to
    unit-test (and to keep the command handler thin).
    """
    body = (
        f"[bold]name:[/bold]             [cyan]{info.name}[/cyan]\n"
        f"[bold]title:[/bold]            {info.title}\n"
        f"[bold]shape:[/bold]            {_shape_cell(info.shape)}\n"
        f"[bold]description:[/bold]      {info.description}\n"
        f"[bold]tags:[/bold]             {_format_tags(list(info.tags))}\n"
        f"[bold]recommended for:[/bold]  {info.recommended_for}\n"
        f"[bold]directory:[/bold]        [dim]{info.directory}[/dim]"
    )
    console.print(
        Panel(
            body,
            title=f"template [cyan]{info.name}[/cyan]",
            title_align="left",
            border_style="cyan",
        )
    )
    _render_file_tree(info.directory)
    # Footer hint — same usage line as the list view, focused on this name.
    console.print(
        f"\n[dim]Scaffold: [bold]mdk init <project-name> -t {info.name}[/bold] "
        f"or [bold]mdk add {info.name}[/bold] inside a project.[/dim]"
    )


def _render_file_tree(root: Path) -> None:
    """Render a truncated file tree of ``root`` for the show view.

    Capped at :data:`_MAX_TREE_ENTRIES` — templates that bundle large KB
    fixtures (hr-policy ships dozens of MD/HTML/PDF files) would
    otherwise dominate the screen. The truncation marker tells the
    operator exactly how many entries were elided.
    """
    tree = Tree(f"[bold]{root.name}[/bold]")
    count = 0
    truncated = 0
    # Walk in deterministic order so two runs render identically.
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        # Hide noisy build/cache artifacts that don't ship with templates
        # but might appear in a development checkout.
        parts = path.relative_to(root).parts
        if any(part.startswith(".") or part == "__pycache__" for part in parts):
            continue
        if count >= _MAX_TREE_ENTRIES:
            truncated += 1
            continue
        rel = path.relative_to(root).as_posix()
        label = f"[dim]{rel}[/dim]" if path.is_dir() else rel
        tree.add(label)
        count += 1
    if truncated:
        tree.add(f"[dim]… {truncated} more entries elided[/dim]")
    console.print(tree)
