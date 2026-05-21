"""``movate eval <agent>`` — score an agent against its dataset and gate on a threshold.

``--baseline <eval-id>`` opts into the regression-detection loop: the
current eval is diffed against the persisted baseline and the CLI exits
non-zero if mean_score or pass_rate dropped past ``--regression-tolerance``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import IntPrompt, Prompt
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
from movate.core.models import EvalRecord, JudgeConfig, JudgeMethod, ModelConfig
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
    gate_retrieval_accuracy: float = typer.Option(
        None,
        "--gate-retrieval-accuracy",
        help=(
            "Minimum mean retrieval-accuracy score (0.0-1.0) required to pass. "
            "Only fires when the agent has grounding context (contexts/ files or "
            "dataset grounding fields) and a judge model. "
            "Exit 1 if retrieval_accuracy mean is below this threshold."
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
    variant: str | None = typer.Option(
        None,
        "--variant",
        help=(
            "Path to a second agent directory to run A/B comparison against. "
            "Runs the same dataset against both agents and prints a side-by-side "
            "score table. The primary agent's gate still applies; the variant "
            "is informational. Example: --variant agents/rag-qa-v2"
        ),
    ),
    judge_model: list[str] = typer.Option(
        [],
        "--judge-model",
        help=(
            "LiteLLM model string for the LLM-as-judge, e.g. "
            "[bold]anthropic/claude-opus-4-7[/bold]. "
            "Overrides [bold]judge.yaml[/bold] — no config file needed. "
            "Requires [bold]--judge-rubric[/bold]. "
            "Repeat the flag twice or more for a multi-judge panel — "
            "judges score independently and the panel mean is used "
            "unless std-dev exceeds the variance threshold, in which "
            "case [bold]--arbitrator-model[/bold] (if set) breaks the tie."
        ),
    ),
    judge_rubric: str = typer.Option(
        None,
        "--judge-rubric",
        help=(
            "Scoring rubric passed verbatim to the LLM judge(s). "
            "Required when [bold]--judge-model[/bold] is set. "
            "Example: [dim]'Score 0-1: 1=all fields correct, 0=any field wrong'[/dim]"
        ),
    ),
    arbitrator_model: str = typer.Option(
        None,
        "--arbitrator-model",
        help=(
            "Tiebreaker model used when panel judges disagree by more "
            "than [bold]--variance-threshold[/bold]. Pick a high-capability "
            "model from a family different from any panel judge. "
            "Only meaningful when 2+ [bold]--judge-model[/bold] are set."
        ),
    ),
    variance_threshold: float = typer.Option(
        0.3,
        "--variance-threshold",
        help=(
            "Std-dev threshold above which the arbitrator is consulted "
            "(0.0-1.0). Lower = more sensitive to disagreement; default "
            "0.3 catches roughly the 'mild divergence' case. Only used "
            "in panel mode."
        ),
    ),
    scorecard: bool = typer.Option(
        False,
        "--scorecard",
        help=(
            "Switch to the new LLM-generated test cases + 10-category "
            "scorecard flow (same as [bold]mdk eval-scorecard[/bold]). "
            "Skips the dataset.jsonl-based scoring entirely; instead "
            "Anthropic generates [bold]--scorecard-count[/bold] test "
            "inputs in the chosen [bold]--scorecard-mix[/bold] and "
            "scores each against accuracy / faithfulness / format / "
            "safety / refusal / hallucination / completeness / "
            "instruction_following / latency / cost. Other flags "
            "(--gate, --baseline, --runs, etc.) are ignored when "
            "--scorecard is set."
        ),
    ),
    scorecard_count: int = typer.Option(
        10,
        "--scorecard-count",
        min=1,
        max=100,
        help=("Number of LLM-generated cases when --scorecard is set (1-100). Ignored otherwise."),
    ),
    scorecard_mix: str = typer.Option(
        "standard",
        "--scorecard-mix",
        help=(
            "Test-case mix when --scorecard is set: standard | edge | "
            "adversarial | domain. Ignored otherwise."
        ),
    ),
    scorecard_judge_model: str | None = typer.Option(
        None,
        "--scorecard-judge-model",
        help=(
            "Override the LLM judge provider/model for the 10-category "
            "rubric when --scorecard is set. Defaults to the agent's own "
            "model."
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

      [dim]# Inline LLM judge — no judge.yaml needed:[/dim]
      $ mdk eval ./ticket-triager \\
          --judge-model anthropic/claude-opus-4-7 \\
          --judge-rubric 'Score 0-1: 1=correct routing, 0=wrong' \\
          --runs 3

      [dim]# NEW: LLM-generated cases + 10-category scorecard (opt-in):[/dim]
      $ mdk eval ./rag-qa --scorecard
      $ mdk eval ./rag-qa --scorecard --scorecard-count 25 --scorecard-mix domain
    """
    # --scorecard short-circuits everything else. Route directly to the
    # new scorecard flow (Phase 1 + Phase 2). Other flags (--gate,
    # --baseline, --runs, etc.) are not meaningful in the scorecard
    # world — emit a warning if any non-default values were supplied
    # so operators don't think they took effect, then dispatch.
    if scorecard:
        if path is None:
            err_console.print(
                "[red]✗[/red] --scorecard requires an agent path (e.g. "
                "[bold]mdk eval agents/rag-qa --scorecard[/bold])"
            )
            raise typer.Exit(code=2)
        from movate.cli.eval_scorecard_cmd import eval_scorecard  # noqa: PLC0415

        eval_scorecard(
            agent=path,
            count=scorecard_count,
            mix=scorecard_mix,
            mock=mock,
            judge_model=scorecard_judge_model,
        )
        return

    if baseline is not None and baseline_file is not None:
        err_console.print("[red]✗[/red] --baseline and --baseline-file are mutually exclusive")
        raise typer.Exit(code=2)

    _valid_gate_modes = ("mean", "min", "p10")
    if gate_mode not in _valid_gate_modes:
        err_console.print(
            f"[red]✗[/red] --gate-mode {gate_mode!r} is not valid. "
            f"Choose one of: {', '.join(_valid_gate_modes)}"
        )
        raise typer.Exit(code=2)

    # --judge-model / --judge-rubric: build an inline JudgeConfig that
    # bypasses judge.yaml. Validated here so errors surface before any
    # network / dataset work begins.
    judge_override: JudgeConfig | None = None
    if judge_model:
        if not judge_rubric:
            err_console.print(
                "[red]✗[/red] --judge-model requires --judge-rubric "
                "(provide a scoring rubric, e.g. 'Score 0-1: 1=correct, 0=wrong')"
            )
            raise typer.Exit(code=2)
        if len(judge_model) == 1:
            if arbitrator_model is not None:
                err_console.print(
                    "[red]✗[/red] --arbitrator-model requires a panel "
                    "(pass --judge-model 2+ times); a single judge has "
                    "nothing to arbitrate."
                )
                raise typer.Exit(code=2)
            judge_override = JudgeConfig(
                method=JudgeMethod.LLM_JUDGE,
                model=ModelConfig(provider=judge_model[0]),
                rubric=judge_rubric,
            )
        else:
            # Multi-judge panel: N >= 2 judges score concurrently;
            # arbitrator breaks ties when std_dev > variance_threshold.
            judge_override = JudgeConfig(
                method=JudgeMethod.PANEL,
                judges=[ModelConfig(provider=m) for m in judge_model],
                rubric=judge_rubric,
                variance_threshold=variance_threshold,
                escalation=(ModelConfig(provider=arbitrator_model) if arbitrator_model else None),
            )
    elif judge_rubric:
        err_console.print(
            "[red]✗[/red] --judge-rubric requires --judge-model "
            "(specify which model should apply the rubric)"
        )
        raise typer.Exit(code=2)
    elif arbitrator_model is not None:
        err_console.print(
            "[red]✗[/red] --arbitrator-model requires --judge-model "
            "(at least 2; the arbitrator only fires inside a panel)"
        )
        raise typer.Exit(code=2)

    # --compare: auto-read+write evals/.last-run.json in the agent dir (or
    # cwd for --all). Resolved to actual path after path resolution below.
    _compare_pending = compare

    # Pre-flight: eval (any path that isn't --mock) needs at least
    # one LLM provider key — both for case generation AND for judge
    # scoring. Surface a missing-keys warning BEFORE the wizard takes
    # the operator through 4-5 prompts only to discover at the end
    # that they can't run. Skipped under --mock (offline mode).
    if not mock:
        # Remind interactive users that --mock is available before hitting
        # the key-check prompt — useful the first time they run mdk eval
        # in a fresh environment or CI without keys configured.
        if sys.stderr.isatty():
            err_console.print(
                "[dim]tip: [bold]mdk eval --mock[/bold] runs without API keys "
                "(deterministic, great for CI and offline validation)[/dim]"
            )
        _require_llm_provider_key_or_offer_setup()

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
        # Scorecard branch: the operator picked "generate fresh" in
        # the test-cases prompt. The wizard ALREADY generated +
        # previewed the cases (and got operator approval) AND asked
        # the gate threshold. Dispatch directly to the orchestrator
        # so we can pass the pre-generated entries through — going
        # via ``eval_scorecard`` (the Typer command) would have no
        # way to forward ``pre_generated_entries`` since it's not a
        # CLI flag.
        if wizard.scorecard and wizard.all_in_project:
            # --all + generate: dispatch to the scorecard's project-
            # wide sweep. The sweep generates per-agent internally;
            # the wizard skipped its own preview step (would have
            # rendered N tables x 30s each).
            from movate.cli.eval_scorecard_cmd import (  # noqa: PLC0415
                GateConfig,
                _run_scorecard_all_in_project,
            )

            _run_scorecard_all_in_project(
                count=wizard.scorecard_count,
                mix=wizard.scorecard_mix,
                mock=wizard.mock,
                judge_model=None,
                gates=GateConfig(overall=wizard.scorecard_gate),
                runs_per_case=wizard.scorecard_runs,
            )
            return
        if wizard.scorecard and wizard.path is not None:
            from movate.cli.eval_scorecard_cmd import (  # noqa: PLC0415
                GateConfig,
                _run_scorecard_single_agent,
            )

            agent_path = Path.cwd() / "agents" / wizard.path
            _run_scorecard_single_agent(
                agent_path_str=str(agent_path),
                count=wizard.scorecard_count,
                mix=wizard.scorecard_mix,
                mock=wizard.mock,
                judge_model=None,
                gates=GateConfig(overall=wizard.scorecard_gate),
                pre_generated_entries=wizard.scorecard_entries,
                runs_per_case=wizard.scorecard_runs,
            )
            return
        # Legacy branch: apply wizard's answers as if they were CLI
        # flags, then fall through to the standard dispatch below.
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
            gate_retrieval_accuracy=gate_retrieval_accuracy,
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
                gate_retrieval_accuracy=gate_retrieval_accuracy,
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
            gate_retrieval_accuracy=gate_retrieval_accuracy,
            judge_override=judge_override,
        )
    )

    # --variant A/B comparison: run the same dataset against a second agent
    # configuration and print a side-by-side score table. The variant eval
    # result is informational — exit code is determined by the primary only.
    if variant is not None:
        try:
            variant_bundle = load_agent(Path(variant))
        except AgentLoadError as exc:
            err_console.print(f"[red]✗ variant load failed:[/red] {exc}")
            raise typer.Exit(code=2) from None

        asyncio.run(
            _run_variant_comparison(
                primary_bundle=bundle,
                variant_bundle=variant_bundle,
                gate=gate,
                gate_mode=gate_mode,
                runs=runs,
                mock=mock,
                objective=objective,
                judge_override=judge_override,
            )
        )


