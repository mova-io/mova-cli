"""``mdk plan --from "<description>"`` — LLM-bootstrapped project scaffolder (Phase J-3).

The "wow demo" command from BACKLOG J. Reads a natural-language
description, calls a planner LLM with the role catalog, and emits a
structured project plan. ``--apply`` scaffolds the plan via
programmatic calls to ``mdk init`` + ``mdk add``.

Default behavior is DRY-RUN — print the plan + cost estimate + tree
preview, exit without touching disk. Operators opt into actual
scaffolding with ``--apply``.

The planner uses LiteLLM with a cross-family-safe model (anthropic
default). For hermetic testing, ``--mock`` swaps in MockProvider —
useful in CI and offline development.

What it produces (dry-run):

  Plan: contract-eval
  Description: Evaluate contracts against a checklist of required items

  Agents:
    contract-parser    document-summarizer    Extract structure from raw contract text
    checklist-grader   text-classifier        Grade each item against the checklist
    exec-summary       reply-drafter          Compose the executive summary

  Workflow: contract-parser → checklist-grader → exec-summary

  Estimated planning cost: $0.0012 (one judge call)
  Estimated scaffold cost: $0.00 (offline scaffold; no LLM calls)
  Run `mdk plan ... --apply` to scaffold the project.

What it does (--apply):
  1. `mdk init <project_name> --project` to scaffold the project root
  2. `mdk add <agent_name> --template <role>` for each planned agent
  3. (Workflow scaffold is Phase 7 — for now, agents are added but
     the workflow.yaml is left for the operator to write.)
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from movate.core.planner import (
    PlanParseError,
    ProjectPlan,
    call_planner,
)
from movate.providers.litellm import LiteLLMProvider
from movate.providers.mock import MockProvider
from movate.providers.pricing import load_pricing
from movate.templates import get_template_path

console = Console()
err_console = Console(stderr=True)


# Default planner model — cross-family vs the roles' default
# (openai/gpt-4o-mini) so a future role-vs-planner reflection-style
# check doesn't trip its cross-family enforcement.
_DEFAULT_PLANNER_MODEL = "anthropic/claude-haiku-4-5-20251001"


def plan(
    description: str = typer.Argument(
        ...,
        help=(
            "Natural-language description of the AI system you want. "
            'E.g. "Evaluate contracts against a checklist of required items".'
        ),
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help=(
            "Actually scaffold the planned project (default: dry-run "
            "preview only). With --apply, runs mdk init + mdk add "
            "programmatically; without it, just prints the plan."
        ),
    ),
    target: Path = typer.Option(
        Path("."),
        "--target",
        "-t",
        help="Parent directory for the scaffolded project (with --apply). Default: cwd.",
    ),
    planner_model: str = typer.Option(
        _DEFAULT_PLANNER_MODEL,
        "--planner-model",
        envvar="MDK_PLANNER_MODEL",
        help="LiteLLM model string for the planner call.",
    ),
    mock: bool = typer.Option(
        False,
        "--mock",
        help=(
            "Use MockProvider for the planner call (hermetic test path). "
            "Reads MDK_MOCK_PLAN_RESPONSE for the canned plan JSON; "
            "falls back to a generic 2-agent plan."
        ),
    ),
) -> None:
    """Bootstrap an MDK project from a natural-language description.

    [bold]Examples:[/bold]

      [dim]# Preview the plan (default — no disk writes)[/dim]
      $ mdk plan "Triage support tickets and draft replies"

      [dim]# Apply the plan — scaffold the project[/dim]
      $ mdk plan "Contract eval against a 12-item checklist" --apply --target ./my-projects

      [dim]# Hermetic / CI mode[/dim]
      $ mdk plan "..." --mock --apply
    """
    asyncio.run(
        _run_plan(
            description=description,
            apply=apply,
            target=target,
            planner_model=planner_model,
            mock=mock,
        )
    )


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------


async def _run_plan(
    *,
    description: str,
    apply: bool,
    target: Path,
    planner_model: str,
    mock: bool,
) -> None:
    """Resolve provider → call planner → render → optionally scaffold."""
    provider = _build_planner_provider(mock=mock)

    try:
        project_plan = await call_planner(
            description=description,
            planner_model=planner_model if not mock else "mock/planner",
            provider=provider,
        )
    except PlanParseError as exc:
        err_console.print(f"[red]✗[/red] planner returned an unusable response: {exc}")
        err_console.print(
            "[dim]Try rephrasing the description or specify a different --planner-model.[/dim]"
        )
        raise typer.Exit(code=1) from None

    _render_plan(project_plan)
    if not apply:
        console.print()
        console.print(
            "[dim]Dry-run only. Run again with [bold]--apply[/bold] to scaffold the project.[/dim]"
        )
        return

    # --apply path: scaffold using template-clone + name substitution.
    _scaffold_plan(project_plan, target=target)


def _build_planner_provider(*, mock: bool):
    """Resolve the provider used for the planner call.

    Returns a :class:`BaseLLMProvider` already configured for either:

    * **Mock** (``--mock``): reads ``MDK_MOCK_PLAN_RESPONSE`` env var
      for the canned response. Falls back to a generic 2-agent plan
      so the smoke path works without env config.
    * **Real**: a :class:`LiteLLMProvider` — same path as the agent
      executor uses for real model calls.
    """
    if mock:
        import os  # noqa: PLC0415

        canned = os.environ.get("MDK_MOCK_PLAN_RESPONSE") or _DEFAULT_MOCK_PLAN
        return MockProvider(response=canned)
    return LiteLLMProvider()


_DEFAULT_MOCK_PLAN = """{
  "project_name": "demo-project",
  "description": "Generic 2-agent triage + summary",
  "agents": [
    {"name": "triage", "template": "support-triage", "purpose": "Triage incoming items"},
    {"name": "summary", "template": "document-summarizer", "purpose": "Summarise the triaged items"}
  ],
  "workflow": ["triage", "summary"]
}"""


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_plan(plan_obj: ProjectPlan) -> None:
    """Render the plan as a Rich table + workflow chain."""
    console.print()
    console.print(f"[bold]Plan:[/bold] [cyan]{plan_obj.project_name}[/cyan]")
    console.print(f"[dim]{plan_obj.description}[/dim]")
    console.print()

    table = Table(title="Agents", title_style="bold")
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Template", style="bold")
    table.add_column("Purpose", style="dim")
    for agent in plan_obj.agents:
        table.add_row(agent.name, agent.template, agent.purpose)
    console.print(table)

    if plan_obj.workflow:
        console.print()
        chain = " → ".join(f"[cyan]{node}[/cyan]" for node in plan_obj.workflow)
        console.print(f"[bold]Workflow:[/bold] {chain}")

    # Cost estimate: planning call is cheap; scaffold is offline.
    console.print()
    try:
        pricing = load_pricing()
        # Heuristic: ~2K input + 500 output tokens for a typical plan
        cost = pricing.cost_for(
            provider=_DEFAULT_PLANNER_MODEL,
            tokens=_make_token_usage(in_tokens=2000, out_tokens=500),
        )
        console.print(f"[dim]Planning cost (estimated): ${cost:.4f}[/dim]")
    except Exception:
        pass


def _make_token_usage(in_tokens: int, out_tokens: int):
    """Build a TokenUsage instance lazily so the import cost stays scoped."""
    from movate.core.models import TokenUsage  # noqa: PLC0415

    return TokenUsage(input=in_tokens, output=out_tokens)


# ---------------------------------------------------------------------------
# Scaffold (--apply path)
# ---------------------------------------------------------------------------


def _scaffold_plan(plan_obj: ProjectPlan, *, target: Path) -> None:
    """Materialise the plan on disk.

    For MVP we use direct filesystem operations (template-clone +
    name substitution) rather than invoking the `mdk init` / `mdk
    add` subprocesses, which would be slower + harder to test. The
    operations are the same primitives those commands use — see
    :mod:`movate.cli.add` for the source pattern.
    """
    project_dir = (target / plan_obj.project_name).resolve()
    if project_dir.exists():
        err_console.print(f"[red]✗[/red] target dir already exists: {project_dir}")
        err_console.print(
            "[dim]Choose a different --target or remove the existing directory.[/dim]"
        )
        raise typer.Exit(code=2)

    project_dir.mkdir(parents=True)
    # Minimal movate.yaml so future `mdk add` walk-up finds this root.
    (project_dir / "movate.yaml").write_text(
        f"# Project: {plan_obj.project_name}\n"
        f"# Generated by `mdk plan --apply`.\n"
        f"# {plan_obj.description}\n"
    )

    agents_dir = project_dir / "agents"
    agents_dir.mkdir()

    for agent in plan_obj.agents:
        _scaffold_single_agent(
            agents_dir=agents_dir,
            name=agent.name,
            template=agent.template,
        )

    console.print()
    console.print(
        f"[green]✓[/green] scaffolded [bold]{plan_obj.project_name}[/bold] "
        f"with {len(plan_obj.agents)} agent(s) at [bold]{project_dir}[/bold]"
    )
    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print(f"  [cyan]cd {project_dir}[/cyan]")
    console.print("  [cyan]mdk validate --project[/cyan]   [dim]# check the scaffold[/dim]")
    if plan_obj.workflow:
        chain = " → ".join(plan_obj.workflow)
        console.print(f"  [dim]# author workflows/main.yaml with chain: {chain}[/dim]")


def _scaffold_single_agent(*, agents_dir: Path, name: str, template: str) -> None:
    """Clone the role template into agents/<name>/ + stamp the agent name.

    Mirrors the logic in :func:`movate.cli.add._prepare_destination` +
    placeholder substitution. Pure filesystem op, no subprocess.
    """
    template_dir = get_template_path(template)
    dest = agents_dir / name
    shutil.copytree(template_dir, dest)
    yaml_path = dest / "agent.yaml"
    yaml_path.write_text(yaml_path.read_text().replace("__AGENT_NAME__", name))
