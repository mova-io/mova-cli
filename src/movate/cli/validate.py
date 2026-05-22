"""``movate validate <path>`` — load + validate an agent or a workflow.

Auto-detects: a path with ``workflow.yaml`` validates as a workflow (compile
+ ``validate_linear`` v0.3 phase gate); otherwise validates as an agent.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

if TYPE_CHECKING:
    from jsonschema import Draft202012Validator

from movate.cli._completion import complete_agent_path
from movate.cli._resolve import walk_up_for_project_root
from movate.cli._workflow_path import is_workflow_path
from movate.core.config import PROJECT_MARKER_FILES, load_project_config
from movate.core.cost_forecast import estimate_eval_cost
from movate.core.loader import AgentBundle, AgentLoadError, load_agent
from movate.core.models import AgentRuntime, AgentSpec, SkillImplementationKind, SkillSpec
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
            "Path to an agent or workflow directory. Omit with "
            "[bold]--all[/bold] to validate every agent + workflow in the "
            "current project."
        ),
        shell_complete=complete_agent_path,
    ),
    all_in_project: bool = typer.Option(
        False,
        "--all",
        help=(
            "Validate every agent under [bold]./agents/[/bold] AND every "
            "workflow under [bold]./workflows/[/bold] in the current "
            "project. Renders a summary table; exits non-zero if any "
            "fail. Pairs with [bold]mdk init --project --with-agents[/bold] "
            "as the natural next step."
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
    json_output: bool = typer.Option(
        False,
        "--json",
        help=(
            "Emit a machine-readable JSON result instead of Rich output. "
            "Useful for CI dashboards and scripts that parse validate output."
        ),
    ),
) -> None:
    """Validate ``agent.yaml`` (or ``workflow.yaml``) plus its references.

    Inside a project you can pass a bare name (``mdk validate rag-qa``);
    it resolves under ``./agents/<name>/`` or ``./workflows/<name>/``.

    Use [bold]--all[/bold] to validate every agent + workflow in the
    current project in one shot — handy right after
    [bold]mdk init --project --with-agents X,Y,Z[/bold].
    """
    if all_in_project:
        # --all is mutually exclusive with a path argument. Passing
        # both is almost certainly a typo — surface it cleanly rather
        # than silently picking one.
        if path is not None and str(path) != ".":
            console.print(
                "[red]✗[/red] [bold]--all[/bold] and an explicit path "
                "argument are mutually exclusive."
            )
            raise typer.Exit(code=2)
        _validate_all(strict=strict, run_linter=not no_lint, json_output=json_output)
        return

    if path is None:
        # No path given → default to --all when inside a project (or any
        # subdirectory of one). Walk up so `mdk validate` works from
        # `agents/rag-qa/` just as well as from the project root.
        if walk_up_for_project_root() is not None:
            console.print(
                "[dim]no path given — defaulting to --all[/dim]",
                highlight=False,
            )
            _validate_all(strict=strict, run_linter=not no_lint, json_output=json_output)
            return
        console.print(
            "[red]✗[/red] not inside a movate project (no [bold]project.yaml[/bold] "
            "/ [bold]policy.yaml[/bold] / [bold]movate.yaml[/bold] up the tree). "
            "Pass an explicit [bold]<path>[/bold] to an agent or workflow, "
            "or run [bold]mdk validate[/bold] from inside a project root."
        )
        raise typer.Exit(code=2)

    # Bare-name resolution: `mdk validate rag-qa` → `./agents/rag-qa`
    # when inside a project. Full paths pass through unchanged.
    from movate.cli._resolve import resolve_agent_or_workflow_arg  # noqa: PLC0415

    path = Path(resolve_agent_or_workflow_arg(str(path)))

    if is_workflow_path(path):
        if json_output:
            try:
                _validate_workflow(path)
                console.print_json(json.dumps({"kind": "workflow", "name": path.name, "ok": True}))
            except typer.Exit:
                console.print_json(json.dumps({"kind": "workflow", "name": path.name, "ok": False}))
                raise
        else:
            _validate_workflow(path)
    elif json_output:
        try:
            _validate_agent(path, strict=strict, run_linter=not no_lint)
            console.print_json(json.dumps({"kind": "agent", "name": path.name, "ok": True}))
        except typer.Exit:
            console.print_json(json.dumps({"kind": "agent", "name": path.name, "ok": False}))
            raise
    else:
        _validate_agent(path, strict=strict, run_linter=not no_lint)


def _validate_all(*, strict: bool, run_linter: bool, json_output: bool = False) -> None:
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
    project_root = walk_up_for_project_root()
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

    # Orphaned-asset scan: contexts + skills that exist on disk but
    # aren't declared by any agent. Runs after per-agent validation so
    # the operator sees both "your agent is broken" and "you also have
    # unused files" in one pass. Skipped on total failure — if every
    # agent failed to load we can't distinguish declared from orphaned.
    if failed < len(rows):
        _check_orphaned_assets(project_root, agent_dirs)

    passed = len(rows) - failed

    if json_output:
        payload = {
            "project": project_root.name,
            "agents_total": len(agent_dirs),
            "workflows_total": len(workflow_dirs),
            "passed": passed,
            "failed": failed,
            "ok": failed == 0,
            "items": [
                {"kind": kind, "name": name, "ok": status == "ok"} for kind, name, status, _ in rows
            ],
        }
        console.print_json(json.dumps(payload))
        if failed:
            raise typer.Exit(code=2)
        return

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

    console.print(
        f"[dim]mdk_validate_summary: "
        f"agents_total={len(agent_dirs)} "
        f"workflows_total={len(workflow_dirs)} "
        f"passed={passed} failed={failed} "
        f"ok={'true' if failed == 0 else 'false'}[/dim]"
    )

    if failed:
        raise typer.Exit(code=2)

    # All-pass success — interactive picker scoped to the VALIDATE
    # domain (per operator feedback 2026-05-19). Previously offered
    # eval/run/deploy as next steps — those are downstream concerns
    # and added scrollback noise when validate was the entry point
    # for "did I configure this right?" Now the picker only surfaces
    # diagnostic / autofix-adjacent commands (``mdk doctor``) — the
    # one tool a validate-passing-but-something-feels-off operator
    # actually wants next. Re-running validate isn't offered because
    # the just-completed run is right above on screen.
    if passed > 0:
        from movate.cli._next_steps import (  # noqa: PLC0415
            NextStep,
            mdk_bin_name,
            prompt_next_step,
        )

        bin_name = mdk_bin_name()
        first_agent_name = agent_dirs[0].name if agent_dirs else None
        steps: list[NextStep] = []
        if first_agent_name:
            # Per-agent health check — the most specific diagnostic
            # operators want after a green bundle validation.
            steps.append(
                NextStep(
                    label=f"Health-check {first_agent_name!r} (env keys, contexts, skills)",
                    command=f"{bin_name} doctor agent {first_agent_name}",
                    argv=[bin_name, "doctor", "agent", first_agent_name],
                )
            )
        steps.append(
            NextStep(
                label="Run project-level doctor (env, paths, provider keys)",
                command=f"{bin_name} doctor",
                argv=[bin_name, "doctor"],
            )
        )
        prompt_next_step(console=console, steps=steps)


def _validate_agent(path: Path, *, strict: bool, run_linter: bool) -> None:
    try:
        bundle = load_agent(path)
    except AgentLoadError as exc:
        console.print(f"[red]✗ validation failed:[/red] {exc}")
        if "contexts resolution failed" in str(exc):
            console.print(
                "[dim]  hint: run [bold]mdk add context <name>[/bold] "
                "to create the missing context.[/dim]"
            )
        elif "skills resolution failed" in str(exc):
            console.print(
                "[dim]  hint: run [bold]mdk add skill <name>[/bold] "
                "to scaffold the missing skill directory.[/dim]"
            )
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
            console.print("[dim]  Install with: uv add 'movate-cli\\[anthropic]'[/dim]")
        elif spec.runtime == AgentRuntime.NATIVE_OPENAI:
            console.print("[dim]  Install with: uv add 'movate-cli\\[openai]'[/dim]")
        elif spec.runtime == AgentRuntime.LANGCHAIN:
            console.print("[dim]  Install with: uv add 'movate-cli\\[langchain]'[/dim]")
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

    # input_guardrails advisory — warn if the policy lists an unknown
    # guardrail name. The Pydantic Literal type catches this at parse
    # time for typed values, but if the YAML contains a string that
    # Pydantic can't parse we still surface a clear message here.
    # This block runs even when ``policy.is_permissive()`` is False
    # because a misspelled guardrail name silently does nothing (the
    # executor skips guardrails it doesn't recognise).
    for guardrail in project_cfg.policy.input_guardrails:
        if guardrail not in _KNOWN_INPUT_GUARDRAILS:
            console.print(
                f"  [yellow]![/yellow] policy.input_guardrails: unknown guardrail "
                f"[bold]{guardrail!r}[/bold] — will be silently ignored at runtime. "
                f"Known values: {sorted(_KNOWN_INPUT_GUARDRAILS)}."
            )

    # HTTP / MCP skill backend advisory. Both backends are implemented
    # but require external resources (a URL / a subprocess command) that
    # can't be validated statically. Warn the operator so they know a
    # runtime failure is possible and what to check.
    for skill in bundle.skills:
        impl = skill.spec.implementation
        if impl.kind == SkillImplementationKind.HTTP:
            console.print(
                f"  [yellow]![/yellow] skill [bold]{skill.spec.name!r}[/bold] uses "
                "[bold]kind: http[/bold] — endpoint reachability is checked at first use, not here."
            )
            if impl.auth and impl.auth.startswith("bearer-from-env:"):
                env_var = impl.auth.split(":", 1)[1]
                if not os.environ.get(env_var):
                    console.print(
                        f"    [yellow]![/yellow] env var [bold]{env_var}[/bold] is unset "
                        f"— set it before running this agent."
                    )
        elif impl.kind == SkillImplementationKind.MCP:
            console.print(
                f"  [yellow]![/yellow] skill [bold]{skill.spec.name!r}[/bold] uses "
                "[bold]kind: mcp[/bold] — subprocess availability is checked at "
                "first use, not here."
            )
        elif impl.kind == SkillImplementationKind.AGENT:
            console.print(
                f"  [yellow]![/yellow] skill [bold]{skill.spec.name!r}[/bold] uses "
                "[bold]kind: agent[/bold] — agent-skill targets a remote agent "
                f"([bold]{impl.target_agent!r}[/bold]); ensure it's deployed before running live."
            )

    # Python skill impl.py reachability. The loader validates skill.yaml
    # via SkillSpec but never imports the module — a missing impl.py
    # only surfaces as ModuleNotFoundError on the first call. Catch it
    # here so the operator learns at validate time, not at runtime.
    for skill in bundle.skills:
        if (
            skill.spec.implementation.kind == SkillImplementationKind.PYTHON
            and not (skill.skill_dir / "impl.py").is_file()
        ):
            console.print(
                f"  [red]✗[/red] skill [bold]{skill.spec.name!r}[/bold]: "
                f"[bold]impl.py[/bold] not found in [dim]{skill.skill_dir}[/dim] — "
                "skill will raise ModuleNotFoundError at runtime."
            )
            console.print(
                "    [dim]hint: create [bold]impl.py[/bold] with an async "
                "[bold]run(input, ctx)[/bold] function, or use "
                "[bold]mdk add <template>[/bold] to scaffold one.[/dim]"
            )
            raise typer.Exit(code=2)

    _check_kb_corpus(bundle)
    _check_vector_kb_empty(bundle, console)
    _check_marketplace_metadata(spec)

    # Context size + content advisory. Contexts are prepended to the system
    # prompt on every call; empty or very large contexts are likely mistakes.
    for ctx_name, ctx_body in bundle.contexts:
        if not ctx_body.strip():
            console.print(
                f"  [yellow]![/yellow] context [bold]{ctx_name!r}[/bold] is empty — "
                "it contributes nothing to the system prompt. "
                f"[dim]hint: populate [bold]contexts/{ctx_name}.md[/bold] "
                "or remove the declaration.[/dim]"
            )
            continue
        size = len(ctx_body.encode())
        if size >= _CTX_ERROR_BYTES:
            console.print(
                f"  [red]✗[/red] context [bold]{ctx_name!r}[/bold] is "
                f"{size:,} bytes — exceeds the {_CTX_ERROR_BYTES:,}-byte limit. "
                "Trim it or split into multiple narrower contexts."
            )
            raise typer.Exit(code=2)
        if size >= _CTX_ADVISORY_BYTES:
            console.print(
                f"  [yellow]![/yellow] context [bold]{ctx_name!r}[/bold] is "
                f"{size:,} bytes — large contexts inflate token spend on every call."
            )

    # Dataset JSONL validation — catches truncated / malformed lines
    # before they silently zero the eval run.
    if bundle.spec.evals.dataset:
        dataset_path = bundle.agent_dir / bundle.spec.evals.dataset
        if dataset_path.is_file():
            _check_dataset_jsonl(dataset_path, input_validator=bundle.input_validator)

    # Prompt linter — runs by default; --no-lint to skip; --strict to
    # promote warnings to errors. Reports BEFORE the success banner so
    # the operator sees lint findings even when the schema check
    # already passed.
    lint_issues: list[LintIssue] = [] if not run_linter else lint_prompt(bundle)
    if lint_issues:
        _render_lint_issues(lint_issues)

    # ── Success banner ──────────────────────────────────────────────────────
    # Collect the metadata lines first so the Panel body is built before
    # we call console.print() — keeps the output as a single atomic render.
    _detail_lines: list[str] = [
        f"api_version: {spec.api_version}",
        f"runtime:     {spec.runtime.value}",
        f"provider:    {spec.model.provider}",
        f"prompt:      {bundle.prompt_hash[:12]}…",
    ]
    if spec.model.fallback:
        fbs = ", ".join(f.provider for f in spec.model.fallback)
        _detail_lines.append(f"fallback:    {fbs}")
    if not policy.is_permissive():
        _detail_lines.append("[dim]policy:      ✓ compliant[/dim]")
    if run_linter and not lint_issues:
        _detail_lines.append("[dim]lint:        ✓ clean[/dim]")

    # Cost forecast — silent when no dataset / no pricing for model /
    # empty dataset. The estimate_eval_cost helper returns None in
    # every "skip" case so this stays a single conditional.
    try:
        pricing = load_pricing()
        forecast = estimate_eval_cost(bundle, pricing=pricing)
    except Exception:  # pragma: no cover — defensive; load_pricing rarely fails
        pricing = None
        forecast = None
    if forecast is not None:
        _detail_lines.append(
            f"[dim]eval cost:   ~${forecast.total_cost_usd:.4f} "
            f"({forecast.cases} cases x "
            f"~{forecast.input_tokens_per_call} in + "
            f"~{forecast.output_tokens_per_call} out tokens)[/dim]"
        )
    elif pricing is not None and bundle.spec.evals.dataset:
        # Dataset is configured but we couldn't produce a forecast.
        # Most common cause: the model isn't in the pricing table.
        # Let the operator know rather than staying silent.
        model_provider = bundle.spec.model.provider
        if model_provider not in pricing.models:
            _detail_lines.append(
                f"[yellow]![/yellow] eval cost: no pricing entry for "
                f"[bold]{model_provider!r}[/bold] — add it to "
                "providers/pricing.yaml for a forecast."
            )

    console.print(
        Panel(
            "\n".join(_detail_lines),
            title=(f"[green]✓[/green] {spec.name} [dim]v{spec.version}[/dim] [dim](agent)[/dim]"),
            title_align="left",
            border_style="green",
        )
    )

    # Exit non-zero if there are real errors (always) or warnings
    # under --strict (CI gate mode).
    has_errors = any(i.severity == "error" for i in lint_issues)
    has_warnings = any(i.severity == "warning" for i in lint_issues)
    if has_errors or (strict and has_warnings):
        raise typer.Exit(code=2)


def _check_dataset_jsonl(
    path: Path,
    *,
    input_validator: Draft202012Validator | None = None,
) -> None:
    """Validate that every non-blank line in a dataset JSONL file is a
    parseable JSON object with at least an ``input`` key.

    Malformed lines cause ``mdk eval`` to skip or crash that case at
    eval time — better to surface them here when validate runs.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        console.print(f"  [yellow]![/yellow] dataset [bold]{path.name}[/bold] unreadable: {exc}")
        return
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if not lines:
        console.print(
            f"  [yellow]![/yellow] dataset [bold]{path.name}[/bold] is empty — "
            "no eval cases will run."
        )
        return
    for i, line in enumerate(lines, start=1):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            console.print(
                f"  [red]✗[/red] dataset [bold]{path.name}:{i}[/bold] invalid JSON: {exc}"
            )
            raise typer.Exit(code=2) from None
        if not isinstance(obj, dict):
            console.print(
                f"  [red]✗[/red] dataset [bold]{path.name}:{i}[/bold] "
                f"must be a JSON object, got {type(obj).__name__}"
            )
            raise typer.Exit(code=2)
        if "input" not in obj:
            console.print(
                f"  [yellow]![/yellow] dataset [bold]{path.name}:{i}[/bold] "
                "missing [bold]'input'[/bold] key — this case will be skipped by mdk eval."
            )
        elif input_validator is not None:
            from jsonschema import ValidationError as _JsonSchemaError  # noqa: PLC0415

            try:
                input_validator.validate(obj["input"])
            except _JsonSchemaError as exc:
                # Report the first validation error as a warning — not fatal
                # because the schema may be intentionally flexible or the
                # dataset may include edge-case inputs the operator wants to
                # explore. Surfacing it here beats a cryptic failure mid-eval.
                short = exc.message[:120]
                console.print(
                    f"  [yellow]![/yellow] dataset [bold]{path.name}:{i}[/bold] "
                    f"input fails schema: {short}"
                )


