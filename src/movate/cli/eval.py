"""``movate eval <agent>`` — score an agent against its dataset and gate on a threshold.

``--baseline <eval-id>`` opts into the regression-detection loop: the
current eval is diffed against the persisted baseline and the CLI exits
non-zero if mean_score or pass_rate dropped past ``--regression-tolerance``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from movate.cli._completion import complete_agent_path
from movate.cli._output import Report
from movate.cli._progress import progress_bar
from movate.cli._runtime import build_local_runtime, shutdown_runtime
from movate.core.baseline import BaselineDiff, compute_baseline_diff, format_delta
from movate.core.client import MovateClient
from movate.core.eval import (
    CaseSummary,
    DimensionalMeans,
    EvalConfigError,
    EvalEngine,
    EvalSummary,
    ObjectiveSummary,
    WorkflowEvalEngine,
)
from movate.core.executor import Executor
from movate.core.loader import AgentBundle, AgentLoadError, load_agent
from movate.core.models import EvalRecord
from movate.core.remote_executor import RemoteExecutor
from movate.core.reporters import render_eval_markdown
from movate.storage.base import StorageProvider

console = Console()
err_console = Console(stderr=True)


def eval_(  # noqa: PLR0912 — orchestrator; branch count reflects flag dispatch + wizard
    path: str | None = typer.Argument(
        None,
        help=(
            "Path to an agent directory OR a base URL of a deployed movate "
            "runtime (http://… or https://…). With a URL, each dataset case "
            "is submitted as a job, polled to terminal, and the resulting "
            "RunRecord is scored — no local provider is invoked. Requires "
            "--agent-yaml so the local copy provides the dataset and output "
            "schema for scoring. Omit with [bold]--all[/bold] to sweep every "
            "agent in the current project."
        ),
        shell_complete=complete_agent_path,
    ),
    all_in_project: bool = typer.Option(
        False,
        "--all",
        help=(
            "Evaluate every agent under [bold]./agents/[/bold] in the current "
            "project. Aggregates results into a summary table; exits non-zero "
            "if any agent fails its gate. The CI eval-gate workflow uses this."
        ),
    ),
    gate: float = typer.Option(0.7, "--gate", help="Per-case score required to pass (0.0-1.0)."),
    gate_mode: str = typer.Option(
        "mean",
        "--gate-mode",
        help="How to aggregate N runs per case into a single score: mean | min | p10.",
    ),
    runs: int = typer.Option(
        1,
        "--runs",
        "-r",
        help="Runs per case. Use 3+ for LLM-as-judge to defeat sampling variance.",
    ),
    mock: bool = typer.Option(
        False, "--mock", help="Use the deterministic MockProvider (no API keys)."
    ),
    baseline: str = typer.Option(
        None,
        "--baseline",
        help="Eval id of a stored EvalRecord to diff against. CLI exits 1 on regression.",
    ),
    baseline_file: Path = typer.Option(
        None,
        "--baseline-file",
        help=(
            "Path to a JSON-serialized EvalRecord. CI-friendly alternative to --baseline "
            "since CI runners have ephemeral sqlite. Mutually exclusive with --baseline."
        ),
    ),
    output_baseline: Path = typer.Option(
        None,
        "--output-baseline",
        help=(
            "Write the current EvalRecord to this path as JSON. Use on main-branch "
            "merges to refresh the committed baseline; CI then diffs PRs against it."
        ),
    ),
    regression_tolerance: float = typer.Option(
        0.0,
        "--regression-tolerance",
        help="Allowable score drop vs baseline before flagging a regression (0.0-1.0).",
    ),
    objective: str = typer.Option(
        None,
        "--objective",
        help=(
            "Only run cases tagged for the named objective (id from "
            "agent.yaml: objectives). Gates on that objective's own "
            "threshold (declared in agent.yaml), not --gate. "
            "Use this in CI to fail PRs that regress a specific objective "
            "while letting others slip — e.g. `--objective routing-accuracy`."
        ),
    ),
    output_format: Report = typer.Option(Report.TABLE, "--output", "-o", case_sensitive=False),
    api_key: str = typer.Option(
        None,
        "--api-key",
        envvar=["MDK_API_KEY", "MOVATE_API_KEY"],
        help=(
            "Bearer token for remote eval (path starts with http(s)://). "
            "Falls back to the MDK_API_KEY env var (legacy MOVATE_API_KEY also accepted)."
        ),
    ),
    agent_yaml: Path = typer.Option(
        None,
        "--agent-yaml",
        help=(
            "Local agent directory used for dataset + output schema when "
            "evaluating a deployed runtime. Required when path is a URL; "
            "ignored when path is a directory."
        ),
    ),
    guided: bool = typer.Option(
        False,
        "--guided",
        "-g",
        help=(
            "Interactive wizard: walks the operator through agent "
            "selection, provider (mock/real), gate, runs-per-case, "
            "and baseline behavior — then runs the resulting eval. "
            "Auto-triggered when [bold]mdk eval[/bold] runs with no "
            "args from a TTY inside a project."
        ),
    ),
    gate_faithfulness: float = typer.Option(
        None,
        "--gate-faithfulness",
        help=(
            "Minimum mean faithfulness score (0.0-1.0) required to pass. "
            "Only fires when the dataset has [bold]grounding[/bold] fields. "
            "Exit 1 if faithfulness mean is below this threshold."
        ),
    ),
    gate_coverage: float = typer.Option(
        None,
        "--gate-coverage",
        help=(
            "Minimum mean coverage score (0.0-1.0) required to pass. "
            "Only fires when the dataset has [bold]expected_coverage[/bold] fields. "
            "Exit 1 if coverage mean is below this threshold."
        ),
    ),
    gate_latency: float = typer.Option(
        None,
        "--gate-latency",
        help=(
            "Minimum mean latency score (0.0-1.0) required to pass. "
            "A score of 1.0 means every case completed within its budget; "
            "decays linearly to 0.0 at 2x the budget. "
            "Exit 1 if latency mean is below this threshold."
        ),
    ),
    gate_context_compliance: float = typer.Option(
        None,
        "--gate-context-compliance",
        help=(
            "Minimum mean context-compliance score (0.0-1.0) required to pass. "
            "Only fires when the agent has declared contexts and a judge model. "
            "Exit 1 if context_compliance mean is below this threshold."
        ),
    ),
    gate_refusal: float = typer.Option(
        None,
        "--gate-refusal",
        help=(
            "Minimum mean refusal score (0.0-1.0) required to pass. "
            "Only fires when the dataset has [bold]refusal_expected[/bold] rows "
            "(generated by [bold]mdk eval-gen --mode refusal[/bold]). "
            "Score 1.0 when the agent refuses as expected, 0.0 when it complies. "
            "Exit 1 if refusal mean is below this threshold."
        ),
    ),
    compare: bool = typer.Option(
        False,
        "--compare",
        help=(
            "Auto-compare against the previous run. Reads "
            "[bold]evals/.last-run.json[/bold] as the baseline (if it exists) "
            "and writes the current run there afterward. Shorthand for "
            "[bold]--baseline-file evals/.last-run.json "
            "--output-baseline evals/.last-run.json[/bold]."
        ),
    ),
) -> None:
    """Run the eval suite for an agent and gate on a threshold.

    [bold]Examples:[/bold]

      [dim]# Exact-match scoring against the dataset[/dim]
      $ mdk eval ./faq-agent --gate 0.7

      [dim]# Compare against a stored (sqlite) baseline by id[/dim]
      $ mdk eval ./faq-agent --baseline 4f8a-... --regression-tolerance 0.05

      [dim]# CI flow: gate against a git-tracked baseline file[/dim]
      $ mdk eval ./faq-agent --mock --baseline-file .movate/baseline.json

      [dim]# Refresh the committed baseline on main-branch merge[/dim]
      $ mdk eval ./faq-agent --mock --output-baseline .movate/baseline.json

      [dim]# Hermetic CI run (no API keys needed)[/dim]
      $ mdk eval ./faq-agent --mock

      [dim]# Black-box eval against a deployed mdk runtime[/dim]
      $ mdk eval https://faq-runtime.example.com \\
          --agent-yaml ./faq-agent --api-key mvt_dev_...

      [dim]# Inside a project, bare names resolve to ./agents/<name>:[/dim]
      $ mdk eval rag-qa --gate 0.7
    """
    if baseline is not None and baseline_file is not None:
        err_console.print("[red]✗[/red] --baseline and --baseline-file are mutually exclusive")
        raise typer.Exit(code=2)

    # --compare: auto-read+write evals/.last-run.json in the agent dir (or
    # cwd for --all). Resolved to actual path after path resolution below.
    _compare_pending = compare

    # Guided wizard — explicit `--guided`, OR auto-trigger when an
    # operator typed bare `mdk eval` with no path and no `--all` from
    # an interactive shell inside a project. CI / pipe / no-args-outside-
    # project paths still fall through to the existing error.
    if not guided and path is None and not all_in_project:
        from movate.core.config import is_project_root  # noqa: PLC0415

        if sys.stdin.isatty() and sys.stdout.isatty() and is_project_root(Path.cwd()):
            guided = True
    if guided:
        wizard = _run_eval_wizard()
        if wizard is None:  # operator hit Ctrl-C / quit
            raise typer.Exit(code=0)
        # Apply wizard's answers as if they were CLI flags, then fall
        # through to the standard dispatch below — no duplicated logic.
        path = wizard.path
        all_in_project = wizard.all_in_project
        mock = wizard.mock
        gate = wizard.gate
        runs = wizard.runs
        baseline_file = wizard.baseline_file
        output_baseline = wizard.output_baseline

    # `--all`: evaluate every agent in the current project. Mutex
    # with a path argument. Dispatches to the sweep helper below.
    if all_in_project:
        if path is not None and path not in (".", ""):
            err_console.print(
                "[red]✗[/red] [bold]--all[/bold] and an explicit path "
                "argument are mutually exclusive."
            )
            raise typer.Exit(code=2)
        _eval_all_in_project(
            gate=gate,
            gate_mode=gate_mode,
            runs=runs,
            mock=mock,
            regression_tolerance=regression_tolerance,
            gate_faithfulness=gate_faithfulness,
            gate_coverage=gate_coverage,
            gate_latency=gate_latency,
            gate_context_compliance=gate_context_compliance,
            gate_refusal=gate_refusal,
        )
        return

    if path is None:
        from movate.core.config import is_project_root  # noqa: PLC0415

        if is_project_root(Path.cwd()):
            err_console.print(
                "[red]✗[/red] no path given and not in a TTY (wizard can't auto-start). "
                "To evaluate all agents: [bold]mdk eval --all[/bold]. "
                "To target one: [bold]mdk eval <agent-name>[/bold]."
            )
        else:
            err_console.print(
                "[red]✗[/red] path required (or pass [bold]--all[/bold] to "
                "evaluate every agent in the project)."
            )
        raise typer.Exit(code=2)

    # Bare-name resolution: `mdk eval rag-qa` → `mdk eval ./agents/rag-qa`
    # when inside a project. URLs + full paths pass through unchanged.
    from movate.cli._resolve import resolve_agent_or_workflow_arg  # noqa: PLC0415

    path = resolve_agent_or_workflow_arg(path)

    remote_url = _resolve_remote_url(path)

    # Workflow path: dispatch to workflow eval engine before attempting
    # load_agent (which would fail with no agent.yaml for a workflow dir).
    from movate.cli._workflow_path import is_workflow_path  # noqa: PLC0415

    if remote_url is None and is_workflow_path(Path(path)):
        if _compare_pending and baseline is None and baseline_file is None:
            wf_last_run = Path(path) / "evals" / ".last-run.json"
            if wf_last_run.is_file():
                baseline_file = wf_last_run
            output_baseline = wf_last_run
        asyncio.run(
            _run_workflow_eval(
                Path(path),
                gate=gate,
                gate_mode=gate_mode,
                runs=runs,
                mock=mock,
                output_format=output_format,
                gate_faithfulness=gate_faithfulness,
                gate_coverage=gate_coverage,
                gate_latency=gate_latency,
                gate_context_compliance=gate_context_compliance,
                gate_refusal=gate_refusal,
                baseline_file=baseline_file,
                output_baseline=output_baseline,
                regression_tolerance=regression_tolerance,
            )
        )
        return

    if remote_url is not None:
        # Remote eval: dataset + schemas come from --agent-yaml, but
        # execution lands on the deployed runtime via MovateClient.
        if agent_yaml is None:
            err_console.print(
                "[red]✗[/red] --agent-yaml is required when path is a URL "
                "(the local copy provides the dataset and output schema for scoring)"
            )
            raise typer.Exit(code=2)
        if not api_key:
            err_console.print(
                "[red]✗[/red] no API key for remote eval — pass --api-key or set MDK_API_KEY"
            )
            raise typer.Exit(code=2)
        try:
            bundle = load_agent(agent_yaml)
        except AgentLoadError as exc:
            err_console.print(f"[red]✗ load failed:[/red] {exc}")
            raise typer.Exit(code=2) from None
    else:
        try:
            bundle = load_agent(Path(path))
        except AgentLoadError as exc:
            err_console.print(f"[red]✗ load failed:[/red] {exc}")
            # Fuzzy-match for typo'd bare names — same UX as `mdk run`.
            if "/" not in path and "\\" not in path:
                from movate.cli._resolve import suggest_similar_agent  # noqa: PLC0415

                suggestion = suggest_similar_agent(path)
                if suggestion:
                    err_console.print(f"[dim]→ did you mean [bold]{suggestion}[/bold]?[/dim]")
            raise typer.Exit(code=2) from None

    # --compare: resolve to evals/.last-run.json inside the agent dir.
    # Only activates when neither --baseline nor --baseline-file was given
    # (those are the explicit forms; --compare is the "lazy" shorthand).
    if _compare_pending and baseline is None and baseline_file is None:
        agent_dir = agent_yaml if agent_yaml is not None else Path(path)
        last_run_path = agent_dir / "evals" / ".last-run.json"
        if last_run_path.is_file():
            baseline_file = last_run_path
        output_baseline = last_run_path

    asyncio.run(
        _run_eval(
            bundle,
            gate=gate,
            gate_mode=gate_mode,
            runs=runs,
            mock=mock,
            baseline_id=baseline,
            baseline_file=baseline_file,
            output_baseline=output_baseline,
            regression_tolerance=regression_tolerance,
            objective=objective,
            output_format=output_format,
            remote_url=remote_url,
            remote_api_key=api_key if remote_url else None,
            gate_faithfulness=gate_faithfulness,
            gate_coverage=gate_coverage,
            gate_latency=gate_latency,
            gate_context_compliance=gate_context_compliance,
            gate_refusal=gate_refusal,
        )
    )


@dataclass
class _EvalWizardChoices:
    """Resolved answers from the interactive eval wizard.

    Maps 1:1 to the CLI flags the dispatch path already handles, so
    the wizard's only job is collecting choices — execution stays in
    the existing code paths.
    """

    path: str | None
    all_in_project: bool
    mock: bool
    gate: float
    runs: int
    baseline_file: Path | None
    output_baseline: Path | None


def _run_eval_wizard() -> _EvalWizardChoices | None:  # noqa: PLR0912 — orchestrator; 5 prompts each with try/except adds linear branch count
    """Interactive Rich-prompted eval setup. Returns None on Ctrl-C / quit.

    Walks the operator through the five most-common decisions an eval
    invocation needs: which agent(s), provider (mock/real), gate
    threshold, runs per case, and baseline behavior. The full
    surface of ``mdk eval`` has 13 flags; the wizard intentionally
    covers only the 5 a casual operator cares about and leaves the
    rest to the explicit CLI path.

    Same visual style as ``mdk menu`` (Rich Panel + numbered
    Prompt.ask choices) so operators see one consistent UX language
    across guided commands.
    """
    from movate.core.config import is_project_root  # noqa: PLC0415

    cwd = Path.cwd()
    if not is_project_root(cwd):
        err_console.print(
            "[red]✗[/red] guided eval needs a project (project.yaml / "
            "policy.yaml / movate.yaml). None found in cwd."
        )
        return None

    console.print()
    console.print(
        Panel(
            "[bold]mdk eval — guided setup[/bold]\n"
            "[dim]Five questions; press Ctrl-C any time to quit. "
            "The resolved command is shown before it runs so you can "
            "copy-paste it next time.[/dim]",
            border_style="cyan",
            title_align="left",
        )
    )

    # Q1: Which agent(s)?
    agents_dir = cwd / "agents"
    agent_names: list[str] = []
    if agents_dir.is_dir():
        agent_names = sorted(
            d.name for d in agents_dir.iterdir() if d.is_dir() and (d / "agent.yaml").is_file()
        )
    if not agent_names:
        err_console.print(
            "[red]✗[/red] no agents in [bold]./agents/[/bold]. "
            "Run [bold]mdk add <template>[/bold] first."
        )
        return None
    # First option is "all"; remaining are the per-agent names. Operator
    # picks by index (matches `mdk menu`'s numbered shape).
    agent_choices = ["all", *agent_names]
    console.print()
    console.print("[bold]Which agent(s)?[/bold]")
    for i, name in enumerate(agent_choices, start=1):
        suffix = (
            f"  [dim](every agent in project — {len(agent_names)} total)[/dim]"
            if name == "all"
            else ""
        )
        console.print(f"  [bold cyan][{i}][/bold cyan] {name}{suffix}")
    try:
        agent_idx = Prompt.ask(
            "\n[bold]Pick[/bold]",
            choices=[str(i) for i in range(1, len(agent_choices) + 1)],
            default="1",
            show_choices=False,
        )
    except (KeyboardInterrupt, EOFError):
        return None
    chosen_agent = agent_choices[int(agent_idx) - 1]

    # Q2: Mock or real provider?
    console.print()
    try:
        use_mock = Confirm.ask(
            "[bold]Use mock provider?[/bold] [dim](deterministic, free; no API keys "
            "needed — recommended for CI / iteration)[/dim]",
            default=True,
        )
    except (KeyboardInterrupt, EOFError):
        return None

    # Q3: Gate threshold.
    gate_choices = {
        "1": (0.0, "no gate (just run + score; never fails)"),
        "2": (0.5, "loose (50%+ pass rate; permissive)"),
        "3": (0.7, "recommended (70%+; CI default)"),
        "4": (0.9, "strict (90%+; expects high-quality datasets)"),
    }
    console.print()
    console.print("[bold]Gate threshold?[/bold]")
    for key, (value, label) in gate_choices.items():
        console.print(f"  [bold cyan][{key}][/bold cyan] {value}  [dim]{label}[/dim]")
    try:
        gate_idx = Prompt.ask(
            "\n[bold]Pick[/bold]",
            choices=list(gate_choices.keys()),
            default="3",
            show_choices=False,
        )
    except (KeyboardInterrupt, EOFError):
        return None
    gate = gate_choices[gate_idx][0]

    # Q4: Runs per case.
    runs_choices = {
        "1": (1, "fast — single run per case"),
        "2": (3, "recommended — defeats LLM-judge sampling variance"),
        "3": (5, "tight CI — most tokens spent, narrowest CI"),
    }
    console.print()
    console.print(
        "[bold]Runs per case?[/bold] [dim](how many times each dataset case is run; "
        "more = tighter confidence)[/dim]"
    )
    for key, (value, label) in runs_choices.items():
        console.print(f"  [bold cyan][{key}][/bold cyan] {value}  [dim]{label}[/dim]")
    try:
        runs_idx = Prompt.ask(
            "\n[bold]Pick[/bold]",
            choices=list(runs_choices.keys()),
            default="1" if use_mock else "2",
            show_choices=False,
        )
    except (KeyboardInterrupt, EOFError):
        return None
    runs = runs_choices[runs_idx][0]

    # Q5: Baseline behavior.
    project_baseline = cwd / ".movate" / "baseline.json"
    has_existing_baseline = project_baseline.is_file()
    baseline_choices: dict[str, tuple[str, str]] = {
        "1": ("none", "just run + show scores (no drift check)"),
        "2": (
            "compare",
            f"compare against [bold].movate/baseline.json[/bold] "
            f"({'exists' if has_existing_baseline else 'MISSING — would skip'})",
        ),
        "3": (
            "write",
            "write a fresh baseline to [bold].movate/baseline.json[/bold] after this run",
        ),
    }
    console.print()
    console.print("[bold]Baseline behavior?[/bold]")
    for key, (_, label) in baseline_choices.items():
        console.print(f"  [bold cyan][{key}][/bold cyan] {label}")
    try:
        baseline_idx = Prompt.ask(
            "\n[bold]Pick[/bold]",
            choices=list(baseline_choices.keys()),
            default="1",
            show_choices=False,
        )
    except (KeyboardInterrupt, EOFError):
        return None
    baseline_mode = baseline_choices[baseline_idx][0]
    baseline_file_arg: Path | None = None
    output_baseline_arg: Path | None = None
    if baseline_mode == "compare":
        if has_existing_baseline:
            baseline_file_arg = project_baseline
        else:
            console.print(
                "[yellow]⚠[/yellow] no baseline file at "
                "[bold].movate/baseline.json[/bold]; running without "
                "drift check this time."
            )
    elif baseline_mode == "write":
        output_baseline_arg = project_baseline
        project_baseline.parent.mkdir(parents=True, exist_ok=True)

    # Resolved → preview the equivalent CLI command so the operator
    # learns the flag-form for next time.
    is_all = chosen_agent == "all"
    path_arg: str | None = None if is_all else chosen_agent
    parts: list[str] = ["mdk", "eval"]
    if is_all:
        parts.append("--all")
    else:
        parts.append(str(chosen_agent))
    if use_mock:
        parts.append("--mock")
    parts.extend(["--gate", str(gate)])
    if runs != 1:
        parts.extend(["--runs", str(runs)])
    if baseline_file_arg is not None:
        parts.extend(["--baseline-file", str(baseline_file_arg)])
    if output_baseline_arg is not None:
        parts.extend(["--output-baseline", str(output_baseline_arg)])

    console.print()
    console.print(
        Panel(
            "[dim]Running:[/dim] [bold cyan]" + " ".join(parts) + "[/bold cyan]",
            border_style="green",
            title="[green]✓[/green] Configured",
            title_align="left",
        )
    )
    console.print()

    return _EvalWizardChoices(
        path=path_arg,
        all_in_project=is_all,
        mock=use_mock,
        gate=gate,
        runs=runs,
        baseline_file=baseline_file_arg,
        output_baseline=output_baseline_arg,
    )


def _eval_all_in_project(  # noqa: PLR0912 — orchestrator; branch count reflects the per-agent state machine
    *,
    gate: float,
    gate_mode: str,
    runs: int,
    mock: bool,
    regression_tolerance: float,
    gate_faithfulness: float | None = None,
    gate_coverage: float | None = None,
    gate_latency: float | None = None,
    gate_context_compliance: float | None = None,
    gate_refusal: float | None = None,
) -> None:
    """Evaluate every agent under ``./agents/`` in the current project.

    Walks ``<project>/agents/*/agent.yaml``, invokes the standard
    ``eval_`` flow once per agent, aggregates results into a Rich
    summary table, and emits a greppable ``mdk_eval_all_summary:``
    line. Exits 0 if every agent's eval passes its gate; exits 2 if
    any fail.

    Used by the CI eval-gate workflow (`.github/workflows/eval-gate.yml`)
    so a single CI step covers the whole project without operators
    maintaining a matrix in their workflow file.
    """
    from rich.table import Table  # noqa: PLC0415

    from movate.core.config import is_project_root  # noqa: PLC0415

    # Walk up to find the project root.
    current = Path.cwd().resolve()
    project_root: Path | None = None
    while True:
        if is_project_root(current):
            project_root = current
            break
        if current.parent == current:
            break
        current = current.parent

    if project_root is None:
        err_console.print(
            "[red]✗[/red] not inside a movate project. "
            "[dim]Run [bold]mdk init <name>[/bold] first, or pass a "
            "path argument to evaluate one agent.[/dim]"
        )
        raise typer.Exit(code=2)

    agents_dir = project_root / "agents"
    agent_dirs = (
        sorted(p.parent for p in agents_dir.glob("*/agent.yaml")) if agents_dir.is_dir() else []
    )
    if not agent_dirs:
        err_console.print(
            "[yellow]⚠[/yellow] no agents found under "
            f"[dim]{agents_dir}[/dim]. "
            "[dim]Add agents via [bold]mdk add <template>[/bold] first.[/dim]"
        )
        # Not an error — empty project is a valid state. Greppable line
        # still fires so CI can detect the empty-eval case.
        console.print("[dim]mdk_eval_all_summary: agents_total=0 passed=0 failed=0 ok=true[/dim]")
        return

    # Per-agent results.
    rows: list[tuple[str, str]] = []
    failed = 0

    for agent_dir in agent_dirs:
        try:
            bundle = load_agent(agent_dir)
        except AgentLoadError as exc:
            rows.append((agent_dir.name, f"[red]✗ load failed[/red]: {str(exc)[:80]}"))
            failed += 1
            continue

        # Run eval; capture pass/fail from the same async path that the
        # single-agent `mdk eval` uses. Re-raises typer.Exit on gate
        # failure — catch and record per-agent rather than aborting.
        try:
            asyncio.run(
                _run_eval(
                    bundle,
                    gate=gate,
                    gate_mode=gate_mode,
                    runs=runs,
                    mock=mock,
                    baseline_id=None,
                    baseline_file=None,
                    output_baseline=None,
                    regression_tolerance=regression_tolerance,
                    objective=None,
                    output_format=Report.TABLE,
                    remote_url=None,
                    remote_api_key=None,
                    gate_faithfulness=gate_faithfulness,
                    gate_coverage=gate_coverage,
                    gate_latency=gate_latency,
                    gate_context_compliance=gate_context_compliance,
                    gate_refusal=gate_refusal,
                )
            )
            rows.append((agent_dir.name, "[green]✓ ok[/green]"))
        except typer.Exit as exc:
            if exc.exit_code == 0:
                rows.append((agent_dir.name, "[green]✓ ok[/green]"))
            else:
                rows.append((agent_dir.name, "[red]✗ gate failed[/red]"))
                failed += 1

    # Render the summary table.
    table = Table(
        title=(
            f"Project eval — [bold]{project_root.name}[/bold] [dim]({len(rows)} agent(s))[/dim]"
        ),
        title_style="bold",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Agent", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    for name, status in rows:
        table.add_row(name, status)
    console.print()
    console.print(table)

    passed = len(rows) - failed
    console.print(
        f"[dim]mdk_eval_all_summary: "
        f"agents_total={len(rows)} "
        f"passed={passed} failed={failed} "
        f"gate={gate} "
        f"ok={'true' if failed == 0 else 'false'}[/dim]"
    )

    if failed:
        raise typer.Exit(code=2)

    # All-pass success — interactive picker (TTY prompts, non-TTY
    # renders the list as documentation only). One surface for
    # next-steps; no separate static block.
    if passed > 0:
        first_agent = rows[0][0]
        from movate.cli._next_steps import (  # noqa: PLC0415
            NextStep,
            mdk_bin_name,
            prompt_next_step,
        )

        bin_name = mdk_bin_name()
        prompt_next_step(
            console=console,
            steps=[
                NextStep(
                    label=f"Quick-run {first_agent!r}",
                    command=f"{bin_name} run {first_agent} --mock",
                    argv=[bin_name, "run", first_agent, "--mock"],
                ),
                NextStep(
                    label="Serve runtime locally (HTTP)",
                    command=f"{bin_name} serve",
                    argv=[bin_name, "serve"],
                ),
                NextStep(
                    label="Deploy agents to Azure dev",
                    command=f"{bin_name} deploy --target dev",
                    argv=[bin_name, "deploy", "--target", "dev"],
                ),
            ],
        )


def _resolve_remote_url(path: str) -> str | None:
    """Return ``path`` if it's an ``http(s)://`` URL, else ``None``.

    Lets the eval command accept either a filesystem agent directory
    OR a deployed runtime base URL in the same positional argument
    without splitting it across two commands.
    """
    lower = path.lower()
    if lower.startswith(("http://", "https://")):
        return path
    return None


async def _run_workflow_eval(  # noqa: PLR0912 — orchestrator mirrors _run_eval
    workflow_dir: Path,
    *,
    gate: float,
    gate_mode: str,
    runs: int,
    mock: bool,
    output_format: Report = Report.TABLE,
    gate_faithfulness: float | None = None,
    gate_coverage: float | None = None,
    gate_latency: float | None = None,
    gate_context_compliance: float | None = None,
    gate_refusal: float | None = None,
    baseline_file: Path | None = None,
    output_baseline: Path | None = None,
    regression_tolerance: float = 0.0,
) -> None:
    """Run a workflow eval end-to-end and apply accuracy + dimensional gates.

    Mirrors :func:`_run_eval` but drives :class:`WorkflowEvalEngine` instead
    of :class:`EvalEngine`. The same display / gate-check code runs on the
    returned :class:`EvalSummary` so the operator sees the same Rich tables.
    """
    from movate.core.workflow.compiler import compile_workflow  # noqa: PLC0415
    from movate.core.workflow.spec import WorkflowSpecLoadError, load_workflow_spec  # noqa: PLC0415

    try:
        spec, wf_dir = load_workflow_spec(workflow_dir)
    except WorkflowSpecLoadError as exc:
        err_console.print(f"[red]✗ workflow load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    if spec.evals is None:
        err_console.print(
            f"[red]✗[/red] {spec.name} has no [bold]evals:[/bold] stanza in workflow.yaml. "
            "Add an [bold]evals:[/bold] block with a [bold]dataset:[/bold] path."
        )
        raise typer.Exit(code=2)

    try:
        graph = compile_workflow(spec, wf_dir)
    except Exception as exc:
        err_console.print(f"[red]✗ workflow compile failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    effective_gate = gate
    effective_runs = runs

    rt = await build_local_runtime(mock=mock)
    baseline_record: EvalRecord | None = None
    try:
        show_progress = output_format == Report.TABLE and not mock
        with _maybe_eval_progress(show_progress) as on_case:
            engine = WorkflowEvalEngine(
                executor=rt.executor,
                storage=rt.storage,
                runs_per_case=effective_runs,
                gate_mode=gate_mode,
                on_case_complete=on_case,
            )
            try:
                summary = await engine.run(
                    graph,
                    wf_dir,
                    spec.evals,
                    workflow_name=spec.name,
                    workflow_version=spec.version,
                    threshold=effective_gate,
                )
            except EvalConfigError as exc:
                err_console.print(f"[red]✗ eval config error:[/red] {exc}")
                raise typer.Exit(code=2) from None

        record = summary.to_record()
        await rt.storage.save_eval(record)
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)

    if baseline_file is not None:
        baseline_record = _resolve_file_baseline(baseline_file)

    if output_baseline is not None:
        _write_baseline_file(output_baseline, record)

    cases_passing = sum(1 for c in summary.cases if c.aggregated_score >= effective_gate)
    overall_pass = summary.sample_count > 0 and cases_passing == summary.sample_count

    diff: BaselineDiff | None = None
    if baseline_record is not None:
        diff = compute_baseline_diff(baseline_record, record)

    if output_format == Report.JSON:
        _emit_json(
            summary,
            record=record,
            gate=effective_gate,
            cases_passing=cases_passing,
            overall_pass=overall_pass,
            diff=diff,
            regression_tolerance=regression_tolerance,
        )
    elif output_format == Report.MARKDOWN:
        print(render_eval_markdown(summary, gate=effective_gate))
    else:
        _emit_header_line(
            overall_pass=overall_pass,
            cases_passing=cases_passing,
            sample_count=summary.sample_count,
            gate=effective_gate,
            objective_filter=None,
        )
        _emit_table(
            summary,
            record=record,
            gate=effective_gate,
            cases_passing=cases_passing,
            overall_pass=overall_pass,
        )
        if _has_extra_dims(summary.dimensional_means):
            _emit_dimensional_breakdown(summary.dimensional_means)
        if diff is not None:
            _emit_diff_table(diff, regression_tolerance=regression_tolerance)
        _print_eval_summary_line(
            summary,
            record=record,
            gate=effective_gate,
            cases_passing=cases_passing,
            overall_pass=overall_pass,
            diff=diff,
            regression_tolerance=regression_tolerance,
        )

    failed_dim = _check_dimensional_gates(
        summary.dimensional_means,
        gate_faithfulness=gate_faithfulness,
        gate_coverage=gate_coverage,
        gate_latency=gate_latency,
        gate_context_compliance=gate_context_compliance,
        gate_refusal=gate_refusal,
    )

    failed_gate = not overall_pass
    failed_regression = diff is not None and diff.is_regression(tolerance=regression_tolerance)
    if failed_gate or failed_regression or failed_dim:
        raise typer.Exit(code=1)


async def _run_eval(  # noqa: PLR0912 — orchestrator; branch count is inherent
    bundle: AgentBundle,
    *,
    gate: float,
    gate_mode: str,
    runs: int,
    mock: bool,
    baseline_id: str | None = None,
    baseline_file: Path | None = None,
    output_baseline: Path | None = None,
    regression_tolerance: float = 0.0,
    objective: str | None = None,
    output_format: Report = Report.TABLE,
    remote_url: str | None = None,
    remote_api_key: str | None = None,
    gate_faithfulness: float | None = None,
    gate_coverage: float | None = None,
    gate_latency: float | None = None,
    gate_context_compliance: float | None = None,
    gate_refusal: float | None = None,
) -> None:
    rt = await build_local_runtime(mock=mock)
    # Dataset-aware mock (PR #104): when running --mock, configure
    # the MockProvider to return dataset[*].expected per call. The
    # eval engine iterates dataset rows in order, so the mock's
    # per-call cycle matches case-by-case — every case gets the
    # exactly-right expected output and scoring passes. Without this
    # the mock returns `{"message": "mock"}` for every case and ALL
    # cases fail schema validation (the previous demo annoyance).
    if mock:
        _configure_mock_for_bundle(rt.provider, bundle)
    # When path was a URL, swap the local in-process Executor for a
    # RemoteExecutor that submits each case as a job and polls. The
    # local runtime is still built because we need its provider (for
    # the LLM judge), storage (for saving the EvalRecord baseline),
    # and tracer — only the executor changes.
    remote_client: MovateClient | None = None
    if remote_url is not None:
        # MovateClient.__aenter__ doesn't actually open a connection
        # (httpx is lazy on first request), so building it here without
        # entering the context manager is safe — RemoteExecutor uses
        # it for the duration of the eval.
        remote_client = MovateClient(base_url=remote_url, api_key=remote_api_key or "")
        executor_for_eval: Executor | RemoteExecutor = RemoteExecutor(remote_client)
    else:
        executor_for_eval = rt.executor
    baseline_record: EvalRecord | None = None
    try:
        # Progress UI is on for human-facing output (table); off for
        # machine-readable formats so JSON / Markdown stay clean if a
        # user accidentally redirects stderr too. Mock mode is fast
        # enough that progress just adds noise — also off.
        show_progress = output_format == Report.TABLE and not mock

        with _maybe_eval_progress(show_progress) as on_case:
            engine = EvalEngine(
                executor=executor_for_eval,  # type: ignore[arg-type]
                provider=rt.provider,
                runs_per_case=runs,
                gate_mode=gate_mode,
                objective_filter=objective,
                on_case_complete=on_case,
            )
            try:
                summary = await engine.run(bundle)
            except EvalConfigError as exc:
                err_console.print(f"[red]✗ eval config error:[/red] {exc}")
                raise typer.Exit(code=2) from None

        record = summary.to_record()
        await rt.storage.save_eval(record)
        baseline_record = await _resolve_storage_baseline(rt.storage, baseline_id)
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)
        if remote_client is not None:
            # MovateClient owns the httpx connection pool; closing here
            # rather than via async-with so the pool stays alive across
            # the whole eval run (one TCP+TLS handshake amortised across
            # every case) instead of being torn down per request.
            await remote_client.aclose()

    # Resolve a file-based baseline outside the storage block — pure I/O,
    # no runtime needed. (`baseline_id` and `baseline_file` are mutually
    # exclusive at the CLI entry point so only one branch fires.)
    if baseline_file is not None:
        baseline_record = _resolve_file_baseline(baseline_file)

    # Write the current run's EvalRecord to disk if requested. Done after
    # storage is closed so a write failure can't corrupt the DB.
    if output_baseline is not None:
        _write_baseline_file(output_baseline, record)

    # Apply CLI gate. When --objective is set, the objective's own
    # threshold (declared in agent.yaml) is the gate; --gate is ignored
    # because the objective's contract lives in the agent definition.
    # Otherwise the eval-wide --gate is applied per-case.
    if objective is not None:
        # Find the matching objective summary (engine validated it exists).
        obj_summary = next(
            (o for o in summary.objective_summaries if o.objective_id == objective),
            None,
        )
        # Engine guarantees this exists when --objective is passed; fall
        # back to defensive defaults to keep type checkers happy.
        effective_gate = obj_summary.threshold if obj_summary else gate
        overall_pass = obj_summary.passed if obj_summary else False
        cases_passing = sum(1 for c in summary.cases if c.passed)
    else:
        effective_gate = gate
        cases_passing = sum(1 for c in summary.cases if c.aggregated_score >= gate)
        overall_pass = summary.sample_count > 0 and cases_passing == summary.sample_count

    diff: BaselineDiff | None = None
    if baseline_record is not None:
        diff = compute_baseline_diff(baseline_record, record)

    if output_format == Report.JSON:
        _emit_json(
            summary,
            record=record,
            gate=effective_gate,
            cases_passing=cases_passing,
            overall_pass=overall_pass,
            diff=diff,
            regression_tolerance=regression_tolerance,
        )
    elif output_format == Report.MARKDOWN:
        print(render_eval_markdown(summary, gate=effective_gate))
    else:
        # Eye-catching banner BEFORE the table — operators scrolling
        # through CI logs see PASS/FAIL at a glance without parsing
        # the "verdict" row buried in the head table.
        _emit_header_line(
            overall_pass=overall_pass,
            cases_passing=cases_passing,
            sample_count=summary.sample_count,
            gate=effective_gate,
            objective_filter=objective,
        )
        _emit_table(
            summary,
            record=record,
            gate=effective_gate,
            cases_passing=cases_passing,
            overall_pass=overall_pass,
            objective_filter=objective,
        )
        # Dimensional breakdown — only shown when at least one dim was
        # scored beyond accuracy. A dataset with no grounding / no
        # expected_coverage / no latency_budget_ms gets the same view
        # as v0.5 (silent, accuracy-only).
        if _has_extra_dims(summary.dimensional_means):
            _emit_dimensional_breakdown(summary.dimensional_means)
        if summary.objective_summaries and objective is None:
            # Per-objective breakdown — only when there are objectives
            # declared AND we're showing the full eval (not a single-
            # objective run, which already focused on that one).
            _emit_objective_breakdown(summary.objective_summaries)
        if diff is not None:
            _emit_diff_table(diff, regression_tolerance=regression_tolerance)
        # Greppable single-line summary at the very end of table mode.
        # CI logs piped through `grep mdk_eval_summary` get one
        # key=value line ready to parse. Skipped in JSON / Markdown
        # modes — they have their own structured surfaces.
        _print_eval_summary_line(
            summary,
            record=record,
            gate=effective_gate,
            cases_passing=cases_passing,
            overall_pass=overall_pass,
            diff=diff,
            regression_tolerance=regression_tolerance,
        )

    # Dimensional gates — checked after the main accuracy gate so the
    # operator sees the full report before any exit.
    failed_dim = _check_dimensional_gates(
        summary.dimensional_means,
        gate_faithfulness=gate_faithfulness,
        gate_coverage=gate_coverage,
        gate_latency=gate_latency,
        gate_context_compliance=gate_context_compliance,
        gate_refusal=gate_refusal,
    )

    # Exit codes: gate failure OR baseline regression OR dim gate all fail.
    failed_gate = not overall_pass
    failed_regression = diff is not None and diff.is_regression(tolerance=regression_tolerance)
    if failed_gate or failed_regression or failed_dim:
        raise typer.Exit(code=1)


def _emit_header_line(
    *,
    overall_pass: bool,
    cases_passing: int,
    sample_count: int,
    gate: float,
    objective_filter: str | None,
) -> None:
    """Print a single eye-catching PASS/FAIL banner above the table.

    Operators scanning CI logs want a one-glance verdict before they
    parse the head table. Renders to stdout (same channel as the
    main table) so log capture keeps banner + table together.
    """
    if overall_pass:
        verdict = "[bold green]✓ Eval PASSED[/bold green]"
    else:
        verdict = "[bold red]✗ Eval FAILED[/bold red]"
    obj_tag = f" · objective={objective_filter}" if objective_filter else ""
    console.print(
        f"{verdict}  [dim]— {cases_passing}/{sample_count} cases at gate "
        f"≥ {gate:.2f}{obj_tag}[/dim]"
    )


def _print_eval_summary_line(
    summary: EvalSummary,
    *,
    record: EvalRecord,
    gate: float,
    cases_passing: int,
    overall_pass: bool,
    diff: BaselineDiff | None,
    regression_tolerance: float,
) -> None:
    """Emit ``mdk_eval_summary: agent=... gate=... ...`` line.

    Mirrors :func:`movate.cli.audit_cmd._print_summary_line` so the
    diagnostic surface across audit/eval/doctor is consistent —
    operators grep one prefix to extract structured eval results
    from a CI log without parsing Rich panels.
    """
    regressed = (
        "true"
        if diff is not None and diff.is_regression(tolerance=regression_tolerance)
        else "false"
    )
    pass_rate = cases_passing / summary.sample_count if summary.sample_count else 0.0
    console.print(
        f"[dim]mdk_eval_summary: "
        f"agent={summary.agent} "
        f"eval_id={record.eval_id[:8]} "
        f"cases={summary.sample_count} "
        f"passing={cases_passing} "
        f"pass_rate={pass_rate:.3f} "
        f"mean_score={summary.mean_score:.3f} "
        f"gate={gate:.2f} "
        f"overall_pass={str(overall_pass).lower()} "
        f"regressed={regressed}[/dim]"
    )


def _emit_table(
    summary: EvalSummary,
    *,
    record: EvalRecord,
    gate: float,
    cases_passing: int,
    overall_pass: bool,
    objective_filter: str | None = None,
) -> None:
    title = f"{summary.agent} v{summary.agent_version} — eval results"
    if objective_filter is not None:
        title += f"  ·  objective={objective_filter}"
    head = Table(
        title=title,
        show_header=False,
    )
    head.add_column("field", style="dim")
    head.add_column("value")
    head.add_row("eval_id", record.eval_id)
    head.add_row("judge", f"{summary.judge.method.value}")
    if summary.judge_provider:
        head.add_row("judge.provider", summary.judge_provider)
    head.add_row("dataset.hash", summary.dataset_hash[:12] + "…")
    head.add_row("runs/case", str(summary.runs_per_case))
    head.add_row("gate_mode", summary.gate_mode)
    gate_label = (
        f"{gate:.2f}  [dim](from objective '{objective_filter}')[/dim]"
        if objective_filter
        else f"{gate:.2f}"
    )
    head.add_row("gate", gate_label)
    head.add_row("cases", str(summary.sample_count))
    head.add_row("mean score", f"{summary.mean_score:.3f}")
    head.add_row(
        "pass rate",
        f"{cases_passing}/{summary.sample_count} "
        f"({(cases_passing / summary.sample_count if summary.sample_count else 0):.0%})",
    )
    head.add_row("total cost", f"${summary.total_cost_usd:.6f}")
    verdict = "[green]PASS[/green]" if overall_pass else "[red]FAIL[/red]"
    head.add_row("verdict", verdict)
    console.print(head)

    cases = Table(title="Cases", show_header=True, header_style="bold")
    cases.add_column("#", style="dim", width=3)
    cases.add_column("score")
    cases.add_column("runs")
    cases.add_column("input", overflow="fold")
    cases.add_column("first rationale", overflow="fold")
    for i, c in enumerate(summary.cases, start=1):
        score_text = f"{c.aggregated_score:.2f}"
        score_styled = f"[green]{score_text}[/green]" if c.passed else f"[red]{score_text}[/red]"
        first_rat = c.runs[0].rationale if c.runs else ""
        scores_per_run = ", ".join(f"{r.score:.2f}" for r in c.runs)
        cases.add_row(
            str(i),
            score_styled,
            scores_per_run,
            _truncate(str(c.case.input), 40),
            _truncate(first_rat, 60),
        )
    console.print(cases)


def _has_extra_dims(means: DimensionalMeans) -> bool:
    """True iff the dataset opted in to faithfulness or coverage.

    Accuracy and latency are scored on every successful run — accuracy is
    the gate input (already shown as ``mean score``), and latency is
    derived from the agent's call budget regardless of whether the
    dataset asked for it. The breakdown table only adds value when a
    dataset opts in to faithfulness (via ``grounding``) or coverage
    (via ``expected_coverage``). Legacy datasets keep the exact v0.5
    single-score view — no extra section.
    """
    return any(
        v is not None
        for v in (means.faithfulness, means.coverage, means.context_compliance, means.refusal)
    )


def _emit_dimensional_breakdown(means: DimensionalMeans) -> None:
    """Render the per-dimension rollup as a small two-column table.

    Only scored dims (non-``None``) get a row — silent on dims the
    dataset didn't opt into. Accuracy is always present (it's the gate)
    but the other three are conditional, so a dataset with grounding
    but no expected_coverage sees a two-row table.
    """
    table = Table(
        title="Dimensional breakdown",
        show_header=True,
        header_style="bold",
    )
    table.add_column("dimension", style="bold")
    table.add_column("mean", justify="right")
    table.add_column("notes", style="dim")

    rows: list[tuple[str, float | None, str]] = [
        ("accuracy", means.accuracy, "judge / exact-match score"),
        ("faithfulness", means.faithfulness, "answer grounded in context"),
        ("coverage", means.coverage, "expected substrings present"),
        ("latency", means.latency, "1.0 within budget, decays to 2x budget"),
        ("context_compliance", means.context_compliance, "output respects context guidelines"),
        ("refusal", means.refusal, "1.0 = agent refused as expected"),
    ]
    for name, value, note in rows:
        if value is None:
            continue
        table.add_row(name, f"{value:.3f}", note)
    console.print(table)


def _check_dimensional_gates(
    means: DimensionalMeans,
    *,
    gate_faithfulness: float | None,
    gate_coverage: float | None,
    gate_latency: float | None,
    gate_context_compliance: float | None = None,
    gate_refusal: float | None = None,
) -> bool:
    """Check per-dimension CI gates. Prints a verdict line for each and
    returns True if any gate failed. Skips silently when the dataset
    didn't score that dimension (no grounding / no expected_coverage).
    """
    failed = False
    _dim_checks = [
        ("faithfulness", gate_faithfulness, means.faithfulness, "grounding"),
        ("coverage", gate_coverage, means.coverage, "expected_coverage"),
        ("latency", gate_latency, means.latency, None),
        ("context_compliance", gate_context_compliance, means.context_compliance, None),
        ("refusal", gate_refusal, means.refusal, "refusal_expected"),
    ]
    for dim_name, threshold, actual, field_hint in _dim_checks:
        if threshold is None:
            continue
        if actual is None:
            hint = f" (dataset has no [bold]{field_hint}[/bold] field)" if field_hint else ""
            console.print(
                f"[yellow]![/yellow] --gate-{dim_name} set but {dim_name} not scored{hint}; "
                "gate skipped"
            )
            continue
        if actual < threshold:
            console.print(
                f"[red]✗[/red] dimensional gate failed: "
                f"{dim_name} {actual:.3f} < {threshold:.2f}"
            )
            failed = True
        else:
            console.print(
                f"[green]✓[/green] dimensional gate passed: "
                f"{dim_name} {actual:.3f} ≥ {threshold:.2f}"
            )
    return failed


def _emit_objective_breakdown(summaries: list[ObjectiveSummary]) -> None:
    """Render the per-objective rollup as its own Rich table.

    Shown beneath the main eval table when the agent has objectives
    declared in agent.yaml. Each row is one objective with its sample
    count, mean score, pass/fail vs its threshold, and the judge method.

    Objectives with zero cases (no dataset rows tagged with their id)
    show with a dim "no cases" placeholder rather than a misleading
    pass/fail verdict.
    """
    table = Table(title="Per-objective breakdown", show_header=True, header_style="bold")
    table.add_column("objective", style="bold")
    table.add_column("cases", justify="right")
    table.add_column("mean", justify="right")
    table.add_column("threshold", justify="right")
    table.add_column("judge", style="dim")
    table.add_column("verdict")

    for s in summaries:
        if s.sample_count == 0:
            table.add_row(
                s.objective_id,
                "0",
                "[dim]—[/dim]",
                f"{s.threshold:.2f}",
                s.judge_method,
                "[dim]no cases[/dim]",
            )
            continue
        mean_txt = f"{s.mean_score:.3f}"
        verdict = "[green]PASS[/green]" if s.passed else "[red]FAIL[/red]"
        table.add_row(
            s.objective_id,
            str(s.sample_count),
            mean_txt,
            f"{s.threshold:.2f}",
            s.judge_method,
            verdict,
        )
    console.print(table)


def _emit_diff_table(diff: BaselineDiff, *, regression_tolerance: float) -> None:
    """Render the baseline diff as a Rich table after the main eval output."""
    table = Table(
        title=f"Baseline diff vs {diff.baseline.eval_id[:8]}…",
        show_header=False,
    )
    table.add_column("field", style="dim")
    table.add_column("value")
    table.add_row("baseline_eval_id", diff.baseline.eval_id)
    table.add_row("baseline_age", _humanize_age(diff.baseline_age_seconds))
    table.add_row(
        "mean_score",
        f"{diff.baseline.mean_score:.3f} → {diff.current.mean_score:.3f}  "
        f"({_signed(diff.mean_score_delta)})",
    )
    table.add_row(
        "pass_rate",
        f"{diff.baseline.pass_rate:.3f} → {diff.current.pass_rate:.3f}  "
        f"({_signed(diff.pass_rate_delta)})",
    )
    table.add_row(
        "sample_count",
        f"{diff.baseline.sample_count} → {diff.current.sample_count}  "
        f"({diff.sample_count_delta:+d})",
    )
    table.add_row(
        "total_cost",
        f"${diff.baseline.total_cost_usd:.6f} → ${diff.current.total_cost_usd:.6f}  "
        f"({_signed(diff.cost_delta)})",
    )
    if diff.dataset_changed:
        table.add_row(
            "dataset",
            "[yellow]changed[/yellow] [dim](hash differs from baseline)[/dim]",
        )

    if diff.is_regression(tolerance=regression_tolerance):
        verdict = f"[red]REGRESSION[/red] [dim](tolerance ±{regression_tolerance:.2f})[/dim]"
    else:
        verdict = "[green]OK[/green]"
    table.add_row("verdict", verdict)
    console.print(table)


def _signed(value: float) -> str:
    delta = format_delta(value)
    if value > 0:
        return f"[green]{delta}[/green]"
    if value < 0:
        return f"[red]{delta}[/red]"
    return f"[dim]{delta}[/dim]"


_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86400


def _humanize_age(seconds: float) -> str:
    if seconds < _SECONDS_PER_MINUTE:
        return f"{seconds:.0f}s ago"
    if seconds < _SECONDS_PER_HOUR:
        return f"{seconds / _SECONDS_PER_MINUTE:.1f} min ago"
    if seconds < _SECONDS_PER_DAY:
        return f"{seconds / _SECONDS_PER_HOUR:.1f} hr ago"
    return f"{seconds / _SECONDS_PER_DAY:.1f} days ago"


def _emit_json(
    summary: EvalSummary,
    *,
    record: EvalRecord,
    gate: float,
    cases_passing: int,
    overall_pass: bool,
    diff: BaselineDiff | None,
    regression_tolerance: float,
) -> None:
    payload: dict[str, object] = {
        "eval_id": record.eval_id,
        "agent": summary.agent,
        "agent_version": summary.agent_version,
        "dataset_hash": summary.dataset_hash,
        "judge_method": summary.judge.method.value,
        "judge_provider": summary.judge_provider,
        "runs_per_case": summary.runs_per_case,
        "gate_mode": summary.gate_mode,
        "gate": gate,
        "sample_count": summary.sample_count,
        "mean_score": round(summary.mean_score, 6),
        "cases_passing": cases_passing,
        "pass_rate": round(cases_passing / summary.sample_count, 6)
        if summary.sample_count
        else 0.0,
        "total_cost_usd": summary.total_cost_usd,
        "overall_pass": overall_pass,
        # Per-dimension rollup. Each value is the mean across cases
        # that scored that dim, or null if no case opted in (e.g. a
        # dataset without grounding fields leaves faithfulness=null).
        "dimensional_means": {
            "accuracy": _round_or_none(summary.dimensional_means.accuracy),
            "faithfulness": _round_or_none(summary.dimensional_means.faithfulness),
            "coverage": _round_or_none(summary.dimensional_means.coverage),
            "latency": _round_or_none(summary.dimensional_means.latency),
        },
        "cases": [
            {
                "input": c.case.input,
                "expected": c.case.expected,
                "score": round(c.aggregated_score, 6),
                "passed": c.passed,
                "scores_per_run": [round(r.score, 6) for r in c.runs],
                "rationales": [r.rationale for r in c.runs],
                # Per-run dimension scores. Each dim is { "value": float|null,
                # "rationale": str }; null means the dim wasn't scored for
                # that case (no grounding / no expected_coverage / no budget).
                "dimensions_per_run": [_serialize_dimensions(r.dimensions) for r in c.runs],
            }
            for c in summary.cases
        ],
    }
    if diff is not None:
        payload["baseline"] = {
            "eval_id": diff.baseline.eval_id,
            "mean_score": diff.baseline.mean_score,
            "pass_rate": diff.baseline.pass_rate,
            "sample_count": diff.baseline.sample_count,
            "dataset_hash": diff.baseline.dataset_hash,
            "created_at": diff.baseline.created_at.isoformat(),
            "mean_score_delta": diff.mean_score_delta,
            "pass_rate_delta": diff.pass_rate_delta,
            "sample_count_delta": diff.sample_count_delta,
            "cost_delta": diff.cost_delta,
            "dataset_changed": diff.dataset_changed,
            "regression": diff.is_regression(tolerance=regression_tolerance),
            "regression_tolerance": regression_tolerance,
        }
    print(json.dumps(payload, indent=2))


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _round_or_none(value: float | None) -> float | None:
    """Round to 6dp for stable JSON output; preserve ``None`` for unscored dims."""
    return None if value is None else round(value, 6)


def _serialize_dimensions(dims: object) -> dict[str, dict[str, float | str | None]]:
    """Serialize a :class:`DimensionScores` to a JSON-safe dict.

    Typed as ``object`` so this helper has no static import dependency
    on :mod:`movate.core.eval`'s dataclass — the JSON emitter doesn't
    need a tight type binding here, and accepting ``object`` keeps this
    file's import surface narrow.
    """
    out: dict[str, dict[str, float | str | None]] = {}
    for name in ("accuracy", "faithfulness", "coverage", "latency"):
        ds = getattr(dims, name)
        out[name] = {
            "value": _round_or_none(ds.value),
            "rationale": ds.rationale,
        }
    return out


async def _resolve_storage_baseline(
    storage: StorageProvider, baseline_id: str | None
) -> EvalRecord | None:
    """Look up a sqlite-stored baseline by id; ``None`` if no id passed.

    Lives as a helper because the lookup must happen before the storage
    layer closes — otherwise ``finally`` would shut storage down first.
    """
    if baseline_id is None:
        return None
    # Local CLI flow — Executor stamps tenant_id="local" on every run.
    # Server-side use (when this lands behind HTTP) will pass the
    # authenticated tenant.
    record = await storage.get_eval(baseline_id, tenant_id="local")
    if record is None:
        err_console.print(f"[red]✗[/red] baseline eval id {baseline_id!r} not found in storage")
        raise typer.Exit(code=2) from None
    return record


def _resolve_file_baseline(baseline_file: Path) -> EvalRecord:
    """Read a JSON-serialized EvalRecord from disk, with a friendly error path."""
    try:
        return _load_baseline_file(baseline_file)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        err_console.print(f"[red]✗[/red] baseline file load failed: {exc}")
        raise typer.Exit(code=2) from None


def _load_baseline_file(path: Path) -> EvalRecord:
    """Read an :class:`EvalRecord` JSON dump back into a model instance.

    Raises :class:`FileNotFoundError`, :class:`json.JSONDecodeError`, or
    :class:`ValueError` (from Pydantic) — the caller turns these into
    ``Exit(2)``.
    """
    raw = path.read_text()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"baseline file must be a JSON object, got {type(payload).__name__}")
    return EvalRecord.model_validate(payload)


