"""``mdk skills`` — operator commands for the skill registry.

Three subcommands:

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
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import typer
from rich.console import Console
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
