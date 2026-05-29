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

from movate.cli._resolve import walk_up_for_project_root
from movate.core.paths import project_state_dir
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
    "hr-policy": (
        "Employee HR question → grounded policy answer + citations + escalation flag.",
        "multi-format KB demo (MD + HTML + PDF + DOCX + images)",
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
    # Skill-demo templates — show how to wire a skill to an agent.
    "calc-agent": (
        "Expression → result + explanation. Python skill (safe AST eval).",
        "Python skill kind demo",
    ),
    "lookup-agent": (
        "User ID + question → direct answer. HTTP skill calling a REST API.",
        "HTTP skill kind demo",
    ),
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
        "hr-policy",
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
    ("HR / Recruiting", ["resume-screener", "hr-policy"]),
    ("Compliance / Ops", ["compliance-checker", "meeting-summarizer"]),
]

# Per-group color so each row's Use-case label and Name share a hue.
# Operators scan the catalog by color band rather than by re-reading
# the group column. Colors picked from Rich's standard ANSI set so
# they're visible in both dark + light terminals.
_GROUP_COLORS: dict[str, str] = {
    "Support": "cyan",
    "Sales / GTM": "green",
    "Engineering": "magenta",
    "Knowledge work": "blue",
    "HR / Recruiting": "yellow",
    "Compliance / Ops": "red",
}


def _role_description(name: str) -> tuple[str, str]:
    """Resolve (one-liner, feature) for a role catalog row.

    ADR 028 — prefers each template's ``template.yaml`` (the per-template
    metadata file) over the hardcoded :data:`_ROLE_DESCRIPTIONS` dict so
    the two never drift. Falls back to the dict when ``template.yaml``
    is missing/invalid (back-compat per CLAUDE.md rule 5 — a partial
    template still renders rather than blanking out the row). The
    ``tags`` from ``template.yaml`` show up in the Highlights column
    when present so operators see the same use-case taxonomy the
    ``mdk templates`` view uses.
    """
    from movate.templates import (  # noqa: PLC0415
        TemplateInfoLoadError,
        load_template_info,
    )

    try:
        info = load_template_info(name)
    except (TemplateInfoLoadError, ValueError):
        return _ROLE_DESCRIPTIONS.get(name, ("", ""))
    feature = ", ".join(info.tags) if info.tags else _ROLE_DESCRIPTIONS.get(name, ("", ""))[1]
    return info.description, feature


def _render_role_catalog_numbered(installed: set[str]) -> list[str]:
    """Render the role catalog with [N] numbers for addable (uninstalled) templates.

    Installed templates are shown dimmed with a ✓ and no number.
    Returns the ordered list of addable template names (index 0 = [1]).
    """
    table = Table(
        title="Role-based templates",
        title_style="bold",
        show_header=True,
        header_style="bold",
    )
    table.add_column("#", no_wrap=True, justify="right")
    table.add_column("Use case", no_wrap=True)
    table.add_column("Name", no_wrap=True)
    table.add_column("What it does")
    table.add_column("Highlights", style="dim")

    addable: list[str] = []
    last_group = ""
    counter = 0
    for group, role_names in _ROLE_GROUPS:
        for name in role_names:
            color = _GROUP_COLORS.get(group, "white")
            label = group if group != last_group else ""
            label_cell = f"[{color}]{label}[/{color}]" if label else ""
            desc, feature = _role_description(name)
            if name in installed:
                table.add_row(
                    "[dim]✓[/dim]",
                    label_cell,
                    f"[dim]{name}[/dim]",
                    f"[dim]{desc}[/dim]",
                    "",
                )
            else:
                counter += 1
                table.add_row(
                    f"[bold cyan][{counter}][/bold cyan]",
                    label_cell,
                    f"[{color}]{name}[/{color}]",
                    desc,
                    feature,
                )
                addable.append(name)
            last_group = group
    console.print(table)
    return addable


def _parse_pick_input(raw: str, max_index: int) -> list[int] | None:
    """Parse a picker input string into a list of 1-based indexes.

    Accepts any combination of:
    * Single integer: ``"3"`` → ``[3]``
    * Comma-separated: ``"1,3,5"`` → ``[1, 3, 5]``
    * Space-separated: ``"1 3 5"`` → ``[1, 3, 5]``
    * Mixed: ``"1, 3 5"`` → ``[1, 3, 5]``

    Returns ``None`` for the skip sentinel (``"s"`` or empty), or
    when any token is non-numeric or out of range. De-duplicates while
    preserving first-occurrence order so ``"1 1 3"`` adds each
    template exactly once.
    """
    text = raw.strip().lower()
    if not text or text == "s":
        return None
    # Allow commas + whitespace as separators interchangeably.
    tokens = text.replace(",", " ").split()
    seen: set[int] = set()
    out: list[int] = []
    for tok in tokens:
        if not tok.isdigit():
            return None
        idx = int(tok)
        if idx < 1 or idx > max_index:
            return None
        if idx not in seen:
            seen.add(idx)
            out.append(idx)
    return out or None


def _pick_and_add_role_agent(bin_name: str) -> None:
    """Show the numbered role catalog + add the chosen agents.

    Supports both single-pick (``3``) and multi-pick (``1 3 5`` or
    ``1,3,5``); each picked template is added in one ``mdk add``
    batch invocation so the operator gets exactly one combined
    summary panel + one post-add menu at the end.

    The downstream ``mdk add`` invocation runs its own post-add menu
    that's now scoped to role-agent management only ("Add another
    role agent" + Skip). Run / Eval / Doctor / Deploy used to live
    here but were dropped — see the comment in :func:`add` for why.
    """
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    from rich.prompt import Prompt  # noqa: PLC0415

    installed = _installed_templates()
    addable = _render_role_catalog_numbered(installed)
    if not addable:
        console.print("[dim]All role templates are already in this project.[/dim]")
        return
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return

    # Free-form Prompt.ask (no choices=…) — operators can type a
    # single number, comma- or space-separated numbers, or `s` to
    # skip. Invalid input re-prompts via the loop below so the
    # operator doesn't lose their place.
    while True:
        try:
            raw = Prompt.ask(
                "\n[bold]Pick[/bold] [dim](one or more numbers — "
                "e.g. [bold]3[/bold] or [bold]1 3 5[/bold] — "
                "or [bold]s[/bold] to skip)[/dim]",
                default="s",
                show_default=False,
            )
        except (KeyboardInterrupt, EOFError):
            return
        picks = _parse_pick_input(raw, max_index=len(addable))
        if picks is None and raw.strip().lower() in ("", "s"):
            return
        if picks:
            break
        console.print(
            f"[yellow]⚠[/yellow] couldn't parse {raw!r} — enter one or more "
            f"numbers between 1 and {len(addable)} (space or comma "
            f"separated), or [bold]s[/bold] to skip."
        )

    templates = [addable[i - 1] for i in picks]
    console.print(f"\n[dim]$ {bin_name} add {' '.join(templates)}[/dim]")
    subprocess.run([bin_name, "add", *templates], check=False)