def _find_project_root_from_bundle(bundle: AgentBundle) -> Path | None:
    """Walk up from bundle.agent_dir to find the project root."""
    for parent in (bundle.agent_dir, *bundle.agent_dir.parents):
        if any((parent / m).is_file() for m in PROJECT_MARKER_FILES):
            return parent
    return None


def _check_kb_corpus(bundle: AgentBundle) -> None:
    """Warn when a kb-lookup skill is declared but the project corpus is missing
    or contains entries that lack required fields.

    Two checks:
    1. File absent → skill falls back to demo corpus silently.
    2. File present but entries missing ``id``/``title``/``resolution`` → skill
       raises KeyError or returns empty matches at eval/run time.
    """
    import json as _json  # noqa: PLC0415

    kb_skills = [s for s in bundle.skills if "kb" in s.spec.name.lower()]
    if not kb_skills:
        return
    project_root = _find_project_root_from_bundle(bundle)
    if project_root is None:
        return
    corpus_path = project_root / "kb" / "kb-lookup-corpus.json"
    if not corpus_path.is_file():
        for skill in kb_skills:
            console.print(
                f"  [yellow]![/yellow] skill [bold]{skill.spec.name!r}[/bold]: "
                f"[bold]kb/kb-lookup-corpus.json[/bold] not found — "
                "skill will use the bundled demo corpus at runtime."
            )
        console.print(f"    [dim]hint: drop your real corpus at [bold]{corpus_path}[/bold][/dim]")
        return

    # Corpus exists — validate entry fields.
    try:
        entries = _json.loads(corpus_path.read_text())
    except (OSError, _json.JSONDecodeError) as exc:
        console.print(
            f"  [yellow]![/yellow] [bold]kb/kb-lookup-corpus.json[/bold] could not be parsed: {exc}"
        )
        return
    if not isinstance(entries, list):
        console.print(
            "  [yellow]![/yellow] [bold]kb/kb-lookup-corpus.json[/bold] must be a JSON array."
        )
        return
    if len(entries) == 0:
        console.print(
            "  [yellow]![/yellow] [bold]kb/kb-lookup-corpus.json[/bold] is empty — "
            "kb-lookup will return no matches at runtime."
            "\n    [dim]hint: run [bold]mdk knowledge add[/bold] to add entries.[/dim]"
        )
        return
    bad: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            bad.append("<non-object entry>")
            continue
        missing = _CORPUS_REQUIRED_FIELDS - entry.keys()
        if missing:
            entry_id = entry.get("id", "<no-id>")
            bad.append(f"{entry_id!r} (missing: {sorted(missing)})")
        if len(bad) >= _CORPUS_MAX_REPORTED_ERRORS:
            break  # cap output — rest of corpus may also have issues
    if bad:
        console.print(
            f"  [yellow]![/yellow] [bold]kb/kb-lookup-corpus.json[/bold] "
            f"has entries missing required fields "
            f"({', '.join(sorted(_CORPUS_REQUIRED_FIELDS))}):"
        )
        for label in bad:
            console.print(f"    [dim]·[/dim] {label}")
        if len(entries) > _CORPUS_MAX_REPORTED_ERRORS:
            console.print(
                f"    [dim]… and possibly more "
                f"(checked first {_CORPUS_MAX_REPORTED_ERRORS} offenders)[/dim]"
            )

    # Duplicate id check — last-write-wins at runtime is a silent data hazard.
    seen_ids: dict[str, int] = {}
    duplicates: list[str] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        eid = entry.get("id")
        if eid is None:
            continue
        if eid in seen_ids:
            duplicates.append(f"{eid!r} (rows {seen_ids[eid]} and {idx})")
        else:
            seen_ids[eid] = idx
    if duplicates:
        console.print(
            "  [yellow]![/yellow] [bold]kb/kb-lookup-corpus.json[/bold] "
            "has duplicate entry ids (last entry wins at runtime — earlier ones are shadowed):"
        )
        for label in duplicates[:_CORPUS_MAX_REPORTED_ERRORS]:
            console.print(f"    [dim]·[/dim] {label}")
        console.print(
            "    [dim]hint: run [bold]mdk knowledge list[/bold] to inspect "
            "and [bold]mdk knowledge remove <id>[/bold] to delete the duplicate.[/dim]"
        )