@dataclass
class _EvalWizardChoices:
    """Resolved answers from the interactive eval wizard.

    Maps 1:1 to the CLI flags the dispatch path already handles, so
    the wizard's only job is collecting choices — execution stays in
    the existing code paths.

    Two dispatch modes:

    * **Legacy** (``scorecard=False``): scores against
      ``evals/dataset.jsonl`` with ``--gate``, ``--runs``,
      ``--baseline-*``. The operator picked "keep existing dataset"
      in the test-cases prompt, so they want the curated dataset
      scored the curated way.

    * **Scorecard** (``scorecard=True``): the operator picked
      "generate fresh cases", which pairs naturally with the
      10-category scorecard rubric. The wizard skips the gate /
      runs / baseline questions (they don't map onto the 10-cat
      rubric) and dispatches to ``eval_scorecard_cmd.eval_scorecard``
      with ``scorecard_count`` + ``scorecard_mix``. Generation happens
      once inside the scorecard (no double-generation).
    """

    path: str | None
    all_in_project: bool
    mock: bool
    gate: float
    runs: int
    baseline_file: Path | None
    output_baseline: Path | None
    # Scorecard-mode dispatch (set when the operator picked
    # "generate fresh cases" in the test-cases prompt).
    scorecard: bool = False
    scorecard_count: int = 10
    scorecard_mix: str = "standard"
    # Pre-generated entries the operator approved in the wizard's
    # preview table. When set, the scorecard skips its internal
    # ``_generate_entries`` call and scores these directly — no
    # double-generation, no surprise about WHAT got scored.
    scorecard_entries: list[dict[str, Any]] | None = None
    # Overall gate threshold the operator picked in the wizard's
    # gate question (added 2026-05-19; previously the scorecard
    # branch skipped the gate prompt entirely).
    scorecard_gate: float = 0.0
    # Runs per case for the scorecard (added 2026-05-19). N=3+
    # averages out judge sampling variance — without it operators
    # routinely see "everything 1.00" because each case is judged
    # exactly once and the judge gives binary-ish scores. The
    # scorecard runs the agent + scoring loop ``scorecard_runs``
    # times and averages per-category scores.
    scorecard_runs: int = 1


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
            "[dim]A few questions; press Ctrl-C any time to quit. "
            "We generate fresh test cases first (via LLM), then ask "
            "how strictly to score them. The resolved command is shown "
            "before it runs so you can copy-paste it next time.[/dim]",
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

    # Mock vs real provider: previously a wizard question, but operators
    # running `mdk eval` interactively almost always want real models
    # (mocks are a CI / hermetic-testing concern, surfaced via the
    # `--mock` CLI flag). Defaulting to real-provider drops one prompt
    # without losing functionality.
    use_mock = False

    # Q2 (NEW): Generate fresh test cases or keep existing?
    # Branches into two scoring models:
    #
    # * "generate" → 10-category scorecard rubric (matches the
    #   scorecard-style workflow this command was redesigned around).
    #   Skips the gate/runs/baseline questions entirely; the scorecard
    #   has its own scoring model.
    # * "keep" → legacy dataset-based scoring. Falls through to the
    #   gate/runs/baseline questions below.
    #
    # Ask "generate / keep" for BOTH single-agent and --all modes.
    # Pre-2026-05-19 the wizard skipped this prompt in --all mode and
    # went straight to legacy gate/runs/baseline — operator running
    # ``mdk eval --all`` had no way to opt into the 10-cat scorecard
    # from the wizard. Now both modes get the same question; the
    # downstream behavior differs only in whether the preview table
    # fires (single-agent: yes; --all: no, since generating + showing
    # 10 tables would take 5+ minutes and overwhelm the operator).
    raw_choice = _prompt_generate_cases(cwd, chosen_agent)
    if raw_choice is _CANCELLED:
        return None
    if raw_choice is not None:
        # Narrow: raw_choice is neither _CANCELLED nor None →
        # it's the (count, mix) tuple from _ask_scorecard_count_and_mix.
        assert isinstance(raw_choice, tuple)
        count, mix = raw_choice
        is_all = chosen_agent == "all"
        if is_all:
            # --all mode: skip the preview table (would render N
            # tables x 30s each) but still ask runs-per-case + the
            # gate threshold so CI scripts can gate on the rollup
            # overall mean.
            runs_per_case = _prompt_runs_per_case()
            if runs_per_case is None:
                return None
            gate = _prompt_scorecard_gate()
            if gate is None:
                return None
            entries: list[dict[str, Any]] | None = None
            console.print()
            console.print(
                Panel(
                    f"[bold]Running:[/bold] scorecard --all "
                    f"[dim]({count} {mix} cases per agent x "
                    f"{runs_per_case} run(s), gate-overall={gate})[/dim]",
                    title="[green]✓[/green] Configured",
                    border_style="green",
                    title_align="left",
                )
            )
        else:
            # Single-agent mode: full preview-and-approve flow.
            preview = _generate_preview_and_gate(
                chosen_agent, count=count, mix=mix, mock=use_mock, cwd=cwd
            )
            if preview is None:
                return None
            entries, runs_per_case, gate = preview
            console.print()
            console.print(
                Panel(
                    f"[bold]Running:[/bold] scorecard against "
                    f"[bold]{chosen_agent}[/bold] "
                    f"[dim]({len(entries)} pre-generated {mix} cases x "
                    f"{runs_per_case} run(s), gate-overall={gate})[/dim]",
                    title="[green]✓[/green] Configured",
                    border_style="green",
                    title_align="left",
                )
            )
        return _EvalWizardChoices(
            path=None if is_all else chosen_agent,
            all_in_project=is_all,
            mock=use_mock,
            gate=0.0,  # ignored in scorecard mode
            runs=1,  # ignored in scorecard mode
            baseline_file=None,
            output_baseline=None,
            scorecard=True,
            scorecard_count=count,
            scorecard_mix=mix,
            scorecard_entries=entries,
            scorecard_gate=gate,
            scorecard_runs=runs_per_case,
        )

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


