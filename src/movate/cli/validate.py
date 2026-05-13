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
from movate.core.models import AgentRuntime
from movate.core.prompt_linter import LintIssue, lint_prompt
from movate.core.workflow import (
    WorkflowCompileError,
    compile_workflow,
    load_workflow_spec,
    validate_linear,
)
from movate.core.workflow.spec import WorkflowSpecLoadError
from movate.providers.pricing import load_pricing


def _available_runtimes() -> frozenset[AgentRuntime]:
    """Which runtimes this install can actually execute.

    LiteLLM is always available (core dep). Native-SDK adapters are
    optional extras — we probe their package import to decide whether
    to mark the runtime available. Probed each call rather than at
    import time so a user who pip-installs ``movate-cli[anthropic]``
    after first run sees the new runtime immediately."""
    available: set[AgentRuntime] = {AgentRuntime.LITELLM}
    try:
        import anthropic  # noqa: F401, PLC0415

        available.add(AgentRuntime.NATIVE_ANTHROPIC)
    except ImportError:
        pass
    try:
        import openai  # noqa: F401, PLC0415

        available.add(AgentRuntime.NATIVE_OPENAI)
    except ImportError:
        pass
    try:
        import langchain_core  # noqa: F401, PLC0415

        available.add(AgentRuntime.LANGCHAIN)
    except ImportError:
        pass
    # Lyzr adapter is HTTP-only — no SDK to probe. It's always
    # available; the LYZR_API_KEY check is deferred to runtime so
    # `mdk validate` of a Lyzr-runtime agent works pre-credential.
    available.add(AgentRuntime.LYZR)
    return frozenset(available)


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

    # Runtime-availability check. Reject agents that declare a runtime
    # this build doesn't wire (e.g. `runtime: native_anthropic` against
    # v0.5 which only ships LiteLLM). Fail fast HERE so the operator
    # learns before any execute attempt — same exit-2 semantics as a
    # bad policy or a bad schema.
    available = _available_runtimes()
    if spec.runtime not in available:
        console.print(
            f"[red]✗ unsupported runtime:[/red] agent {spec.name!r} "
            f"declares [bold]runtime: {spec.runtime.value}[/bold], but "
            f"this install only ships: "
            f"{sorted(r.value for r in available)}."
        )
        # Hint how to enable the missing runtime.
        if spec.runtime == AgentRuntime.NATIVE_ANTHROPIC:
            console.print("[dim]  Install with: uv add 'movate-cli[anthropic]'[/dim]")
        elif spec.runtime == AgentRuntime.NATIVE_OPENAI:
            console.print("[dim]  Install with: uv add 'movate-cli[openai]'[/dim]")
        elif spec.runtime == AgentRuntime.LANGCHAIN:
            console.print("[dim]  Install with: uv add 'movate-cli[langchain]'[/dim]")
        raise typer.Exit(code=2)

    # Project-wide runtime policy. Distinct from the model policy below —
    # this gate is "may this AGENT use this RUNTIME?" rather than "may this
    # model+budget combo run?". Default is permissive; setting
    # ``runtime.allowed: [litellm]`` in movate.yaml enforces 'A by default'.
    project_cfg = load_project_config()
    runtime_violation = project_cfg.runtime.check_agent(spec)
    if runtime_violation is not None:
        console.print(f"[red]✗ runtime policy violation:[/red] {runtime_violation}")
        console.print(
            "[dim]  fix: relax movate.yaml: runtime.allowed, or change the "
            "agent's runtime field.[/dim]"
        )
        raise typer.Exit(code=2)

    # Project-wide model policy. ``check_agent`` returns an empty list
    # if the project has no policy or the agent is compliant. Reported
    # AFTER the load succeeds so the operator gets both the load error
    # (if any) and the policy error in the order they'd hit at runtime.
    policy = project_cfg.policy
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

    # Project-wide skill policy. Checks each resolved skill's
    # ``side_effects`` against the project's allowlist. Same shape as
    # the model-policy block above — multiple skill violations report
    # together so the operator sees the full picture.
    skill_policy = project_cfg.skills
    if not skill_policy.is_permissive():
        skill_violations = skill_policy.check_agent_skills(bundle.skills)
        if skill_violations:
            console.print(
                f"[red]✗ skill policy violation:[/red] agent {spec.name!r} "
                f"uses skills outside the project's allowed side-effects"
            )
            for v in skill_violations:
                console.print(f"  [red]·[/red] {v}")
            console.print(
                "[dim]  fix: relax policy.yaml: skills.allowed_side_effects, "
                "or change the agent's skill list.[/dim]"
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
    console.print(f"  runtime:     {spec.runtime.value}")
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