def _check_vector_kb_empty(bundle: AgentBundle, con: Console) -> None:
    """Warn when a kb-vector-lookup skill is declared but the vector KB has 0 chunks.

    The #1 silent RAG failure: operator adds ``kb-vector-lookup`` to skills
    but never ran ``mdk kb ingest``, so every retrieval call returns empty.
    This check probes the local vector KB and warns if no chunks are indexed.

    Wrapped in a broad ``except Exception`` so a missing or uninitialized
    database never causes validate to fail.
    """
    # Fast path: skip entirely if no KB-vector skill is declared.
    if not any("kb-vector" in s.spec.name.lower() for s in bundle.skills):
        return

    async def _probe() -> bool:
        """Return True if >=1 chunk exists, False if 0.  Raise on any error."""
        from movate.storage import build_storage  # noqa: PLC0415

        s = build_storage()
        await s.init()
        try:
            chunks = await s.list_kb_chunks(agent=bundle.spec.name, tenant_id="local", limit=1)
            return len(chunks) > 0
        finally:
            await s.close()

    try:
        has_chunks = asyncio.run(_probe())
    except Exception:
        return  # storage not ready / DB missing — skip silently

    if not has_chunks:
        con.print(
            "  [yellow]![/yellow] skill [bold]'kb-vector-lookup'[/bold] is wired "
            "but the vector KB has 0 chunks."
        )
        con.print(
            f"    hint: run [bold]mdk kb ingest {bundle.spec.name} "
            f"./agents/{bundle.spec.name}/kb/[/bold]"
        )
        con.print("          or   [bold]mdk kb ingest-all[/bold] to scan the whole project.")


