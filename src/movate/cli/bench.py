"""``movate bench <agent>`` — compare an agent across multiple models.

Reads default model + judge lists from ``movate.yaml: bench`` if present;
override via repeated ``--model`` / ``--judge``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._output import Report
from movate.cli._progress import progress_bar
from movate.cli._runtime import build_local_runtime, shutdown_runtime
from movate.core.bench import BenchEngine, BenchSummary, ModelBenchResult
from movate.core.config import load_project_config
from movate.core.eval import EvalConfigError, load_judge_config
from movate.core.loader import AgentBundle, AgentLoadError, load_agent
from movate.core.models import JudgeConfig, JudgeMethod, ModelConfig
from movate.core.reporters import render_bench_markdown

console = Console()
err_console = Console(stderr=True)


def bench(
    path: Path = typer.Argument(..., help="Path to agent directory."),
    input_arg: str = typer.Argument(
        None,
        metavar="INPUT",
        help=(
            "Input: a plain string (auto-wraps to the agent's single required string field), "
            "JSON object, file path, or '-' for stdin."
        ),
    ),
    input_flag: str = typer.Option(
        None, "--input", "-i", help="Alternative way to pass input (preferred for explicit JSON)."
    ),
    models: list[str] = typer.Option(
        None,
        "--model",
        "-m",
        help="Provider to test. Repeatable. Defaults to bench.models from movate.yaml.",
    ),
    judge: str = typer.Option(
        None,
        "--judge",
        "-j",
        help="Judge provider for quality scoring. Defaults to bench.judges[0] from movate.yaml.",
    ),
    rubric: str = typer.Option(
        None,
        "--rubric",
        help="Inline scoring rubric. Required for LLM-as-judge if judge.yaml has none.",
    ),
    rubric_file: Path = typer.Option(
        None, "--rubric-file", help="Path to a rubric file (overrides --rubric)."
    ),
    runs: int = typer.Option(
        1, "--runs", "-r", help="Runs per model. Use 3+ to smooth latency/cost variance."
    ),
    gate_mode: str = typer.Option(
        "mean", "--gate-mode", help="Score aggregation across N runs: mean | min | p10."
    ),
    mock: bool = typer.Option(
        False, "--mock", help="Use the deterministic MockProvider (no API keys)."
    ),
    output_format: Report = typer.Option(Report.TABLE, "--output", "-o", case_sensitive=False),
) -> None:
    """Benchmark an agent across multiple models (cost / latency / quality).

    [bold]Examples:[/bold]

      [dim]# Use bench.models from movate.yaml + agent's evals/judge.yaml[/dim]
      $ movate bench ./faq-agent "What is movate?"

      [dim]# Two specific models, no judge — just cost+latency[/dim]
      $ movate bench ./faq-agent "hi" \\
            -m openai/gpt-4o-mini-2024-07-18 \\
            -m anthropic/claude-haiku-4-5-20251001

      [dim]# Inline rubric for freeform LLM-as-judge[/dim]
      $ movate bench ./faq-agent --runs 3 --rubric "Is the answer concise and correct?"
    """
    try:
        bundle = load_agent(path)
    except AgentLoadError as exc:
        err_console.print(f"[red]✗ load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    raw = input_flag or input_arg
    if raw is None:
        err_console.print("[red]✗ provide input as a positional arg or via --input[/red]")
        raise typer.Exit(code=2)
    payload = _coerce_input(raw, bundle)

    project = load_project_config()
    providers = list(models) if models else list(project.bench.models)
    if not providers:
        err_console.print(
            "[red]✗ no models specified — pass --model or set bench.models in movate.yaml[/red]"
        )
        raise typer.Exit(code=2)

    judge_cfg = _resolve_judge(
        bundle,
        judge,
        rubric,
        rubric_file,
        project_default_judge=_first_or_none(project.bench.judges),
    )

    asyncio.run(
        _run_bench(
            bundle,
            payload=payload,
            providers=providers,
            judge=judge_cfg,
            rubric=_load_rubric(rubric, rubric_file),
            runs=runs,
            gate_mode=gate_mode,
            mock=mock,
            output_format=output_format,
        )
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _first_or_none(items: list[str]) -> str | None:
    return items[0] if items else None


def _load_rubric(inline: str | None, rubric_file: Path | None) -> str | None:
    if rubric_file is not None:
        return rubric_file.read_text().strip()
    return inline


def _resolve_judge(
    bundle: AgentBundle,
    judge_flag: str | None,
    rubric: str | None,
    rubric_file: Path | None,
    *,
    project_default_judge: str | None,
) -> JudgeConfig | None:
    """Decide whether to score, and with which judge.

    Resolution:
      1. If --judge is given AND a rubric is supplied → llm_judge with that provider
      2. Else if agent has evals/judge.yaml → use it
      3. Else if movate.yaml: bench.judges[0] is set AND a rubric is supplied → llm_judge
      4. Else → no scoring (cost+latency only)
    """
    inline_rubric = _load_rubric(rubric, rubric_file)

    if judge_flag and inline_rubric:
        return JudgeConfig(
            method=JudgeMethod.LLM_JUDGE,
            model=ModelConfig(provider=judge_flag),
            rubric=inline_rubric,
        )

    judge_from_agent = load_judge_config(bundle)
    if judge_from_agent.method == JudgeMethod.LLM_JUDGE:
        return judge_from_agent

    if project_default_judge and inline_rubric:
        return JudgeConfig(
            method=JudgeMethod.LLM_JUDGE,
            model=ModelConfig(provider=project_default_judge),
            rubric=inline_rubric,
        )

    return None  # no scoring


def _coerce_input(arg: str, bundle: AgentBundle) -> dict[str, Any]:
    """Same coercion rules as ``movate run``."""
    if arg == "-":
        return _ensure_dict(json.loads(sys.stdin.read()))
    p = Path(arg)
    if p.is_file():
        return _ensure_dict(json.loads(p.read_text()))
    try:
        parsed = json.loads(arg)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    schema = bundle.input_schema
    required = list(schema.get("required", []))
    properties = schema.get("properties", {}) or {}
    string_required = [n for n in required if properties.get(n, {}).get("type") == "string"]
    if len(string_required) == 1 and len(required) == 1:
        return {string_required[0]: arg}

    raise typer.BadParameter(
        f"input is not valid JSON and cannot be auto-wrapped — agent "
        f"{bundle.spec.name!r} requires {required}."
    )


def _ensure_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise typer.BadParameter(f"input must be a JSON object, got {type(value).__name__}")
    return value


# ---------------------------------------------------------------------------
# Runner + output
# ---------------------------------------------------------------------------


async def _run_bench(
    bundle: AgentBundle,
    *,
    payload: dict[str, Any],
    providers: list[str],
    judge: JudgeConfig | None,
    rubric: str | None,
    runs: int,
    gate_mode: str,
    mock: bool,
    output_format: Report,
) -> None:
    rt = await build_local_runtime(mock=mock)
    show_progress = output_format == Report.TABLE and not mock
    try:
        try:
            with _maybe_bench_progress(show_progress, total=len(providers)) as on_model:
                engine = BenchEngine(
                    executor=rt.executor,
                    provider=rt.provider,
                    runs_per_model=runs,
                    gate_mode=gate_mode,
                    judge=judge,
                    rubric=rubric,
                    on_model_complete=on_model,
                )
                summary = await engine.run(bundle, input_payload=payload, providers=providers)
        except EvalConfigError as exc:
            err_console.print(f"[red]✗ bench config error:[/red] {exc}")
            raise typer.Exit(code=2) from None
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)

    if output_format == Report.JSON:
        _emit_json(summary)
    elif output_format == Report.MARKDOWN:
        print(render_bench_markdown(summary))
    else:
        _emit_table(summary)


def _emit_table(summary: BenchSummary) -> None:
    head = Table(
        title=f"{summary.agent} v{summary.agent_version} — bench results",
        show_header=False,
    )
    head.add_column("field", style="dim")
    head.add_column("value")
    head.add_row("input", _truncate(json.dumps(summary.input), 80))
    head.add_row("runs/model", str(summary.runs_per_model))
    if summary.judge_provider:
        head.add_row("judge", summary.judge_provider)
    head.add_row("gate_mode", summary.gate_mode)
    console.print(head)

    has_score = any(m.aggregated_score(summary.gate_mode) is not None for m in summary.models)

    cols = ["model", "cost/run", "p50 ms", "p95 ms"]
    if has_score:
        cols.append("score")
    cols.extend(["errors", "sample"])
    table = Table(title="Models", show_header=True, header_style="bold")
    for col in cols:
        table.add_column(col, overflow="fold")

    for m in summary.models:
        row: list[str] = [
            m.provider,
            f"${m.cost_mean_usd:.6f}",
            str(m.latency_p50_ms),
            str(m.latency_p95_ms),
        ]
        if has_score:
            score = m.aggregated_score(summary.gate_mode)
            if score is None:
                cell = "[dim]skipped[/dim]" if m.skipped_score else "[dim]—[/dim]"
            else:
                cell = f"{score:.2f}"
            row.append(cell)
        row.append(str(m.error_count))
        row.append(_truncate(json.dumps(m.sample_output) if m.sample_output else "—", 50))
        table.add_row(*row)

    console.print(table)

    skipped = [m for m in summary.models if m.skipped_score]
    if skipped:
        err_console.print(
            f"[yellow]note:[/yellow] judge skipped for "
            f"{', '.join(m.provider for m in skipped)} (same family as judge)."
        )


def _emit_json(summary: BenchSummary) -> None:
    payload = {
        "agent": summary.agent,
        "agent_version": summary.agent_version,
        "input": summary.input,
        "judge_provider": summary.judge_provider,
        "runs_per_model": summary.runs_per_model,
        "gate_mode": summary.gate_mode,
        "models": [_model_to_json(m, summary.gate_mode) for m in summary.models],
    }
    print(json.dumps(payload, indent=2))


def _model_to_json(m: ModelBenchResult, gate_mode: str) -> dict[str, Any]:
    score = m.aggregated_score(gate_mode)
    return {
        "provider": m.provider,
        "cost_mean_usd": m.cost_mean_usd,
        "cost_total_usd": m.cost_total_usd,
        "latency_p50_ms": m.latency_p50_ms,
        "latency_p95_ms": m.latency_p95_ms,
        "score": round(score, 6) if score is not None else None,
        "judge_skipped": m.skipped_score,
        "error_count": m.error_count,
        "sample_output": m.sample_output,
    }


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


@contextmanager
def _maybe_bench_progress(
    enabled: bool, *, total: int
) -> Iterator[Callable[[int, int, ModelBenchResult], None] | None]:
    """Yield a callback for ``BenchEngine.on_model_complete``.

    Suppressed when not rendering for humans (json/markdown) or in
    mock mode (where the per-model loop is fast enough that a bar is
    just noise).
    """
    if not enabled:
        yield None
        return

    with progress_bar(description="models", total=total) as advance:

        def on_model(done: int, total_in_cb: int, result: ModelBenchResult) -> None:
            _ = (done, total_in_cb)
            # Append the just-finished model name so the bar shows
            # progress + which model was last evaluated.
            advance(suffix=f" — {result.provider}")

        yield on_model