# Sentinel for ``_prompt_generate_cases`` Ctrl-C return — using a
# typed object lets the caller distinguish "user cancelled" from
# "user picked keep" (which returns None).
_CANCELLED: object = object()


def _prompt_generate_cases(cwd: Path, agent_name: str) -> tuple[int, str] | None | object:
    """Wizard step: ask whether to keep the existing dataset or
    generate fresh cases via LLM.

    Returns:

    * ``_CANCELLED`` (sentinel) on Ctrl-C / quit.
    * ``None`` if the operator chose to keep the existing dataset
      (or skip generation when no dataset exists). Caller continues
      with legacy gate / runs / baseline questions.
    * ``(count, mix)`` if the operator chose to generate fresh.
      Caller dispatches to the scorecard flow.

    ``agent_name`` may be the literal ``"all"`` — in that case we
    don't look up a single agent's dataset.jsonl (every agent has
    its own); just offer the generate/keep choice generically.

    Defaults:
    * No existing dataset / all mode → default action is "generate".
    * Existing dataset → default action is "keep" (regeneration would
      overwrite a possibly-curated file, so opt-in only).
    """
    is_all = agent_name == "all"
    if is_all:
        # In ``--all`` mode there's no single dataset to count; just
        # offer the two paths. Existing-dataset agents would be
        # scored against their files (legacy path); generate path
        # dispatches to ``scorecard --all`` with fresh cases per
        # agent. Default to generate — operators picking ``--all``
        # interactively are typically iterating on the project's
        # quality, not gating CI against a curated dataset.
        existing_count = 0
        console.print()
        console.print(
            "[bold]Test cases?[/bold]  "
            "[dim](generate fresh cases for every agent, or score "
            "each agent's existing dataset.jsonl)[/dim]"
        )
        choices = {
            "1": (
                "generate",
                "generate fresh cases via LLM for each agent — 10-category scorecard",
            ),
            "2": (
                "keep",
                "score each agent's existing dataset.jsonl — legacy scoring",
            ),
        }
        default = "1"
    else:
        dataset_path = cwd / "agents" / agent_name / "evals" / "dataset.jsonl"
        existing_count = _count_dataset_rows(dataset_path) if dataset_path.is_file() else 0

        console.print()
        if existing_count > 0:
            console.print(
                f"[bold]Test cases?[/bold]  "
                f"[dim](existing dataset has {existing_count} case(s) "
                f"at {dataset_path.relative_to(cwd)})[/dim]"
            )
            choices = {
                "1": ("keep", f"keep existing dataset ({existing_count} cases) — legacy scoring"),
                "2": ("generate", "generate fresh cases via LLM — 10-category scorecard"),
            }
            default = "1"
        else:
            console.print(
                "[bold]Test cases?[/bold]  [dim](no dataset found — recommend generating)[/dim]"
            )
            choices = {
                "1": ("generate", "generate fresh cases via LLM — 10-category scorecard"),
                "2": ("keep", "skip generation — legacy scoring against empty dataset"),
            }
            default = "1"

    for key, (_, label) in choices.items():
        console.print(f"  [bold cyan][{key}][/bold cyan] {label}")
    try:
        action_idx = Prompt.ask(
            "\n[bold]Pick[/bold]",
            choices=list(choices.keys()),
            default=default,
            show_choices=False,
        )
    except (KeyboardInterrupt, EOFError):
        return _CANCELLED

    if choices[action_idx][0] == "keep":
        return None

    return _ask_scorecard_count_and_mix()


def _count_dataset_rows(dataset_path: Path) -> int:
    """Number of non-blank lines in a JSONL dataset, or 0 on read error."""
    try:
        return sum(1 for line in dataset_path.read_text().splitlines() if line.strip())
    except OSError:
        return 0


def _ask_scorecard_count_and_mix() -> tuple[int, str] | object:
    """Drive the count + mix sub-prompts for the scorecard branch.

    Returns ``(count, mix)`` on success, ``_CANCELLED`` on Ctrl-C.
    No generation happens here — the scorecard itself does that
    exactly once when it runs (avoids the double-generation that
    would result from the wizard generating + the scorecard
    regenerating)."""
    # Sub-Q: count. ``c`` picks an arbitrary integer (1-100) so
    # operators iterating on a specific dataset size aren't pinned to
    # 5/10/25/50 — added 2026-05-19 after operator feedback that
    # "I want 8 cases" had no way to surface without the explicit
    # ``--count`` CLI flag.
    count_choices = {
        "1": (5, "5 cases — fast iteration / quick demo"),
        "2": (10, "10 cases — recommended for first pass"),
        "3": (25, "25 cases — tighter coverage, more tokens"),
        "4": (50, "50 cases — exhaustive sweep"),
        "c": (None, "custom — type a number (1-100)"),
    }
    console.print()
    console.print("[bold]How many cases?[/bold]")
    for key, (n, label) in count_choices.items():
        n_display = "" if n is None else f"{n}  "
        # Escape brackets so Rich renders ``[c]`` as literal text.
        # Without escaping, Rich's markup parser treats single-letter
        # tags like ``[c]`` as unrecognized style tags and silently
        # swallows them (numeric ``[1]`` survives because numerics
        # aren't style names); the operator then sees the row
        # indented with no key label, which broke the wizard's
        # custom-row UX on 2026-05-19.
        console.print(f"  [bold cyan]\\[{key}][/bold cyan] {n_display}[dim]{label}[/dim]")
    try:
        count_idx = Prompt.ask(
            "\n[bold]Pick[/bold]",
            choices=list(count_choices.keys()),
            default="2",
            show_choices=False,
        )
    except (KeyboardInterrupt, EOFError):
        return _CANCELLED
    if count_idx == "c":
        try:
            typed = IntPrompt.ask(
                "[bold]Number of cases[/bold] [dim](1-100)[/dim]",
                default=10,
                show_default=True,
            )
        except (KeyboardInterrupt, EOFError):
            return _CANCELLED
        # Clamp rather than re-prompt — operators who type 500 expecting
        # "more thorough" get a useful default rather than a loop.
        count = max(1, min(typed, 100))
    else:
        # Preset rows always carry an int; only the "c" row has None
        # (the custom-input sentinel handled above).
        preset = count_choices[count_idx][0]
        assert preset is not None
        count = preset

    # Sub-Q: mix.
    mix_choices = {
        "1": ("standard", "typical happy-path inputs"),
        "2": ("edge", "boundary / malformed / max-length inputs"),
        "3": ("adversarial", "red-team / prompt injection / jailbreak attempts"),
        "4": ("domain", "KB-aware — seeded from agent's contexts + knowledge files"),
    }
    console.print()
    console.print("[bold]Which mix?[/bold]")
    for key, (m, label) in mix_choices.items():
        console.print(f"  [bold cyan][{key}][/bold cyan] {m}  [dim]{label}[/dim]")
    try:
        mix_idx = Prompt.ask(
            "\n[bold]Pick[/bold]",
            choices=list(mix_choices.keys()),
            default="1",
            show_choices=False,
        )
    except (KeyboardInterrupt, EOFError):
        return _CANCELLED
    mix = mix_choices[mix_idx][0]

    return (count, mix)