# Names of input guardrails the executor currently recognises. Used by
# ``_validate_agent`` to warn on unknown strings in
# ``policy.input_guardrails``. Keep in sync with
# :class:`movate.core.config.ModelPolicy.input_guardrails` Literal type.
_KNOWN_INPUT_GUARDRAILS: frozenset[str] = frozenset({"prompt_injection"})

_CTX_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")

# Context size thresholds. Contexts are prepended to the system prompt
# on every call — large ones silently eat token budget. The advisory
# fires at 4 KB (noticeable cost impact), the error at 16 KB (more
# than a typical system prompt itself; almost certainly a mistake).
_CTX_ADVISORY_BYTES: int = 4_096
_CTX_ERROR_BYTES: int = 16_384

# Minimum required fields in a KB corpus entry. The kb-lookup skill
# uses all three for scoring + response construction; missing any one
# causes a KeyError or empty-match at eval/run time.
_CORPUS_REQUIRED_FIELDS: frozenset[str] = frozenset({"id", "title", "resolution"})
_CORPUS_MAX_REPORTED_ERRORS: int = 3


def _check_marketplace_metadata(spec: AgentSpec) -> None:
    """Validate the optional ``metadata:`` block on an AgentSpec.

    Lightweight advisory checks — none of these are hard errors unless a
    value is structurally invalid (a bad ``owner`` string is just a warning;
    a missing ``output`` key in an ``examples`` entry is also a warning since
    the :class:`movate.core.models.Example` model already allows empty output).

    Three checks run when ``spec.metadata`` is present:

    1. **owner** — if set, must be a non-empty string. An email-shaped value
       (contains ``@``) is accepted without further validation; a non-email
       team name like ``"Platform Team"`` is also fine. Emits a yellow warning
       if the owner field is an empty string (which is technically a bug in
       the agent.yaml — setting ``owner: ""`` explicitly is confusing).

    2. **examples** — if non-empty, each entry must have both ``input`` and
       ``output`` keys. Missing keys emit a yellow advisory (not a hard error)
       because the marketplace can still render the card; only the example
       gallery is degraded.

    3. **no metadata at all** — if ``spec.metadata is None`` (the block is
       entirely absent), emit a dim discovery hint. This is intentionally
       non-intrusive (no color, not a warning) so existing agents don't get
       noisy output. It's purely a discovery prompt for operators who haven't
       heard of the catalog feature yet.
    """
    if spec.metadata is None:
        console.print(
            "  [dim]hint: add a [bold]metadata:[/bold] block to agent.yaml to "
            "populate the Mova iO marketplace catalog "
            "(persona, role, capabilities, owner, examples).[/dim]"
        )
        return

    m = spec.metadata

    # owner check
    if m.owner is not None and not m.owner.strip():
        console.print(
            "  [yellow]![/yellow] metadata.owner is set but empty — "
            "use a non-empty email address or team name, "
            "or omit the field entirely."
        )

    # examples check — each entry should have input + output keys
    for i, ex in enumerate(m.examples):
        missing = []
        if "input" not in ex:
            missing.append("input")
        if "output" not in ex:
            missing.append("output")
        if missing:
            console.print(
                f"  [yellow]![/yellow] metadata.examples[{i}] is missing "
                f"key(s): {missing} — the marketplace example gallery "
                "expects both 'input' and 'output'."
            )


