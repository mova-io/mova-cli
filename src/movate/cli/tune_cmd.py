"""``mdk tune <agent> <input>`` — deterministic knob sweep (Sprint Q).

Helps operators answer "should I lower temperature?" / "is the cheaper
model good enough?" / "what does max_tokens=128 do to the answer?"
without spinning up a real A/B framework.

[bold]Explicitly NOT auto-prompt-engineering.[/bold] Tune doesn't
rewrite prompts, doesn't iterate to convergence, doesn't make
recommendations. It runs the same agent with the same input, sweeps
ONE knob across a list of values, and prints the results side-by-side.
The operator reads the table and decides.

Usage::

  mdk tune triage '{"text": "..."}' --sweep temperature=0.0,0.5,1.0
  mdk tune triage '{"text": "..."}' --sweep max_tokens=128,512,1024
  mdk tune triage '{"text": "..."}' \\
    --sweep model=openai/gpt-4o-mini,anthropic/claude-haiku-4-5-20251001
  mdk tune triage '{"text": "..."}' --sweep temperature=0.0,1.0 --runs 3

Three sweep dimensions supported in MVP:

* ``temperature=X,Y,Z`` — model.params.temperature
* ``max_tokens=X,Y,Z`` — model.params.max_tokens
* ``model=A,B,C`` — model.provider (lets you compare across model families)

Multi-dim cross-products aren't supported (one ``--sweep`` per command).
Compare-across-prompts is ``mdk bench`` territory; tune is purpose-built
for ONE dimension at a time so the comparison table fits on a terminal
without scrolling.

Design rules:

* **Same input, same prompt, different knobs.** No prompt edits.
* **Deterministic ordering** of the sweep values (left-to-right in the
  flag, left-to-right in the output). Lets operators script downstream.
* **--runs N samples** each setting that many times so an operator can
  see variance (LLM-as-judge is noisy; one sample per knob is a trap).
* **No mutation of disk state.** Tune doesn't write the new RunRecord
  to storage by default — they're often noise that pollutes the run
  history. ``--persist`` opts in (so tune runs CAN show up in
  ``mdk costs report`` if the operator wants).
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._runtime import build_local_runtime, shutdown_runtime
from movate.core.loader import AgentBundle, AgentLoadError, load_agent
from movate.core.models import RunRequest, RunResponse

console = Console()
err_console = Console(stderr=True)


# Supported sweep keys. Adding a new one is a one-entry change in
# this map + maybe a coercion case in _coerce_sweep_value.
_SWEEP_KEYS = ("temperature", "max_tokens", "model")

# Minimum samples for stdev to be meaningful. With 1 sample stdev is
# undefined; we render the column as just the mean. Threshold lifted
# to a constant so the rendering logic stays free of magic numbers.
_MIN_SAMPLES_FOR_STDEV = 2


def _resolve_agent_dir(name_or_path: str, project_root: Path) -> Path:
    """Mirror the convention from `mdk inspect agent`."""
    candidate = Path(name_or_path)
    if candidate.is_dir() and (candidate / "agent.yaml").is_file():
        return candidate.resolve()
    by_name = project_root / "agents" / name_or_path
    if by_name.is_dir() and (by_name / "agent.yaml").is_file():
        return by_name.resolve()
    err_console.print(
        f"[red]✗[/red] agent not found: [bold]{name_or_path}[/bold]. "
        "[dim]Looked under [bold]agents/[/bold] and as a literal path.[/dim]"
    )
    raise typer.Exit(code=2)


def _parse_sweep(spec: str) -> tuple[str, list[Any]]:
    """Parse ``key=v1,v2,v3`` into (key, [coerced values]).

    Coercion is per-key — temperature + max_tokens go to numbers,
    model stays a string. Bad format / unknown key → operator error.
    """
    if "=" not in spec:
        err_console.print(f"[red]✗[/red] --sweep must be KEY=VAL1,VAL2,... — got {spec!r}")
        raise typer.Exit(code=2)
    key, _, raw = spec.partition("=")
    key = key.strip()
    if key not in _SWEEP_KEYS:
        err_console.print(
            f"[red]✗[/red] unknown sweep key {key!r}. [dim]Valid: {', '.join(_SWEEP_KEYS)}.[/dim]"
        )
        raise typer.Exit(code=2)
    values_raw = [v.strip() for v in raw.split(",") if v.strip()]
    if not values_raw:
        err_console.print(
            f"[red]✗[/red] --sweep {key} has no values. "
            "[dim]Example: [bold]--sweep temperature=0.0,0.5,1.0[/bold].[/dim]"
        )
        raise typer.Exit(code=2)
    return key, [_coerce_sweep_value(key, v) for v in values_raw]


def _coerce_sweep_value(key: str, raw: str) -> Any:
    """Convert a string sweep value to the type the model spec expects."""
    if key == "temperature":
        try:
            return float(raw)
        except ValueError as exc:
            raise typer.BadParameter(f"temperature must be a number; got {raw!r}") from exc
    if key == "max_tokens":
        try:
            return int(raw)
        except ValueError as exc:
            raise typer.BadParameter(f"max_tokens must be an integer; got {raw!r}") from exc
    return raw  # model: bare string


def _override_bundle(bundle: AgentBundle, key: str, value: Any) -> AgentBundle:
    """Return a bundle with ``key`` swept to ``value`` in its model block.

    Uses pydantic's ``model_copy`` to avoid mutating the original. We
    DO mutate the bundle (dataclass, not frozen) but rebuild the
    spec inside so the loader's downstream invariants stay intact.
    """
    spec = bundle.spec
    if key == "model":
        new_model = spec.model.model_copy(update={"provider": value})
    else:
        new_params = dict(spec.model.params)
        new_params[key] = value
        new_model = spec.model.model_copy(update={"params": new_params})
    new_spec = spec.model_copy(update={"model": new_model})
    # The dataclass is mutable; copy via dataclass field-by-field.
    return AgentBundle(
        spec=new_spec,
        agent_dir=bundle.agent_dir,
        prompt_template=bundle.prompt_template,
        prompt_hash=bundle.prompt_hash,
        input_schema=bundle.input_schema,
        output_schema=bundle.output_schema,
        input_validator=bundle.input_validator,
        output_validator=bundle.output_validator,
        skills=bundle.skills,
        contexts=bundle.contexts,
    )


# ---------------------------------------------------------------------------
# Sweep execution
# ---------------------------------------------------------------------------


async def _run_sweep(
    bundle: AgentBundle,
    payload: dict[str, Any],
    *,
    key: str,
    values: list[Any],
    runs: int,
    mock: bool,
    persist: bool,
    on_progress: Any | None = None,
) -> list[dict[str, Any]]:
    """Run the sweep. Returns one dict per (value, sample) pair.

    ``persist=False`` (default) means the executor still writes its
    runs to storage as part of the normal flow — we don't have a
    "don't persist" hook in the executor today. The flag is reserved
    for a future ``--no-persist`` once the executor exposes that.
    """
    total_calls = len(values) * runs
    rt = await build_local_runtime(mock=mock)
    results: list[dict[str, Any]] = []
    try:
        for value in values:
            modified = _override_bundle(bundle, key, value)
            for sample in range(runs):
                request = RunRequest(agent=modified.spec.name, input=payload)
                response: RunResponse = await rt.executor.execute(modified, request)
                results.append(
                    {
                        "value": value,
                        "sample": sample + 1,
                        "status": str(response.status),
                        # RunResponse uses `data` for the validated output dict.
                        "output": response.data,
                        "cost_usd": response.metrics.cost_usd,
                        "latency_ms": response.metrics.latency_ms,
                        "tokens_in": response.metrics.tokens.input,
                        "tokens_out": response.metrics.tokens.output,
                        "provider": response.metrics.provider,
                        "run_id": response.run_id,
                    }
                )
                if on_progress is not None:
                    on_progress(len(results), total_calls)
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)
    # Suppress unused-arg lint on persist — it's reserved for the future.
    _ = persist
    return results


def _aggregate(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Roll up per-(value, sample) results to one row per value.

    Stats included:
      - n samples
      - mean / stdev cost, latency
      - first output (preview)
    """
    by_value: dict[Any, list[dict[str, Any]]] = {}
    for r in results:
        by_value.setdefault(r["value"], []).append(r)

    rolled: list[dict[str, Any]] = []
    for value, samples in by_value.items():
        costs = [s["cost_usd"] for s in samples]
        latencies = [s["latency_ms"] for s in samples]
        first_output = samples[0]["output"]
        n = len(samples)
        rolled.append(
            {
                "value": value,
                "samples": n,
                "mean_cost_usd": statistics.fmean(costs) if costs else 0.0,
                "stdev_cost_usd": (statistics.stdev(costs) if n >= _MIN_SAMPLES_FOR_STDEV else 0.0),
                "mean_latency_ms": statistics.fmean(latencies) if latencies else 0.0,
                "stdev_latency_ms": (
                    statistics.stdev(latencies) if n >= _MIN_SAMPLES_FOR_STDEV else 0.0
                ),
                "first_output": first_output,
                "provider": samples[0]["provider"],
            }
        )
    return rolled


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int = 80) -> str:
    """Shorten long outputs for tabular display."""
    flat = text.replace("\n", " ").strip()
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _render_table(rolled: list[dict[str, Any]], *, key: str) -> None:
    """Render the sweep results as a Rich table.

    One row per swept value, with mean ± stdev for cost/latency and
    a truncated preview of the first output.
    """
    table = Table(title=f"Tune sweep — {key}", title_style="bold", show_lines=True)
    table.add_column(key, style="cyan", no_wrap=True)
    table.add_column("Samples", justify="right", style="dim", no_wrap=True)
    table.add_column("Cost ($)", justify="right", style="green", no_wrap=True)
    table.add_column("Latency (ms)", justify="right", style="dim", no_wrap=True)
    table.add_column("Output preview", style="dim")
    for row in rolled:
        cost_cell = f"{row['mean_cost_usd']:.6f}"
        if row["samples"] >= _MIN_SAMPLES_FOR_STDEV:
            cost_cell += f" ±{row['stdev_cost_usd']:.6f}"
        latency_cell = f"{row['mean_latency_ms']:.0f}"
        if row["samples"] >= _MIN_SAMPLES_FOR_STDEV:
            latency_cell += f" ±{row['stdev_latency_ms']:.0f}"
        first = row["first_output"] or {}
        preview = _truncate(json.dumps(first))
        table.add_row(
            str(row["value"]),
            str(row["samples"]),
            cost_cell,
            latency_cell,
            preview,
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def tune(
    agent_name: str = typer.Argument(
        ...,
        help=(
            "Agent name (resolved under [bold]agents/<name>[/bold]) or a "
            "literal path to an agent directory."
        ),
        metavar="AGENT",
    ),
    input_arg: str = typer.Argument(
        ...,
        help=(
            "Input as a JSON object, a path to a JSON file, or '-' for stdin. "
            "Same syntax [bold]mdk run[/bold] accepts."
        ),
        metavar="INPUT",
    ),
    sweep: str = typer.Option(
        ...,
        "--sweep",
        help=(
            f"What to sweep, [bold]KEY=V1,V2,V3[/bold] form. "
            f"Valid keys: {', '.join(_SWEEP_KEYS)}. "
            "Example: [dim]--sweep temperature=0.0,0.5,1.0[/dim]."
        ),
    ),
    runs: int = typer.Option(
        1,
        "--runs",
        help=(
            "Samples per swept value. Bump to 3+ to surface variance when "
            "the agent uses non-deterministic decoding. Default 1."
        ),
    ),
    mock: bool = typer.Option(
        False,
        "--mock",
        help="Use MockProvider — offline / hermetic / no API key needed.",
    ),
    persist: bool = typer.Option(
        False,
        "--persist",
        help=(
            "Persist the swept runs to storage. Off by default — sweep "
            "runs are usually noise that pollutes [bold]mdk costs report[/bold]. "
            "[dim]Reserved: today the executor always persists; this flag "
            "will become meaningful once the executor exposes a "
            "no-persist hook (Sprint S+).[/dim]"
        ),
    ),
    project_root: str = typer.Option(
        ".",
        "--project-root",
        envvar="MOVATE_PROJECT_ROOT",
        help="Project root (default: cwd). Used to resolve bare agent names.",
        hidden=True,
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit per-(value, sample) results as JSON. Skips the Rich rendering.",
    ),
) -> None:
    """Sweep one knob across a list of values and show the outputs.

    Same input + same prompt + different knobs. Helps you answer "what
    happens when I [bold]lower temperature[/bold]?" without standing up
    a real A/B framework. [bold]Not[/bold] auto-prompt-engineering —
    no prompt edits, no iteration, no recommendations. You read the
    comparison table and decide.

    [bold]Examples:[/bold]

      [dim]$ mdk tune triage '{"text": "x"}' --sweep temperature=0.0,0.5,1.0[/dim]
      [dim]$ mdk tune triage '{"text": "x"}' --sweep max_tokens=128,512 --runs 3[/dim]
      [dim]$ mdk tune triage '{"text": "x"}' \\[/dim]
      [dim]    --sweep model=openai/gpt-4o-mini,anthropic/claude-haiku-4-5-20251001 --mock[/dim]
    """
    root = Path(project_root).resolve()
    agent_dir = _resolve_agent_dir(agent_name, root)
    try:
        bundle = load_agent(agent_dir)
    except AgentLoadError as exc:
        err_console.print(f"[red]✗ load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    payload = _coerce_input(input_arg)
    key, values = _parse_sweep(sweep)

    if runs < 1:
        err_console.print(f"[red]✗[/red] --runs must be ≥ 1; got {runs}")
        raise typer.Exit(code=2)

    # Upfront sweep-space preview so the operator knows what's coming.
    total_calls = len(values) * runs
    err_console.print(
        f"[dim]Sweep: [bold]{key}[/bold] across {len(values)} value(s) x {runs} run(s) "
        f"= [bold]{total_calls}[/bold] LLM call{'s' if total_calls != 1 else ''}."
        + (" [yellow]Use --mock for a free offline run.[/yellow]" if not mock else "")
        + "[/dim]"
    )

    from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn  # noqa: PLC0415

    total_calls = len(values) * runs
    _show_bar = sys.stderr.isatty() and not mock
    if _show_bar:
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as _prog:
            _sweep_task = _prog.add_task(
                f"Sweeping [bold]{key}[/bold]",
                total=total_calls,
            )

            def _on_progress(cur: int, _tot: int) -> None:
                _prog.update(_sweep_task, completed=cur)

            results = asyncio.run(
                _run_sweep(
                    bundle,
                    payload,
                    key=key,
                    values=values,
                    runs=runs,
                    mock=mock,
                    persist=persist,
                    on_progress=_on_progress,
                )
            )
    else:
        results = asyncio.run(
            _run_sweep(
                bundle,
                payload,
                key=key,
                values=values,
                runs=runs,
                mock=mock,
                persist=persist,
            )
        )

    if json_output:
        # Per-sample raw results — easiest to pipe to jq / pandas.
        console.print_json(json.dumps(results, default=str))
        return

    rolled = _aggregate(results)
    _render_table(rolled, key=key)
    if runs == 1:
        console.print(
            "\n[dim]Hint:[/dim] [bold]--runs 3[/bold] gives variance bands "
            "(stdev ± mean). Critical when [bold]temperature > 0[/bold] — "
            "one sample lies about reproducibility."
        )


def _coerce_input(arg: str) -> dict[str, Any]:
    """JSON object / path / stdin. Subset of `mdk run`'s coercion."""
    if arg == "-":
        return json.loads(sys.stdin.read())
    p = Path(arg)
    if p.is_file():
        return json.loads(p.read_text())
    try:
        parsed = json.loads(arg)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"input is not valid JSON: {exc}") from exc
    raise typer.BadParameter("input must be a JSON object (received a non-object scalar).")