async def _generate_cases_for_preview(
    bundle: Any, *, count: int, mix: str, mock: bool, project_root: Path
) -> list[dict[str, Any]]:
    """Thin async wrapper that calls the scorecard's generation
    primitive with the auto-detect path applied.

    The wizard's preview flow needs to run generation BEFORE the
    scorecard so the operator can see + approve the cases. Reuses
    ``_generate_entries`` from the scorecard's generator module so
    the same prompts, KB seeds, target dimensions, and provider
    auto-detect behavior apply — the wizard just sees the cases
    earlier."""
    from movate.cli.eval_gen_cmd import _generate_entries, _load_kb_seeds  # noqa: PLC0415
    from movate.cli.eval_scorecard_cmd import _resolve_generator_model  # noqa: PLC0415

    # KB seeds for domain mix (same logic as ``_run_scorecard``).
    kb_seeds: list[str] | None = None
    if mix == "domain":
        kb_seeds = _load_kb_seeds(bundle, project_root) or None

    # Auto-detect generator model so the wizard's preview uses the
    # same model the scorecard would have used. None means "let
    # _generate_entries fall back to the bundle's declared provider";
    # we surface the resolved model so the operator sees what's being
    # used in the spinner status line.
    resolved_model, _note = _resolve_generator_model(bundle.spec.model.provider, None)

    return await _generate_entries(
        bundle,
        num=count,
        sample_input=None,
        mock=mock,
        with_dimensions=False,
        mode=mix,
        kb_seeds=kb_seeds,
        generator_model=resolved_model,
    )


def _render_cases_preview_table(entries: list[dict[str, Any]], mix: str) -> None:
    """Render the generated test cases in a Rich table so the operator
    can sanity-check before paying for a full eval run.

    Columns: ``#``, ``Input``, ``Expected``. Values are rendered as
    scannable ``key: value`` lines with Rich color-coding (cyan keys,
    yellow scalars, dim placeholders for nested dicts) — way easier
    to read than the pre-2026-05-19 raw-JSON dump that was wrapped
    + truncated mid-token. Lists of dicts get a compact 1-line
    summary; long strings get truncated with ``…``.
    """
    from rich.table import Table  # noqa: PLC0415

    table = Table(
        title=f"[bold]Generated test cases[/bold] [dim]({len(entries)} x {mix})[/dim]",
        show_header=True,
        header_style="bold magenta",
        # Row dividers — multi-line cells (one line per top-level key)
        # are much easier to scan with explicit separators.
        show_lines=True,
    )
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Input", overflow="fold", max_width=60)
    table.add_column("Expected", overflow="fold", max_width=55)

    for i, entry in enumerate(entries, start=1):
        if _looks_like_rag_case(entry):
            # RAG cases (question + context chunks, citations indexing
            # back into context) get a source-resolved rendering: the
            # numeric citations become the actual cited passages so the
            # operator sees WHERE each expected answer is grounded.
            context = entry["input"]["context"]
            input_str = _format_rag_input_cell(entry["input"], context=context)
            expected_str = _format_rag_expected_cell(entry.get("expected") or {}, context=context)
        else:
            input_str = _format_for_preview_cell(entry.get("input"))
            expected_str = _format_for_preview_cell(entry.get("expected"))
        table.add_row(str(i), input_str, expected_str)

    console.print()
    console.print(table)


# Snippet budgets for the RAG-aware preview cells. Tuned so each line
# roughly fits the preview columns (Input max_width=60, Expected
# max_width=55) without Rich folding mid-passage too aggressively. The
# question is shown in full — operators asked to read the whole prompt.
_RAG_CONTEXT_SNIPPET_CHARS = 54
_RAG_CITATION_SNIPPET_CHARS = 48
_RAG_ANSWER_SNIPPET_CHARS = 180


def _looks_like_rag_case(entry: dict[str, Any]) -> bool:
    """Detect the canonical RAG-QA case shape.

    True when ``input`` carries a non-empty ``question`` string plus a
    ``context`` list of strings, and ``expected`` carries an ``answer``
    string plus a ``citations`` list. This is the only schema where the
    numeric ``citations`` index back into ``context`` (see the rag_qa
    template's output schema), so it's the only one where resolving
    citations → source passages is meaningful. Every other agent falls
    through to the generic ``_format_for_preview_cell`` renderer.
    """
    inp = entry.get("input")
    exp = entry.get("expected")
    if not isinstance(inp, dict) or not isinstance(exp, dict):
        return False
    question = inp.get("question")
    context = inp.get("context")
    if not isinstance(question, str) or not question.strip():
        return False
    if not isinstance(context, list) or not context:
        return False
    if not all(isinstance(c, str) for c in context):
        return False
    if not isinstance(exp.get("answer"), str):
        return False
    return isinstance(exp.get("citations"), list)


def _rag_snippet(text: str, max_chars: int) -> str:
    """Collapse whitespace and truncate ``text`` to ``max_chars`` with an
    ellipsis. Used for the per-passage source snippets in RAG previews."""
    flat = " ".join(text.split())
    if len(flat) > max_chars:
        return flat[: max_chars - 1] + "…"
    return flat


def _format_rag_input_cell(inp: dict[str, Any], *, context: list[str]) -> str:
    """Render a RAG case's Input cell: the full question plus a numbered
    list of context passages so the citation markers in the Expected
    cell line up with a visible source."""
    from rich.markup import escape  # noqa: PLC0415

    lines: list[str] = []
    question = str(inp.get("question", "")).strip()
    if question:
        lines.append("[bold]Question[/bold]")
        lines.append(escape(question))
    n = len(context)
    lines.append(f"[cyan]Context[/cyan] [dim]({n} source passage{'s' if n != 1 else ''})[/dim]")
    for idx, passage in enumerate(context, start=1):
        marker = escape(f"[{idx}]")
        snippet = escape(_rag_snippet(str(passage), _RAG_CONTEXT_SNIPPET_CHARS))
        lines.append(f"[dim]{marker}[/dim] {snippet}")
    return "\n".join(lines)


def _format_rag_expected_cell(exp: dict[str, Any], *, context: list[str]) -> str:
    """Render a RAG case's Expected cell with citations resolved to the
    actual cited passages plus human-friendly grounded/confidence lines.

    ``citations`` are 1-indexed into ``context``; each is resolved to a
    short snippet of the passage it points at. Out-of-range indices
    (LLM hallucinations) are flagged rather than silently dropped."""
    from rich.markup import escape  # noqa: PLC0415

    lines: list[str] = []
    answer = exp.get("answer")
    if isinstance(answer, str) and answer.strip():
        lines.append("[bold]Answer[/bold]")
        lines.append(escape(_rag_snippet(answer.strip(), _RAG_ANSWER_SNIPPET_CHARS)))

    citations = exp.get("citations")
    grounded = exp.get("grounded")
    cite_ints = [c for c in citations if isinstance(c, int)] if isinstance(citations, list) else []
    if cite_ints:
        markers = " ".join(f"[dim]{escape(f'[{c}]')}[/dim]" for c in cite_ints)
        lines.append(f"[green]Cited sources[/green] → {markers}")
        for c in cite_ints:
            marker = escape(f"[{c}]")
            if 1 <= c <= len(context):
                snippet = escape(_rag_snippet(str(context[c - 1]), _RAG_CITATION_SNIPPET_CHARS))
                lines.append(f"  [dim]{marker}[/dim] {snippet}")
            else:
                lines.append(f"  [dim]{marker}[/dim] [red](no such passage)[/red]")
    elif grounded is False:
        lines.append("[yellow]No citations[/yellow] [dim](not grounded)[/dim]")

    meta: list[str] = []
    if isinstance(grounded, bool):
        meta.append("[green]Grounded ✓[/green]" if grounded else "[red]Not grounded ✗[/red]")
    confidence = exp.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        meta.append(f"[yellow]Confidence {round(confidence * 100)}%[/yellow]")
    if meta:
        lines.append("  ".join(meta))

    return "\n".join(lines)


