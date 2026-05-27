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
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from movate.core.models import EvalRecord, JobStatus, RunRecord
from movate.storage import build_storage

console = Console()
err_console = Console(stderr=True)

# A generous default fetch cap — operators in MVP have ~thousands of runs;
# fetching + reducing in Python is unmeasurable at that scale (same call
# the cost reporter makes). Bump via the underlying limit if history is huge.
_FETCH_LIMIT = 10_000

# Pass-rate below this in the per-agent table is rendered red — a quick
# "this agent is failing more than half its evals" eyeball cue.
_LOW_PASS_RATE = 0.5

# Run statuses that count as a "failure" for the top-failing rollup. Mirrors
# the terminal-non-success set the logs/runs commands treat as failed.
_FAILED_STATUSES: frozenset[JobStatus] = frozenset(
    {
        JobStatus.ERROR,
        JobStatus.SAFETY_BLOCKED,
        JobStatus.DEAD_LETTER,
    }
)


# ---------------------------------------------------------------------------
# Aggregation (pure — unit-testable without a DB)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LatencyPercentiles:
    """p50 / p95 / p99 of a set of run latencies (ms).

    ``None`` for every field when there were no runs with a recorded
    latency — the caller renders "N/A" rather than ``0`` so a missing
    signal is distinguishable from a genuinely instant run.
    """

    p50: float | None = None
    p95: float | None = None
    p99: float | None = None
    count: int = 0


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile over an already-sorted, non-empty list.

    Uses the nearest-rank method (no interpolation) so a tiny sample
    (the common offline case) yields a real observed value rather than a
    synthetic interpolated one. ``pct`` in [0, 100].
    """
    if not sorted_values:
        raise ValueError("percentile of empty sequence")
    if len(sorted_values) == 1:
        return sorted_values[0]
    # Nearest-rank: rank = ceil(pct/100 * N), 1-based, clamped to [1, N].
    rank = max(1, min(len(sorted_values), -(-int(pct * len(sorted_values)) // 100)))
    return sorted_values[rank - 1]


def _latency_percentiles(runs: list[RunRecord]) -> LatencyPercentiles:
    """Compute p50/p95/p99 of run latency over ``runs``.

    Runs whose ``metrics.latency_ms`` is missing / non-positive are dropped
    (older records may carry no latency) — they don't contribute a bogus
    ``0`` to the distribution. Empty input → all-``None``.
    """
    latencies = sorted(
        float(r.metrics.latency_ms)
        for r in runs
        if r.metrics.latency_ms and r.metrics.latency_ms > 0
    )
    if not latencies:
        return LatencyPercentiles(count=0)
    return LatencyPercentiles(
        p50=_percentile(latencies, 50),
        p95=_percentile(latencies, 95),
        p99=_percentile(latencies, 99),
        count=len(latencies),
    )


@dataclass(frozen=True)
class AgentRollup:
    """The full rollup for one agent (or workflow) name.

    Pass-rate fields are ``None`` when the agent has no eval runs (cost /
    latency come from agent *runs*, which may exist without any eval).
    """

    name: str
    # --- runs / cost ---
    runs: int = 0
    failed_runs: int = 0
    total_cost_usd: float = 0.0
    latency: LatencyPercentiles = field(default_factory=LatencyPercentiles)
    last_run_at: str = ""
    # --- evals / pass-rate ---
    eval_runs: int = 0
    latest_pass_rate: float | None = None
    mean_pass_rate: float | None = None
    latest_eval_at: str = ""

    @property
    def mean_cost_usd(self) -> float:
        return self.total_cost_usd / self.runs if self.runs else 0.0

    @property
    def failure_rate(self) -> float:
        return self.failed_runs / self.runs if self.runs else 0.0


@dataclass(frozen=True)
class FailingCase:
    """One recurring failing input across runs, for the top-failures table."""

    case: str
    """A short, stable rendering of the failing input (the 'case id')."""
    failures: int
    agents: list[str]
    last_error: str = ""


def _case_key(run: RunRecord) -> str:
    """A stable, compact key for a run's input — the 'case id' we cluster on.

    Prefers an explicit ``case``/``id``/``case_id`` field if the agent's
    input carries one (eval-style inputs often do); otherwise falls back to
    a compact JSON rendering of the whole input. Truncated so the table
    stays readable — clustering identical inputs is the point, not showing
    the full payload.
    """
    payload = run.input or {}
    if isinstance(payload, dict):
        for field_name in ("case_id", "case", "id"):
            val = payload.get(field_name)
            if isinstance(val, str | int) and str(val).strip():
                return str(val).strip()[:120]
    try:
        rendered = json.dumps(payload, sort_keys=True, default=str)
    except (TypeError, ValueError):
        rendered = str(payload)
    return rendered[:120]


def _top_failing_cases(runs: list[RunRecord], *, limit: int = 5) -> list[FailingCase]:
    """Cluster non-success runs by input and rank by failure count.

    The persisted store keeps eval *summaries* (``EvalRecord``), not per-case
    eval detail, so the offline per-instance failure signal is failing
    *runs*: we group them by a stable input key and surface the most
    frequently failing inputs. Empty / all-success input → empty list.
    """
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"failures": 0, "agents": set(), "last_error": ""}
    )
    for run in runs:
        if run.status not in _FAILED_STATUSES:
            continue
        bucket = buckets[_case_key(run)]
        bucket["failures"] += 1
        bucket["agents"].add(run.agent or "(unknown agent)")
        if run.error is not None and run.error.message:
            bucket["last_error"] = run.error.message
    cases = [
        FailingCase(
            case=key,
            failures=b["failures"],
            agents=sorted(b["agents"]),
            last_error=b["last_error"],
        )
        for key, b in buckets.items()
    ]
    # Most failures first; ties broken by case string for deterministic output.
    cases.sort(key=lambda c: (-c.failures, c.case))
    return cases[:limit]


@dataclass(frozen=True)
class Report:
    """The complete rollup — what ``--json`` serialises and the table renders."""

    total_runs: int
    total_failed_runs: int
    total_cost_usd: float
    overall_latency: LatencyPercentiles
    total_eval_runs: int
    overall_latest_pass_rate: float | None
    agents: list[AgentRollup]
    top_failing_cases: list[FailingCase]
    window_days: int = 0
    agent_filter: str | None = None


def _filter_runs_by_since(runs: list[RunRecord], days: int) -> list[RunRecord]:
    """Drop runs older than ``days`` ago (UTC). ``days <= 0`` → keep all.

    Coerces naive datetimes (some SQLite rows hand them back naive) to UTC
    before comparing, so the window never raises on a mixed set.
    """
    if days <= 0:
        return runs
    cutoff = datetime.now(UTC) - timedelta(days=days)
    out: list[RunRecord] = []
    for run in runs:
        ts = run.created_at
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if ts >= cutoff:
            out.append(run)
    return out


def _filter_evals_by_since(evals: list[EvalRecord], days: int) -> list[EvalRecord]:
    """Window helper for eval summaries — same contract as the run variant."""
    if days <= 0:
        return evals
    cutoff = datetime.now(UTC) - timedelta(days=days)
    out: list[EvalRecord] = []
    for ev in evals:
        ts = ev.created_at
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if ts >= cutoff:
            out.append(ev)
    return out


def _build_report(
    runs: list[RunRecord],
    evals: list[EvalRecord],
    *,
    window_days: int = 0,
    agent_filter: str | None = None,
    top_n: int = 5,
) -> Report:
    """Reduce the raw run + eval records into the per-agent + overall rollup.

    Pure: no I/O. ``runs`` and ``evals`` are already store-fetched +
    time-windowed by the caller. An agent appears in the rollup if it has
    *either* runs or evals — so an agent you've only eval'd (no ad-hoc runs)
    still shows its pass-rate, and vice versa.
    """
    # --- per-agent run aggregation ---
    run_buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "runs": 0,
            "failed": 0,
            "cost": 0.0,
            "latencies": [],
            "last_run_at": "",
        }
    )
    for run in runs:
        name = run.agent or "(unknown agent)"
        b = run_buckets[name]
        b["runs"] += 1
        if run.status in _FAILED_STATUSES:
            b["failed"] += 1
        # ``metrics`` is a required field on RunRecord; ``cost_usd`` /
        # ``latency_ms`` default to 0 on older records, so a missing signal
        # degrades to "no contribution" rather than a crash.
        b["cost"] += float(run.metrics.cost_usd or 0.0)
        lat = run.metrics.latency_ms
        if lat and lat > 0:
            b["latencies"].append(float(lat))
        created_iso = run.created_at.isoformat() if run.created_at else ""
        b["last_run_at"] = max(b["last_run_at"], created_iso)

    # --- per-agent eval aggregation (evals are returned newest-first) ---
    eval_buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"eval_runs": 0, "pass_rates": [], "latest_pass_rate": None, "latest_at": ""}
    )
    for ev in evals:
        name = ev.agent or "(unknown agent)"
        b = eval_buckets[name]
        b["eval_runs"] += 1
        b["pass_rates"].append(float(ev.pass_rate))
        created_iso = ev.created_at.isoformat() if ev.created_at else ""
        # First time we see this agent in a newest-first list = latest eval.
        if created_iso >= b["latest_at"]:
            b["latest_at"] = created_iso
            b["latest_pass_rate"] = float(ev.pass_rate)

    names = sorted(set(run_buckets) | set(eval_buckets))
    agents: list[AgentRollup] = []
    for name in names:
        rb = run_buckets.get(name)
        eb = eval_buckets.get(name)
        latencies = sorted(rb["latencies"]) if rb else []
        latency = (
            LatencyPercentiles(
                p50=_percentile(latencies, 50),
                p95=_percentile(latencies, 95),
                p99=_percentile(latencies, 99),
                count=len(latencies),
            )
            if latencies
            else LatencyPercentiles(count=0)
        )
        pass_rates = eb["pass_rates"] if eb else []
        agents.append(
            AgentRollup(
                name=name,
                runs=rb["runs"] if rb else 0,
                failed_runs=rb["failed"] if rb else 0,
                total_cost_usd=rb["cost"] if rb else 0.0,
                latency=latency,
                last_run_at=rb["last_run_at"] if rb else "",
                eval_runs=eb["eval_runs"] if eb else 0,
                latest_pass_rate=eb["latest_pass_rate"] if eb else None,
                mean_pass_rate=(sum(pass_rates) / len(pass_rates)) if pass_rates else None,
                latest_eval_at=eb["latest_at"] if eb else "",
            )
        )
    # Highest spend first — that's usually what the operator scans for.
    agents.sort(key=lambda a: a.total_cost_usd, reverse=True)

    total_failed = sum(1 for r in runs if r.status in _FAILED_STATUSES)
    # Overall latest pass-rate = pass-rate of the single most recent eval run.
    overall_latest_pass_rate: float | None = None
    if evals:
        latest_eval = max(
            evals,
            key=lambda e: e.created_at if e.created_at else datetime.min.replace(tzinfo=UTC),
        )
        overall_latest_pass_rate = float(latest_eval.pass_rate)

    return Report(
        total_runs=len(runs),
        total_failed_runs=total_failed,
        total_cost_usd=sum(float(r.metrics.cost_usd or 0.0) for r in runs),
        overall_latency=_latency_percentiles(runs),
        total_eval_runs=len(evals),
        overall_latest_pass_rate=overall_latest_pass_rate,
        agents=agents,
        top_failing_cases=_top_failing_cases(runs, limit=top_n),
        window_days=window_days,
        agent_filter=agent_filter,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_pct(value: float | None) -> str:
    return f"{value * 100:.0f}%" if value is not None else "N/A"


def _fmt_ms(value: float | None) -> str:
    return f"{value:.0f}ms" if value is not None else "N/A"


def _report_to_json(report: Report) -> dict[str, Any]:
    """Machine-readable shape for ``--json`` — stable for scripting.

    Dataclasses serialise cleanly via ``asdict``; ``FailingCase.agents`` is
    already a list and the nested ``LatencyPercentiles`` flattens to a dict.
    """
    return {
        "agent_filter": report.agent_filter,
        "window_days": report.window_days,
        "totals": {
            "runs": report.total_runs,
            "failed_runs": report.total_failed_runs,
            "cost_usd": report.total_cost_usd,
            "eval_runs": report.total_eval_runs,
            "latest_pass_rate": report.overall_latest_pass_rate,
            "latency_ms": asdict(report.overall_latency),
        },
        "agents": [
            {
                "name": a.name,
                "runs": a.runs,
                "failed_runs": a.failed_runs,
                "failure_rate": a.failure_rate,
                "total_cost_usd": a.total_cost_usd,
                "mean_cost_usd": a.mean_cost_usd,
                "latency_ms": asdict(a.latency),
                "last_run_at": a.last_run_at,
                "eval_runs": a.eval_runs,
                "latest_pass_rate": a.latest_pass_rate,
                "mean_pass_rate": a.mean_pass_rate,
                "latest_eval_at": a.latest_eval_at,
            }
            for a in report.agents
        ],
        "top_failing_cases": [
            {
                "case": c.case,
                "failures": c.failures,
                "agents": c.agents,
                "last_error": c.last_error,
            }
            for c in report.top_failing_cases
        ],
    }


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


__all__ = ["report"]
