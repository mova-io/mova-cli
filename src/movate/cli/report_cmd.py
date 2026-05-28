"""``mdk report`` — offline rollup of how your agents are doing (ADR 031 D3).

The no-infra / works-on-a-laptop answer to "how are my agents doing?".
It reads the **local store** (the same SQLite/Postgres backend
``mdk logs`` / ``mdk costs`` read via ``build_storage()``) and aggregates
what's already persisted — runs (ADR 024 per-step cost + latency on
``RunRecord``) and eval summaries (``EvalRecord``) — into an at-a-glance
summary:

* **Pass-rate trend** — eval pass-rate over the last N eval runs, per
  agent/workflow (an ``EvalRecord`` carries ``pass_rate`` per eval run;
  a workflow eval is persisted under the workflow's name in the same
  ``agent`` column, so it groups transparently).
* **Cost over time** — total + per-run mean cost from ``RunRecord``.
* **Latency percentiles** — p50 / p95 / p99 of run latency.
* **Top failing cases** — the inputs that fail most often across runs
  (non-success ``RunRecord`` status), to point at what to fix. (Per-case
  eval detail is not persisted in the store — only the eval *summary* is —
  so failing *runs* are the offline per-instance failure signal.)
* **Per-agent / per-workflow rollup** — the above grouped by name.

Design rules (mirrors ``mdk costs report``, ADR 031 D3 boundaries):

* **Aggregation/rollup ONLY — not a viz engine.** Rich dashboards live in
  Langfuse (ADR 031 D1) / Grafana / Azure (ADR 031 D2). This is a CLI
  summary.
* **Reads the LOCAL store through the existing read path** —
  ``build_storage()`` → ``list_runs`` / ``list_evals`` /
  ``list_workflow_runs``. No remote runtime call, no new storage backend
  (``cli ⊥ runtime``).
* **Aggregation in-memory** — Postgres + SQLite share the same Protocol,
  so we reduce in Python rather than writing two backend-specific GROUP BYs
  (same rationale as ``costs report``).
* **``--json`` for scripting**; default = a Rich summary for the terminal.
* **Degrades gracefully** — empty store → friendly hint, exit 0; records
  missing cost/latency → omit / treat as 0, never divide-by-zero.
* Exit-code convention: 0 = report rendered (incl. empty), 2 = operator
  error (bad flag value).
"""

from __future__ import annotations

import asyncio
import json

import typer
from rich.console import Console
from rich.table import Table

from movate.core.models import EvalRecord, RunRecord

# The pure rollup is now a backend-agnostic ``core`` module (ADR 032 D2) so
# the runtime (``cli ⊥ runtime``) can reuse it for ``GET /api/v1/report``.
# The CLI imports it and keeps only the terminal rendering + the Typer command.
# The dataclasses / helpers are re-exported below (``_build_report`` etc.) so
# the historical import surface (``from movate.cli.report_cmd import ...``)
# stays stable for existing callers / tests.
from movate.core.reporting import (
    AgentRollup,
    FailingCase,
    LatencyPercentiles,
    Report,
    _filter_evals_by_since,
    _filter_runs_by_since,
    _latency_percentiles,
    _percentile,
    _top_failing_cases,
    build_report,
    report_to_json,
)
from movate.storage import build_storage

# Re-export under the legacy private names the CLI/tests have always used,
# so the move into ``core`` is invisible to importers.
_build_report = build_report
_report_to_json = report_to_json

console = Console()
err_console = Console(stderr=True)

# A generous default fetch cap — operators in MVP have ~thousands of runs;
# fetching + reducing in Python is unmeasurable at that scale (same call
# the cost reporter makes). Bump via the underlying limit if history is huge.
_FETCH_LIMIT = 10_000

# Pass-rate below this in the per-agent table is rendered red — a quick
# "this agent is failing more than half its evals" eyeball cue.
_LOW_PASS_RATE = 0.5


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_pct(value: float | None) -> str:
    return f"{value * 100:.0f}%" if value is not None else "N/A"


def _fmt_ms(value: float | None) -> str:
    return f"{value:.0f}ms" if value is not None else "N/A"


def _render_empty_hint() -> None:
    console.print(
        "[yellow]⚠[/yellow] nothing to report yet — no runs or evals in the local store.\n"
        "[dim]Run [bold]mdk run <agent> '{}'[/bold] or [bold]mdk eval <agent>[/bold] "
        "to populate it, then try again.[/dim]"
    )


