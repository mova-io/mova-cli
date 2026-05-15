"""``mdk add <template>`` — add a role-based agent to an existing project.

A project-aware ergonomic wrapper around ``mdk init <name> -t <template>``.
Where ``mdk init`` is the general scaffold command, ``mdk add`` is the
"I'm inside a project and want to drop in another agent" command:

* **Auto-detects project root** by walking up from cwd looking for
  ``movate.yaml`` — same convention as ``mdk snapshot`` / ``mdk diff``.
  Errors with a clear pointer if no project is found.
* **Defaults target to** ``./agents/<name>/`` if ``agents/`` exists,
  else ``./<name>/``. Operators don't have to pass ``--target``.
* **Defaults agent name to the template name** — ``mdk add rag-qa``
  produces ``./agents/rag-qa/`` with no other typing. Optional
  positional arg renames.
* **``--list``** prints a curated table of role templates with one-line
  descriptions for discoverability.
* Same scaffold path under the hood as ``mdk init -t``; same files
  produced, same templates registry consulted, same validation.

This is the command demos and customers will reach for. ``mdk init``
stays as the general / power-user entry point.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from movate.templates import TEMPLATES, list_templates

console = Console()
err_console = Console(stderr=True)


# Curated descriptions for the `--list` view. Keys must match
# TEMPLATES entries; values are one-line summaries operators see when
# choosing a role. The "feature" column flags which MDK capability
# each template is the best demo of — useful for "which template
# shows X?" prospect questions.
_ROLE_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    # role -> (one-line description, feature highlight)
    "rag-qa": (
        "Grounded Q&A with citation indices.",
        "structured output + contexts pattern",
    ),
    "ticket-triager": (
        "Support ticket → category + priority + routing + draft reply.",
        "multi-field enum output",
    ),
    "email-responder": (
        "Drafted email reply with tone + length controls + needs-review flag.",
        "policy-aware output",
    ),
    "sql-writer": (
        "Natural-language question + schema → SQL + explanation.",
        "schema-as-context",
    ),
    "code-reviewer": (
        "Unified diff → array of structured findings (file/line/severity).",
        "array-of-objects output, LLM-judge-friendly",
    ),
    "lead-qualifier": (
        "Inbound lead → BANT scoring + next-best-action + objections.",
        "nested scoring objects",
    ),
    "meeting-summarizer": (
        "Transcript → decisions + action items + blockers + follow-ups.",
        "structured multi-section output",
    ),
    "resume-screener": (
        "JD + resume → match score + strengths + gaps + interview questions.",
        "multi-input matching",
    ),
    "compliance-checker": (
        "Text + ruleset → violations (rule_id + excerpt + severity + rewording).",
        "rules-as-input policy gating",
    ),
    "research-agent": (
        "Topic + sources → executive summary with per-source citations.",
        "multi-source synthesis with citations",
    ),
    # Core templates also addable via `mdk add`. Shown under a separate
    # heading in --list so the role templates are the first thing
    # operators see.
    "default": ("Minimal echo agent (string in → string out).", "starting point"),
    "faq": ("Question → answer + confidence score.", "exact-match eval"),
    "summarizer": ("Text + max words → summary + word count.", "length-bounded output"),
    "classifier": ("Text + label list → chosen label.", "exact-match classification"),
    "chatbot": ("Single message → single reply (memory-aware).", "stateful chat"),
    "extractor": ("Free text → strict typed fields.", "structured extraction"),
}

# Templates that ARE role-based (the post-v1.0 additions). Used by
# --list to split the table into "Role templates" + "Core templates".
_ROLE_TEMPLATES: frozenset[str] = frozenset(
    {
        "rag-qa",
        "ticket-triager",
        "email-responder",
        "sql-writer",
        "code-reviewer",
        "lead-qualifier",
        "meeting-summarizer",
        "resume-screener",
        "compliance-checker",
        "research-agent",
    }
)


def _resolve_project_root() -> Path | None:
    """Walk up from cwd looking for ``movate.yaml``. Returns ``None``
    if not found — caller surfaces the user-facing error.

    Same convention as ``mdk snapshot`` / ``mdk diff`` so operators
    don't have to relearn project resolution per command.
    """
    current = Path.cwd().resolve()
    while True:
        if (current / "movate.yaml").is_file():
            return current
        if current.parent == current:
            return None
        current = current.parent


def _default_target(project_root: Path) -> Path:
    """Pick the right place to drop the new agent.

    The convention is ``./agents/`` under the project root if that
    directory exists (the layout ``mdk init --project`` creates).
    Older / hand-built projects without an ``agents/`` dir get the
    agent dropped at the project root itself.
    """
    agents_dir = project_root / "agents"
    return agents_dir if agents_dir.is_dir() else project_root


def _render_list() -> None:
    """Print the role-template catalog as a Rich table."""
    role_table = Table(
        title="Role-based templates",
        title_style="bold",
        show_header=True,
        header_style="bold",
    )
    role_table.add_column("Name", style="cyan", no_wrap=True)
    role_table.add_column("What it does")
    role_table.add_column("Highlights", style="dim")

    for name in sorted(_ROLE_TEMPLATES):
        desc, feature = _ROLE_DESCRIPTIONS.get(name, ("", ""))
        role_table.add_row(name, desc, feature)

    console.print(role_table)

    core_table = Table(
        title="Core templates",
        title_style="bold",
        show_header=True,
        header_style="bold",
    )
    core_table.add_column("Name", style="cyan", no_wrap=True)
    core_table.add_column("What it does")
    core_table.add_column("Highlights", style="dim")

    core_names = sorted(set(TEMPLATES.keys()) - _ROLE_TEMPLATES)
    for name in core_names:
        desc, feature = _ROLE_DESCRIPTIONS.get(name, ("", ""))
        core_table.add_row(name, desc, feature)

    console.print()
    console.print(core_table)
    console.print()
    console.print(
        "[dim]Use: [bold]mdk add <name>[/bold] inside a project. "
        "Optional positional arg renames the agent: "
        "[bold]mdk add rag-qa pricing-qa[/bold].[/dim]"
    )


def _suggest_template(unknown: str) -> str | None:
    """Quick suggestion for "mdk add ragqa" → "did you mean: rag-qa?"

    Uses naive substring + edit-distance-like scoring without pulling
    in difflib's heavier APIs (this is a single-call hint, not a
    full fuzzy match).
    """
    normalized = unknown.replace("_", "-").lower()
    candidates = [t for t in TEMPLATES if normalized in t or t in normalized]
    return candidates[0] if candidates else None


def add(
    template: str = typer.Argument(
        None,
        help=(
            "Role / template name to add. Pass [bold]--list[/bold] to see "
            "available options. Examples: [bold]rag-qa[/bold], "
            "[bold]ticket-triager[/bold], [bold]code-reviewer[/bold]."
        ),
    ),
    name: str = typer.Argument(
        None,
        help=(
            "Optional custom name for the new agent (defaults to the "
            "template name). Lowercase, hyphenated."
        ),
    ),
    target: Path = typer.Option(
        None,
        "--target",
        help=(
            "Override the destination directory. Default: "
            "[bold]./agents/<name>/[/bold] if [bold]./agents/[/bold] exists, "
            "else [bold]./<name>/[/bold]."
        ),
    ),
    force: bool = typer.Option(
        False, "--force", "-f", help="Overwrite an existing agent directory."
    ),
    list_only: bool = typer.Option(
        False,
        "--list",
        help="Show the catalog of available role + core templates and exit.",
    ),
) -> None:
    """Add a role-based agent to the current project.

    [bold]Examples:[/bold]

      [dim]# List the available roles[/dim]
      $ mdk add --list

      [dim]# Drop a RAG Q&A agent into ./agents/rag-qa/[/dim]
      $ mdk add rag-qa

      [dim]# Rename: drop a ticket-triager as ./agents/triage/[/dim]
      $ mdk add ticket-triager triage

      [dim]# Add multiple roles to bootstrap a support workspace[/dim]
      $ mdk add rag-qa && mdk add ticket-triager && mdk add email-responder
    """
    if list_only:
        _render_list()
        return

    if not template:
        err_console.print(
            "[red]✗[/red] template name required. "
            "Run [bold]mdk add --list[/bold] to see options, or "
            "[bold]mdk add --help[/bold] for usage."
        )
        raise typer.Exit(code=2)

    if template not in TEMPLATES:
        suggestion = _suggest_template(template)
        hint = (
            f"\n[dim]did you mean [bold]{suggestion}[/bold]?[/dim]"
            if suggestion
            else f"\n[dim]available: {', '.join(list_templates())}[/dim]"
        )
        err_console.print(f"[red]✗[/red] unknown template {template!r}.{hint}")
        raise typer.Exit(code=2)

    # Project-aware resolution. Unlike `mdk init`, `mdk add` REQUIRES a
    # project — adding an agent outside one doesn't make sense.
    project_root = _resolve_project_root()
    if project_root is None:
        err_console.print(
            "[red]✗[/red] not inside a movate project. "
            "[dim]Run [bold]mdk init --project <name>[/bold] first, "
            f"or use [bold]mdk init <name> -t {template}[/bold] to scaffold "
            "outside a project.[/dim]"
        )
        raise typer.Exit(code=2)

    agent_name = name or template
    target_dir = target or _default_target(project_root)

    # Dispatch to the same scaffold function `mdk init` uses, so we get
    # the existing `__AGENT_NAME__` substitution + force-check + template-
    # resolution behaviors for free.
    from movate.cli.init import _init_agent  # noqa: PLC0415

    _init_agent(name=agent_name, template=template, target=target_dir, force=force)

    # Success Panel with project-context summary. Distinct from
    # `mdk init`'s success message so operators can visually
    # distinguish "added to project" from "scaffolded standalone".
    dest = (target_dir / agent_name).resolve()
    body = (
        f"[bold]Added:[/bold]    [cyan]{agent_name}[/cyan] "
        f"[dim](from template [bold]{template}[/bold])[/dim]\n"
        f"[bold]Path:[/bold]     [cyan]{dest}[/cyan]\n"
        f"[bold]Project:[/bold]  [dim]{project_root}[/dim]\n"
        f"\n[bold]Next steps:[/bold]\n"
        f"  [dim]$[/dim] [bold]mdk validate {dest.relative_to(Path.cwd()) if dest.is_relative_to(Path.cwd()) else dest}[/bold]\n"  # noqa: E501
        f"  [dim]$[/dim] [bold]mdk run ./{dest.relative_to(project_root)} --mock '{{...}}'[/bold]\n"
        f"  [dim]$[/dim] [bold]mdk eval ./{dest.relative_to(project_root)} --mock --gate 0.7[/bold]"
    )
    console.print(
        Panel(
            body,
            title=f"[green]✓[/green] Added {template} agent",
            title_align="left",
            border_style="green",
        )
    )

    # Greppable summary line — mirrors mdk_init_summary / audit / eval /
    # doctor / etc. so CI can parse one consistent prefix for every
    # generation-style command.
    console.print(
        f"[dim]mdk_add_summary: "
        f"template={template} "
        f"name={agent_name} "
        f"project={project_root.name} "
        f"target={dest} "
        f"ok=true[/dim]"
    )