def _format_for_preview_cell(value: Any, *, max_lines: int = 6, max_value_chars: int = 60) -> str:
    """Render a generated case's input/expected value for the preview
    table.

    Instead of raw JSON, format as scannable ``key: value`` lines with
    Rich markup so the operator can read the structure at a glance.
    Top-level dict fields get one line each. Lists of dicts collapse
    into a 1-line summary (``list(N) first-vals…``). Strings get
    truncated to ``max_value_chars``.

    The output goes into a Rich Table cell, so Rich measures the
    rendered length correctly (markup tags don't count toward width).
    """
    if value is None:
        return ""
    if not isinstance(value, dict):
        return _format_value(value, max_chars=max_value_chars)

    lines: list[str] = []
    items = list(value.items())
    for key, val in items[:max_lines]:
        lines.append(f"[cyan]{key}[/cyan]: {_format_value(val, max_chars=max_value_chars)}")
    if len(items) > max_lines:
        remaining = len(items) - max_lines
        lines.append(f"[dim]… {remaining} more field(s)[/dim]")
    return "\n".join(lines)


# How many list items show as summary tokens before collapsing
# the remainder into "+N more"; tuned so a typical row fits one
# preview-table line without wrapping.
_LIST_SUMMARY_HEAD = 3
# Max token width inside a list summary — keeps long string items
# from overflowing the preview cell when 3 of them are joined.
_LIST_SUMMARY_TOKEN_MAX = 20


def _format_value(value: Any, *, max_chars: int) -> str:  # noqa: PLR0912 — type-dispatch on 6 kinds; flattening hurts readability
    """Render a single value (right-hand side of ``key: value``) with
    type-appropriate Rich styling. Used by ``_format_for_preview_cell``
    to keep the preview table scannable.

    Type rendering:
    - ``None`` → dim "null"
    - bool / int / float → yellow (scalars stand out)
    - str → truncated to ``max_chars`` with ``…``
    - list → compact ``list(N) val1, val2, +K`` summary
    - dict → dim ``{N field(s)}`` placeholder (deeper nesting hidden;
      operator can use ``-o json`` for the raw structure)
    """
    if value is None:
        return "[dim]null[/dim]"
    if isinstance(value, bool):
        return f"[yellow]{value}[/yellow]"
    if isinstance(value, (int, float)):
        return f"[yellow]{value}[/yellow]"
    if isinstance(value, str):
        if len(value) > max_chars:
            return value[: max_chars - 1] + "…"
        return value
    if isinstance(value, list):
        n = len(value)
        summaries: list[str] = []
        for item in value[:_LIST_SUMMARY_HEAD]:
            if isinstance(item, dict) and item:
                # Use the first field's value as a token for the
                # summary line — typically more informative than
                # "{2 fields}" for cases like
                # ``indicators: [{code: damaged_item}, ...]``.
                _, first_val = next(iter(item.items()))
                if isinstance(first_val, (dict, list)):
                    summaries.append("…")
                else:
                    token = str(first_val)
                    if len(token) > _LIST_SUMMARY_TOKEN_MAX:
                        token = token[: _LIST_SUMMARY_TOKEN_MAX - 1] + "…"
                    summaries.append(token)
            elif isinstance(item, (dict, list)):
                summaries.append("…")
            else:
                token = str(item)
                if len(token) > _LIST_SUMMARY_TOKEN_MAX:
                    token = token[: _LIST_SUMMARY_TOKEN_MAX - 1] + "…"
                summaries.append(token)
        more = f" +{n - _LIST_SUMMARY_HEAD}" if n > _LIST_SUMMARY_HEAD else ""
        joined = ", ".join(summaries) if summaries else ""
        return f"[dim]list({n})[/dim] {joined}{more}".rstrip()
    if isinstance(value, dict):
        n = len(value)
        return f"[dim]{{{n} field(s)}}[/dim]"
    s = str(value)
    if len(s) > max_chars:
        return s[: max_chars - 1] + "…"
    return s


def _truncate_json(value: Any, *, max_chars: int) -> str:
    """JSON-serialize a value, collapse whitespace, truncate to fit
    a table cell. Returns ``""`` for ``None``."""
    if value is None:
        return ""
    import json as _json  # noqa: PLC0415

    try:
        s = _json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(value)
    s = " ".join(s.split())  # collapse newlines + multiple spaces
    if len(s) > max_chars:
        return s[: max_chars - 1] + "…"
    return s


def _prompt_continue_or_regenerate() -> str:
    """After showing the preview table, ask the operator what to do.

    Returns one of ``"continue"`` / ``"regenerate"`` / ``"cancel"``.
    Operator hits Ctrl-C → ``"cancel"`` (caller exits cleanly)."""
    choices = {
        "1": ("continue", "score these cases against the rubric"),
        "2": ("regenerate", "regenerate (different cases via LLM)"),
        "3": ("cancel", "exit without scoring"),
    }
    console.print()
    console.print("[bold]Looks good?[/bold]")
    for key, (_, label) in choices.items():
        console.print(f"  [bold cyan][{key}][/bold cyan] {label}")
    try:
        idx = Prompt.ask(
            "\n[bold]Pick[/bold]",
            choices=list(choices.keys()),
            default="1",
            show_choices=False,
        )
    except (KeyboardInterrupt, EOFError):
        return "cancel"
    return choices[idx][0]


_REQUIRED_EVAL_PROVIDERS: tuple[str, ...] = ("openai", "anthropic")
"""Providers required for ``mdk eval`` to proceed.

The 10-category scorecard's LLM judge runs in a separate provider
family from the case generator + agent execution to defeat
same-family scoring bias (a model judging its own family-mate is a
known calibration failure mode). Cross-family enforcement needs at
least TWO different provider families with working keys — OpenAI
and Anthropic are the two we require because:

1. They cover the two dominant model families operators want to
   evaluate against in 2026.
2. They have the cheapest metadata endpoints for live verify
   (one HTTP roundtrip each, ~200ms).
3. Their pricing tables are wired into the scorecard's cost
   programmatic category.

A future toggle (``--allow-same-family``) could let operators with
only one key opt out of the cross-family rule, but the default
stays strict to protect the eval's judgement quality.
"""


