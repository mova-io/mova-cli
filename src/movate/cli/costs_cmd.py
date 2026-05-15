"""``mdk costs`` — historical cost reporting (Sprint Q Day ?).

Today: one subcommand, ``report``. Answers the top-3 ops question
"how much did each agent spend?" using data we already capture in
:class:`RunRecord`. No new instrumentation needed; this is purely
a reporter over existing storage.

Future siblings (deferred):

* ``costs forecast`` — extrapolate burn-rate from recent history.
* ``costs cap`` — per-tenant / per-agent budget enforcement (lives
  on the executor side; the CLI just visualizes).
* ``costs export`` — CSV / Parquet for stuffing into BI tools.

Design rules followed here:

* **No external calls.** Pure storage reader. Works offline.
* **Aggregation in-memory.** We don't push GROUP BY into SQL because
  Postgres + SQLite share the same Protocol — easier to keep the
  reporter SQL-free than to write two backend-specific aggregators.
  For the run volumes operators have in MVP (~thousands), the cost
  of fetching + reducing in Python is unmeasurable.
* **``--json`` for piping** to jq / dashboards / CI annotations.
* **Same exit-code convention** as other Sprint Q commands: 0 = report
  rendered, 2 = operator error (bad flag combo).
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import typer
from rich.console import Console
from rich.table import Table

from movate.core.models import RunRecord
from movate.storage import build_storage

console = Console()
err_console = Console(stderr=True)


costs_app = typer.Typer(
    name="costs",
    help=(
        "Historical cost reports from recorded runs. "
        "[bold]mdk costs report[/bold] summarises spend per agent / provider."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


# ---------------------------------------------------------------------------
# Aggregation (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostRollup:
    """Aggregated cost stats for one group (agent or provider).

    ``mean_cost_usd`` is total / count — operators glance at this to
    spot the agent with anomalously expensive individual calls
    (versus high volume + cheap calls).
    """

    key: str
    runs: int
    total_cost_usd: float
    total_tokens_in: int
    total_tokens_out: int
    last_run_at: str = ""

    @property
    def mean_cost_usd(self) -> float:
        return self.total_cost_usd / self.runs if self.runs else 0.0


def _rollup_runs(runs: list[RunRecord], *, group_by: str) -> list[CostRollup]:
    """Group ``runs`` by ``agent`` or ``provider`` and aggregate cost stats.

    Returns rollups sorted by total cost descending so the highest
    spenders surface first — that's almost always what the operator
    is asking about.
    """
    buckets: dict[str, dict] = defaultdict(
        lambda: {
            "runs": 0,
            "total_cost_usd": 0.0,
            "total_tokens_in": 0,
            "total_tokens_out": 0,
            "last_run_at": "",
        }
    )

    for run in runs:
        key = _group_key(run, group_by)
        bucket = buckets[key]
        bucket["runs"] += 1
        bucket["total_cost_usd"] += float(run.metrics.cost_usd or 0.0)
        bucket["total_tokens_in"] += int(run.metrics.tokens.input or 0)
        bucket["total_tokens_out"] += int(run.metrics.tokens.output or 0)
        # ``created_at`` is a datetime; compare ISO-string-wise so we
        # don't drag timezone gotchas into the comparison.
        created_iso = run.created_at.isoformat() if run.created_at else ""
        bucket["last_run_at"] = max(bucket["last_run_at"], created_iso)

    rollups = [
        CostRollup(
            key=key,
            runs=b["runs"],
            total_cost_usd=b["total_cost_usd"],
            total_tokens_in=b["total_tokens_in"],
            total_tokens_out=b["total_tokens_out"],
            last_run_at=b["last_run_at"],
        )
        for key, b in buckets.items()
    ]
    rollups.sort(key=lambda r: r.total_cost_usd, reverse=True)
    return rollups


def _group_key(run: RunRecord, group_by: str) -> str:
    """Pick the bucket key based on ``--by``."""
    if group_by == "provider":
        return run.provider or "(unknown provider)"
    return run.agent or "(unknown agent)"


def _filter_by_since(runs: list[RunRecord], days: int) -> list[RunRecord]:
    """Drop runs older than ``days`` ago (UTC).

    ``days <= 0`` is a no-op — keep everything.
    """
    if days <= 0:
        return runs
    cutoff = datetime.now(UTC) - timedelta(days=days)
    out: list[RunRecord] = []
    for run in runs:
        ts = run.created_at
        if ts is None:
            continue
        # SQLite rows hand us naive datetimes; coerce to UTC.
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if ts >= cutoff:
            out.append(run)
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_table(
    rollups: list[CostRollup],
    *,
    group_by: str,
    total_runs: int,
) -> None:
    """Render the rollup list as a Rich table."""
    if not rollups:
        console.print(
            "[yellow]⚠[/yellow] no runs recorded yet. "
            "[dim]Run [bold]mdk run <agent> '{}'[/bold] to populate the log.[/dim]"
        )
        return

    grand_total = sum(r.total_cost_usd for r in rollups)
    title = f"Cost report — by {group_by} ({total_runs} run(s), ${grand_total:.4f} total)"
    table = Table(title=title, title_style="bold", show_lines=False)
    table.add_column(group_by.title(), style="cyan", no_wrap=True)
    table.add_column("Runs", justify="right", no_wrap=True)
    table.add_column("Total $", justify="right", style="green")
    table.add_column("Mean $", justify="right", style="dim")
    table.add_column("Tokens in", justify="right", style="dim")
    table.add_column("Tokens out", justify="right", style="dim")
    table.add_column("Last run", style="dim", no_wrap=True)

    for r in rollups:
        table.add_row(
            r.key,
            f"{r.runs:,}",
            f"${r.total_cost_usd:.4f}",
            f"${r.mean_cost_usd:.6f}",
            f"{r.total_tokens_in:,}",
            f"{r.total_tokens_out:,}",
            (r.last_run_at or "—").split("T", 1)[0],  # date only — keeps the row tight
        )
    console.print(table)


def _as_json(rollups: list[CostRollup], *, group_by: str, total_runs: int) -> dict:
    """Serializable shape for ``--json``."""
    return {
        "group_by": group_by,
        "total_runs": total_runs,
        "total_cost_usd": sum(r.total_cost_usd for r in rollups),
        "rollups": [
            {
                "key": r.key,
                "runs": r.runs,
                "total_cost_usd": r.total_cost_usd,
                "mean_cost_usd": r.mean_cost_usd,
                "total_tokens_in": r.total_tokens_in,
                "total_tokens_out": r.total_tokens_out,
                "last_run_at": r.last_run_at,
            }
            for r in rollups
        ],
    }


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


@costs_app.command("report")
def report(
    by: str = typer.Option(
        "agent",
        "--by",
        help=(
            "Group rollups by [bold]agent[/bold] (default) or [bold]provider[/bold]. "
            'Provider rollup answers "which model is eating my budget?"'
        ),
    ),
    agent: str = typer.Option(
        "",
        "--agent",
        "-a",
        help="Filter to one agent (e.g. [dim]--agent triage[/dim]).",
    ),
    tenant_id: str = typer.Option(
        "",
        "--tenant-id",
        help="Filter to one tenant (omit for cross-tenant in single-tenant deploys).",
    ),
    since_days: int = typer.Option(
        0,
        "--since-days",
        help=(
            "Only count runs from the last N days. 0 (default) = no time filter. "
            "Use 7 / 30 for weekly / monthly views."
        ),
    ),
    limit: int = typer.Option(
        10000,
        "--limit",
        help=(
            "Maximum runs to fetch from storage before aggregating. Bump if your history is huge."
        ),
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON instead of a Rich table — pipe to jq / BI tools.",
    ),
) -> None:
    """Summarise spend per agent or provider from recorded runs.

    [bold]Examples:[/bold]

      [dim]$ mdk costs report                          # per-agent, all-time[/dim]
      [dim]$ mdk costs report --by provider            # which model burns the most[/dim]
      [dim]$ mdk costs report --since-days 7           # last week[/dim]
      [dim]$ mdk costs report --agent triage           # one agent only[/dim]
      [dim]$ mdk costs report --json | jq '.rollups'   # pipe to BI[/dim]
    """
    if by not in ("agent", "provider"):
        err_console.print(f"[red]✗[/red] --by must be 'agent' or 'provider'; got {by!r}")
        raise typer.Exit(code=2)

    storage = build_storage()

    async def fetch() -> list[RunRecord]:
        # All storage providers expose init() — open the connection
        # before issuing queries. Close on the way out so we don't
        # leak SQLite handles for short-lived CLI processes.
        await storage.init()
        try:
            return await storage.list_runs(
                agent=agent or None,
                tenant_id=tenant_id or None,
                limit=limit,
            )
        finally:
            await storage.close()

    runs = asyncio.run(fetch())
    runs = _filter_by_since(runs, since_days)

    rollups = _rollup_runs(runs, group_by=by)

    if json_output:
        console.print_json(json.dumps(_as_json(rollups, group_by=by, total_runs=len(runs))))
    else:
        _render_table(rollups, group_by=by, total_runs=len(runs))