def _check_orphaned_assets(project_root: Path, agent_dirs: list[Path]) -> None:
    """Warn about contexts and skills that exist on disk but no agent declares.

    An orphaned asset isn't an error — it could be staged for future use.
    But it's the most common source of "I added the file and nothing changed"
    confusion, so surfacing it at validate time closes the feedback loop.
    """
    # Collect all declared names across every loadable agent.
    declared_contexts: set[str] = set()
    declared_skills: set[str] = set()
    for agent_dir in agent_dirs:
        try:
            b = load_agent(agent_dir)
            declared_contexts.update(b.spec.contexts or [])
            declared_skills.update(s.spec.name for s in b.skills)
        except AgentLoadError:
            pass  # already reported by per-agent validation

    # Orphaned contexts: files in contexts/ not declared by any agent.
    # Skip README / documentation files (stem contains uppercase or
    # doesn't follow the lowercase-hyphen context naming convention).
    ctx_dir = project_root / "contexts"
    if ctx_dir.is_dir():
        orphaned = sorted(
            f
            for f in ctx_dir.glob("*.md")
            if _CTX_NAME_RE.match(f.stem) and f.stem not in declared_contexts
        )
        for f in orphaned:
            console.print(
                f"  [yellow]![/yellow] [bold]contexts/{f.name}[/bold] exists but "
                "is not declared by any agent."
            )
            console.print(
                f"    [dim]hint: add [bold]{f.stem!r}[/bold] to "
                "[bold]agent.yaml: contexts:[/bold], or run "
                "[bold]mdk add context[/bold] to scaffold a declaration.[/dim]"
            )

    # Orphaned skills: directories in skills/ not declared by any agent.
    # Also validates skill.yaml against SkillSpec so malformed orphan
    # skills are caught before someone declares them in agent.yaml.
    skills_dir = project_root / "skills"
    if skills_dir.is_dir():
        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir() or not (skill_dir / "skill.yaml").is_file():
                continue
            try:
                import yaml as _yaml  # noqa: PLC0415
                from pydantic import ValidationError as _PydanticVE  # noqa: PLC0415

                raw = _yaml.safe_load((skill_dir / "skill.yaml").read_text())
                try:
                    parsed_spec = SkillSpec.model_validate(raw) if isinstance(raw, dict) else None
                    skill_name = parsed_spec.name if parsed_spec else skill_dir.name
                except _PydanticVE as ve:
                    # Malformed skill.yaml — emit an error regardless of
                    # whether the skill is declared, since declaring a broken
                    # skill causes an AgentLoadError anyway.
                    console.print(
                        f"  [red]✗[/red] [bold]skills/{skill_dir.name}/skill.yaml[/bold] "
                        f"failed SkillSpec validation: {ve}"
                    )
                    continue
            except Exception:
                skill_name = skill_dir.name
            if skill_name not in declared_skills:
                console.print(
                    f"  [yellow]![/yellow] skill [bold]{skill_name!r}[/bold] is registered "
                    "but not declared by any agent."
                )
                console.print(
                    f"    [dim]hint: add [bold]{skill_name!r}[/bold] to "
                    "[bold]agent.yaml: skills:[/bold] to use it.[/dim]"
                )


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

    if spec.evals is None:
        console.print(
            "[yellow]![/yellow] no [bold]evals:[/bold] stanza in workflow.yaml — "
            "this workflow cannot be evaluated with [bold]mdk eval[/bold]. "
            "[dim]Add an evals: block with a dataset: path.[/dim]"
        )