def _require_llm_provider_key_or_offer_setup() -> None:  # noqa: PLR0912 — orchestrator; verify/auth/retry branches add linear count
    """Pre-flight check: BOTH OpenAI and Anthropic keys must be set
    AND verify against their respective metadata endpoints.

    Strictness lifted 2026-05-19 (was "at least one verified"): the
    one-key path silently degraded to same-family judging, which the
    LLM-as-judge cross-family enforcement was supposed to prevent.
    The scorecard's 10-category rubric needs a generator + judge in
    DIFFERENT provider families to avoid a model scoring its own
    family-mate (a known calibration failure mode).

    Behavior matrix (live-verify each via ``_provider_status``):

    * Both verified → proceed.
    * Any missing/rejected, all others verified → block with panel
      naming which key(s) need attention; offer ``mdk auth login``
      inline (TTY) or exit 2 with hint (non-TTY). Login loop runs
      up to 3 times so an operator who sets only ONE key in the
      first pass gets re-prompted for the missing one.
    * Unverifiable (network error) → warn + proceed; provider may
      be down + key might still work. Don't hard-block on outages.
    """
    import os  # noqa: PLC0415
    import sys  # noqa: PLC0415

    from movate.cli.auth import (  # noqa: PLC0415
        _provider_status,
        _verify_cache,
    )

    def _statuses() -> dict[str, str]:
        # Live-verify both providers (cached after the first call).
        return {p: _provider_status(p) for p in _REQUIRED_EVAL_PROVIDERS}

    def _all_verified(statuses: dict[str, str]) -> bool:
        return all(s == "verified" for s in statuses.values())

    statuses = _statuses()
    if _all_verified(statuses):
        return

    missing = [p for p, s in statuses.items() if s == "unset"]
    rejected = [p for p, s in statuses.items() if s == "rejected"]
    unverifiable = [p for p, s in statuses.items() if s == "unverifiable"]

    # Network-error path: don't block on a provider outage. Operator
    # may hit the same issue later, but blocking pre-flight on a
    # transient is wrong.
    if unverifiable and not missing and not rejected:
        unv_list = ", ".join(unverifiable)
        err_console.print(
            f"[yellow]⚠[/yellow] could not verify key(s) for "
            f"[bold]{unv_list}[/bold] (network error). Proceeding — if "
            f"eval fails with AuthError, your key may also be wrong."
        )
        return

    # At least one of {missing, rejected} is non-empty — render a
    # panel naming both required providers + each one's status.
    err_console.print()
    needs_lines: list[str] = []
    for provider in _REQUIRED_EVAL_PROVIDERS:
        state = statuses[provider]
        if state == "verified":
            needs_lines.append(f"  [green]✓[/green] [bold]{provider}[/bold] — verified")
        elif state == "unset":
            needs_lines.append(
                f"  [red]✗[/red] [bold]{provider}[/bold] — not configured "
                f"([dim]run [bold]mdk auth login {provider}[/bold][/dim])"
            )
        elif state == "rejected":
            needs_lines.append(
                f"  [red]✗[/red] [bold]{provider}[/bold] — key set but rejected "
                f"([dim]rotate via [bold]mdk auth login {provider}[/bold][/dim])"
            )
        else:  # unverifiable
            needs_lines.append(
                f"  [yellow]?[/yellow] [bold]{provider}[/bold] — could not verify "
                f"([dim]network error; will retry mid-eval[/dim])"
            )
    needs_block = "\n".join(needs_lines)
    err_console.print(
        Panel(
            "[bold yellow]⚠ Eval needs BOTH OpenAI and Anthropic keys verified[/bold yellow]\n\n"
            "[dim]The 10-category scorecard's LLM judge runs in a DIFFERENT "
            "provider family from the case generator to defeat same-family "
            "scoring bias. That cross-family rule needs both providers with "
            "working keys. Status:[/dim]\n\n"
            f"{needs_block}\n\n"
            "[dim]Fix the failing one(s) below, or pass [bold]--mock[/bold] "
            "for offline eval (no real LLM calls).[/dim]",
            border_style="yellow",
            title_align="left",
        )
    )

    if not sys.stdin.isatty():
        # Non-interactive context (CI / piped). No place to prompt.
        err_console.print()
        # Name the FIRST missing/rejected provider in the exit hint
        # — operators piping output need an actionable next step,
        # not a generic "set one up".
        target = (missing + rejected)[0] if (missing or rejected) else "anthropic"
        err_console.print(
            f"[red]✗[/red] not in a TTY; can't prompt for inline setup. "
            f"Run [bold]mdk auth login {target}[/bold] and retry, or "
            f"re-invoke with [bold]--mock[/bold]."
        )
        raise typer.Exit(code=2)

    err_console.print()
    try:
        answer = Prompt.ask(
            "[bold]Set up the missing provider(s) now?[/bold]",
            choices=["y", "n"],
            default="y",
            show_choices=False,
        )
    except (KeyboardInterrupt, EOFError):
        raise typer.Exit(code=2) from None

    if (answer or "").strip().lower() != "y":
        err_console.print(
            "[dim]→ skipped. Run [bold]mdk auth login[/bold] for each missing "
            "provider before retrying.[/dim]"
        )
        raise typer.Exit(code=2)

    # Launch ``mdk auth login`` inline. The picker shows ✓/✗ markers
    # per provider so the operator can see which ones still need work.
    from movate.cli.auth import login  # noqa: PLC0415
    from movate.credentials.store import CredentialsStore  # noqa: PLC0415

    # Loop the login call up to N times — the operator might set only
    # ONE provider on the first pass, and we still need the other.
    # Cap iterations to prevent an infinite loop if something goes
    # wrong with verify.
    max_login_loops = 3
    for _attempt in range(max_login_loops):
        before = CredentialsStore().read()
        try:
            login()
        except typer.Exit:
            # Operator cancelled the picker or verify failed.
            raise typer.Exit(code=2) from None

        # Inject newly-saved keys into ``os.environ`` so the rest of
        # this CLI invocation sees them (autoload already ran at
        # startup; without this refresh the in-flight wizard would
        # still see the pre-login state).
        after = CredentialsStore().read()
        for env_var, value in after.items():
            before_value = before.get(env_var, "")
            if value and value.strip() and value != before_value:
                os.environ[env_var] = value.strip()

        # Clear the verify cache so the re-check below probes the
        # FRESH key (cache still holds the pre-login "rejected" /
        # "unset" state otherwise).
        _verify_cache.clear()

        statuses = _statuses()
        if _all_verified(statuses):
            return

        # Still missing one — explain what's left and re-prompt.
        still_missing = [p for p, s in statuses.items() if s != "verified"]
        still_str = ", ".join(still_missing)
        err_console.print()
        err_console.print(
            f"[yellow]⚠[/yellow] still need: [bold]{still_str}[/bold]. "
            f"Launching auth picker again — pick the missing provider "
            f"this time, or [bold]Ctrl+C[/bold] to abort."
        )

    err_console.print(
        "[red]✗[/red] could not get both OpenAI + Anthropic verified after "
        f"{max_login_loops} login attempts. Aborting eval — run "
        "[bold]mdk auth status[/bold] to debug."
    )
    raise typer.Exit(code=2)


def _prompt_runs_per_case() -> int | None:
    """Wizard prompt: how many times to run each case (scores averaged).

    Multi-run averaging widens the score distribution. Without it (N=1)
    the judge's per-category score is a single roll — operators
    routinely see "everything 1.00 across all 10 cases" because each
    case is judged exactly once + the judge often gives binary-ish
    scores. With N=3+ each case's per-category mean reveals real
    variance (e.g. 0.67 means 2-of-3 runs scored 1.0 + one scored 0).

    Returns the chosen runs count, or ``None`` on Ctrl-C."""
    choices = {
        "1": (1, "1 run — fast iteration (no variance signal)"),
        "2": (3, "3 runs — recommended (averages out judge noise)"),
        "3": (5, "5 runs — tight CI (most tokens spent)"),
        "c": (None, "custom — type a number (1-10)"),
    }
    console.print()
    console.print(
        "[bold]Runs per case?[/bold] [dim](higher = wider score range; "
        "0/1.00 binary scores usually mean N=1)[/dim]"
    )
    for key, (value, label) in choices.items():
        value_display = "" if value is None else f"{value}  "
        # Escape ``[`` so Rich renders ``[c]`` as literal text (same
        # rationale as the count-prompt above — single-letter
        # tags get parsed as style names and silently dropped).
        console.print(f"  [bold cyan]\\[{key}][/bold cyan] {value_display}[dim]{label}[/dim]")
    try:
        idx = Prompt.ask(
            "\n[bold]Pick[/bold]",
            choices=list(choices.keys()),
            default="2",
            show_choices=False,
        )
    except (KeyboardInterrupt, EOFError):
        return None
    if idx == "c":
        try:
            n = IntPrompt.ask(
                "[bold]Runs per case[/bold] [dim](1-10)[/dim]",
                default=3,
                show_default=True,
            )
        except (KeyboardInterrupt, EOFError):
            return None
        # Clamp matches ``_run_scorecard``'s defensive range —
        # consistent behavior wizard ↔ CLI flag.
        return max(1, min(n, 10))
    return choices[idx][0]


