"""``mdk inspect`` — show the resolved view of an agent / skill / workflow.

Different from ``mdk show`` (which prints fields from the raw YAML).
``inspect`` shows what the **executor actually sees** after all the
resolution layers run:

* Defaults from ``movate.yaml: defaults:`` and ``policy.yaml: defaults:``
  layered onto the agent's own ``model`` block.
* Inline-shorthand schemas (``query: string``, ``count?: integer``)
  expanded to full JSON Schemas.
* Contexts prepended to the prompt with the canonical separator.
* Skills resolved through the skill registry — backend kind + entry
  visible alongside the raw skill name.

Answers the operator question: *"the prompt I wrote isn't what the
executor sent — what changed?"*

Subcommands:

* ``agent <name>`` — resolved AgentBundle (the MVP).
* Future: ``workflow <name>``, ``skill <name>`` for the same lens
  on those primitives.

Design rules:

* **No external calls.** Pure local resolution. Operators reach for
  ``inspect`` exactly when something's broken; we never want it to
  fail because Azure / Langfuse / etc. is down.
* **Default surface = everything.** Inspect is the "give me the whole
  resolved picture" command; the operator already knows they want
  the deep view. ``--only <section>`` narrows; ``--json`` swaps
  the rendering for machine output.
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

from movate.core.loader import AgentBundle, AgentLoadError, load_agent

console = Console()
err_console = Console(stderr=True)


inspect_app = typer.Typer(
    name="inspect",
    help=(
        "Show the [bold]resolved[/bold] view of an agent / workflow / skill — "
        "defaults applied, schemas compiled, contexts prepended. "
        "Different from [bold]mdk show[/bold] (which prints raw YAML fields)."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


# Section ids the operator can name in --only. Keeping the list small
# + stable matters because operators will alias these in scripts.
_SECTIONS = (
    "identity",
    "model",
    "prompt",
    "schemas",
    "skills",
    "contexts",
)


def _resolve_agent_path(name_or_path: str, project_root: Path) -> Path:
    """Resolve a positional arg into a real agent directory.

    Accepts either a bare agent name (looked up under ``agents/<name>``)
    or a path. Path takes precedence when both work — operators
    occasionally have a name that collides with a directory.
    """
    candidate = Path(name_or_path)
    if candidate.is_dir() and (candidate / "agent.yaml").is_file():
        return candidate.resolve()
    # Bare name → assume agents/<name>/.
    by_name = project_root / "agents" / name_or_path
    if by_name.is_dir() and (by_name / "agent.yaml").is_file():
        return by_name.resolve()
    err_console.print(
        f"[red]✗[/red] agent not found: [bold]{name_or_path}[/bold]. "
        "[dim]Looked under [bold]agents/[/bold] and as a literal path.[/dim]"
    )
    raise typer.Exit(code=2)


# ---------------------------------------------------------------------------
# Section renderers (each takes the bundle, prints to console)
# ---------------------------------------------------------------------------


def _render_identity(bundle: AgentBundle) -> None:
    spec = bundle.spec
    table = Table(title="Identity", title_style="bold", show_header=False, box=None)
    table.add_column(style="dim", no_wrap=True)
    table.add_column()
    table.add_row("name", spec.name)
    table.add_row("version", spec.version)
    table.add_row("description", spec.description or "[dim]—[/dim]")
    table.add_row("owner", spec.owner or "[dim]—[/dim]")
    table.add_row("agent_dir", str(bundle.agent_dir))
    table.add_row("prompt_hash", bundle.prompt_hash[:16] + "…")
    console.print(table)


def _render_model(bundle: AgentBundle) -> None:
    spec = bundle.spec
    table = Table(title="Model (resolved)", title_style="bold", show_header=False, box=None)
    table.add_column(style="dim", no_wrap=True)
    table.add_column()
    table.add_row("provider", spec.model.provider)
    if spec.model.params:
        table.add_row("params", json.dumps(spec.model.params, indent=2))
    else:
        table.add_row("params", "[dim](none)[/dim]")
    if spec.model.fallback:
        chain = " → ".join(f.provider for f in spec.model.fallback)
        table.add_row("fallback", chain)
    else:
        table.add_row("fallback", "[dim](none)[/dim]")
    console.print(table)


def _render_prompt(bundle: AgentBundle) -> None:
    """Show the prompt the executor would render.

    We can't fully render Jinja without a real input dict, so we hand
    the operator the template *with contexts prepended* — that's the
    part defaults / contexts affect. Jinja placeholders stay literal so
    the operator sees exactly which variables get interpolated.
    """
    # Build the same prefix the executor would use, without rendering
    # the template against a fake input (StrictUndefined would explode).
    from movate.core.context_loader import build_context_prefix  # noqa: PLC0415

    prefix = build_context_prefix(bundle.contexts)
    body = prefix + bundle.prompt_template
    console.print(
        Panel(
            Syntax(body, "markdown", theme="ansi_dark", word_wrap=True),
            title="Prompt (with contexts prepended)",
            title_align="left",
            border_style="cyan",
        )
    )


def _render_schemas(bundle: AgentBundle) -> None:
    """Print the resolved JSON Schemas — both input and output."""
    in_text = json.dumps(bundle.input_schema, indent=2)
    out_text = json.dumps(bundle.output_schema, indent=2)
    console.print(
        Panel(
            Syntax(in_text, "json", theme="ansi_dark", line_numbers=False),
            title="Input schema (resolved)",
            title_align="left",
            border_style="cyan",
        )
    )
    console.print(
        Panel(
            Syntax(out_text, "json", theme="ansi_dark", line_numbers=False),
            title="Output schema (resolved)",
            title_align="left",
            border_style="cyan",
        )
    )


def _render_skills(bundle: AgentBundle) -> None:
    if not bundle.skills:
        console.print(
            "[dim]Skills:[/dim] [yellow]none[/yellow] [dim](single-shot mode — "
            "executor skips the tool-use loop)[/dim]"
        )
        return
    table = Table(title=f"Skills ({len(bundle.skills)})", title_style="bold")
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Backend", no_wrap=True)
    table.add_column("Entry", style="dim")
    table.add_column("Side effects", style="dim", no_wrap=True)
    for skill in bundle.skills:
        # SkillBundle has .spec.name / .spec.implementation.kind /
        # .spec.implementation.entry / .spec.side_effects — defensive
        # attribute access so a future SkillBundle field rename doesn't
        # crash inspect.
        spec = getattr(skill, "spec", skill)
        name = getattr(spec, "name", "?")
        impl = getattr(spec, "implementation", None)
        kind = getattr(impl, "kind", "?")
        entry = getattr(impl, "entry", "")
        side = getattr(spec, "side_effects", "")
        table.add_row(str(name), str(kind), str(entry), str(side))
    console.print(table)


def _render_contexts(bundle: AgentBundle) -> None:
    if not bundle.contexts:
        console.print(
            "[dim]Contexts:[/dim] [yellow]none[/yellow] [dim](prompt rendered as-is)[/dim]"
        )
        return
    for name, body in bundle.contexts:
        snippet = (
            body if len(body) <= _CONTEXT_PREVIEW_CHARS else body[:_CONTEXT_PREVIEW_CHARS] + "…"
        )
        console.print(
            Panel(
                snippet,
                title=f"Context: [cyan]{name}[/cyan]",
                title_align="left",
                border_style="dim",
            )
        )


# Truncate context bodies past this length in the Rich render so a
# single fat context doesn't dominate the panel. Operators who want
# the full body run `--json` or read the file directly.
_CONTEXT_PREVIEW_CHARS = 400


_RENDERERS = {
    "identity": _render_identity,
    "model": _render_model,
    "prompt": _render_prompt,
    "schemas": _render_schemas,
    "skills": _render_skills,
    "contexts": _render_contexts,
}


# ---------------------------------------------------------------------------
# JSON output (single function — serializes the whole bundle)
# ---------------------------------------------------------------------------


def _bundle_to_json(bundle: AgentBundle) -> dict[str, Any]:
    """Serialize the resolved bundle to a JSON-friendly dict.

    Mirrors the section layout of the Rich rendering so a CI tool can
    pick the same parts via ``--only`` and the JSON view's keys.
    """
    spec = bundle.spec

    skills_json: list[dict[str, Any]] = []
    for skill in bundle.skills:
        s = getattr(skill, "spec", skill)
        impl = getattr(s, "implementation", None)
        skills_json.append(
            {
                "name": str(getattr(s, "name", "?")),
                "backend": str(getattr(impl, "kind", "?")),
                "entry": str(getattr(impl, "entry", "")),
                "side_effects": str(getattr(s, "side_effects", "")),
            }
        )

    from movate.core.context_loader import build_context_prefix  # noqa: PLC0415

    return {
        "identity": {
            "name": spec.name,
            "version": spec.version,
            "description": spec.description or "",
            "owner": spec.owner or "",
            "agent_dir": str(bundle.agent_dir),
            "prompt_hash": bundle.prompt_hash,
        },
        "model": {
            "provider": spec.model.provider,
            "params": spec.model.params,
            "fallback": [f.provider for f in spec.model.fallback],
        },
        "prompt": {
            "template_with_contexts": (
                build_context_prefix(bundle.contexts) + bundle.prompt_template
            ),
            "template_only": bundle.prompt_template,
        },
        "schemas": {
            "input": bundle.input_schema,
            "output": bundle.output_schema,
        },
        "skills": skills_json,
        "contexts": [{"name": n, "body": b} for n, b in bundle.contexts],
    }


# ---------------------------------------------------------------------------
# Subcommand: agent
# ---------------------------------------------------------------------------


@inspect_app.command("agent")
def inspect_agent(
    name: str = typer.Argument(
        ...,
        help=(
            "Agent name (resolved under [bold]agents/<name>[/bold]) or a "
            "literal path to an agent directory."
        ),
        metavar="AGENT",
    ),
    only: list[str] = typer.Option(
        None,
        "--only",
        help=(
            f"Show only the named section(s). Repeatable. Valid ids: "
            f"{', '.join(_SECTIONS)}. Default: show every section."
        ),
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the resolved bundle as JSON — pipe-friendly for CI / jq.",
    ),
    show_raw: bool = typer.Option(
        False,
        "--raw",
        help=(
            "Also print the raw agent.yaml + prompt.md alongside the "
            'resolved view. Makes [italic]"what changed under resolution?"[/italic] '
            "debugging trivial — both sides on screen at once."
        ),
    ),
    project_root: str = typer.Option(
        ".",
        "--project-root",
        envvar="MOVATE_PROJECT_ROOT",
        help="Project root (default: cwd). Used to resolve bare agent names.",
        hidden=True,
    ),
) -> None:
    """Show the [bold]resolved[/bold] view of an agent — what the executor sees.

    Different from [bold]mdk show[/bold] (raw YAML fields). [bold]inspect[/bold]
    walks the agent through the same loader the executor uses, then
    surfaces every layer that contributed to the final state:

      • Model defaults from [bold]movate.yaml[/bold] / [bold]policy.yaml[/bold]
      • JSON Schemas after inline-shorthand expansion
      • Contexts prepended to the prompt
      • Skills resolved through the registry (backend + entry)

    [bold]Examples:[/bold]

      [dim]$ mdk inspect agent triage                  # full resolved view[/dim]
      [dim]$ mdk inspect agent triage --only prompt    # just the prompt[/dim]
      [dim]$ mdk inspect agent triage --only model --only schemas[/dim]
      [dim]$ mdk inspect agent triage --json | jq '.model'[/dim]
    """
    root = Path(project_root).resolve()
    agent_path = _resolve_agent_path(name, root)

    try:
        bundle = load_agent(agent_path)
    except AgentLoadError as exc:
        err_console.print(f"[red]✗ load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    # Validate --only ids early so the operator sees the typo instead
    # of a silently-empty render.
    selected: tuple[str, ...] = tuple(only or ()) or _SECTIONS
    bad = [s for s in selected if s not in _SECTIONS]
    if bad:
        err_console.print(
            f"[red]✗[/red] unknown section(s): {', '.join(bad)}. "
            f"[dim]Valid: {', '.join(_SECTIONS)}.[/dim]"
        )
        raise typer.Exit(code=2)

    if json_output:
        full = _bundle_to_json(bundle)
        if only:
            full = {k: v for k, v in full.items() if k in selected}
        console.print_json(json.dumps(full))
        return

    # Rich render: emit each requested section in canonical order.
    for section in _SECTIONS:
        if section not in selected:
            continue
        _RENDERERS[section](bundle)
        # Blank line between sections for readability — Rich Panels
        # already have their own borders, but a blank line clarifies
        # which Panel goes with which section title.
        console.print()

    if show_raw:
        _render_raw_sources(agent_path)


def _render_raw_sources(agent_dir: Path) -> None:
    """Print agent.yaml + prompt.md as untouched source.

    Pairs with the resolved view so operators can directly compare
    "what I wrote" against "what the executor sees." Useful when a
    movate.yaml default is silently overriding an agent's own
    declaration and the operator can't figure out why.
    """
    for filename, syntax in (("agent.yaml", "yaml"), ("prompt.md", "markdown")):
        path = agent_dir / filename
        if not path.is_file():
            continue
        body = path.read_text()
        console.print(
            Panel(
                Syntax(body, syntax, theme="ansi_dark", word_wrap=False),
                title=f"Raw: [cyan]{filename}[/cyan]",
                title_align="left",
                border_style="dim",
            )
        )
        console.print()