def _render_report(report: Report) -> None:
    """Render the rollup as a set of Rich tables for the terminal."""
    scope = report.agent_filter or "all agents"
    window = f"last {report.window_days}d" if report.window_days > 0 else "all-time"
    console.print(
        f"[bold]Report[/bold] — {scope} [dim]({window})[/dim]\n"
        f"[dim]{report.total_runs} run(s), {report.total_eval_runs} eval run(s), "
        f"${report.total_cost_usd:.4f} total[/dim]"
    )

    # ---- headline summary ----
    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="dim", no_wrap=True)
    summary.add_column()
    summary.add_row(
        "latest pass-rate",
        _fmt_pct(report.overall_latest_pass_rate),
    )
    fail_pct = report.total_failed_runs / report.total_runs if report.total_runs else 0.0
    summary.add_row(
        "failed runs",
        f"{report.total_failed_runs}/{report.total_runs} ({fail_pct * 100:.0f}%)",
    )
    lat = report.overall_latency
    summary.add_row(
        "latency p50/p95/p99",
        f"{_fmt_ms(lat.p50)} / {_fmt_ms(lat.p95)} / {_fmt_ms(lat.p99)}",
    )
    console.print(summary)

    # ---- per-agent rollup ----
    if report.agents:
        console.print()
        table = Table(title="Per-agent / workflow rollup", title_style="bold")
        table.add_column("Agent / workflow", style="cyan", no_wrap=True)
        table.add_column("Runs", justify="right")
        table.add_column("Fail %", justify="right")
        table.add_column("Pass-rate", justify="right", style="green")
        table.add_column("Total $", justify="right", style="green")
        table.add_column("Mean $", justify="right", style="dim")
        table.add_column("p50/p95/p99", justify="right", style="dim")
        for a in report.agents:
            pr = a.latest_pass_rate
            pr_cell = _fmt_pct(pr)
            if pr is not None and pr < _LOW_PASS_RATE:
                pr_cell = f"[red]{pr_cell}[/red]"
            table.add_row(
                a.name,
                f"{a.runs:,}",
                f"{a.failure_rate * 100:.0f}%",
                pr_cell,
                f"${a.total_cost_usd:.4f}",
                f"${a.mean_cost_usd:.6f}",
                f"{_fmt_ms(a.latency.p50)}/{_fmt_ms(a.latency.p95)}/{_fmt_ms(a.latency.p99)}",
            )
        console.print(table)

    # ---- top failing cases ----
    if report.top_failing_cases:
        console.print()
        fails = Table(title="Top failing cases", title_style="bold")
        fails.add_column("Case (input)", style="yellow", overflow="fold")
        fails.add_column("Failures", justify="right", style="red")
        fails.add_column("Agent(s)", style="dim")
        fails.add_column("Last error", style="dim", overflow="fold")
        for c in report.top_failing_cases:
            fails.add_row(
                c.case,
                str(c.failures),
                ", ".join(c.agents),
                c.last_error or "—",
            )
        console.print(fails)


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def report(
    agent: str = typer.Argument(
        "",
        help="Scope to one agent or workflow name (positional). Omit for all.",
    ),
    agent_opt: str = typer.Option(
        "",
        "--agent",
        "-a",
        help="Scope to one agent/workflow (same as the positional arg).",
    ),
    last: int = typer.Option(
        0,
        "--last",
        "-n",
        help=(
            "Only count runs / evals from the last N days. "
            "0 (default) = no time window. Use 7 / 30 for weekly / monthly."
        ),
    ),
    tenant_id: str = typer.Option(
        "",
        "--tenant-id",
        help="Filter to one tenant (omit for cross-tenant in single-tenant deploys).",
    ),
    top: int = typer.Option(
        5,
        "--top",
        help="How many failing cases to surface in the top-failures table.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit a machine-readable rollup instead of a Rich summary — pipe to jq / CI.",
    ),
) -> None:
    """Offline rollup of agent health from the local store (ADR 031 D3).

    Aggregates the runs + eval summaries already persisted locally —
    pass-rate, cost, latency percentiles, top failing cases, per-agent /
    per-workflow — into an at-a-glance summary. No infra, no remote
    runtime: the offline answer to "how are my agents doing?".

    [bold]Examples:[/bold]

      [dim]$ mdk report                       # everything, all-time[/dim]
      [dim]$ mdk report triage                # one agent[/dim]
      [dim]$ mdk report --agent triage --last 7   # one agent, last week[/dim]
      [dim]$ mdk report --json | jq '.agents' # pipe to scripting / CI[/dim]
    """
    # Positional arg wins, then --agent. Both empty = all agents.
    scope_agent = (agent or agent_opt).strip() or None

    if last < 0:
        err_console.print(f"[red]✗[/red] --last must be ≥ 0 (0 = no window); got {last}")
        raise typer.Exit(code=2)
    if top < 1:
        err_console.print(f"[red]✗[/red] --top must be ≥ 1; got {top}")
        raise typer.Exit(code=2)

    storage = build_storage()

    async def fetch() -> tuple[list[RunRecord], list[EvalRecord]]:
        # Every StorageProvider exposes init(); open the connection before
        # querying and close it on the way out so a short-lived CLI process
        # doesn't leak a SQLite handle.
        await storage.init()
        try:
            runs = await storage.list_runs(
                agent=scope_agent,
                tenant_id=tenant_id or None,
                limit=_FETCH_LIMIT,
            )
            evals = await storage.list_evals(
                agent=scope_agent,
                tenant_id=tenant_id or None,
                limit=_FETCH_LIMIT,
            )
            return runs, evals
        finally:
            await storage.close()

    runs, evals = asyncio.run(fetch())
    runs = _filter_runs_by_since(runs, last)
    evals = _filter_evals_by_since(evals, last)

    if not runs and not evals:
        if json_output:
            console.print_json(
                json.dumps(
                    _report_to_json(
                        _build_report([], [], window_days=last, agent_filter=scope_agent, top_n=top)
                    )
                )
            )
        else:
            _render_empty_hint()
        return

    report_data = _build_report(
        runs,
        evals,
        window_days=last,
        agent_filter=scope_agent,
        top_n=top,
    )

    if json_output:
        console.print_json(json.dumps(_report_to_json(report_data)))
    else:
        _render_report(report_data)


# The aggregation now lives in ``movate.core.reporting`` (ADR 032 D2); these
# are re-exported so the historical ``from movate.cli.report_cmd import ...``
# surface (used by tests + scripts) keeps working after the move.
__all__ = [
    "AgentRollup",
    "FailingCase",
    "LatencyPercentiles",
    "Report",
    "_build_report",
    "_filter_evals_by_since",
    "_filter_runs_by_since",
    "_latency_percentiles",
    "_percentile",
    "_report_to_json",
    "_top_failing_cases",
    "build_report",
    "report",
    "report_to_json",
]
