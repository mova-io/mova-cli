"""``mdk templates`` — discover scaffolds that ``mdk init -t`` / ``mdk add`` accept.

The template name → packaged-dir mapping lives in
:mod:`movate.templates`. This command surfaces it interactively so an
operator who's forgotten the exact name doesn't have to read
``mdk init --help`` or grep the source.

Only one subcommand today (``list``); kept as a subapp so future
additions (``show <name>``, ``preview <name>``) slot in cleanly
without breaking the surface.
"""

from __future__ import annotations

from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table

from movate.templates import TEMPLATES, TEMPLATES_DIR

# Table-column cap so long descriptions don't blow out the layout.
# Module-level constant satisfies N806 + lets future tests assert on it.
_MAX_DESC_LEN = 70

app = typer.Typer(
    name="templates",
    help="Discover the agent + skill templates `mdk init -t` and `mdk add` accept.",
    no_args_is_help=True,
)

console = Console()


def _read_description(template_dir: Path) -> str:
    """Best-effort one-liner pulled from the template's agent.yaml.

    Falls back to '—' when the template ships without an agent.yaml
    (rare; the role-based templates all have one). Never raises —
    a broken template should still appear in the list, just with no
    description.
    """
    agent_yaml = template_dir / "agent.yaml"
    if not agent_yaml.is_file():
        return "—"
    try:
        data = yaml.safe_load(agent_yaml.read_text()) or {}
    except yaml.YAMLError:
        return "—"
    desc = data.get("description") or data.get("summary") or "—"
    # Compact whitespace + cap length — long descriptions wreck table
    # columns and the operator can always read the full agent.yaml.
    desc = " ".join(str(desc).split())
    return desc if len(desc) <= _MAX_DESC_LEN else desc[: _MAX_DESC_LEN - 3] + "…"


@app.command("list")
def list_cmd() -> None:
    """List every template `mdk init -t <name>` / `mdk add <name>` accepts.

    Renders a Rich table with name, on-disk directory, and a one-line
    description pulled from each template's ``agent.yaml``.
    """
    table = Table(
        title=f"Available templates ({len(TEMPLATES)} total)",
        title_style="bold",
        header_style="bold cyan",
    )
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Directory", style="dim")
    table.add_column("Description")

    for name in sorted(TEMPLATES.keys()):
        dir_name = TEMPLATES[name]
        desc = _read_description(TEMPLATES_DIR / dir_name)
        table.add_row(name, dir_name, desc)

    console.print(table)
    console.print(
        "\n[dim]Usage: [bold]mdk init <project-name> -t <template>[/bold] "
        "or [bold]mdk add <template>[/bold] inside a project.[/dim]"
    )
