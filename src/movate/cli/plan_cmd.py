"""``mdk plan --from "<description>"`` — LLM-generated project plan (BACKLOG #126).

Given a natural-language description of what the operator wants to build,
asks a planner LLM to emit a structured JSON project plan: which agents to
create, which templates to use, which skills and contexts to wire up, and
how the agents compose into a workflow.

  $ mdk plan --from "a support triage system that routes tickets and drafts replies"
  $ mdk plan --from "sql assistant with approval workflow" --apply
  $ mdk plan --from "rag Q&A over company docs" --mock

Dry-run (default): renders the plan as a Rich panel + tree. No files written.
``--apply``: calls ``mdk init`` for each agent, ``mdk add skill`` / context /
kb as declared, and writes ``workflow.yaml`` when the plan has > 1 agent.

Design choices:

* Haiku is the default planner — the plan is short JSON, speed matters more
  than raw capability here. Override with ``--model``.
* ``--mock`` returns a deterministic stub plan for offline tests and CI.
* Agent names in the plan are slugified from the description + role.
* Skill names map directly to SKILL_TEMPLATES when available; unknown skills
  get a stub scaffold (same as ``mdk add skill <name>``).
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.tree import Tree

from movate.cli._runtime import build_local_runtime, shutdown_runtime
from movate.templates import ROLE_TEMPLATES, SKILL_TEMPLATES, TEMPLATES

console = Console()
err_console = Console(stderr=True)

_DEFAULT_MODEL = "anthropic/claude-haiku-4-5-20251001"

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_PLAN_SYSTEM_PROMPT = """\
You are a project planner for the Movate AI agent platform. Given a description of what \
the operator wants to build, you produce a structured JSON project plan.

Available agent shape templates:
{shape_templates}

Available role templates (preferred — polished, ready-to-deploy):
{role_templates}

Available skill templates:
{skill_templates}

Rules:
1. Prefer role templates over shape templates when a role fits.
2. Use only templates from the lists above. For skills not in the skill template list,
   use "custom" as the template and describe the impl in the purpose field.
3. Agent names must be lowercase slugs (letters, digits, hyphens only).
4. Include at most 6 agents. Keep it focused.
5. workflow is a list of agent names in execution order. For a single agent, omit it.
6. contexts is a list of context names (e.g. ["brand-voice", "kb-policy"]).
   Use an empty list if no shared contexts are needed.

