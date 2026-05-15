"""``movate validate <path>`` — load + validate an agent or a workflow.

Auto-detects: a path with ``workflow.yaml`` validates as a workflow (compile
+ ``validate_linear`` v0.3 phase gate); otherwise validates as an agent.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

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
    path: Path | None = typer.Argument(
        None,
        help=(
            "Path (or bare name) of an agent or workflow. Omit to "
            "validate EVERY agent + workflow in the current project — "
            "the default since the typical flow after "
            "[bold]mdk init --project --with-agents X,Y,Z[/bold] is "
            "'check everything I just made'."
        ),
        shell_complete=complete_agent_path,
    ),
    all_in_project: bool = typer.Option(
        False,
        "--all",
        help=(
            "Explicit form of the no-arg default — validate every agent + "
            "workflow in the current project. Kept for back-compat with "
            "scripts that pass [bold]--all[/bold] verbatim. Mutually "
            "exclusive with a path argument."
        ),
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
    """Validate ``agent.yaml`` (or ``workflow.yaml``) plus its references.

    Three modes depending on arguments:

    * **No path** (default): validate every agent + workflow in the
      current project. Renders a summary table; exits non-zero if any
      fail. Same behavior as the older [bold]--all[/bold] flag.
    * **Path / bare name**: validate just that one. Inside a project
      you can pass a bare name ([bold]mdk validate rag-qa[/bold]); it
      resolves under [bold]./agents/<name>/[/bold] or
      [bold]./workflows/<name>/[/bold].
    * **Outside a project + no path**: error with a pointer to
      [bold]mdk init --project[/bold].

    [bold]--all[/bold] is the explicit form of the no-arg default —
    kept for back-compat with scripts that pass it verbatim.
    """
    # Mutex: passing BOTH a path AND --all is almost certainly a typo;
    # reject explicitly rather than silently picking one.
    if all_in_project and path is not None and str(path) != ".":
        console.print(
            "[red]✗[/red] [bold]--all[/bold] and an explicit path argument are mutually exclusive."
        )
        raise typer.Exit(code=2)

    # No path arg → check if we're inside a project and default to
    # whole-project validation. Outside a project this errors with a
    # hint (same shape as the old --all-required behavior, but the
    # operator never had to type the flag).
    if path is None or all_in_project:
        if _resolve_project_root() is None:
            console.print(
                "[red]✗[/red] not inside a movate project — nothing to "
                "validate.\n"
                "[dim]Either pass a path ([bold]mdk validate <path>[/bold]) "
                "or run [bold]mdk init --project <name>[/bold] first.[/dim]"
            )
            raise typer.Exit(code=2)
        _validate_all(strict=strict, run_linter=not no_lint)
        return

    # Bare-name resolution: `mdk validate rag-qa` → `./agents/rag-qa`
    # when inside a project. Full paths pass through unchanged.
    from movate.cli._resolve import resolve_agent_or_workflow_arg  # noqa: PLC0415

    path = Path(resolve_agent_or_workflow_arg(str(path)))

    if is_workflow_path(path):
        _validate_workflow(path)
    else:
        _validate_agent(path, strict=strict, run_linter=not no_lint)


def _resolve_project_root() -> Path | None:
    """Walk up from cwd looking for ``movate.yaml`` — same convention
    used by ``mdk add`` / ``mdk snapshot`` / ``mdk diff``.

    Local to validate.py to avoid an import cycle through ``mdk add``
    (which is the canonical owner of the walk-up routine).
    """
    current = Path.cwd().resolve()
    while True:
        if (current / "movate.yaml").is_file():
            return current
        if current.parent == current:
            return None
        current = current.parent


def _validate_all(*, strict: bool, run_linter: bool) -> None:
    """Validate every agent + workflow in the current project.

    Walks ``./agents/*/agent.yaml`` and ``./workflows/*/workflow.yaml``
    under the project root, runs the same validation each is subject
    to as a single ``mdk validate <name>`` invocation, then renders a
    Rich Table summarizing pass/fail per item. Exits 0 if every item
    passed, 2 if any failed.

    The greppable ``mdk_validate_summary:`` line at the end (same
    prefix family as ``mdk_init_summary`` / ``mdk_audit_summary`` etc.)
    lets CI tail one stable token to learn workspace-level
    validation health.
    """
    project_root = _resolve_project_root()
    if project_root is None:
        console.print(
            "[red]✗[/red] not inside a movate project. "
            "[dim]Run [bold]mdk init --project <name>[/bold] first, or "
            "pass a path argument to validate one item.[/dim]"
        )
        raise typer.Exit(code=2)

    # Discover targets. Sorted for deterministic output across runs.
    agent_dirs = (
        sorted(p.parent for p in (project_root / "agents").glob("*/agent.yaml"))
        if (project_root / "agents").is_dir()
        else []
    )
    workflow_dirs = (
        sorted(p.parent for p in (project_root / "workflows").glob("*/workflow.yaml"))
        if (project_root / "workflows").is_dir()
        else []
    )

    if not agent_dirs and not workflow_dirs:
        console.print(
            "[yellow]⚠[/yellow] no agents or workflows found under "
            f"[dim]{project_root}/agents/[/dim] or "
            f"[dim]{project_root}/workflows/[/dim]."
        )
        # Not an error — operator may have just bootstrapped an empty
        # project. Exit 0 with the warning so CI can decide.
        console.print(
            "[dim]mdk_validate_summary: "
            "agents_total=0 workflows_total=0 "
            "passed=0 failed=0 ok=true[/dim]"
        )
        return

    # Per-item results: (kind, name, status, detail).
    rows: list[tuple[str, str, str, str]] = []
    failed = 0

    for agent_dir in agent_dirs:
        try:
            _validate_agent(agent_dir, strict=strict, run_linter=run_linter)
            rows.append(("agent", agent_dir.name, "ok", ""))
        except typer.Exit:
            # _validate_agent already printed the failure detail.
            rows.append(("agent", agent_dir.name, "failed", ""))
            failed += 1

    for workflow_dir in workflow_dirs:
        try:
            _validate_workflow(workflow_dir)
            rows.append(("workflow", workflow_dir.name, "ok", ""))
        except typer.Exit:
            rows.append(("workflow", workflow_dir.name, "failed", ""))
            failed += 1

    # Render the summary table.
    table = Table(
        title=(
            f"Project validation — [bold]{project_root.name}[/bold] "
            f"[dim]({len(rows)} item(s))[/dim]"
        ),
        title_style="bold",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Kind", no_wrap=True)
    table.add_column("Name", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    for kind, name, status, _detail in rows:
        marker = "[green]✓ ok[/green]" if status == "ok" else "[red]✗ failed[/red]"
        table.add_row(kind, name, marker)
    console.print()
    console.print(table)

    passed = len(rows) - failed
    console.print(
        f"[dim]mdk_validate_summary: "
        f"agents_total={len(agent_dirs)} "
        f"workflows_total={len(workflow_dirs)} "
        f"passed={passed} failed={failed} "
        f"ok={'true' if failed == 0 else 'false'}[/dim]"
    )

    if failed:
        raise typer.Exit(code=2)


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
