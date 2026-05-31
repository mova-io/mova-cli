"""``mdk skills`` — operator commands for the skill registry.

Six subcommands:

* ``mdk skills list`` — print every skill in the project's ``skills/``
  folder. Defaults to the current directory; pass ``--project``
  to point elsewhere. Surfaces name + version + backend kind + cost
  per call.
* ``mdk skills scaffold <name>`` — drop a sample
  ``skills/<name>/skill.yaml`` + ``impl.py`` + ``README.md`` from
  the packaged template. Discoverable starting point — the operator
  fills in the schema, writes the function body, and the wiring is
  already correct.
* ``mdk skills run <name> '<json-input>'`` — invoke a skill directly
  without an agent. Critical for iterating on ``impl.py`` without
  paying the LLM cost of a full tool-use loop.
* ``mdk skills search [--tag TAG] [--query TEXT] [--kind KIND]`` —
  filter the project's skill registry by tag, freetext, and/or kind.
* ``mdk skills info <name>`` — full detail for a skill: schema,
  capabilities, cost, usage example.
* ``mdk skills validate [<agent>]`` — validate declared skills are
  compatible with the agent(s) that use them.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from movate.core.skill_backend import (
    SkillError,
    SkillExecutionContext,
    dispatch_skill,
)
from movate.core.skill_loader import (
    SkillLoadError,
    load_skill,
    load_skill_registry,
)
from movate.templates import TEMPLATES_DIR


def _register_skill_backends() -> None:
    """Import each available backend for its registration side-effect.

    Done lazily on first command-invocation (rather than module
    top-level) so the imports don't run at every ``mdk`` startup —
    just when an operator actually uses a ``mdk skills`` subcommand.
    Same pattern the executor follows from its tool-use loop entry.

    All three backends (python, http, mcp) are imported here.
    HTTP and MCP are wrapped in contextlib.suppress(ImportError) so
    the command stays usable if a backend module is absent.
    """
    # importlib over `from movate... import python` so mypy stays
    # happy under strict mode (the package's __init__ doesn't re-
    # export the submodule, and re-exporting clashes with the stdlib
    # ``http`` package name).
    import contextlib  # noqa: PLC0415
    import importlib  # noqa: PLC0415

    importlib.import_module("movate.core.skill_backend.python")
    # HTTP and MCP backends were added incrementally; best-effort
    # imports keep this command working regardless of whether each
    # backend module is in the build. dispatch_skill surfaces a clean
    # error if a skill references a kind whose backend isn't loaded.
    with contextlib.suppress(ImportError):
        importlib.import_module("movate.core.skill_backend.http")
    with contextlib.suppress(ImportError):
        importlib.import_module("movate.core.skill_backend.mcp")
    with contextlib.suppress(ImportError):
        importlib.import_module("movate.core.skill_backend.agent")


console = Console()
err_console = Console(stderr=True)


skills_app = typer.Typer(
    name="skills",
    help=(
        "Inspect, scaffold, and test the skill registry. See "
        "docs/adr/002-skills-and-contexts.md for the design."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


# Default budget for `mdk skills run` invocations. Generous — operators
# are usually testing slow HTTP skills here and the alternative is
# baking it into a flag that nobody passes.
_DEFAULT_SKILL_RUN_TIMEOUT_MS = 60_000


# ---------------------------------------------------------------------------
# `mdk skills list`
# ---------------------------------------------------------------------------


@skills_app.command("list")
def list_skills(
    project: Path = typer.Option(
        Path("."),
        "--project",
        "-p",
        help="Project root containing ``skills/<name>/``. Defaults to cwd.",
    ),
) -> None:
    """Print every skill registered in the project's ``skills/`` folder.

    [bold]Examples:[/bold]

      [dim]# From the project root[/dim]
      $ mdk skills list

      [dim]# From elsewhere[/dim]
      $ mdk skills list --project ~/work/my-project
    """
    _register_skill_backends()
    try:
        registry = load_skill_registry(project)
    except SkillLoadError as exc:
        err_console.print(f"[red]✗ skill registry load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    if not registry:
        console.print(f"[dim]no skills registered under {project.resolve()}/skills/[/dim]")
        console.print("[dim]hint: `mdk skills scaffold <name>` to drop a starter skill.[/dim]")
        return

    table = Table(
        title=f"skills under {project.resolve()}/skills/",
        show_header=True,
        header_style="bold",
    )
    table.add_column("name")
    table.add_column("version", style="dim")
    table.add_column("backend")
    table.add_column("entry", overflow="fold")
    table.add_column("cost/call", justify="right")
    table.add_column("side-effects", style="dim")
    for name in sorted(registry):
        bundle = registry[name]
        spec = bundle.spec
        cost = spec.cost.per_call_usd
        cost_str = f"${cost:.4f}" if cost > 0 else "—"
        table.add_row(
            name,
            spec.version,
            spec.implementation.kind.value,
            spec.implementation.entry,
            cost_str,
            spec.side_effects.value,
        )
    console.print(table)


# ---------------------------------------------------------------------------
# `mdk skills scaffold <name>`
# ---------------------------------------------------------------------------


@skills_app.command("scaffold")
def scaffold(
    name: str = typer.Argument(
        ...,
        help="Skill name (lowercase, hyphen-separated). Matches the directory created.",
    ),
    project: Path = typer.Option(
        Path("."),
        "--project",
        "-p",
        help="Project root. The skill lands at ``<project>/skills/<name>/``.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite ``skills/<name>/`` if it already exists.",
    ),
) -> None:
    """Scaffold a starter skill from the packaged template.

    Creates ``<project>/skills/<name>/`` with a complete sample:
    ``skill.yaml`` wired to ``impl.py:run``, a starter Python function
    that echoes its input, and a README that walks through how to test
    it (`mdk skills run`) and wire it into an agent.

    [bold]Examples:[/bold]

      [dim]# Drop a starter calculator skill in cwd[/dim]
      $ mdk skills scaffold calculator

      [dim]# Drop it in a specific project[/dim]
      $ mdk skills scaffold calculator --project ~/work/my-project
    """
    template_dir = TEMPLATES_DIR / "skill_init"
    if not template_dir.is_dir():  # pragma: no cover — install-time invariant
        err_console.print(f"[red]✗ skill template missing:[/red] {template_dir}")
        raise typer.Exit(code=2)

    dest = (project / "skills" / name).resolve()
    if dest.exists() and not force:
        err_console.print(f"[red]✗[/red] {dest} already exists (pass --force to overwrite)")
        raise typer.Exit(code=2)
    if dest.exists() and force:
        shutil.rmtree(dest)

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template_dir, dest)

    # Substitute the skill name into every templated file. Same
    # pattern as `mdk init` for agents — keeps the scaffold's wiring
    # ready to validate without extra fiddling.
    for template_file in (
        dest / "skill.yaml",
        dest / "impl.py",
        dest / "README.md",
    ):
        if template_file.exists():
            template_file.write_text(template_file.read_text().replace("__SKILL_NAME__", name))

    console.print(f"[green]✓[/green] scaffolded skill [bold]{name}[/bold] at [bold]{dest}[/bold]")
    console.print("\nNext steps:")
    console.print(f"  [dim]# Edit[/dim] {dest}/skill.yaml [dim]+[/dim] impl.py")
    console.print(f"  mdk skills run {name} '{{...}}'")
    console.print("  [dim]# Then list it in your agent.yaml's `skills:` field[/dim]")


# ---------------------------------------------------------------------------
# `mdk skills run <name> '<json-input>'`
# ---------------------------------------------------------------------------


@skills_app.command("run")
def run(
    name: str = typer.Argument(
        ...,
        help="Skill name as it appears in the project's skills/ registry.",
    ),
    input_json: str = typer.Argument(
        ...,
        help="JSON input dict, matching the skill's input schema.",
    ),
    project: Path = typer.Option(
        Path("."),
        "--project",
        "-p",
        help="Project root containing ``skills/<name>/``.",
    ),
    timeout_ms: int = typer.Option(
        _DEFAULT_SKILL_RUN_TIMEOUT_MS,
        "--timeout-ms",
        help=(
            "Per-call timeout in milliseconds. Used for debug invocations; "
            "agent-driven calls inherit from agent.yaml instead."
        ),
    ),
) -> None:
    """Invoke a skill directly, bypassing the agent + tool-use loop.

    Validates the input against the skill's schema, dispatches via
    the matching backend (python | http | mcp), validates the output,
    and prints the result as JSON. The same code path the executor
    runs for each tool call inside a real agent.

    Use this to iterate on ``impl.py`` (or the HTTP API shape behind
    an http skill) without paying the LLM cost of a full agent run.

    [bold]Examples:[/bold]

      [dim]# Run a Python skill with a synthetic input[/dim]
      $ mdk skills run calculator '{"expression": "41 + 1"}'

      [dim]# Run an HTTP skill (env vars for auth are read from the shell)[/dim]
      $ CRM_TOKEN=... mdk skills run warranty-lookup '{"case_id": "abc-123"}'
    """
    # Parse the input JSON first so a typo fails before any skill load.
    try:
        input_data = json.loads(input_json)
    except json.JSONDecodeError as exc:
        err_console.print(f"[red]✗ input is not valid JSON:[/red] {exc}")
        raise typer.Exit(code=2) from None
    if not isinstance(input_data, dict):
        err_console.print(
            f"[red]✗ input must be a JSON object;[/red] got {type(input_data).__name__}"
        )
        raise typer.Exit(code=2)

    skill_dir = (project / "skills" / name).resolve()
    if not skill_dir.is_dir():
        err_console.print(f"[red]✗ no skills/{name}/ folder under {project.resolve()}[/red]")
        # Suggest `scaffold` as the fix — operator may have typed the
        # name wrong, but it's also possible they're brand new.
        err_console.print(
            f"[dim]hint: `mdk skills list -p {project}` to see what's registered, "
            f"or `mdk skills scaffold {name}` to create it.[/dim]"
        )
        raise typer.Exit(code=2)

    _register_skill_backends()
    try:
        bundle = load_skill(skill_dir)
    except SkillLoadError as exc:
        err_console.print(f"[red]✗ skill load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    ctx = SkillExecutionContext(
        trace_id="cli-skills-run",
        tenant_id="local",
        run_id="",
        call_ms_budget=timeout_ms,
    )
    try:
        output = asyncio.run(dispatch_skill(bundle, input_data, ctx))
    except SkillError as exc:
        err_console.print(
            f"[red]✗ skill error[/red] [yellow]{exc.type.value}[/yellow]: {exc.message}"
        )
        raise typer.Exit(code=1) from None

    # Print the result. JSON on stdout so it's pipeable; the success
    # banner stays on stderr so a redirect just captures the payload.
    err_console.print(f"[green]✓ {name}[/green]")
    print(json.dumps(output, indent=2))


# ---------------------------------------------------------------------------
# `mdk skills search [--tag TAG] [--query TEXT] [--kind KIND]`
# ---------------------------------------------------------------------------


@skills_app.command("search")
def search(
    tag: str | None = typer.Option(
        None,
        "--tag",
        "-t",
        help="Filter by tag (exact match, case-insensitive).",
    ),
    query: str | None = typer.Option(
        None,
        "--query",
        "-q",
        help="Freetext match against skill name and description (case-insensitive).",
    ),
    kind: str | None = typer.Option(
        None,
        "--kind",
        "-k",
        help="Filter by backend kind: python | http | mcp | agent.",
    ),
    project: Path = typer.Option(
        Path("."),
        "--project",
        "-p",
        help="Project root containing ``skills/<name>/``. Defaults to cwd.",
    ),
) -> None:
    """Search the project's skill registry by tag, freetext, and/or kind.

    Outputs a Rich table: name | kind | version | tags | description.

    [bold]Examples:[/bold]

      [dim]# All skills tagged 'crm'[/dim]
      $ mdk skills search --tag crm

      [dim]# Skills whose name or description contains 'lookup'[/dim]
      $ mdk skills search --query lookup

      [dim]# HTTP-backed skills only[/dim]
      $ mdk skills search --kind http

      [dim]# Combine filters[/dim]
      $ mdk skills search --tag crm --kind http
    """
    _register_skill_backends()
    try:
        registry = load_skill_registry(project)
    except SkillLoadError as exc:
        err_console.print(f"[red]✗ skill registry load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    if not registry:
        console.print(f"[dim]no skills registered under {project.resolve()}/skills/[/dim]")
        console.print("[dim]hint: `mdk skills scaffold <name>` to drop a starter skill.[/dim]")
        return

    # Normalise filter values.
    tag_lc = tag.lower() if tag else None
    query_lc = query.lower() if query else None
    kind_lc = kind.lower() if kind else None

    matches = []
    for name_key in sorted(registry):
        bundle = registry[name_key]
        spec = bundle.spec

        # Tag filter — any of the skill's tags must match.
        if tag_lc is not None and not any(t.lower() == tag_lc for t in spec.tags):
            continue

        # Kind filter.
        if kind_lc is not None and spec.implementation.kind.value != kind_lc:
            continue

        # Freetext — match against name OR description.
        if query_lc is not None:
            haystack = f"{spec.name} {spec.description}".lower()
            if query_lc not in haystack:
                continue

        matches.append(bundle)

    if not matches:
        msg = "no skills matched"
        parts = []
        if tag:
            parts.append(f"tag={tag!r}")
        if query:
            parts.append(f"query={query!r}")
        if kind:
            parts.append(f"kind={kind!r}")
        if parts:
            msg += " (" + ", ".join(parts) + ")"
        console.print(f"[dim]{msg}[/dim]")
        return

    table = Table(
        title=f"skills — {project.resolve()}/skills/",
        show_header=True,
        header_style="bold",
    )
    table.add_column("name")
    table.add_column("kind", style="dim")
    table.add_column("version", style="dim")
    table.add_column("tags", style="dim")
    table.add_column("description", overflow="fold")

    for bundle in matches:
        spec = bundle.spec
        tags_str = ", ".join(spec.tags) if spec.tags else "—"
        desc = spec.description.strip().splitlines()[0] if spec.description.strip() else "—"
        table.add_row(
            spec.name,
            spec.implementation.kind.value,
            spec.version,
            tags_str,
            desc,
        )
    console.print(table)


# ---------------------------------------------------------------------------
# `mdk skills info <name>`
# ---------------------------------------------------------------------------


@skills_app.command("info")
def info(
    name: str = typer.Argument(
        ...,
        help="Skill name as it appears in the project's skills/ registry.",
    ),
    project: Path = typer.Option(
        Path("."),
        "--project",
        "-p",
        help="Project root containing ``skills/<name>/``.",
    ),
) -> None:
    """Show full detail for a skill.

    Renders: name, version, kind, description, owner, tags,
    input/output schemas (pretty-printed JSON Schema), side_effects,
    capabilities, cost, and a usage snippet showing how to declare
    it in agent.yaml. Also surfaces a README snippet or examples/
    directory note when present.

    [bold]Examples:[/bold]

      $ mdk skills info calculator
      $ mdk skills info warranty-lookup --project ~/work/my-project
    """
    _register_skill_backends()
    try:
        registry = load_skill_registry(project)
    except SkillLoadError as exc:
        err_console.print(f"[red]✗ skill registry load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    if name not in registry:
        available = sorted(registry.keys())
        hint = str(available) if available else "(empty registry)"
        err_console.print(f"[red]✗ skill {name!r} not found.[/red] Available: {hint}")
        raise typer.Exit(code=2)

    bundle = registry[name]
    spec = bundle.spec
    caps = spec.capabilities

    # ── Main detail panel ──────────────────────────────────────────────
    lines: list[str] = [
        f"[bold]name:[/bold]        {spec.name}",
        f"[bold]version:[/bold]     {spec.version}",
        f"[bold]kind:[/bold]        {spec.implementation.kind.value}",
        f"[bold]owner:[/bold]       {spec.owner or '—'}",
        f"[bold]tags:[/bold]        {', '.join(spec.tags) if spec.tags else '—'}",
        f"[bold]side_effects:[/bold] {spec.side_effects.value}",
        "[bold]cost/call:[/bold]   "
        + (f"${spec.cost.per_call_usd:.4f}" if spec.cost.per_call_usd > 0 else "free"),
    ]

    # Capabilities block (only rendered when at least one flag is set).
    cap_parts = []
    if caps.read_only is not None:
        cap_parts.append(f"read_only={caps.read_only}")
    if caps.deterministic is not None:
        cap_parts.append(f"deterministic={caps.deterministic}")
    if caps.network is not None:
        cap_parts.append(f"network={caps.network}")
    if caps.mutating is not None:
        cap_parts.append(f"mutating={caps.mutating}")
    if cap_parts:
        lines.append(f"[bold]capabilities:[/bold] {', '.join(cap_parts)}")
    else:
        lines.append("[bold]capabilities:[/bold] [dim]not declared[/dim]")

    if spec.description:
        lines.append("")
        lines.append("[bold]description:[/bold]")
        for dline in spec.description.strip().splitlines():
            lines.append(f"  {dline}")

    console.print(
        Panel(
            "\n".join(lines),
            title=f"[bold]{spec.name}[/bold] [dim]v{spec.version}[/dim]",
            title_align="left",
            border_style="blue",
        )
    )

    # ── Input schema ──────────────────────────────────────────────────
    console.print("[bold]Input schema:[/bold]")
    console.print(
        Syntax(
            json.dumps(bundle.input_schema, indent=2),
            "json",
            theme="ansi_dark",
            word_wrap=True,
        )
    )

    # ── Output schema ─────────────────────────────────────────────────
    console.print("[bold]Output schema:[/bold]")
    console.print(
        Syntax(
            json.dumps(bundle.output_schema, indent=2),
            "json",
            theme="ansi_dark",
            word_wrap=True,
        )
    )

    # ── Usage example (agent.yaml snippet) ───────────────────────────
    console.print("[bold]agent.yaml usage:[/bold]")
    usage = f"skills:\n  - {spec.name}  # declares this skill for use in the tool-use loop\n"
    console.print(Syntax(usage, "yaml", theme="ansi_dark"))

    # ── README snippet ────────────────────────────────────────────────
    readme_path = bundle.skill_dir / "README.md"
    if readme_path.is_file():
        readme_text = readme_path.read_text(encoding="utf-8", errors="replace")
        snippet_lines = readme_text.splitlines()[:20]
        if snippet_lines:
            console.print("[bold]README (first 20 lines):[/bold]")
            console.print(
                Syntax(
                    "\n".join(snippet_lines),
                    "markdown",
                    theme="ansi_dark",
                    word_wrap=True,
                )
            )

    # ── examples/ directory note ──────────────────────────────────────
    examples_dir = bundle.skill_dir / "examples"
    if examples_dir.is_dir():
        example_files = sorted(examples_dir.iterdir())
        if example_files:
            preview_count = 5
            names_preview = ", ".join(f.name for f in example_files[:preview_count])
            ellipsis_suffix = " …" if len(example_files) > preview_count else ""
            console.print(
                f"[dim]examples/ directory: "
                f"{len(example_files)} file(s) — "
                f"{names_preview}{ellipsis_suffix}[/dim]"
            )


# ---------------------------------------------------------------------------
# `mdk skills validate [<agent>]`
# ---------------------------------------------------------------------------


@skills_app.command("validate")
def validate_skills(
    agent: str | None = typer.Argument(
        None,
        help=(
            "Agent name (resolves under ``agents/<name>/``) or path to an agent directory. "
            "Omit to validate every agent in the project."
        ),
    ),
    project: Path = typer.Option(
        Path("."),
        "--project",
        "-p",
        help="Project root containing ``agents/`` and ``skills/``. Defaults to cwd.",
    ),
) -> None:
    """Validate skill compatibility for agent(s).

    For each agent x skill pair checks:

    (a) each skill name resolves in the project's skill registry;
    (b) the skill's output schema has the fields the agent's prompt
        references via ``{{skill_name.field}}`` patterns
        (best-effort parse);
    (c) produces a pass/fail table per agent x skill.

    [bold]Examples:[/bold]

      [dim]# Validate all agents[/dim]
      $ mdk skills validate

      [dim]# Validate one agent[/dim]
      $ mdk skills validate rag-qa

      [dim]# From a different project root[/dim]
      $ mdk skills validate --project ~/work/my-project
    """
    _register_skill_backends()

    project_root = project.resolve()

    # Load the skill registry once.
    try:
        registry = load_skill_registry(project_root)
    except SkillLoadError as exc:
        err_console.print(f"[red]✗ skill registry load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    # Discover agent directories.
    if agent is not None:
        # Resolve single agent: bare name → agents/<name>/, or a path.
        candidate = project_root / "agents" / agent
        if candidate.is_dir():
            agent_dirs = [candidate]
        else:
            as_path = Path(agent).resolve()
            if as_path.is_dir():
                agent_dirs = [as_path]
            else:
                err_console.print(
                    f"[red]✗ agent {agent!r} not found.[/red] Looked at {candidate} and {as_path}."
                )
                raise typer.Exit(code=2)
    else:
        agents_root = project_root / "agents"
        if not agents_root.is_dir():
            console.print(
                f"[dim]no agents/ directory under {project_root} — nothing to validate.[/dim]"
            )
            return
        agent_dirs = sorted(
            d for d in agents_root.iterdir() if d.is_dir() and (d / "agent.yaml").is_file()
        )

    if not agent_dirs:
        console.print("[dim]no agents found — nothing to validate.[/dim]")
        return

    # Per-agent, per-skill validation.
    # Row: (agent_name, skill_name, status, detail)
    rows: list[tuple[str, str, str, str]] = []
    failed = 0

    for agent_dir in agent_dirs:
        yaml_path = agent_dir / "agent.yaml"
        if not yaml_path.is_file():
            continue

        import yaml  # noqa: PLC0415

        try:
            raw = yaml.safe_load(yaml_path.read_text())
        except Exception as exc:  # broad catch — YAML can raise many exception types
            rows.append((agent_dir.name, "(parse error)", "✗", str(exc)))
            failed += 1
            continue

        declared_skills: list[str] = raw.get("skills", []) if isinstance(raw, dict) else []

        # Read the prompt for field-reference parsing.
        prompt_text = ""
        if isinstance(raw, dict) and "prompt" in raw:
            prompt_file = agent_dir / raw["prompt"]
            if prompt_file.is_file():
                prompt_text = prompt_file.read_text(encoding="utf-8", errors="replace")

        if not declared_skills:
            # No skills declared — nothing to check.
            rows.append((agent_dir.name, "(no skills)", "✓", "no skills declared"))
            continue

        for skill_name in declared_skills:
            # (a) name resolves in registry.
            if skill_name not in registry:
                rows.append(
                    (
                        agent_dir.name,
                        skill_name,
                        "✗",
                        "not found in skill registry",
                    )
                )
                failed += 1
                continue

            bundle = registry[skill_name]
            issues: list[str] = []

            # (b) Prompt field-reference check.
            # Pattern: {{skill_name.field}} where skill_name matches.
            pattern = re.compile(
                r"\{\{\s*" + re.escape(skill_name) + r"\s*\.\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}"
            )
            referenced_fields = pattern.findall(prompt_text)
            if referenced_fields:
                output_props = bundle.output_schema.get("properties", {})
                for field in referenced_fields:
                    if output_props and field not in output_props:
                        issues.append(
                            f"prompt references {{{{ {skill_name}.{field} }}}} "
                            f"but output schema has no property {field!r} "
                            f"(available: {sorted(output_props.keys())})"
                        )

            if issues:
                rows.append((agent_dir.name, skill_name, "✗", "; ".join(issues)))
                failed += 1
            else:
                rows.append((agent_dir.name, skill_name, "✓", ""))

    # ── Render results table ──────────────────────────────────────────
    table = Table(
        title="skills validate",
        show_header=True,
        header_style="bold",
    )
    table.add_column("agent")
    table.add_column("skill")
    table.add_column("status", no_wrap=True)
    table.add_column("detail", overflow="fold")

    for agent_name, skill_name, status, detail in rows:
        status_cell = "[green]✓[/green]" if status == "✓" else "[red]✗[/red]"
        table.add_row(agent_name, skill_name, status_cell, detail)

    console.print(table)

    if failed:
        err_console.print(
            f"[red]✗ {failed} issue(s) found.[/red] Fix the skill declarations and re-run."
        )
        raise typer.Exit(code=2)
    else:
        console.print(f"[green]✓[/green] all {len(rows)} agent x skill check(s) passed.")


# ---------------------------------------------------------------------------
# ``mdk skills remote`` — manage the runtime's MANAGED skills registry over the
# API (ADR 060 D3). Sibling to ``mdk project`` / ``mdk agent``: every verb talks
# to a deployed runtime via :class:`MovateClient`, resolved by ``--target``. The
# local subcommands above (``list`` / ``info`` / ``run`` / ...) operate on the
# bundle's ``skills/`` folder and are unchanged; these manage the durable,
# tenant-scoped, versioned registry the hosted/multi-tenant story uses.
# ---------------------------------------------------------------------------

from movate.cli._console import (  # noqa: E402
    confirm_destructive,
    echo_remote_context,
    error,
    get_global_target,
    hint,
    success,
)
from movate.cli._output import TableJson  # noqa: E402
from movate.core.client import MovateClient, MovateClientError  # noqa: E402
from movate.core.user_config import (  # noqa: E402
    UserConfigError,
    resolve_bearer_token,
    resolve_target,
)

skills_remote_app = typer.Typer(
    name="remote",
    help="Manage the runtime's MANAGED skills registry over the API (ADR 060). Needs --target.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
skills_app.add_typer(skills_remote_app, name="remote")


def _build_skills_client(target: str | None, *, suppress: bool = False) -> MovateClient:
    """Resolve a target name → MovateClient (mirrors ``project._build_client``)."""
    try:
        target_name, target_cfg = resolve_target(target or get_global_target())
        token = resolve_bearer_token(target_cfg)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None
    echo_remote_context(target_name, target_cfg, suppress=suppress)
    return MovateClient(base_url=target_cfg.url, api_key=token)


@skills_remote_app.command("list")
def remote_list_skills(
    target: str = typer.Option(None, "--target", "-t"),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """List the tenant's managed skills (latest version per name)."""
    client = _build_skills_client(target, suppress=output_format == TableJson.JSON)
    asyncio.run(_remote_skills_list(client, fmt=output_format))


