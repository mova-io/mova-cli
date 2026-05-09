"""``movate init <name>`` — scaffold a new agent directory from a packaged template."""

from __future__ import annotations

import shutil
from pathlib import Path

import typer
from rich.console import Console

from movate.templates import get_template_path, list_templates

console = Console()


def init(
    name: str = typer.Argument(..., help="Agent name (lowercase, hyphenated)."),
    template: str = typer.Option(
        "default",
        "--template",
        "-t",
        help=f"Template to scaffold from. One of: {', '.join(list_templates())}.",
    ),
    target: Path = typer.Option(Path("."), "--target", help="Parent directory for the new agent."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing directory."),
) -> None:
    """Scaffold a new agent directory under ``<target>/<name>/``.

    Available templates:

      [bold]default[/bold]    — minimal echo agent (string-in, string-out)
      [bold]faq[/bold]        — question → answer + confidence
      [bold]summarizer[/bold] — text + max_words → summary + word_count
      [bold]classifier[/bold] — text + labels → chosen label
    """
    try:
        template_dir = get_template_path(template)
    except ValueError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2) from None

    dest = (target / name).resolve()
    if dest.exists() and not force:
        console.print(f"[red]error:[/red] {dest} already exists (use --force to overwrite)")
        raise typer.Exit(code=2)
    if dest.exists() and force:
        shutil.rmtree(dest)

    shutil.copytree(template_dir, dest)

    yaml_path = dest / "agent.yaml"
    contents = yaml_path.read_text().replace("__AGENT_NAME__", name)
    yaml_path.write_text(contents)

    console.print(
        f"[green]✓[/green] scaffolded [bold]{template}[/bold] agent at [bold]{dest}[/bold]"
    )
    console.print("\nNext steps:")
    console.print(f"  movate validate {dest}")
    console.print(f"  movate run {dest} --mock '{{}}'   # provide input matching schema/input.json")
