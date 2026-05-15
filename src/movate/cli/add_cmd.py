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

# Use-case grouping for the role catalog. Each role belongs to exactly
# one bucket — operators scanning the catalog can answer "what do you
# have for sales?" in two seconds. Order within each bucket is the
# order the templates render. If you add a new role template, also
# add it here (Templates show as ungrouped if missing).
_ROLE_GROUPS: list[tuple[str, list[str]]] = [
    ("Support", ["ticket-triager", "email-responder"]),
    ("Sales / GTM", ["lead-qualifier"]),
    ("Engineering", ["code-reviewer", "sql-writer"]),
    ("Knowledge work", ["rag-qa", "research-agent"]),
    ("HR / Recruiting", ["resume-screener"]),
    ("Compliance / Ops", ["compliance-checker", "meeting-summarizer"]),
]


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


def _matches_search(name: str, search: str) -> bool:
    """Substring match for ``--search`` filter.

    Matches against the template name AND its one-line description so
    operators can search by either capability name (``--search rag``)
    or by problem domain (``--search support``).
    """
    needle = search.lower()
    desc, feature = _ROLE_DESCRIPTIONS.get(name, ("", ""))
    return (
        needle in name.lower()
        or needle in desc.lower()
        or needle in feature.lower()
    )


def _render_list(search: str | None = None) -> None:
    """Print the role-template catalog as Rich tables.

    The role tier is grouped by use case (Support / Sales / Engineering
    / Knowledge work / HR / Compliance) so operators scanning the
    catalog can answer "what do you have for X?" without reading the
    full list. The core tier — minimal templates from the original
    v0.1 set — gets a separate table beneath.

    ``search`` filters both tiers by substring match against the
    template name, description, or feature highlight. When the search
    eliminates every entry, render a "no matches" hint with the
    available role names.
    """
    role_rows: list[tuple[str, str, str, str]] = []
    for group, role_names in _ROLE_GROUPS:
        for name in role_names:
            if search and not _matches_search(name, search):
                continue
            desc, feature = _ROLE_DESCRIPTIONS.get(name, ("", ""))
            role_rows.append((group, name, desc, feature))

    role_table = Table(
        title=(
            f"Role-based templates  [dim](filtered by {search!r})[/dim]"
            if search
            else "Role-based templates"
        ),
        title_style="bold",
        show_header=True,
        header_style="bold",
    )
    role_table.add_column("Use case", style="magenta", no_wrap=True)
    role_table.add_column("Name", style="cyan", no_wrap=True)
    role_table.add_column("What it does")
    role_table.add_column("Highlights", style="dim")

    if role_rows:
        # Print each group once — leave subsequent rows in the same
        # group blank in the Use-case column so the visual grouping
        # reads cleanly without horizontal-rule overhead.
        last_group = ""
        for group, name, desc, feature in role_rows:
            label = group if group != last_group else ""
            role_table.add_row(label, name, desc, feature)
            last_group = group
        console.print(role_table)
    elif search:
        console.print(
            f"[yellow]⚠[/yellow] no role templates match "
            f"[dim]{search!r}[/dim]."
        )

    # Core tier — minimal templates from the original set. Same
    # filter logic; less interesting in demos so it renders below.
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
    core_rows_added = 0
    for name in core_names:
        if search and not _matches_search(name, search):
            continue
        desc, feature = _ROLE_DESCRIPTIONS.get(name, ("", ""))
        core_table.add_row(name, desc, feature)
        core_rows_added += 1

    console.print()
    if core_rows_added:
        console.print(core_table)
    elif search:
        console.print(f"[dim]no core templates match {search!r}.[/dim]")
    console.print()
    console.print(
        "[dim]Use: [bold]mdk add <name>[/bold] inside a project. "
        "Batch-add: [bold]mdk add rag-qa ticket-triager[/bold]. "
        "Filter: [bold]mdk add --list --search <term>[/bold].[/dim]"
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
    args: list[str] = typer.Argument(
        None,
        help=(
            "One or more template names to add. Pass [bold]--list[/bold] to "
            "see available options. Single-template form supports a "
            "trailing rename: [bold]mdk add rag-qa pricing-qa[/bold]."
        ),
    ),
    name: str = typer.Option(
        None,
        "--name",
        help=(
            "Explicit agent name override. Only valid when adding a "
            "single template. Use this instead of the positional "
            "rename form when scripting (less ambiguous)."
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
    search: str = typer.Option(
        None,
        "--search",
        help=(
            "Filter [bold]--list[/bold] output by substring match against "
            "the template name, description, or highlight."
        ),
    ),
    no_validate: bool = typer.Option(
        False,
        "--no-validate",
        help=(
            "Skip the post-scaffold validation pass. By default, every "
            "added agent is loaded via [bold]load_agent[/bold] to confirm "
            "schemas + prompt + references all resolve."
        ),
    ),
) -> None:
    """Add one or more role-based agents to the current project.

    [bold]Examples:[/bold]

      [dim]# List the available roles[/dim]
      $ mdk add --list

      [dim]# Filter the list[/dim]
      $ mdk add --list --search support

      [dim]# Drop a RAG Q&A agent into ./agents/rag-qa/[/dim]
      $ mdk add rag-qa

      [dim]# Rename: drop a ticket-triager as ./agents/triage/[/dim]
      $ mdk add ticket-triager triage

      [dim]# Batch: bootstrap a support workspace in one command[/dim]
      $ mdk add rag-qa ticket-triager email-responder
    """
    if list_only:
        _render_list(search=search)
        return

    if not args:
        err_console.print(
            "[red]✗[/red] template name required. "
            "Run [bold]mdk add --list[/bold] to see options, or "
            "[bold]mdk add --help[/bold] for usage."
        )
        raise typer.Exit(code=2)

    # Detect the two positional shapes we accept:
    # 1. `mdk add <template>` — single template (also covers shape #3
    #    of length 1).
    # 2. `mdk add <template> <rename>` — single template + rename
    #    (where the second arg is NOT a valid template name).
    # 3. `mdk add <template> <template> ...` — batch (every arg IS a
    #    valid template name).
    # The heuristic: if all positional args are valid templates, treat
    # as batch. If exactly 2 args and only the first is a template,
    # treat the second as a rename target. Anything else falls through
    # to the validation step below, which surfaces the unknown name.
    # 2 positionals = the only shape that COULD be template-plus-rename.
    # Pulled into a constant so ruff PLR2004 stays satisfied and the
    # intent is named at the comparison site.
    rename_arg_count = 2
    positional_rename: str | None = None
    if (
        len(args) == rename_arg_count
        and args[0] in TEMPLATES
        and args[1] not in TEMPLATES
    ):
        templates = [args[0]]
        positional_rename = args[1]
    else:
        templates = list(args)

    # --name and positional rename are mutually exclusive — both
    # being set is almost certainly a typo.
    if positional_rename and name:
        err_console.print(
            "[red]✗[/red] [bold]--name[/bold] conflicts with the positional "
            "rename form. Pick one."
        )
        raise typer.Exit(code=2)
    rename = positional_rename or name

    # Renaming with multiple templates makes no sense — every added
    # agent would land at the same path. Reject loudly.
    if len(templates) > 1 and rename:
        err_console.print(
            "[red]✗[/red] cannot rename when adding multiple templates "
            "([bold]--name[/bold] / positional rename only applies to a "
            "single-template add)."
        )
        raise typer.Exit(code=2)

    # Validate every template name up-front so a typo in slot 3 doesn't
    # silently scaffold slots 1 and 2 before erroring.
    invalid = [t for t in templates if t not in TEMPLATES]
    if invalid:
        # Use the first unknown name for the typo suggestion — most
        # operators only mistype one template at a time anyway.
        suggestion = _suggest_template(invalid[0])
        hint = (
            f"\n[dim]did you mean [bold]{suggestion}[/bold]?[/dim]"
            if suggestion
            else f"\n[dim]available: {', '.join(list_templates())}[/dim]"
        )
        unknown_str = ", ".join(repr(t) for t in invalid)
        err_console.print(f"[red]✗[/red] unknown template(s): {unknown_str}.{hint}")
        raise typer.Exit(code=2)

    # Project-aware resolution. Unlike `mdk init`, `mdk add` REQUIRES a
    # project — adding an agent outside one doesn't make sense.
    project_root = _resolve_project_root()
    if project_root is None:
        first_template = templates[0]
        err_console.print(
            "[red]✗[/red] not inside a movate project. "
            "[dim]Run [bold]mdk init --project <name>[/bold] first, "
            f"or use [bold]mdk init <name> -t {first_template}[/bold] "
            "to scaffold outside a project.[/dim]"
        )
        raise typer.Exit(code=2)

    # Scaffold each template in order. Each gets its own success Panel
    # and summary line so output stays parseable; the batch shape is
    # an ergonomic shortcut, not a different output format.
    target_dir = target or _default_target(project_root)
    for template in templates:
        agent_name = rename if rename else template
        _add_one(
            template=template,
            agent_name=agent_name,
            target_dir=target_dir,
            force=force,
            project_root=project_root,
            no_validate=no_validate,
        )


def _add_one(
    *,
    template: str,
    agent_name: str,
    target_dir: Path,
    force: bool,
    project_root: Path,
    no_validate: bool,
) -> None:
    """Scaffold a single template, render success Panel, emit summary.

    Extracted so the batch path loops over this helper without
    duplicating the Panel + summary-line logic per iteration.
    """
    # Dispatch to the same scaffold function `mdk init` uses, so we get
    # the existing `__AGENT_NAME__` substitution + force-check + template-
    # resolution behaviors for free.
    from movate.cli.init import _init_agent  # noqa: PLC0415

    _init_agent(name=agent_name, template=template, target=target_dir, force=force)

    dest = (target_dir / agent_name).resolve()

    # Auto-validation: load the scaffolded agent and confirm it
    # round-trips through the loader. Catches "template references a
    # non-existent skill" and similar issues that the bare scaffold-
    # copy path won't surface until first run.
    validation_status = _try_post_scaffold_validate(dest, skip=no_validate)

    body = (
        f"[bold]Added:[/bold]    [cyan]{agent_name}[/cyan] "
        f"[dim](from template [bold]{template}[/bold])[/dim]\n"
        f"[bold]Path:[/bold]     [cyan]{dest}[/cyan]\n"
        f"[bold]Project:[/bold]  [dim]{project_root}[/dim]\n"
    )
    if validation_status is not None:
        body += f"[bold]Validates:[/bold] {validation_status}\n"
    body += (
        f"\n[bold]Next steps:[/bold]\n"
        f"  [dim]$[/dim] [bold]mdk run ./{dest.relative_to(project_root)} "
        f"--mock '{{...}}'[/bold]\n"
        f"  [dim]$[/dim] [bold]mdk eval ./{dest.relative_to(project_root)} "
        f"--mock --gate 0.7[/bold]"
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
    # generation-style command. The `validates=` field is new: true on
    # successful auto-validate, false on failed auto-validate, skipped
    # when --no-validate was passed.
    if no_validate:
        validates_token = "skipped"
    elif validation_status and "✓" in validation_status:
        validates_token = "true"
    else:
        validates_token = "false"
    console.print(
        f"[dim]mdk_add_summary: "
        f"template={template} "
        f"name={agent_name} "
        f"project={project_root.name} "
        f"target={dest} "
        f"validates={validates_token} "
        f"ok=true[/dim]"
    )


def _try_post_scaffold_validate(agent_dir: Path, *, skip: bool) -> str | None:
    """Run ``load_agent`` on the scaffolded directory and return a
    status string for the success Panel.

    Returns ``None`` when ``skip`` is True (the Panel omits the row
    entirely). Otherwise returns ``"✓ ok"`` on success or
    ``"⚠ <error>"`` on failure. We never raise — a scaffold that
    almost-works is more useful to the operator than no scaffold at
    all; the warning surfaces the issue so they can fix it before
    running.
    """
    if skip:
        return None

    from movate.core.loader import AgentLoadError, load_agent  # noqa: PLC0415

    try:
        load_agent(agent_dir)
    except AgentLoadError as exc:
        # Truncate so long Pydantic error trees don't break Panel
        # rendering. Operators get the full version by running
        # `mdk validate` directly on the dir.
        snippet = str(exc).splitlines()[0][:120]
        return (
            f"[yellow]⚠ {snippet}[/yellow] "
            f"[dim](run `mdk validate` for full error)[/dim]"
        )
    return "[green]✓ ok[/green]"
