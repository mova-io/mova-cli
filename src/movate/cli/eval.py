"""``movate eval <agent>`` — score an agent against its dataset and gate on a threshold."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._runtime import build_local_runtime, shutdown_runtime
from movate.core.eval import EvalConfigError, EvalEngine, EvalSummary
from movate.core.loader import AgentBundle, AgentLoadError, load_agent
from movate.core.reporters import render_eval_markdown

console = Console()
err_console = Console(stderr=True)


def eval_(
    path: Path = typer.Argument(..., help="Path to agent directory."),
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
    output_format: str = typer.Option("table", "--output", "-o", help="table | json | markdown"),
) -> None:
    """Run the eval suite for an agent and gate on a threshold.

    [bold]Examples:[/bold]

      [dim]# Exact-match scoring against the dataset[/dim]
      $ movate eval ./faq-agent --gate 0.7

      [dim]# LLM-as-judge with 3 runs per case to smooth variance[/dim]
      $ movate eval ./faq-agent --runs 3 --gate-mode mean

      [dim]# Hermetic CI run (no API keys needed)[/dim]
      $ movate eval ./faq-agent --mock
    """
    try:
        bundle = load_agent(path)
    except AgentLoadError as exc:
        err_console.print(f"[red]✗ load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    asyncio.run(
        _run_eval(
            bundle,
            gate=gate,
            gate_mode=gate_mode,
            runs=runs,
            mock=mock,
            output_format=output_format,
        )
    )
    # _run_eval handles output + exit code via raise; if we get here, all good.


async def _run_eval(
    bundle: AgentBundle,
    *,
    gate: float,
    gate_mode: str,
    runs: int,
    mock: bool,
    output_format: str = "table",
) -> None:
    rt = await build_local_runtime(mock=mock)
    try:
        engine = EvalEngine(
            executor=rt.executor,
            provider=rt.provider,
            runs_per_case=runs,
            gate_mode=gate_mode,
        )
        try:
            summary = await engine.run(bundle)
        except EvalConfigError as exc:
            err_console.print(f"[red]✗ eval config error:[/red] {exc}")
            raise typer.Exit(code=2) from None

        record = summary.to_record()
        await rt.storage.save_eval(record)
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)

    # Apply CLI gate (overrides judge.threshold for "case passes")
    cases_passing = sum(1 for c in summary.cases if c.aggregated_score >= gate)
    overall_pass = summary.sample_count > 0 and cases_passing == summary.sample_count

    if output_format == "json":
        _emit_json(summary, gate=gate, cases_passing=cases_passing, overall_pass=overall_pass)
    elif output_format == "markdown":
        # Markdown goes to stdout so it's pipe-friendly (e.g. `gh pr comment -F -`).
        print(render_eval_markdown(summary, gate=gate))
    else:
        _emit_table(summary, gate=gate, cases_passing=cases_passing, overall_pass=overall_pass)

    if not overall_pass:
        raise typer.Exit(code=1)


def _emit_table(
    summary: EvalSummary, *, gate: float, cases_passing: int, overall_pass: bool
) -> None:
    head = Table(
        title=f"{summary.agent} v{summary.agent_version} — eval results",
        show_header=False,
    )
    head.add_column("field", style="dim")
    head.add_column("value")
    head.add_row("judge", f"{summary.judge.method.value}")
    if summary.judge_provider:
        head.add_row("judge.provider", summary.judge_provider)
    head.add_row("dataset.hash", summary.dataset_hash[:12] + "…")
    head.add_row("runs/case", str(summary.runs_per_case))
    head.add_row("gate_mode", summary.gate_mode)
    head.add_row("gate", f"{gate:.2f}")
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


def _emit_json(
    summary: EvalSummary, *, gate: float, cases_passing: int, overall_pass: bool
) -> None:
    payload = {
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
        "cases": [
            {
                "input": c.case.input,
                "expected": c.case.expected,
                "score": round(c.aggregated_score, 6),
                "passed": c.passed,
                "scores_per_run": [round(r.score, 6) for r in c.runs],
                "rationales": [r.rationale for r in c.runs],
            }
            for c in summary.cases
        ],
    }
    print(json.dumps(payload, indent=2))


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"