def _prompt_scorecard_gate() -> float | None:
    """Wizard's scorecard-branch gate-threshold prompt.

    Same shape as the legacy branch's gate question, but expressed
    as the scorecard's ``--gate-overall`` flag (which gates on the
    overall composite, NOT per-category — operators using the
    scorecard typically want one knob, not 10). Returns the float
    threshold on success, ``None`` on Ctrl-C.
    """
    choices = {
        "1": (0.0, "no gate (just score; never fails)"),
        "2": (0.5, "loose (50%+ overall; permissive)"),
        "3": (0.7, "recommended (70%+ overall; CI default)"),
        "4": (0.9, "strict (90%+ overall; production-ready bar)"),
    }
    console.print()
    console.print("[bold]Gate threshold?[/bold] [dim](applied to overall composite)[/dim]")
    for key, (value, label) in choices.items():
        console.print(f"  [bold cyan][{key}][/bold cyan] {value}  [dim]{label}[/dim]")
    try:
        idx = Prompt.ask(
            "\n[bold]Pick[/bold]",
            choices=list(choices.keys()),
            default="3",
            show_choices=False,
        )
    except (KeyboardInterrupt, EOFError):
        return None
    return choices[idx][0]


class _GenLogCapture(logging.Handler):
    """Capture ``movate.cli.eval_gen_cmd`` log records during the
    wizard's preview-gen spinner block.

    The generator emits ``log.warning(...)`` for every per-case
    failure (schema validation, non-JSON response, AuthError,
    generator-call exception). Those records normally propagate to
    the root logger and print to stderr — which clobbers the
    spinner line and floods the operator's terminal with multi-line
    log records.

    This handler buffers the records so the spinner stays clean.
    ``_render_generation_summary`` then folds them into one
    summary line categorized by reason — "5 requested → 4 generated,
    1 skipped (schema_validation)" — much more readable than the
    raw multi-line WARNING dump.
    """

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _classify_gen_record(message: str) -> str:
    """Bucket a captured generator-log message into a stable category
    so the summary line aggregates by REASON, not by raw text.

    Categories (matched on substrings emitted by ``_generate_entries``
    / ``_generate_one_input`` in eval_gen_cmd.py):

    * ``schema_validation`` — generated input didn't match the
      agent's input schema (e.g. extra fields, wrong enum value).
    * ``non_json`` — model returned text that isn't parseable JSON.
    * ``non_dict`` — JSON parsed but wasn't a dict at the top level.
    * ``auth_error`` — provider rejected the key mid-generation.
    * ``generator_call`` — any other generator-call exception
      (network, timeout, content filter, etc.).
    * ``other`` — uncategorized; surfaces a raw count without
      mis-labeling the cause.
    """
    msg = message.lower()
    if "schema validation" in msg or "failed schema validation" in msg:
        return "schema_validation"
    if "non-json" in msg:
        return "non_json"
    if "non-dict" in msg:
        return "non_dict"
    if "autherror" in msg or "authentication" in msg:
        return "auth_error"
    if "generator call failed" in msg:
        return "generator_call"
    return "other"


def _render_generation_summary(
    *, requested: int, generated: int, captured: list[logging.LogRecord]
) -> None:
    """One-line clean summary after generation.

    Replaces the previous interleaved-with-spinner per-case warnings
    with a single greppable line operators can read at a glance:

        ↳ requested 5 → generated 4, skipped 1 (schema_validation)

    When everything succeeds it stays quiet (no false positives in
    the success path). The original raw warnings are NOT lost — they
    were captured by ``_GenLogCapture`` and can be surfaced via
    ``MDK_VERBOSE_GEN_WARNINGS=1`` for debugging (future toggle).
    """
    skipped = max(0, requested - generated)
    if skipped == 0 and not captured:
        # Clean success — keep the wizard tight, no extra noise.
        return

    # Aggregate by reason. Captured records may exceed the
    # ``skipped`` count if a single case triggered retry +
    # eventually failed (each attempt logs). Show the distinct
    # reasons; suppress duplicates from retry chains.
    reasons: dict[str, int] = {}
    for record in captured:
        reasons[_classify_gen_record(record.getMessage())] = (
            reasons.get(_classify_gen_record(record.getMessage()), 0) + 1
        )
    if reasons:
        # Sort by count desc so the dominant cause leads.
        reason_str = ", ".join(
            f"{cat}={n}" if n > 1 else cat
            for cat, n in sorted(reasons.items(), key=lambda kv: -kv[1])
        )
        detail = f" ([yellow]{reason_str}[/yellow])"
    else:
        detail = ""

    # Color the headline by ratio: all green if everything succeeded
    # (we returned early above), yellow if some skipped, red if all
    # failed (caught by the empty-entries branch).
    headline_color = "yellow" if generated > 0 else "red"
    console.print(
        f"  [dim]↳[/dim] [bold]requested[/bold] {requested} → "
        f"[bold]generated[/bold] [{headline_color}]{generated}[/{headline_color}], "
        f"[bold]skipped[/bold] {skipped}{detail}"
    )


def _generate_preview_and_gate(
    chosen_agent: str,
    *,
    count: int,
    mix: str,
    mock: bool,
    cwd: Path,
) -> tuple[list[dict[str, Any]], int, float] | None:
    """Wizard sub-flow: load bundle, loop on generate-and-preview
    until the operator says "continue", then ask runs-per-case +
    gate threshold.

    Returns ``(entries, runs_per_case, gate)`` on success, ``None``
    on cancel.

    The generation runs INSIDE the wizard (rather than waiting for
    the scorecard to do it later) so the operator sees what's about
    to be scored and can regenerate before paying for the full
    judge sweep. The returned ``entries`` are passed to the
    scorecard via ``pre_generated_entries`` so the scorecard
    doesn't re-generate.
    """
    from movate.core.loader import AgentLoadError, load_agent  # noqa: PLC0415

    agent_path = cwd / "agents" / chosen_agent
    try:
        bundle = load_agent(agent_path)
    except AgentLoadError as exc:
        err_console.print(f"[red]✗[/red] could not load agent {chosen_agent}: {exc}")
        return None

    while True:
        # Generate with a spinner so the operator sees something
        # happening during the 10-30s LLM call. Capture per-case
        # warnings from ``movate.cli.eval_gen_cmd`` (schema-validation
        # fails, non-JSON responses, generator-call errors) so they
        # DON'T interleave with the spinner line — instead they get
        # rolled up into the clean post-generation summary below.
        capture = _GenLogCapture()
        gen_logger = logging.getLogger("movate.cli.eval_gen_cmd")
        # Suppress propagation to the root logger (whose default
        # handler prints to stderr) for the duration of the spinner.
        original_propagate = gen_logger.propagate
        gen_logger.addHandler(capture)
        gen_logger.propagate = False
        try:
            with console.status(
                f"[bold cyan]Generating {count} {mix} cases for "
                f"[white]{chosen_agent}[/white]…[/bold cyan]",
                spinner="dots",
            ):
                try:
                    entries = asyncio.run(
                        _generate_cases_for_preview(
                            bundle, count=count, mix=mix, mock=mock, project_root=cwd
                        )
                    )
                except Exception as exc:
                    err_console.print(
                        f"[red]✗[/red] generation failed: {type(exc).__name__}: {exc}"
                    )
                    return None
        finally:
            gen_logger.removeHandler(capture)
            gen_logger.propagate = original_propagate

        # Render a clean summary line BEFORE the preview table —
        # operator sees at a glance "requested N → got M, K skipped:
        # schema_validation=2 non_json=1" instead of N raw warning
        # lines clobbering the spinner.
        _render_generation_summary(
            requested=count,
            generated=len(entries),
            captured=capture.records,
        )

        if not entries:
            err_console.print(
                f"[red]✗[/red] generator returned 0 cases for {chosen_agent}. "
                "Try [bold]mdk doctor[/bold] to verify provider keys, or "
                "[bold]--mock[/bold] for offline generation."
            )
            return None

        _render_cases_preview_table(entries, mix)
        choice = _prompt_continue_or_regenerate()
        if choice == "continue":
            break
        if choice == "cancel":
            return None
        # "regenerate" → loop, generate again.

    runs = _prompt_runs_per_case()
    if runs is None:
        return None
    gate = _prompt_scorecard_gate()
    if gate is None:
        return None
    return entries, runs, gate