def _write_baseline_file(path: Path, record: EvalRecord) -> None:
    """Persist ``record`` as pretty-printed JSON.

    Creates parent directories so users can drop the baseline at
    ``.movate/baseline.json`` without pre-creating the dir.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.model_dump_json(indent=2) + "\n")
    err_console.print(f"[green]✓[/green] wrote baseline → {path}")


@contextmanager
def _maybe_eval_progress(
    enabled: bool,
) -> Iterator[Callable[[int, int, CaseSummary], None] | None]:
    """Yield a callback suitable for ``EvalEngine.on_case_complete``.

    When ``enabled``, drives a stderr progress bar that updates after
    each case with running mean score. When disabled, yields ``None``
    so the engine sees no progress hook (clean JSON / Markdown output).

    The bar's total isn't known until the engine starts iterating
    (``load_dataset`` runs inside ``EvalEngine.run``), so we set total
    on the first callback via ``advance(total=...)``.
    """
    if not enabled:
        yield None
        return

    running_total = 0.0

    with progress_bar(description="cases", total=None) as advance:

        def on_case(done: int, total: int, summary: CaseSummary) -> None:
            nonlocal running_total
            running_total += summary.aggregated_score
            mean = running_total / done if done else 0.0
            advance(total=total, suffix=f" (mean={mean:.2f})")

        yield on_case


def _configure_mock_for_bundle(provider: object, bundle: AgentBundle) -> None:
    """If ``provider`` is a :class:`MockProvider` and ``bundle`` ships an
    evals dataset, configure the mock to cycle through the dataset's
    ``expected`` outputs in order. See PR #104 — keeps ``mdk eval --mock``
    from failing every case on schema_error against templates with
    non-trivial output schemas (lead-qualifier, ticket-triager, etc.).

    Mirrors the same helper in ``run.py``. Lives here (rather than in a
    shared module) because both call-sites are short + the import surface
    of ``movate.providers.mock`` is intentionally narrow.
    """
    from movate.providers.mock import MockProvider, load_dataset_expecteds  # noqa: PLC0415

    if not isinstance(provider, MockProvider):
        return
    dataset_decl = getattr(bundle.spec.evals, "dataset", None) if bundle.spec.evals else None
    if not dataset_decl:
        return
    dataset_path = (bundle.agent_dir / dataset_decl).resolve()
    expecteds = load_dataset_expecteds(dataset_path)
    if expecteds:
        provider.configure_dataset(expecteds)