Respond with ONLY a JSON object — no prose, no markdown, no code fences:
{{
  "project_name": "<slug>",
  "description": "<one-line summary>",
  "agents": [
    {{"name": "<slug>", "template": "<template-name>", "purpose": "<what this agent does>"}}
  ],
  "skills": [
    {{"name": "<skill-slug>", "template": "<template-or-custom>", "used_by": ["<agent>"]}}
  ],
  "contexts": ["<context-name>"],
  "workflow": ["<agent-name>"]
}}
"""


def _build_system_prompt() -> str:
    shapes = "\n".join(f"  - {n}" for n in sorted(TEMPLATES.keys()))
    roles = "\n".join(f"  - {n}" for n in sorted(ROLE_TEMPLATES.keys()))
    skills = "\n".join(f"  - {n}" for n in sorted(SKILL_TEMPLATES.keys()))
    return _PLAN_SYSTEM_PROMPT.format(
        shape_templates=shapes,
        role_templates=roles,
        skill_templates=skills,
    )


# ---------------------------------------------------------------------------
# Mock plan for offline / hermetic use
# ---------------------------------------------------------------------------


def _mock_plan(description: str) -> dict[str, Any]:
    slug = re.sub(r"[^a-z0-9]+", "-", description.lower()).strip("-")[:40] or "my-project"
    return {
        "project_name": slug,
        "description": description,
        "agents": [{"name": "primary-agent", "template": "faq", "purpose": "Primary agent (mock)"}],
        "skills": [],
        "contexts": [],
        "workflow": [],
    }


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


async def _call_planner(description: str, *, model: str, mock: bool) -> dict[str, Any]:
    rt = await build_local_runtime(mock=mock)
    try:
        if mock:
            return _mock_plan(description)

        from movate.providers.base import CompletionRequest, Message  # noqa: PLC0415

        request = CompletionRequest(
            provider=model,
            messages=[
                Message(role="system", content=_build_system_prompt()),
                Message(role="user", content=f"Build a project for: {description}"),
            ],
            params={"temperature": 0.3, "max_tokens": 1024},
        )
        response = await rt.provider.complete(request)
        text = (response.text or "").strip()

        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        plan: dict[str, Any] = json.loads(text)
        if not isinstance(plan, dict):
            raise ValueError("plan LLM returned non-object JSON")
        return plan
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_plan(plan: dict[str, Any]) -> None:
    project_name = plan.get("project_name", "unnamed")
    desc = plan.get("description", "")
    agents = plan.get("agents") or []
    skills = plan.get("skills") or []
    contexts = plan.get("contexts") or []
    workflow = plan.get("workflow") or []

    tree = Tree(f"[bold]{project_name}[/bold]  [dim]{desc}[/dim]")

    agents_branch = tree.add("[cyan]agents[/cyan]")
    for a in agents:
        agents_branch.add(
            f"[green]{a['name']}[/green]  [dim]template:[/dim] {a.get('template', '?')}"
            f"  [dim]—[/dim] {a.get('purpose', '')}"
        )

    if skills:
        skills_branch = tree.add("[yellow]skills[/yellow]")
        for s in skills:
            used = ", ".join(s.get("used_by") or [])
            skills_branch.add(
                f"[yellow]{s['name']}[/yellow]  [dim]template:[/dim] {s.get('template', '?')}"
                + (f"  [dim]used by:[/dim] {used}" if used else "")
            )

    if contexts:
        ctx_branch = tree.add("[magenta]contexts[/magenta]")
        for c in contexts:
            ctx_branch.add(f"[magenta]{c}[/magenta]")

    if workflow:
        wf_branch = tree.add("[blue]workflow[/blue]")
        wf_branch.add(" → ".join(workflow))

    console.print(
        Panel(
            tree,
            title="[bold]mdk plan[/bold]",
            subtitle="[dim]--apply to scaffold[/dim]",
            border_style="bright_blue",
            padding=(1, 2),
        )
    )


# ---------------------------------------------------------------------------
# Apply mode
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")


def _apply_plan(plan: dict[str, Any], target: Path) -> None:
    from movate.cli.init import _init_agent  # noqa: PLC0415

    agents = plan.get("agents") or []
    skills = plan.get("skills") or []
    contexts = plan.get("contexts") or []
    workflow = plan.get("workflow") or []

    agents_dir = target / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    for a in agents:
        name = _slugify(a.get("name", "agent"))
        template = a.get("template", "default")
        console.print(f"  [dim]→[/dim] init [bold]{name}[/bold] (template: {template})")
        try:
            _init_agent(name=name, template=template, target=agents_dir, force=False, quiet=True)
        except Exception as exc:
            err_console.print(f"[yellow]![/yellow] could not init {name}: {exc}")

    for s in skills:
        sname = _slugify(s.get("name", "skill"))
        stemplate = s.get("template", "default")
        skill_dir = target / "skills" / sname
        if skill_dir.exists():
            console.print(f"  [dim]→[/dim] skill [bold]{sname}[/bold] already exists, skipping")
            continue
        console.print(f"  [dim]→[/dim] scaffold skill [bold]{sname}[/bold] (template: {stemplate})")
        try:
            from movate.templates import SKILL_TEMPLATES, TEMPLATES_DIR  # noqa: PLC0415

            if stemplate in SKILL_TEMPLATES:
                src = TEMPLATES_DIR / SKILL_TEMPLATES[stemplate]
                shutil.copytree(src, skill_dir)
            else:
                _scaffold_stub_skill(skill_dir, sname)
        except Exception as exc:
            err_console.print(f"[yellow]![/yellow] could not scaffold skill {sname}: {exc}")

    contexts_dir = target / "contexts"
    contexts_dir.mkdir(parents=True, exist_ok=True)
    for c in contexts:
        cname = _slugify(c)
        ctx_file = contexts_dir / f"{cname}.md"
        if ctx_file.exists():
            continue
        console.print(f"  [dim]→[/dim] context [bold]{cname}[/bold]")
        ctx_file.write_text(f"# {cname}\n\n<!-- Add your {cname} context here. -->\n")

    if len(agents) > 1 and workflow:
        _write_workflow(target, workflow, agents)


def _scaffold_stub_skill(skill_dir: Path, name: str) -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text(
        f"api_version: movate/v1\nkind: Skill\n\nname: {name}\nversion: 0.1.0\n"
        f"description: '{name} skill — fill in implementation'\n\n"
        f"schema:\n  input: {{}}\n  output: {{}}\n\n"
        f"implementation:\n  kind: python\n  entry: impl:run\n\n"
        f"cost:\n  per_call_usd: 0.0\n\nside_effects: read-only\n"
    )
    (skill_dir / "impl.py").write_text(
        "from __future__ import annotations\nfrom typing import Any\n\n\n"
        "async def run(input: dict[str, Any], ctx: Any) -> dict[str, Any]:\n"
        "    raise NotImplementedError\n"
    )


def _write_workflow(target: Path, workflow: list[str], agents: list[dict[str, Any]]) -> None:
    agent_names = {_slugify(a.get("name", "")) for a in agents}
    steps = [_slugify(n) for n in workflow if _slugify(n) in agent_names]
    if not steps:
        return
    wf_path = target / "workflow.yaml"
    if wf_path.exists():
        console.print("  [dim]→[/dim] workflow.yaml already exists, skipping")
        return
    lines = ["api_version: movate/v1", "kind: Workflow", "", "steps:"]
    for i, step in enumerate(steps):
        lines.append(f"  - name: {step}")
        lines.append(f"    agent: {step}")
        if i < len(steps) - 1:
            lines.append(f"    next: {steps[i + 1]}")
    console.print("  [dim]→[/dim] workflow.yaml")
    wf_path.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def plan(
    description: str = typer.Option(
        ...,
        "--from",
        "-f",
        help="Natural-language description of the project to build.",
        show_default=False,
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Scaffold the plan into the current directory (default: dry-run).",
    ),
    mock: bool = typer.Option(
        False,
        "--mock",
        help="Return a stub plan without calling an LLM (for tests / offline use).",
        hidden=True,
    ),
    model: str = typer.Option(
        _DEFAULT_MODEL,
        "--model",
        help="Provider/model string to use for the planner call.",
    ),
    output_json: bool = typer.Option(
        False,
        "--json",
        help="Print the raw plan as JSON and exit (useful for piping).",
    ),
) -> None:
    """Generate a project plan from a natural-language description.

    [bold]Examples:[/bold]
      [dim]$ mdk plan --from "support triage that routes tickets and drafts replies"[/dim]
      [dim]$ mdk plan --from "rag Q&A over company docs" --apply[/dim]
      [dim]$ mdk plan --from "sql assistant" --json | jq .agents[/dim]
    """
    try:
        plan_data = asyncio.run(_call_planner(description, model=model, mock=mock))
    except json.JSONDecodeError as exc:
        err_console.print(f"[red]✗[/red] planner returned invalid JSON: {exc}")
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        err_console.print(f"[red]✗[/red] plan failed: {exc}")
        raise typer.Exit(code=1) from exc

    if output_json:
        console.print_json(json.dumps(plan_data))
        return

    _render_plan(plan_data)

    if apply:
        target = Path.cwd()
        console.print("\n[bold]Applying plan …[/bold]")
        _apply_plan(plan_data, target=target)
        console.print("\n[green]✓[/green] plan applied — run [bold]mdk validate[/bold] to check.")
    else:
        console.print("\n[dim]Dry-run. Pass [bold]--apply[/bold] to scaffold the project.[/dim]")
