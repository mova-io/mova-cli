"""``mdk eval harvest <agent>`` — harvest prod runs into proposed eval cases.

ADR 016 Decision D1, CLI side. Selects this project's local runs for an agent
by feedback/sample signal and turns them into **proposed** eval-dataset cases.

The dominant safety property is **proposed-not-applied**: by default a harvest
writes the proposals to a review file (``evals/harvested.jsonl``) or prints
them — it NEVER touches the live ``evals/dataset.jsonl``. An explicit
``--accept`` is required to append the reviewed cases to the live dataset. This
human gate is what prevents feedback-poisoning (noisy / adversarial
thumbs-down silently corrupting the test set).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console

from movate.cli._completion import complete_agent_path
from movate.cli._output import Report
from movate.core.harvest import HarvestResult

console = Console()
err_console = Console(stderr=True)


def harvest(
    agent: str = typer.Argument(
        ...,
        help=(
            "Agent directory OR bare name (resolved to ./agents/<name> inside "
            "a project). Its [bold]evals/[/bold] folder is where the review "
            "file is written and where the live dataset.jsonl lives."
        ),
        shell_complete=complete_agent_path,
    ),
    source: str = typer.Option(
        "thumbs-down",
        "--source",
        help=(
            "Which prod signal selects candidate runs: "
            "[bold]thumbs-down[/bold] (cases to fix) | "
            "[bold]thumbs-up[/bold] (golden cases) | "
            "[bold]low-score[/bold] | [bold]sample[/bold] (signal-agnostic)."
        ),
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        "-n",
        min=0,
        help="Maximum number of proposed cases to harvest.",
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help=(
            "ISO-8601 timestamp; only consider runs/feedback at or after this "
            "instant (e.g. 2026-05-01). Omit for no cutoff."
        ),
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help=(
            "Write the proposed cases (JSONL) here. Defaults to "
            "[bold]<agent>/evals/harvested.jsonl[/bold]. Use [bold]-[/bold] for "
            "stdout. Ignored when [bold]--accept[/bold] is set."
        ),
    ),
    accept: bool = typer.Option(
        False,
        "--accept",
        help=(
            "APPEND the proposed cases to the live "
            "[bold]<agent>/evals/dataset.jsonl[/bold]. This is the explicit "
            "human-gate step — without it, harvesting only proposes and never "
            "mutates the dataset."
        ),
    ),
    output_format: Report = typer.Option(
        Report.TABLE, "--format", case_sensitive=False, help="Summary output format."
    ),
) -> None:
    """Harvest prod runs into [bold]proposed[/bold] eval cases (ADR 016 D1).

    [bold]Examples:[/bold]

      [dim]# Propose cases from thumbs-down runs → evals/harvested.jsonl[/dim]
      $ mdk eval harvest rag-qa

      [dim]# Golden cases from thumbs-up runs, to stdout[/dim]
      $ mdk eval harvest rag-qa --source thumbs-up -o -

      [dim]# Review, then explicitly append to the live dataset[/dim]
      $ mdk eval harvest rag-qa --source low-score --accept

    By default this NEVER modifies evals/dataset.jsonl — it writes a review
    file. Pass [bold]--accept[/bold] to append the proposals to the live
    dataset (the deliberate human-review gate).
    """
    from movate.cli._resolve import resolve_agent_or_workflow_arg  # noqa: PLC0415
    from movate.core.harvest import resolve_source  # noqa: PLC0415

    resolved = resolve_agent_or_workflow_arg(agent)
    agent_dir = Path(resolved)
    if not (agent_dir / "agent.yaml").is_file():
        err_console.print(
            f"[red]✗[/red] no agent.yaml at [bold]{agent_dir}[/bold] — "
            f"pass an agent directory or a bare name inside a project."
        )
        raise typer.Exit(code=2)

    try:
        harvest_source = resolve_source(source)
    except ValueError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    since_dt: datetime | None = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            err_console.print(
                f"[red]✗[/red] invalid --since {since!r}; expected ISO-8601 "
                f"(e.g. 2026-05-01 or 2026-05-01T00:00:00Z)."
            )
            raise typer.Exit(code=2) from None

    # Read the agent's name from agent.yaml — runs are keyed by agent name,
    # not directory name (they can differ).
    from movate.core.loader import load_agent  # noqa: PLC0415

    try:
        bundle = load_agent(agent_dir)
    except Exception as exc:  # AgentLoadError + any parse error
        err_console.print(f"[red]✗ load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None
    agent_name = bundle.spec.name

    result = asyncio.run(
        _run_harvest(
            agent_name=agent_name,
            source=harvest_source.value,
            limit=limit,
            since=since_dt,
        )
    )

    rows = result.to_rows()

    if accept:
        _append_to_dataset(agent_dir, rows)
        _print_summary(result, output_format, accepted_to=agent_dir / "evals" / "dataset.jsonl")
        return

    # Proposed-not-applied: write the review file (or stdout), then tell the
    # operator how to accept. NEVER touch dataset.jsonl here.
    if output is not None and str(output) == "-":
        for row in rows:
            console.print_json(json.dumps(row))
        _print_summary(result, output_format, wrote_to=None)
        return

    review_path = output if output is not None else (agent_dir / "evals" / "harvested.jsonl")
    review_path.parent.mkdir(parents=True, exist_ok=True)
    with review_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    _print_summary(result, output_format, wrote_to=review_path)


async def _run_harvest(
    *, agent_name: str, source: str, limit: int, since: datetime | None
) -> HarvestResult:
    """Build the local runtime, harvest, and tear down cleanly."""
    from movate.cli._runtime import build_local_runtime, shutdown_runtime  # noqa: PLC0415
    from movate.core.harvest import HarvestSource, harvest_runs  # noqa: PLC0415

    runtime = await build_local_runtime(mock=True)
    try:
        return await harvest_runs(
            runtime.storage,
            agent=agent_name,
            # Local CLI runs are persisted under the "local" tenant (see
            # build_local_runtime's Executor tenant_id) — scope to it.
            tenant_id="local",
            source=HarvestSource(source),
            limit=limit,
            since=since,
        )
    finally:
        await shutdown_runtime(runtime.storage, runtime.tracer)


def _append_to_dataset(agent_dir: Path, rows: list[dict[str, object]]) -> None:
    """Append harvested rows to the live ``evals/dataset.jsonl`` (the explicit
    accept step). Creates the file/folder if absent; appends otherwise so an
    existing hand-authored dataset is preserved."""
    dataset_path = agent_dir / "evals" / "dataset.jsonl"
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    # Ensure we start on a fresh line if the existing file lacks a trailing
    # newline, so we never glue a new row onto an old one.
    needs_leading_newline = (
        dataset_path.exists()
        and dataset_path.stat().st_size > 0
        and not dataset_path.read_bytes().endswith(b"\n")
    )
    with dataset_path.open("a", encoding="utf-8") as fh:
        if needs_leading_newline:
            fh.write("\n")
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _print_summary(
    result: HarvestResult,
    output_format: Report,
    *,
    wrote_to: Path | None = None,
    accepted_to: Path | None = None,
) -> None:
    """Print the harvest summary in the requested format."""
    if output_format == Report.JSON:
        console.print_json(
            json.dumps(
                {
                    "agent": result.agent,
                    "source": result.source.value,
                    "proposed_count": result.proposed_count,
                    "needs_review_count": result.needs_review_count,
                    "golden_count": result.golden_count,
                    "runs_considered": result.runs_considered,
                    "applied": accepted_to is not None,
                    "wrote_to": str(wrote_to) if wrote_to else None,
                }
            )
        )
        return

    n = result.proposed_count
    if n == 0:
        console.print(
            f"[yellow]No runs matched[/yellow] source "
            f"[bold]{result.source.value}[/bold] for agent "
            f"[bold]{result.agent}[/bold] "
            f"(considered {result.runs_considered})."
        )
        return

    if accepted_to is not None:
        console.print(
            f"[green]✓[/green] Appended [bold]{n}[/bold] harvested case(s) to "
            f"[bold]{accepted_to}[/bold] "
            f"({result.needs_review_count} need a reviewer to supply expected)."
        )
        return

    where = "stdout" if wrote_to is None else f"[bold]{wrote_to}[/bold]"
    console.print(
        f"[green]✓[/green] [bold]{n}[/bold] proposed case(s) "
        f"({result.golden_count} golden, {result.needs_review_count} need-review) "
        f"written to {where}."
    )
    console.print(
        "[dim]Review, then append to evals/dataset.jsonl — "
        "re-run with [bold]--accept[/bold] to append automatically. "
        "Nothing was added to the live dataset.[/dim]"
    )


__all__ = ["harvest"]
