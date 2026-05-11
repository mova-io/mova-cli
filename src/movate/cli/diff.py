"""``movate diff <a> <b>`` — compare two agents side-by-side.

Built for PR review. Renders the metadata delta, the prompt unified
diff, and (collapsibly) the input/output schema diffs. Pick the output
format that fits the audience:

* ``-o table`` (default) — interactive terminal review.
* ``-o json`` — pipe into ``jq`` / scripts / drift tracking.
* ``-o markdown`` — paste into a PR description / CI annotation.

The implementation is split: pure comparison + JSON/Markdown rendering
live in :mod:`movate.core.diff`; this module owns CLI flags and Rich
rendering. Tests can exercise the core module directly without booting
Typer's ``CliRunner``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from movate.cli._completion import complete_agent_path
from movate.cli._output import Report
from movate.core.diff import (
    AgentDiff,
    AgentDiffError,
    diff_agents,
    render_diff_json,
    render_diff_markdown,
)

console = Console()


def diff(
    a: Path = typer.Argument(
        ...,
        help="Path to agent A (the 'before' side).",
        shell_complete=complete_agent_path,
    ),
    b: Path = typer.Argument(
        ...,
        help="Path to agent B (the 'after' side).",
        shell_complete=complete_agent_path,
    ),
    output: Report = typer.Option(
        Report.TABLE,
        "--output",
        "-o",
        help=(
            "Output format. table = human review; json = pipe-friendly; "
            "markdown = PR description."
        ),
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show unchanged metadata rows too (default: only changed rows).",
    ),
    prompt_only: bool = typer.Option(
        False,
        "--prompt-only",
        help="Only render the prompt diff; suppress metadata + schemas.",
    ),
    schemas_only: bool = typer.Option(
        False,
        "--schemas-only",
        help="Only render the schema diffs; suppress metadata + prompt.",
    ),
    fail_on_change: bool = typer.Option(
        False,
        "--fail-on-change",
        help=(
            "Exit 1 if any difference is detected (default exits 0 regardless). "
            "Useful for CI checks like 'PR touching prompt must bump version'."
        ),
    ),
) -> None:
    """Show the structural diff between two agents.

    Examples:

      [dim]# Compare two agents on disk[/dim]
      $ movate diff ./agents/faq-agent ./agents/faq-agent-v2

      [dim]# Just the prompt change[/dim]
      $ movate diff agent-a/ agent-b/ --prompt-only

      [dim]# Output a PR-description-ready snippet[/dim]
      $ movate diff agent-a/ agent-b/ -o markdown

    Exit codes:

      [bold]0[/bold] — diff produced; no error (regardless of whether agents differ).
      [bold]1[/bold] — both agents loaded but at least one difference detected
                       (only when [bold]--fail-on-change[/bold] is set; default is 0).
      [bold]2[/bold] — load failure on one or both sides; nothing rendered.
    """
    if prompt_only and schemas_only:
        console.print("[red]✗[/red] --prompt-only and --schemas-only are mutually exclusive.")
        raise typer.Exit(code=2)

    try:
        d = diff_agents(a, b)
    except AgentDiffError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(code=2) from None

    if output is Report.JSON:
        typer.echo(render_diff_json(d))
    elif output is Report.MARKDOWN:
        typer.echo(render_diff_markdown(d))
    else:
        _render_rich(d, verbose=verbose, prompt_only=prompt_only, schemas_only=schemas_only)

    if fail_on_change and d.has_any_change():
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Rich rendering
# ---------------------------------------------------------------------------


def _render_rich(
    d: AgentDiff,
    *,
    verbose: bool,
    prompt_only: bool,
    schemas_only: bool,
) -> None:
    if not d.has_any_change():
        console.print(
            f"[green]✓ no differences[/green] "
            f"[dim]{d.a_name} v{d.a_version} ↔ {d.b_name} v{d.b_version}[/dim]"
        )
        return

    header = (
        f"[bold]{d.a_name}[/bold] v{d.a_version}  →  "
        f"[bold]{d.b_name}[/bold] v{d.b_version}"
    )
    console.print(Panel(header, expand=False))

    if not prompt_only and not schemas_only:
        _render_metadata(d, verbose=verbose)

    if not schemas_only and d.prompt_changed:
        _render_prompt(d)

    if not prompt_only:
        if d.input_schema_changed:
            _render_schema(d, which="input")
        if d.output_schema_changed:
            _render_schema(d, which="output")

    if not prompt_only and not schemas_only and d.dataset_changed:
        _render_dataset(d)


def _render_metadata(d: AgentDiff, *, verbose: bool) -> None:
    rows = d.field_deltas if verbose else d.changed_field_deltas()
    if not rows:
        return

    table = Table(title="Metadata", show_header=True, header_style="bold")
    table.add_column("field", style="dim")
    table.add_column("before")
    table.add_column("after")

    for delta in rows:
        a_cell, b_cell = _format_value(delta.a), _format_value(delta.b)
        if delta.changed:
            table.add_row(delta.name, f"[red]{a_cell}[/red]", f"[green]{b_cell}[/green]")
        else:
            table.add_row(delta.name, a_cell, b_cell, style="dim")
    console.print(table)


def _render_prompt(d: AgentDiff) -> None:
    console.print(
        f"\n[bold]Prompt[/bold]   "
        f"[dim]{d.a_prompt_hash[:12]}…[/dim] → "
        f"[dim]{d.b_prompt_hash[:12]}…[/dim]"
    )
    diff_text = d.prompt_unified_diff()
    if not diff_text:
        # Hash differs but unified-diff is empty (e.g. encoding-only change).
        console.print("[dim](hashes differ; content equal byte-for-byte under unified diff)[/dim]")
        return
    console.print(Syntax(diff_text, "diff", theme="ansi_dark", background_color="default"))


def _render_schema(d: AgentDiff, *, which: str) -> None:
    label = "input.schema" if which == "input" else "output.schema"
    console.print(f"\n[bold]{label}[/bold]")
    diff_text = d.schema_unified_diff(which)
    console.print(Syntax(diff_text, "diff", theme="ansi_dark", background_color="default"))


def _render_dataset(d: AgentDiff) -> None:
    console.print("\n[bold]evals.dataset[/bold]")

    def _fmt(ds: Any) -> str:
        if ds is None:
            return "[dim]—[/dim]"
        if not ds.exists:
            return f"[red]{ds.path} (missing)[/red]"
        return f"{ds.path} ({ds.case_count} cases, sha={ds.sha256[:12]}…)"

    console.print(f"  before:  {_fmt(d.a_dataset)}")
    console.print(f"  after:   {_fmt(d.b_dataset)}")


def _format_value(v: Any) -> str:
    if v is None:
        return "[dim]—[/dim]"
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v)
    return str(v)
