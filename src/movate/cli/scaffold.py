"""``movate scaffold <target> <name>`` — generate boilerplate for movate artifacts.

Today the only target is ``tool`` — generates a tool directory with
``tool.yaml`` + handler stub + input/output schemas. Future targets
(``workflow``, ``mcp-server``, ``eval`` scaffolds) drop in alongside
without restructuring the top-level CLI.

Distinct from ``movate init <name>``: ``init`` scaffolds an AGENT
(it predates this sub-app and the verb has stuck in muscle memory).
Everything else goes through ``scaffold``.

Important caveat: tools today are scaffolded artifacts only — the
runtime doesn't yet read ``tool.yaml`` or invoke handlers. That's
blocked on the A/B/C architectural decision in the high-priority
list. Scaffold lands first so users have a one-command path from
"I need a tool" to "here's the boilerplate" the moment the runtime
ships.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import typer

from movate.cli._console import error, hint, success
from movate.templates import TEMPLATES_DIR

scaffold_app = typer.Typer(
    name="scaffold",
    help="Generate boilerplate for movate artifacts (tools, etc).",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_TOOL_TEMPLATE_DIR = TEMPLATES_DIR / "tool_init"


@scaffold_app.command("tool")
def tool(
    name: str = typer.Argument(
        ...,
        help="Tool name (lowercase, hyphenated). e.g. 'web-search', 'sql-query'.",
    ),
    target: Path = typer.Option(
        Path("./tools"),
        "--target",
        help="Parent directory for the new tool.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite an existing tool directory at <target>/<name>/.",
    ),
) -> None:
    """Generate a new tool directory under ``<target>/<name>/``.

    [bold]Example:[/bold]

      [dim]# Scaffold a web-search tool[/dim]
      $ movate scaffold tool web-search
      ✓ scaffolded tool at ./tools/web-search

      [dim]# Custom location[/dim]
      $ movate scaffold tool sql-query --target ./shared/tools

    Generated layout:

      tools/<name>/
        tool.yaml          — metadata + handler ref + schema refs
        handler.py         — async def handler(input) -> dict stub
        schema/input.json  — TODO: replace stub field(s)
        schema/output.json — TODO: replace stub field(s)

    [dim]The tool runtime that actually invokes handler.py lands as
    Tier-2 work (#9 in the high-priority list). Scaffolding today
    means the boilerplate is ready the moment the runtime ships.[/dim]
    """
    if not _NAME_RE.match(name):
        error(
            f"tool name {name!r} must be lowercase + hyphens only "
            "(e.g. 'web-search', not 'WebSearch' or 'web_search')"
        )
        raise typer.Exit(code=2)

    dest = (target / name).resolve()
    if dest.exists() and not force:
        error(f"{dest} already exists (use --force to overwrite)")
        raise typer.Exit(code=2)
    if dest.exists() and force:
        shutil.rmtree(dest)

    shutil.copytree(_TOOL_TEMPLATE_DIR, dest)
    _substitute_placeholders(dest, {"__TOOL_NAME__": name})

    success(f"scaffolded tool at {dest}")
    hint(
        "[dim]Next steps:\n"
        f"  1. edit {dest / 'tool.yaml'} — replace TODO description\n"
        f"  2. edit {dest / 'schema' / 'input.json'} and "
        f"{dest / 'schema' / 'output.json'} — declare your real fields\n"
        f"  3. edit {dest / 'handler.py'} — implement the handler\n"
        "[/dim]"
    )


def _substitute_placeholders(dest: Path, replacements: dict[str, str]) -> None:
    """Walk ``dest`` and replace placeholder tokens in every text file.

    Used to splice the user-provided name into the templated files.
    Binary files are skipped (handled by UnicodeDecodeError); the
    bundled templates are all text so this is just defensive."""
    for path in dest.rglob("*"):
        if not path.is_file():
            continue
        try:
            content = path.read_text()
        except UnicodeDecodeError:
            continue
        new_content = content
        for token, value in replacements.items():
            new_content = new_content.replace(token, value)
        if new_content != content:
            path.write_text(new_content)


__all__ = ["scaffold_app"]