def _run_with_sample_input(
    *,
    bin_name: str,
    template_or_name: str,
    agent_dir: Path,
    project_root: Path,
) -> None:
    """Interactive sample-input smoke test: prompt for input, run
    ``mdk run --mock``, render a Rich panel with status + latency + cost.

    Originally extracted from the auto-smoke in
    :func:`_pick_and_add_role_agent` to become a menu option. The
    post-add menu has since been scoped down to role-agent management
    only, so this helper is no longer wired into the menu — but it's
    kept as an importable helper for tests + potential future
    re-wiring (e.g. a dedicated ``mdk run-sample`` command).
    """
    import json as _json  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    from rich.prompt import Prompt  # noqa: PLC0415
    from rich.syntax import Syntax  # noqa: PLC0415

    default_payload = _first_dataset_input(agent_dir)
    console.print(
        "\n[bold]Try it now[/bold] [dim](edit or press Enter to use the dataset example)[/dim]"
    )
    try:
        user_input = Prompt.ask("  Input", default=default_payload, show_default=True)
    except (KeyboardInterrupt, EOFError):
        return

    rel = agent_dir.relative_to(project_root)
    cmd = [bin_name, "run", f"./{rel}", "--mock", user_input]
    console.print(f"\n[dim]$ {' '.join(cmd)}[/dim]")
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)

    raw_stdout = result.stdout.strip()
    raw_stderr = result.stderr.strip()
    try:
        data = _json.loads(raw_stdout)
        status = data.get("status", "unknown")
        output_data = data.get("data", {})
        metrics = data.get("metrics", {})
        run_id = data.get("run_id", "")
        latency = metrics.get("latency_ms")
        cost = metrics.get("cost_usd")

        status_color = "green" if status == "success" else "red"
        status_badge = f"[{status_color}]{status}[/{status_color}]"
        body_parts: list[str] = [f"[bold]Status:[/bold] {status_badge}"]
        if run_id:
            body_parts.append(f"[bold]Run ID:[/bold] [dim]{run_id[:8]}…[/dim]")
        if latency is not None:
            body_parts.append(f"[bold]Latency:[/bold] [dim]{latency} ms[/dim]")
        if cost is not None:
            body_parts.append(f"[bold]Cost:[/bold] [dim]${cost:.6f}[/dim]")

        console.print(
            Panel(
                "\n".join(body_parts),
                title=f"[bold]mdk run[/bold] [dim]·[/dim] {template_or_name}",
                title_align="left",
                border_style=status_color,
            )
        )
        if output_data:
            console.print("[bold cyan]Output:[/bold cyan]")
            console.print(
                Syntax(
                    _json.dumps(output_data, indent=2),
                    "json",
                    theme="monokai",
                    word_wrap=True,
                )
            )
        elif status != "success" and data.get("error"):
            err_info = data["error"]
            console.print(
                f"[red]Error:[/red] {err_info.get('type', '?')}: {err_info.get('message', '?')}"
            )
    except (_json.JSONDecodeError, AttributeError):
        if raw_stdout:
            console.print(raw_stdout)
        if raw_stderr:
            console.print(f"[dim]{raw_stderr}[/dim]")


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
    return needle in name.lower() or needle in desc.lower() or needle in feature.lower()


