"""``movate show <path>`` — print the resolved configuration for an agent or workflow."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from movate.cli._completion import complete_agent_path
from movate.cli._workflow_path import is_workflow_path
from movate.core.loader import AgentLoadError, load_agent
from movate.core.models import AgentSpec
from movate.core.workflow import (
    WorkflowCompileError,
    WorkflowGraph,
    compile_workflow,
    load_workflow_spec,
)
from movate.core.workflow.spec import WorkflowSpecLoadError

console = Console()


def show(
    path: Path | None = typer.Argument(
        None,
        help=(
            "Path to an agent, workflow, or skill directory. "
            "Omit with [bold]--project[/bold] for a project-wide asset map."
        ),
        shell_complete=complete_agent_path,
    ),
    project: bool = typer.Option(
        False,
        "--project",
        help=(
            "Show a project-wide asset map: contexts, skills, and KB files "
            "with which agents declare each one."
        ),
    ),
) -> None:
    """Show the resolved spec for an agent, workflow, or skill.

    Pass [bold]--project[/bold] to get a full map of every context, skill,
    and KB file in the project and which agents declare each one.
    """
    if project:
        _show_project()
        return
    if path is None:
        console.print(
            "[red]✗[/red] provide a path argument or pass [bold]--project[/bold] "
            "for the project-wide asset map."
        )
        raise typer.Exit(code=2)
    # Dispatch by what file the directory contains. Order matters —
    # workflows have their own ``workflow.yaml``; skills have
    # ``skill.yaml``; agents have ``agent.yaml``. Each is mutually
    # exclusive in the canonical project layout.
    if is_workflow_path(path):
        _show_workflow(path)
    elif (path / "skill.yaml").exists():
        _show_skill(path)
    else:
        _show_agent(path)


# ---------------------------------------------------------------------------
# Project-wide asset map
# ---------------------------------------------------------------------------


def _show_project() -> None:  # noqa: PLR0912 — three independent asset tables
    """Render a project-wide map: which agents declare each context/skill/KB file."""
    import yaml  # noqa: PLC0415

    from movate.cli._resolve import walk_up_for_project_root  # noqa: PLC0415

    project_root = walk_up_for_project_root()
    if project_root is None:
        console.print(
            "[red]✗[/red] not inside a movate project (no project.yaml / policy.yaml "
            "/ movate.yaml up the tree)."
        )
        raise typer.Exit(code=2)

    agent_dirs = (
        sorted(p.parent for p in (project_root / "agents").glob("*/agent.yaml"))
        if (project_root / "agents").is_dir()
        else []
    )

    # Collect declared items per agent. Maps: name → list[agent_name].
    contexts_map: dict[str, list[str]] = {}
    skills_map: dict[str, list[str]] = {}

    for agent_dir in agent_dirs:
        try:
            raw = yaml.safe_load((agent_dir / "agent.yaml").read_text())
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        for ctx in raw.get("contexts") or []:
            contexts_map.setdefault(ctx, []).append(agent_dir.name)
        for skill in raw.get("skills") or []:
            skills_map.setdefault(skill, []).append(agent_dir.name)

    console.print(f"\n[bold]Project asset map — {project_root.name}[/bold]\n")

    # Contexts table.
    ctx_table = Table(title="Contexts", show_header=True, header_style="bold")
    ctx_table.add_column("Name", no_wrap=True)
    ctx_table.add_column("Size", no_wrap=True, justify="right")
    ctx_table.add_column("Declared by")

    ctx_root = project_root / "contexts"
    ctx_files = sorted(
        f for f in (ctx_root.glob("*.md") if ctx_root.is_dir() else [])
        if f.name.lower() != "readme.md"
    )
    for ctx_file in ctx_files:
        size = ctx_file.stat().st_size
        agents_using = contexts_map.get(ctx_file.stem, [])
        ctx_table.add_row(
            ctx_file.name,
            f"{size:,} B",
            ", ".join(agents_using) if agents_using else "[dim]orphaned[/dim]",
        )
    if not ctx_files:
        ctx_table.add_row("[dim]none[/dim]", "", "")
    console.print(ctx_table)

    # Skills table.
    skill_table = Table(title="Skills", show_header=True, header_style="bold")
    skill_table.add_column("Name", no_wrap=True)
    skill_table.add_column("Kind", no_wrap=True)
    skill_table.add_column("Declared by")

    skills_root = project_root / "skills"
    skill_dirs = (
        [d for d in sorted(skills_root.iterdir()) if d.is_dir() and (d / "skill.yaml").is_file()]
        if skills_root.is_dir()
        else []
    )
    for skill_dir in skill_dirs:
        try:
            raw_skill = yaml.safe_load((skill_dir / "skill.yaml").read_text())
            kind = (raw_skill.get("implementation") or {}).get("kind", "?")
        except Exception:
            kind = "?"
        agents_using = skills_map.get(skill_dir.name, [])
        skill_table.add_row(
            skill_dir.name,
            kind,
            ", ".join(agents_using) if agents_using else "[dim]orphaned[/dim]",
        )
    if not skill_dirs:
        skill_table.add_row("[dim]none[/dim]", "", "")
    console.print(skill_table)

    # KB files table.
    kb_table = Table(title="KB Files", show_header=True, header_style="bold")
    kb_table.add_column("File", no_wrap=True)
    kb_table.add_column("Entries", no_wrap=True, justify="right")
    kb_table.add_column("Used by agents with kb-lookup")

    kb_root = project_root / "kb"
    kb_files = sorted(kb_root.glob("*.json")) if kb_root.is_dir() else []
    kb_agents = [a for a in skills_map.get("kb-lookup", [])]
    for kb_file in kb_files:
        try:
            import json as _json  # noqa: PLC0415

            data = _json.loads(kb_file.read_text())
            entry_count = str(len(data)) if isinstance(data, list) else "?"
        except Exception:
            entry_count = "?"
        kb_table.add_row(
            kb_file.name,
            entry_count,
            ", ".join(kb_agents) if kb_agents else "[dim]none[/dim]",
        )
    if not kb_files:
        kb_table.add_row("[dim]none[/dim]", "", "")
    console.print(kb_table)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


def _show_agent(path: Path) -> None:
    try:
        bundle = load_agent(path)
    except AgentLoadError as exc:
        console.print(f"[red]✗ load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    spec = bundle.spec
    table = Table(title=f"{spec.name} v{spec.version}", show_header=False)
    table.add_column("field", style="dim")
    table.add_column("value")

    table.add_row("api_version", spec.api_version)
    table.add_row("kind", spec.kind)
    table.add_row("name", spec.name)
    table.add_row("version", spec.version)
    table.add_row("description", spec.description or "[dim]—[/dim]")
    table.add_row("owner", spec.owner or "[dim]—[/dim]")
    _add_marketplace_metadata_rows(table, spec)
    table.add_row("", "")
    table.add_row("model.provider", spec.model.provider)
    if spec.model.params:
        table.add_row("model.params", json.dumps(spec.model.params))
    if spec.model.fallback:
        fb = ", ".join(f.provider for f in spec.model.fallback)
        table.add_row("model.fallback", fb)
    table.add_row("", "")
    table.add_row("prompt", str(spec.prompt))
    table.add_row("prompt_hash", bundle.prompt_hash[:16] + "…")
    table.add_row("input.schema", _render_schema_ref(spec.schemas.input))
    in_required = bundle.input_schema.get("required", [])
    table.add_row("  required", ", ".join(in_required) if in_required else "[dim]—[/dim]")
    table.add_row("output.schema", _render_schema_ref(spec.schemas.output))
    out_required = bundle.output_schema.get("required", [])
    table.add_row("  required", ", ".join(out_required) if out_required else "[dim]—[/dim]")
    # Skills section: only render if the agent declares any. Keeps
    # the table tight for the single-shot (no-skills) common case.
    if spec.skills:
        table.add_row("", "")
        table.add_row("skills", ", ".join(spec.skills))
        for skill_bundle in bundle.skills:
            cost = skill_bundle.spec.cost.per_call_usd
            cost_str = f" [dim]${cost:.4f}/call[/dim]" if cost > 0 else ""
            table.add_row(
                f"  {skill_bundle.spec.name}",
                f"{skill_bundle.spec.implementation.kind.value}: "
                f"{skill_bundle.spec.implementation.entry}{cost_str}",
            )
    # Contexts: shared markdown fragments prepended to the prompt at
    # render time. Show byte size per context so operators can spot a
    # runaway context file inflating prompt cost.
    if spec.contexts:
        table.add_row("", "")
        table.add_row("contexts", ", ".join(spec.contexts))
        for ctx_name, ctx_body in bundle.contexts:
            byte_count = len(ctx_body.encode("utf-8"))
            table.add_row(f"  {ctx_name}", f"[dim]{byte_count:,} bytes[/dim]")
    table.add_row("", "")
    if spec.evals.dataset:
        ds_path = (bundle.agent_dir / spec.evals.dataset).resolve()
        if ds_path.exists():
            raw = ds_path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()[:12]
            count = sum(1 for line in raw.decode().splitlines() if line.strip())
            table.add_row("evals.dataset", f"{spec.evals.dataset} ({count} cases, sha={digest}…)")
        else:
            table.add_row("evals.dataset", f"{spec.evals.dataset} [red](missing)[/red]")
    table.add_row("timeouts", f"call={spec.timeouts.call_ms}ms total={spec.timeouts.total_ms}ms")
    table.add_row("budget", f"${spec.budget.max_cost_usd_per_run:.4f}/run")
    if spec.tags:
        table.add_row("tags", ", ".join(spec.tags))

    console.print(table)
    console.print(f"[dim]agent_dir: {bundle.agent_dir}[/dim]")


def _add_marketplace_metadata_rows(table: Table, spec: AgentSpec) -> None:
    """Append role / persona / capabilities rows to the agent show table.

    Only adds rows the agent actually populated, so pre-v0.8 agents
    (where these fields default to empty) keep the same compact
    table as before the marketplace-metadata extension landed. Pulled
    out of ``_show_agent`` to keep that function's branch count under
    the ruff PLR0912 ceiling.

    See AgentSpec.persona / role / capabilities (item 29 from
    BACKLOG.md Group F).
    """
    if spec.role:
        table.add_row("role", spec.role)
    if spec.persona:
        table.add_row("persona", spec.persona)
    if spec.capabilities:
        table.add_row("capabilities", ", ".join(spec.capabilities))
    if spec.role or spec.persona or spec.capabilities:
        table.add_row(
            "",
            "[dim]roles/persona/capabilities are catalog discovery metadata; "
            "execution routing is not implemented[/dim]",
        )


def _render_schema_ref(ref: str | dict[str, object]) -> str:
    """Format a schemas.input / schemas.output value for the show table.

    Path strings render verbatim ("./schema/input.json"); inline
    shorthand dicts render as the literal "[dim]<inline>[/dim]" — the
    field list below the row already exposes what's inside, so dumping
    the raw dict would just be noise.
    """
    if isinstance(ref, dict):
        return "[dim]<inline>[/dim]"
    return str(ref)


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


def _show_skill(path: Path) -> None:
    """Render a skill spec as a Rich table — mirror of ``_show_agent``."""
    # Local import to avoid pulling skill_loader into show.py's
    # module-load path for the agent + workflow code paths that
    # don't need it.
    from movate.core.skill_loader import SkillLoadError, load_skill  # noqa: PLC0415

    try:
        bundle = load_skill(path)
    except SkillLoadError as exc:
        console.print(f"[red]✗ load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    spec = bundle.spec
    table = Table(
        title=f"{spec.name} v{spec.version} [dim](skill)[/dim]",
        show_header=False,
        title_justify="center",
    )
    table.add_column("Key", style="dim")
    table.add_column("Value", overflow="fold")
    table.add_row("api_version", spec.api_version)
    table.add_row("kind", spec.kind)
    table.add_row("name", spec.name)
    table.add_row("version", spec.version)
    table.add_row("description", spec.description or "[dim]—[/dim]")
    table.add_row("owner", spec.owner or "[dim]—[/dim]")
    table.add_row("", "")
    table.add_row("backend.kind", spec.implementation.kind.value)
    table.add_row("backend.entry", spec.implementation.entry or "[dim]—[/dim]")
    table.add_row("", "")
    table.add_row("input.schema", _render_schema_ref(spec.schemas.input))
    in_required = bundle.input_schema.get("required", [])
    table.add_row("  required", ", ".join(in_required) if in_required else "[dim]—[/dim]")
    table.add_row("output.schema", _render_schema_ref(spec.schemas.output))
    out_required = bundle.output_schema.get("required", [])
    table.add_row("  required", ", ".join(out_required) if out_required else "[dim]—[/dim]")
    table.add_row("", "")
    table.add_row("cost.per_call_usd", f"${spec.cost.per_call_usd:.4f}")
    table.add_row("side_effects", spec.side_effects.value)
    if spec.timeout_call_ms is not None:
        table.add_row("timeout_call_ms", str(spec.timeout_call_ms))
    if spec.tags:
        table.add_row("tags", ", ".join(spec.tags))
    console.print(table)
    console.print(f"[dim]skill_dir: {bundle.skill_dir}[/dim]")


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


def _show_workflow(path: Path) -> None:
    try:
        spec, parent = load_workflow_spec(path)
    except WorkflowSpecLoadError as exc:
        console.print(f"[red]✗ load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None
    try:
        graph = compile_workflow(spec, parent)
    except WorkflowCompileError as exc:
        console.print(f"[red]✗ compile failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    # Header table
    header = Table(title=f"{graph.name} v{graph.version} [dim](workflow)[/dim]", show_header=False)
    header.add_column("field", style="dim")
    header.add_column("value")
    header.add_row("api_version", spec.api_version)
    header.add_row("kind", "Workflow")
    header.add_row("description", graph.description or "[dim]—[/dim]")
    header.add_row("entrypoint", graph.entrypoint)
    header.add_row("nodes", str(len(graph.nodes)))
    header.add_row("edges", str(len(graph.edges)))
    state_required = graph.state_schema.get("required", [])
    header.add_row(
        "state.required",
        ", ".join(state_required) if state_required else "[dim]—[/dim]",
    )
    console.print(header)

    # Nodes
    nodes = Table(title="Nodes", show_header=True, header_style="bold")
    nodes.add_column("#", style="dim", width=3)
    nodes.add_column("id")
    nodes.add_column("type")
    nodes.add_column("ref", overflow="fold")
    for i, node_id in enumerate(graph.topological_order(), start=1):
        node = graph.nodes[node_id]
        nodes.add_row(str(i), node_id, node.type.value, node.ref)
    console.print(nodes)

    # Topology — ASCII chain (small, scannable)
    chain = " → ".join(graph.topological_order())
    console.print(f"\n[dim]topology:[/dim] {chain}\n")

    # Mermaid block — copy-paste into a GitHub PR description
    mermaid = render_mermaid(graph)
    console.print("[dim]Mermaid (paste into a PR):[/dim]")
    console.print(Syntax(mermaid, "mermaid", theme="ansi_dark", line_numbers=False))
    console.print(f"[dim]workflow_dir: {graph.workflow_dir}[/dim]")


def render_mermaid(graph: WorkflowGraph) -> str:
    """Render a Mermaid ``flowchart LR`` for the workflow's topology."""
    lines = ["flowchart LR"]
    for node_id in graph.topological_order():
        node = graph.nodes[node_id]
        # Use the node id as both the mermaid id and the visible label.
        lines.append(f"    {node_id}[{node_id}<br/><sub>{node.type.value}</sub>]")
    for edge in graph.edges:
        lines.append(f"    {edge.from_id} --> {edge.to_id}")
    return "\n".join(lines)
