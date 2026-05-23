"""``mdk explain <run-id>`` — decision chain visualization for a completed run.

Renders the reasoning chain behind a run: input, each LLM call's metrics
(tokens, latency, cost), output, and any error.  With ``--steps``, also
renders the per-skill-call breakdown captured by the executor's tool-use
loop — no Langfuse backend required.

When a Langfuse tracer IS configured (``MOVATE_TRACER=langfuse``), the
richer span tree is available via ``mdk trace`` instead.

Exit codes:
    0 — record found and rendered.
    1 — run not found (unknown id / empty storage).
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from movate.cli._runtime import build_storage
from movate.core.explain import explain_run
from movate.core.models import JobStatus, RunRecord, SkillCallRecord

console = Console()
err = Console(stderr=True)

_STEP_TRACER_HINT = (
    "For richer span-level traces configure: "
    "[bold]MOVATE_TRACER=langfuse[/bold]  "
    "(then use [bold]mdk trace[/bold])"
)


# ---------------------------------------------------------------------------
# Public command
# ---------------------------------------------------------------------------


def explain(
    run_id: Annotated[
        str | None,
        typer.Argument(help="Run ID to explain.  Omit with --last to explain the most-recent run."),
    ] = None,
    last: Annotated[
        bool,
        typer.Option("--last", help="Explain the most-recent run (ignores RUN_ID if both given)."),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON instead of the human view."),
    ] = False,
    steps: Annotated[
        bool,
        typer.Option(
            "--steps",
            help=(
                "Render per-skill-call breakdown from the executor's tool-use loop. "
                "Shows each skill invoked, its input, output (truncated), and latency. "
                "No Langfuse backend required — data is captured by the executor itself."
            ),
        ),
    ] = False,
) -> None:
    """Render the decision chain behind a completed run.

    Shows the input, LLM call metrics (model, tokens, cost, latency), and
    the final output in the order the executor processed them. When the run
    failed, the error is shown instead of an output section.

    Add ``--steps`` to also see each skill/tool call made during the run:
    which skill, what input the LLM passed, what it returned, and how long
    it took.  No external tracing backend required.

    Examples::

        mdk explain abc123               # explain run abc123
        mdk explain --last               # explain the most-recent run
        mdk explain abc123 --json        # machine-readable JSON
        mdk explain --last --steps       # include per-skill step breakdown
    """
    import asyncio  # noqa: PLC0415

    asyncio.run(_cmd(run_id=run_id, last=last, as_json=as_json, steps=steps))


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------


async def _cmd(*, run_id: str | None, last: bool, as_json: bool, steps: bool = False) -> None:
    storage = build_storage()
    await storage.init()

    record = await _resolve(storage, run_id=run_id, last=last)
    if record is None:
        err.print("[red]✗[/red] run not found")
        raise typer.Exit(code=1)

    if as_json:
        console.print_json(_to_json(record, steps=steps))
        return

    _render_chain(record, show_steps=steps)


async def _resolve(storage: Any, *, run_id: str | None, last: bool) -> RunRecord | None:
    if last or run_id is None:
        runs = await storage.list_runs(limit=1)
        return runs[0] if runs else None
    return await storage.get_run(run_id, tenant_id="local")


# ---------------------------------------------------------------------------
# JSON serialisation
# ---------------------------------------------------------------------------


def _to_json(record: RunRecord, *, steps: bool = False) -> str:
    """Machine-readable representation of the decision chain.

    Thin wrapper over :func:`movate.core.explain.explain_run` (the shared
    record→dict seam reused by the runtime's ``/runs/{id}/explain`` endpoint)
    that serialises the resulting dict to the pretty JSON string
    ``console.print_json`` expects.
    """
    return json.dumps(explain_run(record, steps=steps), indent=2, default=str)


# ---------------------------------------------------------------------------
# Human-readable rendering
# ---------------------------------------------------------------------------


def _status_icon(status: str) -> str:
    if status == JobStatus.SUCCESS:
        return "[green]✓ success[/green]"
    if status == JobStatus.ERROR:
        return "[red]✗ error[/red]"
    if status == JobStatus.SAFETY_BLOCKED:
        return "[red]✗ safety_blocked[/red]"
    if status == JobStatus.DEAD_LETTER:
        return "[red]✗ dead_letter[/red]"
    return f"[yellow]{status}[/yellow]"


def _render_chain(record: RunRecord, *, show_steps: bool = False) -> None:
    """Render the full decision chain for *record* to stdout."""
    m = record.metrics

    # ---- header ----
    header = Text()
    header.append("Run  ", style="dim")
    header.append(record.run_id, style="bold cyan")
    header.append("  ")
    header.append_text(Text.from_markup(_status_icon(record.status)))
    header.append("  ")
    header.append(f"{record.agent} v{record.agent_version}", style="bold")
    console.print(header)
    console.print(Rule(style="dim"))

    # ---- Input ----
    console.print("[bold]Input[/bold]")
    _print_indented_json(record.input)

    # ---- Skill calls (tool-use loop steps) ----
    skill_calls = record.skill_calls or []
    if skill_calls and show_steps:
        console.print()
        console.print(f"[bold]Skill calls[/bold]  ({len(skill_calls)} step(s))")
        _render_skill_calls(skill_calls)
    elif skill_calls:
        console.print()
        console.print(
            f"  [dim]{len(skill_calls)} skill call(s) captured — "
            "add [bold]--steps[/bold] to see details[/dim]"
        )

    # ---- LLM call summary ----
    console.print()
    console.print("[bold]LLM call[/bold]")
    console.print(f"  [dim]Model:[/dim]   {m.provider or record.provider}")

    if m.tokens.input or m.tokens.output:
        cached_note = f" (cached: {m.tokens.cached_input})" if m.tokens.cached_input else ""
        console.print(
            f"  [dim]Tokens:[/dim]  {m.tokens.input} in → {m.tokens.output} out{cached_note}"
        )

    if m.cost_usd:
        console.print(f"  [dim]Cost:[/dim]    [green]${m.cost_usd:.6f}[/green]")

    if m.latency_ms:
        console.print(f"  [dim]Latency:[/dim] [cyan]{m.latency_ms} ms[/cyan]")

    # ---- Output or Error ----
    if record.output is not None:
        console.print()
        console.print("[bold]Output[/bold]")
        _print_indented_json(record.output)
    elif record.error:
        console.print()
        error = record.error
        console.print(f"[red bold]Error[/red bold]  [dim]{error.type}[/dim]\n  {error.message}")
        if error.hint:
            console.print(f"  [dim]Hint:[/dim] {error.hint}")

    # ---- Tracer hint ----
    console.print()
    console.print(f"[dim]{_STEP_TRACER_HINT}[/dim]")


# Maximum characters to show for skill input/output in the step table.
_STEP_PREVIEW_CHARS = 120
# Maximum characters to show for KB chunk content in the inline chunk table.
_KB_CONTENT_PREVIEW_CHARS = 80


def _render_skill_calls(calls: list[SkillCallRecord]) -> None:
    """Render a Rich table of per-step skill invocations."""
    table = Table(show_lines=True, expand=False)
    table.add_column("step", justify="right", style="dim", no_wrap=True)
    table.add_column("skill", style="bold cyan", no_wrap=True)
    table.add_column("latency", justify="right", style="cyan", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("input → output", overflow="fold", max_width=70)

    for call in calls:
        if call.error:
            status = f"[red]✗ {call.error[:60]}[/red]"
            io_preview = _json_preview(call.input)
        else:
            status = "[green]✓[/green]"
            in_str = _json_preview(call.input)
            out_str = _json_preview(call.output or {})
            io_preview = f"{in_str}  →  {out_str}"

        table.add_row(
            str(call.step),
            call.skill,
            f"{call.latency_ms:.0f} ms",
            status,
            io_preview,
        )
    console.print(table)

    # For KB skill calls, render retrieved chunks as a separate readable table.
    for call in calls:
        if "kb" in call.skill.lower() and call.output:
            chunks = call.output.get("chunks") if isinstance(call.output, dict) else None
            if chunks and isinstance(chunks, list):
                _render_kb_chunks_inline(call.skill, chunks, call.latency_ms)


def _render_kb_chunks_inline(skill_name: str, chunks: list[Any], latency_ms: float) -> None:
    """Render KB chunks from a skill output as a readable dim table."""
    console.print(
        Rule(
            f"[dim]  {skill_name} — {len(chunks)} chunk(s) retrieved  "
            f"[dim]({latency_ms:.0f} ms)[/dim]",
            style="dim",
        )
    )
    table = Table(style="dim", show_lines=False, expand=False)
    table.add_column("#", justify="right", style="dim", no_wrap=True, width=3)
    table.add_column("score", justify="right", no_wrap=True, width=6)
    table.add_column("source", overflow="fold", max_width=30)
    table.add_column("content preview", overflow="fold", max_width=60)

    for i, chunk in enumerate(chunks, 1):
        if not isinstance(chunk, dict):
            continue
        score = chunk.get("score") or chunk.get("similarity") or ""
        score_str = f"{float(score):.2f}" if score != "" else "—"
        source = str(chunk.get("source", chunk.get("chunk_id", "—")))
        source_short = source.rsplit("/", maxsplit=1)[-1][:30]  # just filename
        content = str(chunk.get("content", chunk.get("text", ""))).replace("\n", " ")
        content_preview = (
            content[:_KB_CONTENT_PREVIEW_CHARS] + "…"
            if len(content) > _KB_CONTENT_PREVIEW_CHARS
            else content
        )
        table.add_row(str(i), score_str, source_short, content_preview)

    console.print(table)


def _json_preview(data: dict[str, Any], max_chars: int = _STEP_PREVIEW_CHARS) -> str:
    """Compact single-line JSON preview, truncated to *max_chars*."""
    raw = json.dumps(data, separators=(",", ":"), default=str)
    if len(raw) > max_chars:
        raw = raw[:max_chars] + "…"
    return raw


def _print_indented_json(data: dict[str, Any]) -> None:
    """Print *data* as pretty JSON with a two-space left indent."""
    raw = json.dumps(data, indent=2, default=str)
    for line in raw.splitlines():
        console.print("  " + line)