@skills_remote_app.command("get")
def remote_get_skill(
    name: str = typer.Argument(..., help="Skill name."),
    version: str | None = typer.Option(None, "--version", help="Pin an exact version."),
    target: str = typer.Option(None, "--target", "-t"),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """Show one managed skill (latest, or a pinned ``--version``)."""
    client = _build_skills_client(target, suppress=output_format == TableJson.JSON)
    asyncio.run(_remote_skills_get(client, name=name, version=version, fmt=output_format))


@skills_remote_app.command("versions")
def remote_skill_versions(
    name: str = typer.Argument(..., help="Skill name."),
    target: str = typer.Option(None, "--target", "-t"),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """List a managed skill's version history (newest-first)."""
    client = _build_skills_client(target, suppress=output_format == TableJson.JSON)
    asyncio.run(_remote_skills_versions(client, name=name, fmt=output_format))


@skills_remote_app.command("create")
def remote_create_skill(
    skill_dir: Path = typer.Argument(
        ...,
        help="Local skill bundle dir (containing skill.yaml + optional impl.py/corpus.json).",
        exists=True,
        file_okay=False,
        readable=True,
    ),
    target: str = typer.Option(None, "--target", "-t"),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """Publish a skill bundle to the managed registry (first version).

    Reads ``skill.yaml`` (required) + optional ``impl.py`` / ``corpus.json`` /
    ``README.md`` from the dir. The ``version`` comes from the skill.yaml.
    """
    files, name, version = _read_skill_dir(skill_dir)
    client = _build_skills_client(target, suppress=output_format == TableJson.JSON)
    asyncio.run(
        _remote_skills_upsert(
            client, name=name, version=version, files=files, fmt=output_format, verb="create"
        )
    )


@skills_remote_app.command("update")
def remote_update_skill(
    skill_dir: Path = typer.Argument(
        ...,
        help="Local skill bundle dir; its skill.yaml version becomes the new registry version.",
        exists=True,
        file_okay=False,
        readable=True,
    ),
    target: str = typer.Option(None, "--target", "-t"),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """Publish a NEW version of a managed skill (rows are immutable)."""
    files, name, version = _read_skill_dir(skill_dir)
    client = _build_skills_client(target, suppress=output_format == TableJson.JSON)
    asyncio.run(
        _remote_skills_upsert(
            client, name=name, version=version, files=files, fmt=output_format, verb="update"
        )
    )


@skills_remote_app.command("delete")
def remote_delete_skill(
    name: str = typer.Argument(..., help="Skill name."),
    version: str | None = typer.Option(
        None, "--version", help="Delete just this version (omit to delete all)."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirm prompt."),
    target: str = typer.Option(None, "--target", "-t"),
) -> None:
    """Delete a managed skill (a version, or all versions of the name)."""
    confirm_destructive(
        f"Delete managed skill {name}{'@' + version if version else ' (ALL versions)'}?",
        yes=yes,
    )
    client = _build_skills_client(target)
    asyncio.run(_remote_skills_delete(client, name=name, version=version))


@skills_remote_app.command("attach")
def remote_attach_skill(
    name: str = typer.Argument(..., help="Registry skill name to attach."),
    agent: str = typer.Option(..., "--agent", "-a", help="Agent to attach the skill to."),
    version: str | None = typer.Option(None, "--version", help="Pin an exact skill version."),
    target: str = typer.Option(None, "--target", "-t"),
) -> None:
    """Attach a registry skill to an agent (records the ref; D4 resolves it)."""
    client = _build_skills_client(target)
    asyncio.run(_remote_skills_attach(client, agent=agent, ref=name, version=version))


def _read_skill_dir(skill_dir: Path) -> tuple[dict[str, str], str, str]:
    """Read a local skill bundle dir into a ``(files, name, version)`` tuple.

    ``skill.yaml`` is required; ``impl.py`` / ``corpus.json`` / ``README.md``
    are optional. ``name`` + ``version`` are parsed from the skill.yaml so the
    upsert lands at the right registry coordinates.
    """
    import yaml  # noqa: PLC0415

    yaml_path = skill_dir / "skill.yaml"
    if not yaml_path.is_file():
        error(f"no skill.yaml in {skill_dir}")
        raise typer.Exit(code=2)
    files: dict[str, str] = {"skill.yaml": yaml_path.read_text(encoding="utf-8")}
    for optional in ("impl.py", "corpus.json", "README.md"):
        p = skill_dir / optional
        if p.is_file():
            files[optional] = p.read_text(encoding="utf-8")
    try:
        spec = yaml.safe_load(files["skill.yaml"]) or {}
    except yaml.YAMLError as exc:
        error(f"skill.yaml is not valid YAML: {exc}")
        raise typer.Exit(code=2) from None
    name = spec.get("name")
    version = str(spec.get("version", ""))
    if not name or not version:
        error("skill.yaml must declare both 'name' and 'version'")
        raise typer.Exit(code=2)
    return files, str(name), version


async def _remote_skills_list(client: MovateClient, *, fmt: TableJson) -> None:
    async with client:
        try:
            listing = await client.list_skills()
        except MovateClientError as exc:
            error(str(exc), context="skills.remote.list")
            raise typer.Exit(code=1) from None
    if fmt == TableJson.JSON:
        console.print(listing.model_dump_json(indent=2), soft_wrap=True, highlight=False)
        return
    if listing.count == 0:
        hint("[dim]no managed skills yet[/dim]")
        return
    table = Table(title=f"managed skills ({listing.count})")
    table.add_column("name", style="bold cyan")
    table.add_column("version")
    table.add_column("description")
    for s in listing.skills:
        table.add_row(s.name, s.version, (s.description or "")[:50])
    console.print(table)


async def _remote_skills_get(
    client: MovateClient, *, name: str, version: str | None, fmt: TableJson
) -> None:
    async with client:
        try:
            view = await client.get_skill(name, version=version)
        except MovateClientError as exc:
            error(str(exc), context="skills.remote.get")
            raise typer.Exit(code=1) from None
    if fmt == TableJson.JSON:
        console.print(view.model_dump_json(indent=2), soft_wrap=True, highlight=False)
        return
    table = Table(title=f"skill {view.name}", show_header=False)
    table.add_column("field", style="dim")
    table.add_column("value")
    table.add_row("name", view.name)
    table.add_row("version", view.version)
    table.add_row("description", view.description or "")
    table.add_row("content_hash", view.content_hash[:12])
    table.add_row("files", ", ".join(sorted(view.files)))
    console.print(table)


async def _remote_skills_versions(client: MovateClient, *, name: str, fmt: TableJson) -> None:
    async with client:
        try:
            listing = await client.list_skill_versions(name)
        except MovateClientError as exc:
            error(str(exc), context="skills.remote.versions")
            raise typer.Exit(code=1) from None
    if fmt == TableJson.JSON:
        console.print(listing.model_dump_json(indent=2), soft_wrap=True, highlight=False)
        return
    table = Table(title=f"{name} versions ({listing.count})")
    table.add_column("version", style="bold")
    table.add_column("created_at", style="dim")
    table.add_column("content_hash", style="dim")
    for v in listing.versions:
        table.add_row(v.version, v.created_at.isoformat(), v.content_hash[:12])
    console.print(table)


async def _remote_skills_upsert(
    client: MovateClient,
    *,
    name: str,
    version: str,
    files: dict[str, str],
    fmt: TableJson,
    verb: str,
) -> None:
    async with client:
        try:
            view = await client.upsert_skill(name, version=version, files=files)
        except MovateClientError as exc:
            error(str(exc), context=f"skills.remote.{verb}")
            raise typer.Exit(code=1) from None
    if fmt == TableJson.JSON:
        console.print(view.model_dump_json(indent=2), soft_wrap=True, highlight=False)
        return
    success(f"published skill {view.name} v{view.version}")


async def _remote_skills_delete(client: MovateClient, *, name: str, version: str | None) -> None:
    async with client:
        try:
            await client.delete_skill(name, version=version)
        except MovateClientError as exc:
            error(str(exc), context="skills.remote.delete")
            raise typer.Exit(code=1) from None
    success(f"deleted skill {name}{'@' + version if version else ' (all versions)'}")


async def _remote_skills_attach(
    client: MovateClient, *, agent: str, ref: str, version: str | None
) -> None:
    async with client:
        try:
            view = await client.attach_skill_to_agent(agent, ref=ref, version=version)
        except MovateClientError as exc:
            error(str(exc), context="skills.remote.attach")
            raise typer.Exit(code=1) from None
    if view.attached:
        success(f"attached skill {ref} to agent {agent}")
    else:
        hint(f"[dim]{ref} was already attached to {agent}[/dim]")