def _installed_templates() -> set[str]:
    """Return the set of template names already scaffolded in the
    current project.

    Reads ``./agents/<name>/agent.yaml`` for each role/core template
    name. When run outside a project (no walk-up match), returns the
    empty set — the catalog renders unchanged.

    The list rendering uses this to decorate already-installed
    templates with a ``✓ installed`` marker so operators can answer
    "what do I already have?" without leaving the catalog view.
    """
    project_root = walk_up_for_project_root()
    if project_root is None:
        return set()
    agents_dir = project_root / "agents"
    if not agents_dir.is_dir():
        return set()
    return {
        candidate.name
        for candidate in agents_dir.iterdir()
        if candidate.is_dir()
        and (candidate / "agent.yaml").is_file()
        and candidate.name in TEMPLATES
    }


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

    When run inside a project, templates already scaffolded in
    ``./agents/`` are decorated with a ``✓ installed`` marker — quick
    visual cue of project state.
    """
    installed = _installed_templates()
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
    # No column-level style on Use case + Name — color is applied per
    # row via markup so each category's label + name share a hue.
    role_table.add_column("Use case", no_wrap=True)
    role_table.add_column("Name", no_wrap=True)
    role_table.add_column("What it does")
    role_table.add_column("Highlights", style="dim")

    if role_rows:
        # Print each group once — leave subsequent rows in the same
        # group blank in the Use-case column so the visual grouping
        # reads cleanly without horizontal-rule overhead. Each row's
        # Use-case label + Name share the group's color so the eye
        # can scan by color band instead of re-reading the column.
        last_group = ""
        for group, name, desc, feature in role_rows:
            color = _GROUP_COLORS.get(group, "white")
            label = group if group != last_group else ""
            label_cell = f"[{color}]{label}[/{color}]" if label else ""
            # Append an "✓ installed" tag inside the Name cell when
            # the template is already scaffolded in this project.
            # Keeps the column count stable (alternative: separate
            # "Installed" column adds clutter for the common "outside
            # a project" case).
            tag = " [green]✓ installed[/green]" if name in installed else ""
            name_cell = f"[{color}]{name}[/{color}]{tag}"
            role_table.add_row(label_cell, name_cell, desc, feature)
            last_group = group
        console.print(role_table)
    elif search:
        console.print(f"[yellow]⚠[/yellow] no role templates match [dim]{search!r}[/dim].")

    console.print()
    console.print(
        "[dim]Use: [bold]mdk add <name>[/bold] inside a project. "
        "Batch-add: [bold]mdk add rag-qa ticket-triager[/bold]. "
        "Filter: [bold]mdk add --list --search <term>[/bold].[/dim]"
    )
    console.print(
        "[dim]Blank start: [bold]mdk add default <name>[/bold] — a minimal agent "
        "(string in → string out) to draft your own prompt. Other core templates: "
        "[bold]faq, summarizer, classifier, chatbot, extractor[/bold].[/dim]"
    )


def _suggest_template(unknown: str) -> str | None:
    """Quick suggestion for "mdk add ragqa" → "did you mean: rag-qa?"

    Two-pass: substring match (catches "ragqa" → "rag-qa" via
    normalization) then difflib edit-distance (catches harder typos
    like "rqa" → "rag-qa" or "chtbot" → "chatbot"). Substring runs
    first because it's faster and cheaper on common cases.
    """
    from difflib import get_close_matches  # noqa: PLC0415

    normalized = unknown.replace("_", "-").lower()
    # Substring pass — handles missing/extra dashes and contractions.
    substr = [t for t in TEMPLATES if normalized in t or t in normalized]
    if substr:
        return substr[0]
    # Edit-distance pass — cutoff=0.6 catches one or two typos but
    # rejects truly unrelated words (so "rag-qa" doesn't suggest itself
    # for input "deploy").
    close = get_close_matches(normalized, list(TEMPLATES.keys()), n=1, cutoff=0.6)
    return close[0] if close else None


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
    preview: bool = typer.Option(
        False,
        "--preview",
        help=(
            "Print the template's files to stdout without writing anything. "
            "Useful for answering 'what does this template look like?' "
            "without changing project state."
        ),
    ),
    remove: bool = typer.Option(
        False,
        "--remove",
        help=(
            "Remove an existing agent (counterpart to [bold]add[/bold]). "
            "Surfaces dangling references in workflows + baselines. "
            "Dry-run by default; pass [bold]--apply[/bold] to commit."
        ),
    ),
    update: bool = typer.Option(
        False,
        "--update",
        help=(
            "Refresh an existing agent against the latest template version. "
            "Shows a per-file diff. Dry-run by default; pass "
            "[bold]--apply[/bold] to commit (preserves the agent's name)."
        ),
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help=(
            "Commit changes for [bold]--remove[/bold] or [bold]--update[/bold]. "
            "Without [bold]--apply[/bold], those flags run as a dry-run "
            "preview only — no files written."
        ),
    ),
    no_skills: bool = typer.Option(
        False,
        "--no-skills",
        help=(
            "Skip auto-scaffolding of skills declared in the template's "
            "[bold]skills:[/bold] field. By default, declared skills are "
            "scaffolded under [bold]skills/<name>/[/bold] if missing."
        ),
    ),
) -> None:
    """Add one or more role-based agents to the current project.

    [bold]Examples:[/bold]

      [dim]# List the available roles[/dim]
      $ mdk add --list

      [dim]# Preview a template's files without writing anything[/dim]
      $ mdk add rag-qa --preview

      [dim]# Drop a RAG Q&A agent into ./agents/rag-qa/[/dim]
      $ mdk add rag-qa

      [dim]# Start blank — minimal agent (string in → string out), draft the prompt yourself[/dim]
      $ mdk add default my-agent

      [dim]# Batch: bootstrap a support workspace in one command[/dim]
      $ mdk add rag-qa ticket-triager email-responder

      [dim]# Remove an agent (dry-run; --apply to commit)[/dim]
      $ mdk add --remove rag-qa --apply

      [dim]# Refresh against the latest template version[/dim]
      $ mdk add --update rag-qa --apply

    [bold]See also:[/bold] [bold]mdk dev <name>[/bold] for a guided
    scaffold → edit → live-test → deploy loop (the front door for
    authoring a single agent end-to-end).
    """
    if list_only:
        # `--list` (and the `mdk add list` subcommand alias below)
        # historically just rendered the catalog and exited. Operators
        # consistently typed `mdk add --list` expecting to add an agent
        # afterward — so when stdin/stdout are both ttys, follow the
        # render with the numbered picker. Scripts piping the output
        # still get the plain table because the picker short-circuits
        # on non-tty inside `_pick_and_add_role_agent`.
        import sys  # noqa: PLC0415

        if sys.stdin.isatty() and sys.stdout.isatty() and search is None:
            from movate.cli._next_steps import mdk_bin_name  # noqa: PLC0415

            _pick_and_add_role_agent(mdk_bin_name())
        else:
            _render_list(search=search)
        return

    if preview:
        _render_preview(args)
        return

    if remove:
        _do_remove(args, apply=apply)
        return

    if update:
        _do_update(args, apply=apply)
        return

    # `mdk add context <name>` — create a shared context file in the
    # current project. Intercepted before template validation so the
    # word "context" doesn't get flagged as an unknown template name.
    # `mdk add list` — alias for `mdk add --list` so both spellings
    # work. Same TTY/script split: interactive operators get the
    # picker, pipes see the plain catalog.
    if args and args[0] == "list":
        import sys  # noqa: PLC0415

        if sys.stdin.isatty() and sys.stdout.isatty() and search is None:
            from movate.cli._next_steps import mdk_bin_name  # noqa: PLC0415

            _pick_and_add_role_agent(mdk_bin_name())
        else:
            _render_list(search=search)
        return

    if args and args[0] == "context":
        _do_add_context(args[1:])
        return

    # `mdk add kb` — scaffold kb/kb-lookup-corpus.json with an example entry.
    if args and args[0] == "kb":
        _do_add_kb(args[1:])
        return

    # `mdk add skill <name>` — scaffold a bare custom skill (not a curated template).
    if args and args[0] == "skill":
        _do_add_skill_bare(args[1:])
        return

    if not args:
        # Interactive operators get the numbered picker — same flow
        # as `mdk add --list` / `mdk add list`. Scripted callers (no
        # tty) still see the original "name required" error so the
        # exit code stays diagnostic for CI.
        import sys  # noqa: PLC0415

        if sys.stdin.isatty() and sys.stdout.isatty():
            from movate.cli._next_steps import mdk_bin_name  # noqa: PLC0415

            _pick_and_add_role_agent(mdk_bin_name())
            return
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
    if len(args) == rename_arg_count and args[0] in TEMPLATES and args[1] not in TEMPLATES:
        templates = [args[0]]
        positional_rename = args[1]
    else:
        templates = list(args)

    # --name and positional rename are mutually exclusive — both
    # being set is almost certainly a typo.
    if positional_rename and name:
        err_console.print(
            "[red]✗[/red] [bold]--name[/bold] conflicts with the positional rename form. Pick one."
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
    project_root = walk_up_for_project_root()
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
    added_names: list[str] = []
    for template in templates:
        agent_name = rename if rename else template
        _add_one(
            template=template,
            agent_name=agent_name,
            target_dir=target_dir,
            force=force,
            project_root=project_root,
            no_validate=no_validate,
            no_skills=no_skills,
        )
        added_names.append(agent_name)

    # End-of-batch hint: when more than one agent was added, surface a
    # single "Next steps for the SET" Panel pointing at the workspace-
    # level commands (ci eval, deploy). Per-agent next-steps already
    # render inside each _add_one Panel; this is the multi-agent
    # follow-up that today's customers miss.
    if len(added_names) > 1:
        _render_batch_summary(added_names, project_root=project_root)


def _add_one(
    *,
    template: str,
    agent_name: str,
    target_dir: Path,
    force: bool,
    project_root: Path,
    no_validate: bool,
    no_skills: bool = False,
    quiet: bool = False,
) -> dict[str, object] | None:
    """Scaffold a single template, render success Panel, emit summary.

    Extracted so the batch path loops over this helper without
    duplicating the Panel + summary-line logic per iteration.

    ``quiet=True`` suppresses BOTH the legacy plain-text output from
    ``_init_agent`` AND the per-agent success Panel. Used by batch
    callers (``mdk init --with-agents``) that render a single combined
    summary at the end instead of one Panel per agent. The greppable
    ``mdk_add_summary:`` line still fires for CI parsing — it's not
    visual clutter.

    Returns a per-agent summary dict (``{"name", "template", "path",
    "validates", "skills_scaffolded"}``) when ``quiet=True`` so the
    batch caller can fold each agent into its combined render. Returns
    ``None`` in verbose mode — the Panel IS the summary.
    """
    # Dispatch to the same scaffold function `mdk init` uses, so we get
    # the existing `__AGENT_NAME__` substitution + force-check + template-
    # resolution behaviors for free. Pass `quiet` through so the
    # legacy "scaffolded / Next steps" text is also suppressed.
    from movate.cli.init import _init_agent  # noqa: PLC0415

    # Always pass quiet=True — `_add_one` renders its OWN Rich Panel
    # below, which is the canonical `mdk add` output. The plain-text
    # "scaffolded / Next steps" from `_init_agent` would duplicate
    # (with WORSE next-steps: absolute paths + literal '{}' payload
    # instead of dataset-pulled examples).
    _init_agent(
        name=agent_name,
        template=template,
        target=target_dir,
        force=force,
        quiet=True,
    )

    dest = (target_dir / agent_name).resolve()

    # Stamp the template source into agent.yaml so `mdk add --update`
    # knows which template + version produced this agent. The field
    # name is underscore-prefixed so Pydantic's `extra="ignore"` on
    # AgentSpec doesn't fight us when the loader runs.
    _stamp_template_source(dest, template=template)

    # Create agents/<name>/kb/ for KB-using templates so that
    # `mdk kb ingest-all` discovers the directory automatically.
    kb_dir_created = _maybe_create_kb_dir(dest)

    # Auto-scaffold any skills the template declares. Closes the rough
    # edge where a template references `skills: [web-search]` but the
    # skill dir doesn't exist in the project — without this the agent
    # would load but fail at first tool-use call.
    skills_scaffolded: list[str] = []
    if not no_skills:
        skills_scaffolded = _maybe_scaffold_declared_skills(
            agent_dir=dest, project_root=project_root
        )

    # Contexts: two-step copy.
    #
    # Step 1 — TEMPLATE-shipped contexts. If the agent template directory
    # has a `contexts/` subdir (curated content the template author
    # wrote: rubrics, style guides, etc.), copy those into the project
    # FIRST so they exist before validation. Skips files the operator
    # already authored.
    #
    # Step 2 — declared-but-missing contexts auto-scaffold. After step 1
    # there may still be `contexts:` entries in agent.yaml that nobody
    # pre-authored. Those get empty placeholder stubs. Reuses the
    # --no-skills flag (a separate --no-contexts is overkill; the two
    # paths align in practice).
    contexts_scaffolded: list[str] = []
    if not no_skills:
        # Resolve the template directory using the same registry mapping
        # the scaffolder already consulted; cheap re-lookup.
        from movate.templates import get_template_path  # noqa: PLC0415

        try:
            template_src_dir = get_template_path(template)
        except ValueError:
            template_src_dir = None
        if template_src_dir is not None:
            contexts_scaffolded = _maybe_copy_template_contexts(
                template_dir=template_src_dir, project_root=project_root
            )
        contexts_scaffolded.extend(
            _maybe_scaffold_declared_contexts(agent_dir=dest, project_root=project_root)
        )

    # Auto-validation: load the scaffolded agent and confirm it
    # round-trips through the loader. Catches "template references a
    # non-existent skill" and similar issues that the bare scaffold-
    # copy path won't surface until first run.
    validation_status = _try_post_scaffold_validate(dest, skip=no_validate)

    # Greppable summary fields. Computed once; emitted regardless of
    # quiet mode (machine-readable, not visual clutter).
    if no_validate:
        validates_token = "skipped"
    elif validation_status and "✓" in validation_status:
        validates_token = "true"
    else:
        validates_token = "false"

    if quiet:
        # Batch caller renders one combined Panel at the end.
        # Emit the greppable line + return the per-agent info dict.
        console.print(
            f"[dim]mdk_add_summary: "
            f"template={template} "
            f"name={agent_name} "
            f"project={project_root.name} "
            f"target={dest} "
            f"validates={validates_token} "
            f"ok=true[/dim]"
        )
        return {
            "template": template,
            "name": agent_name,
            "path": dest,
            "validates": validates_token,
            "skills_scaffolded": skills_scaffolded,
            "contexts_scaffolded": contexts_scaffolded,
            "kb_dir_created": kb_dir_created,
        }

    body = (
        f"[bold]Added:[/bold]    [cyan]{agent_name}[/cyan] "
        f"[dim](from template [bold]{template}[/bold])[/dim]\n"
        f"[bold]Path:[/bold]     [cyan]{dest}[/cyan]\n"
        f"[bold]Project:[/bold]  [dim]{project_root}[/dim]\n"
    )
    if kb_dir_created:
        body += (
            f"[bold]KB dir:[/bold]   [cyan]agents/{agent_name}/kb/[/cyan] "
            f"[dim](drop PDFs/docs here, then run mdk kb ingest-all)[/dim]\n"
        )
    if skills_scaffolded:
        skills_str = ", ".join(skills_scaffolded)
        body += f"[bold]Skills:[/bold]   [cyan]{skills_str}[/cyan] (auto-scaffolded)\n"
    if contexts_scaffolded:
        ctx_str = ", ".join(contexts_scaffolded)
        body += f"[bold]Contexts:[/bold] [cyan]{ctx_str}[/cyan] (auto-scaffolded)\n"
    if validation_status is not None:
        body += f"[bold]Validates:[/bold] {validation_status}\n"
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
    console.print(
        f"[dim]mdk_add_summary: "
        f"template={template} "
        f"name={agent_name} "
        f"project={project_root.name} "
        f"target={dest} "
        f"validates={validates_token} "
        f"ok=true[/dim]"
    )

    # Static next-steps hint — always printed on the verbose success
    # path (both TTY and piped/CI), so the onboarding flow continues
    # instead of dead-ending after the success Panel. The interactive
    # picker below only fires on a TTY and is scoped to "add another
    # agent"; this block is the explicit "now run / test / validate
    # THIS agent" pointer that operators kept asking for. Scoped to the
    # success path because it lives inside `_add_one`, reached only
    # after a successful scaffold (the --list/--search/--preview/
    # --remove/--update paths all return before the scaffold loop).
    _render_next_steps_hint(agent_name=agent_name, dest=dest, project_root=project_root)

    # Interactive 'What next?' picker — TTY-gated, shared with init/
    # validate/eval. The helper renders the menu and shells out; this
    # call site just assembles the per-agent step list.
    #
    # Scope: this menu is intentionally scoped to ROLE-AGENT
    # MANAGEMENT actions only — picking another template from the
    # catalog. Run / Eval / Doctor / Deploy used to surface here too,
    # but operators in the middle of scaffolding a project rarely
    # want to context-switch into one of those flows mid-stream; the
    # noise drowned out the natural next step (add another agent).
    # Those commands are still one keystroke away via `mdk run`,
    # `mdk eval`, `mdk doctor`, `mdk deploy` from the shell, and
    # `mdk menu` surfaces them as workspace-level actions once the
    # project is set up.
    from movate.cli._next_steps import NextStep, mdk_bin_name, prompt_next_step  # noqa: PLC0415

    bin_name = mdk_bin_name()
    steps: list[NextStep] = []
    if kb_dir_created:
        steps.append(
            NextStep(
                label=f"Ingest KB docs for {agent_name!r} (drop files in agents/{agent_name}/kb/)",
                command=f"{bin_name} kb ingest-all --dry-run",
                argv=[bin_name, "kb", "ingest-all", "--dry-run"],
            )
        )
    steps.append(
        NextStep(
            label="Add another role agent",
            command=f"{bin_name} add --list",
            argv=[bin_name, "add", "--list"],
            callback=lambda: _pick_and_add_role_agent(bin_name),
        )
    )
    prompt_next_step(console=console, steps=steps)
    return None


def _render_next_steps_hint(*, agent_name: str, dest: Path, project_root: Path) -> None:
    """Print a short "next steps" hint after a successful single-agent add.

    Additive, human-output only: continues the onboarding flow instead
    of dead-ending after the success Panel. Points at the three
    natural follow-ups on the *just-added* agent — live-test loop,
    one-shot mock run, project validate.

    The run path is rendered project-relative (``./agents/<name>``)
    when ``dest`` is under ``project_root`` — the form `mdk run`
    accepts — falling back to the absolute ``dest`` otherwise (e.g. a
    ``--target`` outside the project tree).
    """
    try:
        rel = dest.relative_to(project_root)
        agent_path = f"./{rel}"
    except ValueError:
        agent_path = str(dest)

    console.print(
        f"\n[green]✓[/green] added [cyan]{agent_name}[/cyan]\n"
        f"  [bold]next:[/bold]  [bold]mdk dev {agent_name}[/bold]"
        f"                      [dim]# live-test loop (edit → see output)[/dim]\n"
        f'         [bold]mdk run {agent_path} "<input>" --mock[/bold]'
        f"   [dim]# one-shot local run[/dim]\n"
        f"         [bold]mdk validate[/bold]"
        f"                        [dim]# check the project[/dim]"
    )


def _render_batch_summary(added_names: list[str], *, project_root: Path) -> None:
    """Render the end-of-batch summary Panel after a multi-template add.

    Per-agent Panels handle the "what's next for THIS agent" question.
    This Panel answers the multi-agent follow-up: "what's next for the
    workspace?" — gate every agent with `mdk eval --all`, deploy them all
    with `mdk deploy`, etc.

    Post-PR-#92 the canonical commands are ``mdk validate --all`` and
    ``mdk eval --all``; the legacy ``mdk ci eval`` was a placeholder
    that never shipped. Post-PR-#88 the active-target is resolvable
    from user config instead of hard-coding ``prod`` (which most
    operators don't have configured).
    """
    # Resolve the active target if one is configured; fall back to
    # the literal `<your-target>` so the line still parses and the
    # operator sees they need to register one.
    try:
        from movate.core.user_config import load_user_config  # noqa: PLC0415

        active = load_user_config().active or "<your-target>"
    except Exception:  # best-effort hint; never block the Panel
        active = "<your-target>"

    names_str = ", ".join(f"[cyan]{n}[/cyan]" for n in added_names)
    body = (
        f"[bold]Added {len(added_names)} agents:[/bold] {names_str}\n"
        f"[bold]Project:[/bold] [dim]{project_root}[/dim]"
    )
    console.print(
        Panel(
            body,
            title=f"[green]✓[/green] Workspace ready ({len(added_names)} agents)",
            title_align="left",
            border_style="green",
        )
    )

    # Interactive picker — the single next-steps surface for the
    # batch-add path (no static block inside the Panel; the picker
    # renders the same list in both TTY and non-TTY modes).
    #
    # Domain-scoped (2026-05-19 operator feedback): ``mdk add``'s menu
    # only surfaces the IMMEDIATE-next actions on what we just
    # scaffolded — validate the bundle, doctor-check the agent.
    # Removed eval + deploy: those are downstream concerns owned by
    # their own commands' menus. ``active``  (the project target
    # name) is unused now that deploy is gone — kept in the function
    # signature for caller compat.
    from movate.cli._next_steps import NextStep, mdk_bin_name, prompt_next_step  # noqa: PLC0415

    _ = active  # silence unused-variable in this scoped-down menu
    bin_name = mdk_bin_name()

    # Check whether any of the added agents created a kb/ dir. If so,
    # surface the ingest-all step before the validate step so the operator
    # sees the KB hint before running validation.
    any_kb_dir = any((project_root / "agents" / name / "kb").is_dir() for name in added_names)
    batch_steps: list[NextStep] = []
    if any_kb_dir:
        batch_steps.append(
            NextStep(
                label="Ingest KB docs (drop files in agents/<name>/kb/ first)",
                command=f"{bin_name} kb ingest-all --dry-run",
                argv=[bin_name, "kb", "ingest-all", "--dry-run"],
            )
        )
    batch_steps.extend(
        [
            NextStep(
                label="Validate all agents",
                command=f"{bin_name} validate --all",
                argv=[bin_name, "validate", "--all"],
            ),
            NextStep(
                label=f"Health-check {added_names[0]!r}",
                command=f"{bin_name} doctor agent {added_names[0]}",
                argv=[bin_name, "doctor", "agent", added_names[0]],
            ),
        ]
    )
    prompt_next_step(
        console=console,
        steps=batch_steps,
    )


def _maybe_create_kb_dir(agent_dir: Path) -> bool:
    """Create ``<agent_dir>/kb/`` when the agent template uses a kb-vector skill.

    Reads ``agent.yaml`` directly (without calling ``load_agent``, which would
    fail before skills are scaffolded) and checks whether any declared skill
    name contains ``"kb-vector"``.  Creates ``<agent_dir>/kb/`` and returns
    ``True`` when the directory is freshly created; returns ``False`` when the
    agent has no kb-vector skill or when the YAML can't be read.

    Used by ``_add_one`` so that ``mdk kb ingest-all`` (which discovers
    ``agents/*/kb/``) finds the directory on the very first run.
    """
    yaml_path = agent_dir / "agent.yaml"
    if not yaml_path.is_file():
        return False

    import yaml as _yaml  # noqa: PLC0415

    try:
        data = _yaml.safe_load(yaml_path.read_text()) or {}
    except Exception:
        return False

    declared_skills: list[str] = data.get("skills") or []
    if not any("kb-vector" in skill_name.lower() for skill_name in declared_skills):
        return False

    (agent_dir / "kb").mkdir(exist_ok=True)
    return True


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
        return f"[yellow]⚠ {snippet}[/yellow] [dim](run `mdk validate` for full error)[/dim]"
    return "[green]✓ ok[/green]"


# ---------------------------------------------------------------------------
# Bundle C: preview / remove / update / template-source / skills-autoscaffold
# ---------------------------------------------------------------------------

# Marker written as a YAML comment to agent.yaml so `mdk add --update`
# can identify which template + MDK version produced this agent. Stored
# as a comment (not a parsed field) so AgentSpec's `extra="forbid"`
# Pydantic config doesn't reject the marker at load time.
_TEMPLATE_SOURCE_COMMENT_PREFIX = "# _template_source:"


def _first_dataset_input(agent_dir: Path) -> str:
    """Return a copy-pasteable example payload from the agent's first
    eval-dataset row. Falls back to literal ``'{...}'`` when the agent
    ships without a dataset.

    Used by the success Panel's "Next steps" so operators see a real
    payload shape instead of an opaque placeholder. ``mdk run --mock
    '<payload>'`` becomes copy-paste-ready.

    Best-effort: any read / JSON failure falls back silently. Never
    raises — a malformed dataset shouldn't block the success Panel.
    """
    import json  # noqa: PLC0415

    dataset = agent_dir / "evals" / "dataset.jsonl"
    if not dataset.is_file():
        return "{...}"
    try:
        first_line = dataset.read_text().splitlines()[0]
        row = json.loads(first_line)
        payload = row.get("input")
        if payload is None:
            return "{...}"
        # Compact JSON (no spaces around separators) keeps the line
        # short enough to fit in a Panel without wrapping.
        return json.dumps(payload, separators=(",", ":"))
    except (OSError, IndexError, json.JSONDecodeError):
        return "{...}"


def _stamp_template_source(agent_dir: Path, *, template: str) -> None:
    """Append a `# _template_source: <template>@<mdk-version>` comment
    line to agent.yaml.

    Used by `mdk add --update` to identify which template version
    produced the agent. The line is a YAML comment (not a parsed field)
    so AgentSpec's `extra="forbid"` validation doesn't reject the
    marker — yaml.safe_load ignores it, but `_read_template_source`
    can pick it up by reading the file line-by-line.
    """
    from movate import __version__  # noqa: PLC0415

    yaml_path = agent_dir / "agent.yaml"
    if not yaml_path.is_file():
        return
    source = f"{template}@{__version__}"
    contents = yaml_path.read_text()
    # If the marker already exists (re-run / --force), skip silently.
    if _TEMPLATE_SOURCE_COMMENT_PREFIX in contents:
        return
    suffix = "\n" if contents.endswith("\n") else "\n\n"
    yaml_path.write_text(
        contents
        + suffix
        + "# Stamped by `mdk add` — used by `mdk add --update` to know\n"
        + "# which template version produced this agent. Safe to delete\n"
        + "# if you want to lock the agent out of automatic updates.\n"
        + f"{_TEMPLATE_SOURCE_COMMENT_PREFIX} {source}\n"
    )


def _read_template_source(agent_dir: Path) -> tuple[str, str] | None:
    """Read `# _template_source: <template>@<mdk-version>` from agent.yaml.

    Returns (template_name, mdk_version) on success, None if the
    marker is missing or malformed. `mdk add --update` uses this to
    pick the right template for the diff.
    """
    yaml_path = agent_dir / "agent.yaml"
    if not yaml_path.is_file():
        return None
    prefix = _TEMPLATE_SOURCE_COMMENT_PREFIX
    for line in yaml_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        value = stripped[len(prefix) :].strip()
        if "@" not in value:
            return None
        template_name, version = value.split("@", 1)
        return template_name.strip(), version.strip()
    return None


def _maybe_scaffold_declared_skills(*, agent_dir: Path, project_root: Path) -> list[str]:
    """Auto-scaffold any skills declared in the agent's `skills:` field
    that don't yet exist in `<project>/skills/<name>/`.

    Returns the list of skill names that were freshly scaffolded.
    Failures (skill scaffold raises, skill dir already partially
    populated) log a warning and continue — the agent itself is
    already on disk and useful even if a skill stub failed.
    """
    yaml_path = agent_dir / "agent.yaml"
    if not yaml_path.is_file():
        return []

    import yaml as _yaml  # noqa: PLC0415

    try:
        data = _yaml.safe_load(yaml_path.read_text()) or {}
    except _yaml.YAMLError:
        return []

    declared_skills = data.get("skills") or []
    if not declared_skills:
        return []

    scaffolded: list[str] = []
    skills_dir = project_root / "skills"
    for skill_name in declared_skills:
        skill_path = skills_dir / skill_name
        if skill_path.exists():
            continue
        try:
            _scaffold_one_skill(name=skill_name, project_root=project_root)
            scaffolded.append(skill_name)
        except Exception as exc:
            err_console.print(
                f"[yellow]⚠[/yellow] could not auto-scaffold skill [bold]{skill_name}[/bold]: {exc}"
            )
    return scaffolded


def _maybe_copy_template_contexts(*, template_dir: Path, project_root: Path) -> list[str]:
    """Copy any hand-written `contexts/*.md` shipped inside an agent
    template into the project's `<project>/contexts/` directory.

    Mechanics: each role-agent template directory MAY ship a
    ``contexts/`` subdir alongside its ``agent.yaml`` /
    ``prompt.md`` / ``schema/`` / ``evals/``. Files there are
    template-author-curated content (rubrics, style guides,
    domain primers) meant to ship with the agent — distinct from
    the empty placeholders that ``_maybe_scaffold_declared_contexts``
    produces when an operator declares a context that nobody
    pre-authored.

    Returns the list of context names that were freshly copied
    (skipping any that already exist in the project — operators
    shouldn't have hand-written content silently overwritten by
    a re-scaffold). Empty list if the template ships no contexts.

    Names returned are the file basenames minus `.md`, matching
    the convention `context_loader.py` uses for lookups.
    """
    src_contexts = template_dir / "contexts"
    if not src_contexts.is_dir():
        return []

    project_contexts = project_root / "contexts"
    project_contexts.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    for src in sorted(src_contexts.glob("*.md")):
        dest = project_contexts / src.name
        if dest.exists():
            # Don't overwrite — operator may have customized.
            continue
        try:
            dest.write_text(src.read_text())
            copied.append(src.stem)
        except OSError as exc:
            err_console.print(
                f"[yellow]⚠[/yellow] could not copy template context [bold]{src.name}[/bold]: {exc}"
            )
    return copied


def _maybe_scaffold_declared_contexts(*, agent_dir: Path, project_root: Path) -> list[str]:
    """Auto-scaffold any contexts declared in the agent's ``contexts:``
    field that don't yet exist in ``<project>/contexts/<name>.md``.

    Mirrors :func:`_maybe_scaffold_declared_skills`. Returns the list of
    context names that were freshly scaffolded. Empty list if no
    contexts declared, the YAML can't be parsed, or every context
    already exists.

    Contexts have no schema — they're plain Markdown files. The
    scaffold is therefore a deliberately-minimal placeholder Markdown
    blob with comments explaining what to fill in. Operators see
    immediately what shape is expected without having to find docs.

    Failures (write-perm denied, etc.) log a warning and continue.
    The agent itself is on disk and useful even if a context stub
    failed to land.
    """
    yaml_path = agent_dir / "agent.yaml"
    if not yaml_path.is_file():
        return []

    import yaml as _yaml  # noqa: PLC0415

    try:
        data = _yaml.safe_load(yaml_path.read_text()) or {}
    except _yaml.YAMLError:
        return []

    declared_contexts = data.get("contexts") or []
    if not declared_contexts:
        return []

    scaffolded: list[str] = []
    contexts_dir = project_root / "contexts"
    for context_name in declared_contexts:
        context_path = contexts_dir / f"{context_name}.md"
        if context_path.exists():
            continue
        try:
            _scaffold_one_context(name=context_name, project_root=project_root)
            scaffolded.append(context_name)
        except Exception as exc:
            err_console.print(
                f"[yellow]⚠[/yellow] could not auto-scaffold context "
                f"[bold]{context_name}[/bold]: {exc}"
            )
    return scaffolded


# Placeholder Markdown for auto-scaffolded contexts. Intentionally
# brief — operators see the shape (`# H1` header, `## Purpose` /
# `## Content` sections, dim TODO markers) without having to grep
# docs for the convention.
_CONTEXT_PLACEHOLDER = """\
# {name}

<!--
This context was auto-scaffolded by `mdk add` because an agent's
`contexts:` field referenced [bold]{name}[/bold] but the file didn't
exist yet. Contexts are reusable Markdown that gets prepended to the
agent's prompt at build time, so multiple agents can share consistent
style guides, safety policies, domain primers, etc.

Replace this placeholder with the actual content you want injected.
The whole file body is included verbatim — there's no schema, no
Jinja, no YAML envelope. Just write what you want the model to read.
-->

## Purpose

TODO: Describe what this context provides + which agents should
reference it.

## Content

TODO: Write the actual Markdown content here. This is what gets
prepended to the agent's prompt.
"""


def _scaffold_one_context(*, name: str, project_root: Path) -> None:
    """Write an empty placeholder ``<project>/contexts/<name>.md``.

    Contexts have no template directory (they're single files), so
    this just materializes :data:`_CONTEXT_PLACEHOLDER` with the name
    stamped in. The placeholder has the operator-facing TODO
    structure that makes "what goes here?" answerable without docs.
    """
    contexts_dir = project_root / "contexts"
    contexts_dir.mkdir(parents=True, exist_ok=True)
    dest = contexts_dir / f"{name}.md"
    dest.write_text(_CONTEXT_PLACEHOLDER.format(name=name))


def _scaffold_one_skill(*, name: str, project_root: Path) -> None:
    """Dispatch to the skill-scaffolding code path used by
    `mdk skills scaffold <name>`.

    Wrapped here so the auto-scaffold path can reuse the same
    canonical scaffold logic without invoking the CLI command (which
    would re-render its own Panel etc.).

    The skill-template lookup tries a per-name match first
    (``SKILL_TEMPLATES["web-search"] = "skill_web_search"`` for a
    skill literally named ``web-search``), falling back to the
    ``default`` echo template otherwise. That lets the curated
    skills (web-search, lint-runner, kb-lookup) ship REAL impls
    that an operator can `mdk skills run` immediately, while ad-hoc
    skills still get a working echo stub.
    """
    import shutil  # noqa: PLC0415

    from movate.templates import SKILL_TEMPLATES, TEMPLATES_DIR  # noqa: PLC0415

    # Per-name lookup first; default fallback otherwise.
    template_subdir = SKILL_TEMPLATES.get(name) or SKILL_TEMPLATES["default"]
    src = TEMPLATES_DIR / template_subdir
    if not src.is_dir():
        return

    dest = project_root / "skills" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)

    # Stamp the skill's name across every file that uses the
    # `__SKILL_NAME__` placeholder. skill.yaml is the canonical
    # required substitution; impl.py + README.md docstrings reference
    # the same token. A single walk catches all of them and skips
    # binary files cleanly.
    for path in dest.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        if "__SKILL_NAME__" not in text:
            continue
        path.write_text(text.replace("__SKILL_NAME__", name))


def _render_preview(args: list[str]) -> None:
    """Print template files to stdout without writing anything.

    Single-template form expected (args=[template]). Multi-template
    preview would just be redundant — operators pipe one template at
    a time when answering "what does this look like?"
    """
    if not args:
        err_console.print("[red]✗[/red] [bold]--preview[/bold] needs a template name.")
        raise typer.Exit(code=2)
    if len(args) > 1:
        err_console.print("[red]✗[/red] [bold]--preview[/bold] takes one template at a time.")
        raise typer.Exit(code=2)

    template = args[0]
    if template not in TEMPLATES:
        suggestion = _suggest_template(template)
        hint = (
            f"\n[dim]did you mean [bold]{suggestion}[/bold]?[/dim]"
            if suggestion
            else f"\n[dim]available: {', '.join(list_templates())}[/dim]"
        )
        err_console.print(f"[red]✗[/red] unknown template {template!r}.{hint}")
        raise typer.Exit(code=2)

    from movate.templates import get_template_path  # noqa: PLC0415

    template_dir = get_template_path(template)
    console.print(
        Panel(
            f"Preview of template [bold]{template}[/bold] [dim](from {template_dir})[/dim]",
            border_style="yellow",
        )
    )

    # Walk the template's files. Skip dot-files and __pycache__.
    files = sorted(
        f for f in template_dir.rglob("*") if f.is_file() and "__pycache__" not in f.parts
    )
    for file_path in files:
        rel = file_path.relative_to(template_dir)
        try:
            body = file_path.read_text()
        except (UnicodeDecodeError, OSError):
            console.print(f"\n[bold]{rel}[/bold] [dim](binary, skipped)[/dim]")
            continue
        console.print(f"\n[bold cyan]── {rel} ──[/bold cyan]")
        console.print(f"[dim]{body}[/dim]")

    console.print()
    console.print(f"[dim]To scaffold this template, run [bold]mdk add {template}[/bold].[/dim]")


def _do_add_context(args: list[str]) -> None:
    """Create a new shared context file in the current project.

    Called when the operator runs ``mdk add context <name>``. Creates
    ``<project>/contexts/<name>.md`` with a placeholder body and prints
    a success panel pointing at the next steps.

    Errors with a clear message if:
    * No context name is provided.
    * Not inside a movate project.
    * The context already exists (won't silently overwrite).
    """
    if not args:
        err_console.print(
            "[red]✗[/red] context name required. Usage: [bold]mdk add context <name>[/bold]"
        )
        raise typer.Exit(code=2)

    context_name = args[0]
    project_root = walk_up_for_project_root()
    if project_root is None:
        err_console.print(
            "[red]✗[/red] not inside a movate project. "
            "[dim]Run [bold]mdk init --project <name>[/bold] first.[/dim]"
        )
        raise typer.Exit(code=2)

    contexts_dir = project_root / "contexts"
    dest = contexts_dir / f"{context_name}.md"
    if dest.exists():
        err_console.print(
            f"[red]✗[/red] context [bold]{context_name}[/bold] already exists "
            f"at [dim]{dest}[/dim]. "
            "Edit it directly, or delete it first."
        )
        raise typer.Exit(code=2)

    _scaffold_one_context(name=context_name, project_root=project_root)

    console.print(
        Panel(
            f"[bold]Context:[/bold] [cyan]{context_name}[/cyan]\n"
            f"[bold]Path:[/bold]    [cyan]{dest}[/cyan]\n"
            f"[bold]Project:[/bold] [dim]{project_root}[/dim]\n\n"
            f"[dim]Edit the file to add the content that will be prepended to "
            f"your agent's prompt. Then reference it in [bold]agent.yaml[/bold]:\n\n"
            f"  contexts:\n"
            f"    - {context_name}[/dim]",
            title=f"[green]✓[/green] Created context {context_name!r}",
            title_align="left",
            border_style="green",
        )
    )
    console.print(
        f"[dim]Tip: wire it into an agent in one step with "
        f"[bold]mdk contexts attach {context_name} --agent <agent>[/bold] — or use "
        f"[bold]mdk contexts create[/bold] (its [bold]--agent[/bold] auto-attaches).[/dim]"
    )
    console.print(
        f"[dim]mdk_add_context_summary: "
        f"name={context_name} "
        f"project={project_root.name} "
        f"path={dest} "
        f"ok=true[/dim]"
    )


_KB_CORPUS_EXAMPLE = """\
[
  {
    "id": "KB-001",
    "category": "general",
    "title": "Example entry — replace with your content",
    "symptom": "Describe the problem or trigger phrase here.",
    "resolution": "Describe the answer or resolution here.",
    "tags": ["example"]
  }
]
"""

_SKILL_YAML_TEMPLATE = """\
api_version: movate/v1
kind: Skill
name: {name}
version: 0.1.0
description: ""
schema:
  input:
    query: string
  output:
    result: string
implementation:
  kind: python
  entry: {module}.impl:run
side_effects: read-only
"""

_SKILL_IMPL_TEMPLATE = """\
from __future__ import annotations

from typing import Any


async def run(input: dict[str, Any], ctx: Any) -> dict[str, Any]:
    \"\"\"Skill entry point. Replace with your implementation.

    Args:
        input: validated against the skill's input schema (query: string).
        ctx:   SkillExecutionContext — call_ms_budget, trace_id, etc.

    Returns:
        dict matching the skill's output schema (result: string).
    \"\"\"
    return {"result": input.get("query", "")}
"""


def _do_add_kb(args: list[str]) -> None:
    """Scaffold ``kb/kb-lookup-corpus.json`` with one example entry.

    Called when the operator runs ``mdk add kb``. Creates the file at
    ``<project>/kb/kb-lookup-corpus.json`` and prints a panel with the
    required field schema.

    Errors if the file already exists (won't silently overwrite).
    """
    project_root = walk_up_for_project_root()
    if project_root is None:
        err_console.print(
            "[red]✗[/red] not inside a movate project. "
            "[dim]Run [bold]mdk init --project <name>[/bold] first.[/dim]"
        )
        raise typer.Exit(code=2)

    kb_dir = project_root / "kb"
    dest = kb_dir / "kb-lookup-corpus.json"
    if dest.exists():
        err_console.print(
            f"[red]✗[/red] [bold]kb/kb-lookup-corpus.json[/bold] already exists "
            f"at [dim]{dest}[/dim]. "
            "Edit it directly, or run [bold]mdk knowledge validate[/bold] to check coverage."
        )
        raise typer.Exit(code=2)

    kb_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(_KB_CORPUS_EXAMPLE)

    console.print(
        Panel(
            f"[bold]Path:[/bold]    [cyan]{dest}[/cyan]\n"
            f"[bold]Project:[/bold] [dim]{project_root}[/dim]\n\n"
            "[dim]Each entry requires:\n"
            "  [bold]id[/bold]         unique identifier\n"
            "  [bold]title[/bold]      short label (used for search scoring)\n"
            "  [bold]symptom[/bold]    trigger phrase / problem description\n"
            "  [bold]resolution[/bold] the answer (scored by kb-lookup at runtime)\n"
            "  [bold]tags[/bold]       list of string labels\n\n"
            "Check coverage with: [bold]mdk knowledge validate[/bold][/dim]",
            title="[green]✓[/green] Created kb/kb-lookup-corpus.json",
            title_align="left",
            border_style="green",
        )
    )
    console.print(f"[dim]mdk_add_kb_summary: project={project_root.name} path={dest} ok=true[/dim]")


def _do_add_skill_bare(args: list[str]) -> None:
    """Scaffold a bare custom skill at ``skills/<name>/``.

    Called when the operator runs ``mdk add skill <name>``. Creates
    a minimal valid ``skill.yaml`` + stub ``impl.py``. The skill is
    NOT wired to any curated template — it's a blank slate for a
    custom implementation.

    Errors if the skill directory already exists or the name is invalid.
    """
    import re as _re  # noqa: PLC0415

    if not args:
        err_console.print(
            "[red]✗[/red] skill name required. Usage: [bold]mdk add skill <name>[/bold]"
        )
        raise typer.Exit(code=2)

    name = args[0]
    if not _re.fullmatch(r"[a-z][a-z0-9-]*", name):
        err_console.print(
            f"[red]✗[/red] skill name [bold]{name!r}[/bold] is invalid. "
            "Must be lowercase alphanumeric with hyphens (e.g. [bold]my-skill[/bold])."
        )
        raise typer.Exit(code=2)

    project_root = walk_up_for_project_root()
    if project_root is None:
        err_console.print(
            "[red]✗[/red] not inside a movate project. "
            "[dim]Run [bold]mdk init --project <name>[/bold] first.[/dim]"
        )
        raise typer.Exit(code=2)

    dest = project_root / "skills" / name
    if dest.exists():
        err_console.print(
            f"[red]✗[/red] skill [bold]{name}[/bold] already exists "
            f"at [dim]{dest}[/dim]. "
            "Edit it directly, or delete the directory to start fresh."
        )
        raise typer.Exit(code=2)

    dest.mkdir(parents=True)
    module = name.replace("-", "_")
    (dest / "skill.yaml").write_text(_SKILL_YAML_TEMPLATE.format(name=name, module=module))
    (dest / "impl.py").write_text(_SKILL_IMPL_TEMPLATE)

    console.print(
        Panel(
            f"[bold]Skill:[/bold]   [cyan]{name}[/cyan]\n"
            f"[bold]Path:[/bold]    [cyan]{dest}[/cyan]\n"
            f"[bold]Project:[/bold] [dim]{project_root}[/dim]\n\n"
            f"[dim]Edit [bold]skills/{name}/impl.py[/bold] to implement the skill, "
            f"then declare it in [bold]agent.yaml[/bold]:\n\n"
            f"  skills:\n"
            f"    - {name}[/dim]",
            title=f"[green]✓[/green] Created skill {name!r}",
            title_align="left",
            border_style="green",
        )
    )
    console.print(
        f"[dim]mdk_add_skill_summary: "
        f"name={name} "
        f"project={project_root.name} "
        f"path={dest} "
        f"ok=true[/dim]"
    )


def _do_remove(args: list[str], *, apply: bool) -> None:
    """Remove an existing agent (dry-run by default).

    Surfaces dangling references in workflows + baselines so operators
    see what's about to be orphaned. With --apply, deletes the agent
    directory; references aren't auto-cleaned (operator decides what
    to do with each).
    """
    if not args or len(args) > 1:
        err_console.print("[red]✗[/red] [bold]--remove[/bold] needs exactly one agent name.")
        raise typer.Exit(code=2)

    agent_name = args[0]
    project_root = walk_up_for_project_root()
    if project_root is None:
        err_console.print("[red]✗[/red] not inside a movate project — nothing to remove from.")
        raise typer.Exit(code=2)

    # Locate the agent. Try the default ./agents/<name>/ first.
    candidates = [
        project_root / "agents" / agent_name,
        project_root / agent_name,
    ]
    agent_dir = next((c for c in candidates if c.is_dir()), None)
    if agent_dir is None:
        err_console.print(
            f"[red]✗[/red] agent [bold]{agent_name}[/bold] not found under "
            f"{project_root}/agents/ or {project_root}/."
        )
        raise typer.Exit(code=2)

    # Scan for dangling references.
    references: list[str] = []

    # Workflow references.
    workflows_dir = project_root / "workflows"
    if workflows_dir.is_dir():
        for wf_yaml in workflows_dir.rglob("workflow.yaml"):
            try:
                contents = wf_yaml.read_text()
            except OSError:
                continue
            if agent_name in contents:
                references.append(f"workflow: {wf_yaml.relative_to(project_root)}")

    # Eval baseline.
    baseline_candidates = [
        project_state_dir(agent_dir) / "baseline.json",
        project_state_dir(project_root) / agent_name / "baseline.json",
    ]
    for baseline in baseline_candidates:
        if baseline.is_file():
            references.append(f"baseline: {baseline.relative_to(project_root)}")

    body = (
        f"[bold]Agent:[/bold]   [cyan]{agent_name}[/cyan]\n"
        f"[bold]Path:[/bold]    [cyan]{agent_dir}[/cyan]\n"
        f"[bold]Project:[/bold] [dim]{project_root}[/dim]\n"
    )
    if references:
        body += "\n[bold]Dangling references after removal:[/bold]\n"
        for ref in references:
            body += f"  [yellow]⚠[/yellow] {ref}\n"
        body += (
            "\n[dim]These won't be auto-cleaned. Decide per reference: edit / delete / leave.[/dim]"
        )
    else:
        body += "\n[green]✓[/green] no dangling references."

    if not apply:
        console.print(
            Panel(
                body + "\n\n[dim]Dry-run. Re-run with [bold]--apply[/bold] to "
                "actually delete.[/dim]",
                title="[yellow]Would remove[/yellow]",
                border_style="yellow",
            )
        )
        console.print(
            f"[dim]mdk_remove_summary: "
            f"name={agent_name} dry_run=true "
            f"refs={len(references)} ok=true[/dim]"
        )
        return

    # Commit: shutil.rmtree the agent dir.
    import shutil  # noqa: PLC0415

    shutil.rmtree(agent_dir)
    console.print(
        Panel(
            body + "\n\n[green]✓ removed.[/green]",
            title="[red]Removed[/red]",
            border_style="red",
        )
    )
    console.print(
        f"[dim]mdk_remove_summary: "
        f"name={agent_name} dry_run=false "
        f"refs={len(references)} ok=true[/dim]"
    )


def _do_update(args: list[str], *, apply: bool) -> None:
    """Refresh an existing agent against the latest template version.

    Reads `_template_source` from the agent's agent.yaml, diffs each
    template file against the agent's current version, and surfaces
    per-file drift. With --apply, overwrites the agent's files with
    the current template (preserving the agent's name).
    """
    if not args or len(args) > 1:
        err_console.print("[red]✗[/red] [bold]--update[/bold] needs exactly one agent name.")
        raise typer.Exit(code=2)

    agent_name = args[0]
    project_root = walk_up_for_project_root()
    if project_root is None:
        err_console.print("[red]✗[/red] not inside a movate project — nothing to update.")
        raise typer.Exit(code=2)

    candidates = [project_root / "agents" / agent_name, project_root / agent_name]
    agent_dir = next((c for c in candidates if c.is_dir()), None)
    if agent_dir is None:
        err_console.print(f"[red]✗[/red] agent [bold]{agent_name}[/bold] not found.")
        raise typer.Exit(code=2)

    source = _read_template_source(agent_dir)
    if source is None:
        err_console.print(
            f"[red]✗[/red] agent [bold]{agent_name}[/bold] has no "
            f"[bold]_template_source[/bold] marker comment — can't determine "
            f"which template to compare against. "
            f"[dim](Agents scaffolded before bundle C ship don't have "
            f"this marker; add it manually as "
            f"`{_TEMPLATE_SOURCE_COMMENT_PREFIX} <template>@<version>` "
            f"on a line in agent.yaml.)[/dim]"
        )
        raise typer.Exit(code=2)

    template_name, agent_template_version = source
    if template_name not in TEMPLATES:
        err_console.print(
            f"[red]✗[/red] agent claims template [bold]{template_name}[/bold] "
            f"but it's not in the current TEMPLATES registry."
        )
        raise typer.Exit(code=2)

    from movate import __version__ as current_mdk_version  # noqa: PLC0415
    from movate.templates import get_template_path  # noqa: PLC0415

    template_dir = get_template_path(template_name)

    # Diff every template file against the agent's current files.
    import difflib  # noqa: PLC0415

    drift: list[tuple[Path, str]] = []  # (relative_path, diff_text)
    template_files = sorted(
        f for f in template_dir.rglob("*") if f.is_file() and "__pycache__" not in f.parts
    )
    for tmpl_file in template_files:
        rel = tmpl_file.relative_to(template_dir)
        agent_file = agent_dir / rel
        try:
            tmpl_contents = tmpl_file.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if not agent_file.is_file():
            drift.append((rel, "[new file in template — agent doesn't have it]"))
            continue
        try:
            agent_contents = agent_file.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        # Substitute __AGENT_NAME__ in the template before diffing so
        # the rename isn't reported as drift.
        tmpl_normalized = tmpl_contents.replace("__AGENT_NAME__", agent_name)
        # Strip the _template_source marker + its narration comments
        # from the agent side (they're not in the template; only
        # appear in scaffolded agent.yaml files).
        agent_normalized_lines: list[str] = []
        skip_narration = False
        for ln in agent_contents.splitlines():
            stripped = ln.strip()
            if stripped.startswith("# Stamped by `mdk add`"):
                skip_narration = True
                continue
            if skip_narration and stripped.startswith("#"):
                # The 2-line narration block continues until either
                # the marker line or an empty/non-comment line.
                if stripped.startswith(_TEMPLATE_SOURCE_COMMENT_PREFIX):
                    skip_narration = False
                continue
            if stripped.startswith(_TEMPLATE_SOURCE_COMMENT_PREFIX):
                skip_narration = False
                continue
            agent_normalized_lines.append(ln)
        agent_normalized = "\n".join(agent_normalized_lines)
        if tmpl_normalized.strip() == agent_normalized.strip():
            continue
        diff = "".join(
            difflib.unified_diff(
                agent_normalized.splitlines(keepends=True),
                tmpl_normalized.splitlines(keepends=True),
                fromfile=f"agent/{rel}",
                tofile=f"template/{rel}",
                n=2,
            )
        )
        drift.append((rel, diff[:2000]))

    if not drift:
        console.print(
            Panel(
                f"[bold]Agent:[/bold]    [cyan]{agent_name}[/cyan]\n"
                f"[bold]Template:[/bold] [cyan]{template_name}[/cyan] "
                f"(stamped from MDK {agent_template_version}; "
                f"current MDK {current_mdk_version})\n"
                f"\n[green]✓ no drift — agent matches the current template "
                f"version.[/green]",
                title="[green]Up to date[/green]",
                border_style="green",
            )
        )
        console.print(
            f"[dim]mdk_update_summary: "
            f"name={agent_name} template={template_name} "
            f"drifted_files=0 dry_run={str(not apply).lower()} ok=true[/dim]"
        )
        return

    # Render the drift.
    console.print(
        Panel(
            f"[bold]Agent:[/bold]    [cyan]{agent_name}[/cyan]\n"
            f"[bold]Template:[/bold] [cyan]{template_name}[/cyan] "
            f"(stamped from MDK {agent_template_version}; "
            f"current MDK {current_mdk_version})\n"
            f"\n[yellow]⚠ {len(drift)} file(s) drift from template[/yellow]",
            title="[yellow]Drift detected[/yellow]",
            border_style="yellow",
        )
    )
    for rel, diff in drift:
        console.print(f"\n[bold cyan]── {rel} ──[/bold cyan]")
        console.print(diff if diff else "[dim](identical after normalization)[/dim]")

    if not apply:
        console.print(
            "\n[dim]Dry-run. Re-run with [bold]--apply[/bold] to overwrite "
            "the agent's files with the current template "
            "(preserving the agent's name).[/dim]"
        )
        console.print(
            f"[dim]mdk_update_summary: "
            f"name={agent_name} template={template_name} "
            f"drifted_files={len(drift)} dry_run=true ok=true[/dim]"
        )
        return

    # Commit: overwrite the files in the agent dir from the current
    # template, doing the __AGENT_NAME__ substitution + stamping the
    # current template-source version.
    import shutil  # noqa: PLC0415

    for tmpl_file in template_files:
        rel = tmpl_file.relative_to(template_dir)
        dest_file = agent_dir / rel
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            tmpl_contents = tmpl_file.read_text()
            dest_file.write_text(tmpl_contents.replace("__AGENT_NAME__", agent_name))
        except (UnicodeDecodeError, OSError):
            shutil.copy2(tmpl_file, dest_file)
    # Re-stamp the source field with the current version.
    _stamp_template_source(agent_dir, template=template_name)

    console.print(
        f"\n[green]✓ overwrote {len(drift)} file(s) from the current "
        f"template. Re-run [bold]mdk doctor agent {agent_name}[/bold] to "
        f"confirm.[/green]"
    )
    console.print(
        f"[dim]mdk_update_summary: "
        f"name={agent_name} template={template_name} "
        f"drifted_files={len(drift)} dry_run=false ok=true[/dim]"
    )
