"""``movate validate <path>`` — load + validate an agent or a workflow.

Auto-detects: a path with ``workflow.yaml`` validates as a workflow (compile
+ ``validate_linear`` v0.3 phase gate); otherwise validates as an agent.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from movate.cli._completion import complete_agent_path
from movate.cli._workflow_path import is_workflow_path
from movate.core.config import load_project_config
from movate.core.cost_forecast import estimate_eval_cost
from movate.core.loader import AgentLoadError, load_agent
from movate.core.prompt_linter import LintIssue, lint_prompt
from movate.core.workflow import (
    WorkflowCompileError,
    compile_workflow,
    load_workflow_spec,
    validate_linear,
)
from movate.core.workflow.spec import WorkflowSpecLoadError
from movate.providers.pricing import load_pricing

console = Console()


def validate(
    path: Path = typer.Argument(
        ...,
        help="Path to an agent or workflow directory.",
        shell_complete=complete_agent_path,
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Promote lint warnings to errors (exit 2 on any warning). CI gate flag.",
    ),
    no_lint: bool = typer.Option(
        False,
        "--no-lint",
        help="Skip the prompt linter (schema + policy checks still run).",
    ),
) -> None:
    """Validate ``agent.yaml`` (or ``workflow.yaml``) plus its references."""
    if is_workflow_path(path):
        _validate_workflow(path)
    else:
        _validate_agent(path, strict=strict, run_linter=not no_lint)


def _validate_agent(path: Path, *, strict: bool, run_linter: bool) -> None:
    try:
        bundle = load_agent(path)
    except AgentLoadError as exc:
        console.print(f"[red]✗ validation failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    spec = bundle.spec

    # Project-wide model policy. ``check_agent`` returns an empty list
    # if the project has no policy or the agent is compliant. Reported
    # AFTER the load succeeds so the operator gets both the load error
    # (if any) and the policy error in the order they'd hit at runtime.
    policy = load_project_config().policy
    if not policy.is_permissive():
        violations = policy.check_agent(spec)
        if violations:
            console.print(
                f"[red]✗ policy violation:[/red] agent {spec.name!r} violates movate.yaml: policy"
            )
            for v in violations:
                console.print(f"  [red]·[/red] {v}")
            console.print(
                "[dim]  fix: relax the policy in movate.yaml, or change the agent to comply.[/dim]"
            )
            raise typer.Exit(code=2)

    # Prompt linter — runs by default; --no-lint to skip; --strict to
    # promote warnings to errors. Reports BEFORE the success banner so
    # the operator sees lint findings even when the schema check
    # already passed.
    lint_issues: list[LintIssue] = [] if not run_linter else lint_prompt(bundle)
    if lint_issues:
        _render_lint_issues(lint_issues)

    console.print(f"[green]✓[/green] {spec.name} [dim]v{spec.version}[/dim] [dim](agent)[/dim]")
    console.print(f"  api_version: {spec.api_version}")
    console.print(f"  provider:    {spec.model.provider}")
    console.print(f"  prompt:      {bundle.prompt_hash[:12]}…")
    if spec.model.fallback:
        fbs = ", ".join(f.provider for f in spec.model.fallback)
        console.print(f"  fallback:    {fbs}")
    if not policy.is_permissive():
        console.print("  [dim]policy:      ✓ compliant[/dim]")
    if run_linter and not lint_issues:
        console.print("  [dim]lint:        ✓ clean[/dim]")

    # Cost forecast — silent when no dataset / no pricing for model /
    # empty dataset. The estimate_eval_cost helper returns None in
    # every "skip" case so this stays a single conditional.
    try:
        forecast = estimate_eval_cost(bundle, pricing=load_pricing())
    except Exception:  # pragma: no cover — defensive; load_pricing rarely fails
        forecast = None
    if forecast is not None:
        console.print(
            f"  [dim]eval cost:   ~${forecast.total_cost_usd:.4f} "
            f"({forecast.cases} cases x "
            f"~{forecast.input_tokens_per_call} in + "
            f"~{forecast.output_tokens_per_call} out tokens)[/dim]"
        )

    # Exit non-zero if there are real errors (always) or warnings
    # under --strict (CI gate mode).
    has_errors = any(i.severity == "error" for i in lint_issues)
    has_warnings = any(i.severity == "warning" for i in lint_issues)
    if has_errors or (strict and has_warnings):
        raise typer.Exit(code=2)


def _render_lint_issues(issues: list[LintIssue]) -> None:
    """Print lint findings — errors first, then warnings. Each issue
    gets a single-line summary with severity color + code, plus an
    optional dim hint line below."""
    # Sort: errors first (so the most important findings are at the
    # top of the output), then by code for stable ordering across
    # invocations.
    ordered = sorted(
        issues,
        key=lambda i: (0 if i.severity == "error" else 1, i.code),
    )
    for issue in ordered:
        color = "red" if issue.severity == "error" else "yellow"
        label = "✗" if issue.severity == "error" else "!"
        console.print(
            f"  [{color}]{label}[/{color}] [{color}]{issue.code}[/{color}]: {issue.message}"
        )
        if issue.hint:
            console.print(f"    [dim]hint: {issue.hint}[/dim]")


def _validate_workflow(path: Path) -> None:
    try:
        spec, parent = load_workflow_spec(path)
    except WorkflowSpecLoadError as exc:
        console.print(f"[red]✗ workflow.yaml load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None
    try:
        graph = compile_workflow(spec, parent)
        validate_linear(graph)
    except WorkflowCompileError as exc:
        console.print(f"[red]✗ workflow validation failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    console.print(
        f"[green]✓[/green] {graph.name} [dim]v{graph.version}[/dim] [dim](workflow)[/dim]"
    )
    console.print(f"  api_version: {spec.api_version}")
    console.print(f"  entrypoint:  {graph.entrypoint}")
    console.print(f"  nodes:       {len(graph.nodes)}")
    console.print(f"  edges:       {len(graph.edges)}")
    chain = " → ".join(graph.topological_order())
    console.print(f"  topology:    {chain}")