def _eval_all_in_project(
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
    gate_retrieval_accuracy: float | None = None,
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

    def _run_one_agent(agent_dir: Path) -> None:
        """Run eval for a single agent dir; mutates ``rows`` / ``failed``."""
        nonlocal failed
        try:
            bundle = load_agent(agent_dir)
        except AgentLoadError as exc:
            rows.append((agent_dir.name, f"[red]✗ load failed[/red]: {str(exc)[:80]}"))
            failed += 1
            return

        # Run eval; capture pass/fail from the same async path that the
        # single-agent `mdk eval` uses. Re-raises typer.Exit on gate
        # failure — catch and record per-agent rather than aborting.
        # ``compact=True`` skips the per-agent verbose tables (head,
        # cases, dimensional, objectives, diff) — we only want the
        # one-line greppable summary per agent + the final rollup.
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
                    gate_retrieval_accuracy=gate_retrieval_accuracy,
                    compact=True,
                )
            )
            rows.append((agent_dir.name, "[green]✓ ok[/green]"))
        except typer.Exit as exc:
            if exc.exit_code == 0:
                rows.append((agent_dir.name, "[green]✓ ok[/green]"))
            else:
                rows.append((agent_dir.name, "[red]✗ gate failed[/red]"))
                failed += 1

    with progress_bar(
        description="[dim]Evaluating agents[/dim]",
        total=len(agent_dirs),
        transient=True,
    ) as advance:
        for agent_dir in agent_dirs:
            # Update description to show the agent currently being evaluated,
            # then run, then advance the counter. The compact eval output
            # (one greppable summary line per agent) goes to stdout so it
            # doesn't fight the progress bar on stderr.
            advance(0, suffix=f" [dim]({agent_dir.name})[/dim]")
            _run_one_agent(agent_dir)
            advance()

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

    # Previously rendered a Quick-run / Serve / Deploy / Skip picker
    # here as a "next steps" cue. Removed 2026-05-19: operators reported
    # the menu was noise after a green eval — they already know what
    # comes next (run, serve, deploy) and the extra prompt cluttered
    # the scrollback right when the agents-table was the most
    # interesting thing on screen. The greppable
    # ``mdk_eval_all_summary`` line above is the only post-eval
    # output now; CI scripts that scrape it are unaffected.


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
    gate_retrieval_accuracy: float | None = None,
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
                provider=rt.provider,
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
        gate_retrieval_accuracy=gate_retrieval_accuracy,
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
    gate_retrieval_accuracy: float | None = None,
    judge_override: JudgeConfig | None = None,
    compact: bool = False,
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
                executor=executor_for_eval,
                provider=rt.provider,
                runs_per_case=runs,
                gate_mode=gate_mode,
                objective_filter=objective,
                on_case_complete=on_case,
                judge_override=judge_override,
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
    elif compact:
        # Compact mode (used by ``mdk eval --all`` for per-agent runs).
        # The per-agent verbose tables (head + cases + dimensional +
        # objectives + diff) used to flood the terminal — N agents
        # produced N full tables before the rollup summary. Now each
        # per-agent run is silent at the table layer; only the
        # greppable summary line emits so CI grep + the orchestrator's
        # rollup row both still work.
        _print_eval_summary_line(
            summary,
            record=record,
            gate=effective_gate,
            cases_passing=cases_passing,
            overall_pass=overall_pass,
            diff=diff,
            regression_tolerance=regression_tolerance,
        )
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
        gate_retrieval_accuracy=gate_retrieval_accuracy,
    )

    # Exit codes: gate failure OR baseline regression OR dim gate all fail.
    failed_gate = not overall_pass
    failed_regression = diff is not None and diff.is_regression(tolerance=regression_tolerance)
    if failed_gate or failed_regression or failed_dim:
        raise typer.Exit(code=1)


async def _run_variant_comparison(
    primary_bundle: AgentBundle,
    variant_bundle: AgentBundle,
    *,
    gate: float,
    gate_mode: str,
    runs: int,
    mock: bool,
    objective: str | None,
    judge_override: JudgeConfig | None,
) -> None:
    """Run the dataset against the variant bundle and print a side-by-side table.

    Both primary and variant are run fresh here so the comparison is on equal
    footing (same runtime, same mock config). The primary eval already ran and
    its exit code is already settled — this function is purely informational.
    """
    summaries: list[EvalSummary] = []
    for label, bundle in (("primary", primary_bundle), ("variant", variant_bundle)):
        rt = await build_local_runtime(mock=mock)
        if mock:
            _configure_mock_for_bundle(rt.provider, bundle)
        try:
            engine = EvalEngine(
                executor=rt.executor,
                provider=rt.provider,
                runs_per_case=runs,
                gate_mode=gate_mode,
                objective_filter=objective,
                judge_override=judge_override,
            )
            try:
                s = await engine.run(bundle)
            except EvalConfigError as exc:
                err_console.print(f"[red]✗ {label} variant eval error:[/red] {exc}")
                return
        finally:
            await shutdown_runtime(rt.storage, rt.tracer)
        summaries.append(s)

    primary_summary, variant_summary = summaries
    _print_variant_comparison_table(primary_summary, variant_summary)


def _print_variant_comparison_table(
    primary: EvalSummary,
    variant: EvalSummary,
) -> None:
    """Print a side-by-side A/B score table comparing primary vs variant."""
    console.print()
    console.rule("[bold cyan]A/B Variant Comparison[/bold cyan]")

    # Determine which extra dimensions are present in BOTH summaries.
    dim_names: list[str] = []
    for dim_attr in (
        "accuracy",
        "faithfulness",
        "coverage",
        "latency",
        "context_compliance",
        "refusal",
        "retrieval_accuracy",
        "completeness",
        "tool_usage",
        "safety",
        "ux_tone",
        "task_success",
    ):
        p_val = getattr(primary.dimensional_means, dim_attr, None)
        v_val = getattr(variant.dimensional_means, dim_attr, None)
        if p_val is not None and v_val is not None:
            dim_names.append(dim_attr)

    table = Table(show_lines=False)
    table.add_column("Agent", style="bold cyan")
    table.add_column("Mean Score", justify="right")
    table.add_column("Cases", justify="right")
    table.add_column("Pass Rate", justify="right")
    for dim in dim_names:
        table.add_column(dim.replace("_", " ").title(), justify="right")

    def _fmt(v: float | None) -> str:
        return f"{v:.3f}" if v is not None else "—"

    for label, s in (("primary", primary), ("variant", variant)):
        row: list[str] = [
            f"{s.agent} ({label})",
            _fmt(s.mean_score),
            str(s.sample_count),
            _fmt(s.pass_rate),
        ]
        for dim in dim_names:
            row.append(_fmt(getattr(s.dimensional_means, dim, None)))
        table.add_row(*row)

    console.print(table)

    # Winner announcement.
    if primary.mean_score >= variant.mean_score:
        winner_label = f"{primary.agent} (primary)"
    else:
        winner_label = f"{variant.agent} (variant)"
    console.print(
        f"\n\U0001f3c6  {winner_label} wins with mean score "
        f"{max(primary.mean_score, variant.mean_score):.3f}"
    )
    console.print()


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
        # Panel mode: judge_provider is "a+b+c" — truncate for display
        jp = summary.judge_provider
        display_jp = jp if len(jp) <= 60 else jp[:57] + "…"  # noqa: PLR2004
        if "+" in jp:
            judges = jp.split("+")
            display_jp = f"{judges[0]} +{len(judges) - 1} more"
        head.add_row("judge.provider", display_jp)
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
        for v in (
            means.faithfulness,
            means.coverage,
            means.context_compliance,
            means.refusal,
            means.retrieval_accuracy,
        )
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
        ("retrieval_accuracy", means.retrieval_accuracy, "retrieved context relevant to question"),
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
    gate_retrieval_accuracy: float | None = None,
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
        ("retrieval_accuracy", gate_retrieval_accuracy, means.retrieval_accuracy, None),
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
                f"[red]✗[/red] dimensional gate failed: {dim_name} {actual:.3f} < {threshold:.2f}"
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
            "retrieval_accuracy": _round_or_none(summary.dimensional_means.retrieval_accuracy),
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
    for name in ("accuracy", "faithfulness", "coverage", "latency", "retrieval_accuracy"):
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
