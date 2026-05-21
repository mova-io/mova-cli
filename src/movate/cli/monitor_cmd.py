"""``mdk monitor`` — live runs dashboard (Sprint Q).

Polls SQLite (or Postgres) for recent runs and re-renders a Rich Live
table at a configurable interval. Operator-facing answer to "what's
happening right now?" — pairs with ``mdk costs report`` (the
historical view) the way ``htop`` pairs with ``ps``.

Typical usage::

  mdk monitor                        # live tail of the last 20 runs, 3s refresh
  mdk monitor --agent triage         # filter to one agent
  mdk monitor --status error         # only failures
  mdk monitor --interval 1           # tighter refresh
  mdk monitor --once                 # one-shot snapshot, no live loop

What surfaces per row:

* Created-at timestamp (HH:MM:SS, since the live view tracks
  "what happened in the last few minutes")
* Run ID (truncated to 8 chars — same convention as snapshot hashes)
* Agent name
* Status (color-coded: success=green, error=red, queued=cyan)
* Provider (truncated to keep the row compact)
* Cost USD (6dp; matches ``mdk costs report``)
* Latency in ms
* Tokens in/out

Why this lives at the CLI:

* No Prometheus / Grafana dependency. The data lives in our own
  storage; tailing it directly is one less moving part.
* Same Protocol on SQLite + Postgres — operators on a local dev DB
  and operators tailing a prod Postgres see the same view.
* ``mdk monitor`` is what an engineer reaches for during a 2am cost
  spike. The fewer screens to flip between, the better.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from typing import Any

import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table

from movate.core.models import RunRecord
from movate.storage import build_storage

console = Console()
err_console = Console(stderr=True)


# Display caps. Lifted to constants so a future "wider terminals get
# more" enhancement is a one-line tweak rather than a magic-number
# hunt.
_DEFAULT_LIMIT = 20
_DEFAULT_INTERVAL_SECONDS = 3.0
_MIN_INTERVAL_SECONDS = 0.5

# Match snapshot short-hash convention so operators see a single
# truncation length across snapshot / run / job IDs.
_SHORT_ID_CHARS = 8

# Status → Rich style. Anything not listed renders unstyled (dim).
_STATUS_STYLES = {
    "success": "green",
    "queued": "cyan",
    "running": "cyan",
    "error": "red",
    "safety_blocked": "yellow",
    "dead_letter": "magenta",
}


# ---------------------------------------------------------------------------
# Rendering (pure — easy to test without a Live loop)
# ---------------------------------------------------------------------------


def _short_run_id(run_id: str) -> str:
    """Trim a run_id to 8 chars for the dashboard. Mirrors snapshot
    short-hash convention."""
    return run_id[:_SHORT_ID_CHARS] if len(run_id) > _SHORT_ID_CHARS else run_id


def _short_time(ts: datetime | None) -> str:
    """Display the time-of-day, not the full ISO string — the live
    view is short-term; the date doesn't add value."""
    if ts is None:
        return "—"
    return ts.strftime("%H:%M:%S")


def _short_provider(provider: str, *, max_chars: int = 28) -> str:
    """Truncate the provider string so it doesn't push the cost / token
    columns off-screen. 28 is the elbow where openai/gpt-4o-mini-* fits
    but a longer provider+version string starts to wrap."""
    return provider if len(provider) <= max_chars else provider[: max_chars - 1] + "…"


def render_dashboard(
    runs: list[RunRecord],
    *,
    title: str = "mdk monitor",
) -> Table:
    """Build the Rich table for one snapshot of recent runs.

    Pure function — takes runs, returns a Table. Lets tests assert on
    structure without spinning up a Live loop. The CLI driver wraps
    this in ``Live`` to refresh on a timer.
    """
    table = Table(title=title, title_style="bold", show_lines=False)
    table.add_column("Time", style="dim", no_wrap=True)
    table.add_column("Run", style="cyan", no_wrap=True)
    table.add_column("Agent", style="cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Provider", style="dim", no_wrap=True)
    table.add_column("Cost ($)", justify="right", style="green", no_wrap=True)
    table.add_column("Latency (ms)", justify="right", style="dim", no_wrap=True)
    table.add_column("Tokens in/out", justify="right", style="dim", no_wrap=True)

    if not runs:
        table.add_row("—", "—", "—", "—", "—", "—", "—", "—")
        return table

    for run in runs:
        status = str(run.status)
        style = _STATUS_STYLES.get(status, "")
        status_cell = f"[{style}]{status}[/{style}]" if style else status
        table.add_row(
            _short_time(run.created_at),
            _short_run_id(run.run_id),
            run.agent or "—",
            status_cell,
            _short_provider(run.provider or "—"),
            f"${run.metrics.cost_usd:.6f}",
            f"{run.metrics.latency_ms}",
            f"{run.metrics.tokens.input}/{run.metrics.tokens.output}",
        )
    return table


# ---------------------------------------------------------------------------
# Fetch helper — reused by --once + the live loop
# ---------------------------------------------------------------------------


async def _fetch_recent(
    *,
    agent: str | None,
    tenant_id: str | None,
    status: str | None,
    limit: int,
) -> list[RunRecord]:
    """Open storage, fetch, close. One-shot per call so we never hold
    the connection across a sleep — important for the live loop."""
    storage = build_storage()
    await storage.init()
    try:
        return await storage.list_runs(
            agent=agent,
            tenant_id=tenant_id,
            status=status,
            limit=limit,
        )
    finally:
        await storage.close()


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def monitor(
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
    status: str = typer.Option(
        "",
        "--status",
        help=(
            "Filter to one status. Common: [bold]success[/bold] / "
            "[bold]error[/bold] / [bold]queued[/bold]. Empty = show all."
        ),
    ),
    limit: int = typer.Option(
        _DEFAULT_LIMIT,
        "--limit",
        help=f"How many recent runs to show. Default {_DEFAULT_LIMIT}.",
    ),
    interval: float = typer.Option(
        _DEFAULT_INTERVAL_SECONDS,
        "--interval",
        "-i",
        help=(
            f"Refresh interval in seconds (default {_DEFAULT_INTERVAL_SECONDS}). "
            f"Minimum {_MIN_INTERVAL_SECONDS} to avoid hammering storage."
        ),
    ),
    once: bool = typer.Option(
        False,
        "--once",
        help=(
            "Render one snapshot and exit. Useful for cron / CI smoke "
            "(the live loop expects a TTY)."
        ),
    ),
    clear: bool = typer.Option(
        False,
        "--clear",
        help=(
            "Use alternate-screen mode so the live table doesn't accumulate "
            "scrollback. Restores the terminal on exit. Recommended for "
            "long-running sessions."
        ),
    ),
) -> None:
    """Live dashboard of recent runs.

    Polls storage on an interval, re-renders a Rich table in place.
    [bold]Ctrl+C[/bold] to quit. Use [bold]--once[/bold] for a single
    snapshot if you don't have a TTY.

    Pairs with [bold]mdk costs report[/bold] (historical view) the
    way [bold]htop[/bold] pairs with [bold]ps[/bold].

    [bold]Examples:[/bold]

      [dim]$ mdk monitor                       # live, 3s refresh[/dim]
      [dim]$ mdk monitor --agent triage        # one agent[/dim]
      [dim]$ mdk monitor --status error        # only failures[/dim]
      [dim]$ mdk monitor --interval 1          # tighter refresh[/dim]
      [dim]$ mdk monitor --once                # one snapshot, exit[/dim]
    """
    if interval < _MIN_INTERVAL_SECONDS and not once:
        err_console.print(
            f"[red]✗[/red] --interval below {_MIN_INTERVAL_SECONDS}s is too aggressive. "
            "[dim]Storage will spend more time answering polls than serving real work.[/dim]"
        )
        raise typer.Exit(code=2)
    if limit < 1:
        err_console.print(f"[red]✗[/red] --limit must be ≥ 1; got {limit}")
        raise typer.Exit(code=2)

    _valid_statuses = ("success", "error", "queued", "running", "safety_blocked", "dead_letter")
    if status and status not in _valid_statuses:
        err_console.print(
            f"[red]✗[/red] --status {status!r} is not a valid run status. "
            f"Valid values: {', '.join(_valid_statuses)}"
        )
        raise typer.Exit(code=2)

    title = _title(agent=agent, status=status, limit=limit)

    if once:
        runs = asyncio.run(
            _fetch_recent(
                agent=agent or None,
                tenant_id=tenant_id or None,
                status=status or None,
                limit=limit,
            )
        )
        console.print(render_dashboard(runs, title=title))
        return

    # Live loop. Suppress KeyboardInterrupt so Ctrl+C exits cleanly
    # instead of dumping a traceback.
    asyncio.run(
        _live_loop(
            agent=agent or None,
            tenant_id=tenant_id or None,
            status=status or None,
            limit=limit,
            interval=interval,
            title=title,
            screen=clear,
        )
    )


async def _live_loop(
    *,
    agent: str | None,
    tenant_id: str | None,
    status: str | None,
    limit: int,
    interval: float,
    title: str,
    screen: bool = False,
) -> None:
    """Inner refresh loop. Refreshes the Live table every ``interval`` s.

    ``screen=True`` enables alternate-screen mode — the terminal is
    cleared on entry and restored on exit. Keeps long sessions from
    bloating scrollback. Defaults off so the dashboard's last frame
    stays visible after Ctrl+C (most operator-friendly default).
    """
    with (
        contextlib.suppress(KeyboardInterrupt),
        Live(
            render_dashboard([], title=title),
            refresh_per_second=4,
            screen=screen,
        ) as live,
    ):
        while True:
            runs = await _fetch_recent(
                agent=agent,
                tenant_id=tenant_id,
                status=status,
                limit=limit,
            )
            live.update(render_dashboard(runs, title=title))
            await asyncio.sleep(interval)


def _title(*, agent: str, status: str, limit: int) -> str:
    """Title row reflecting active filters, so the operator can glance
    at the top of the screen and remember what they're tailing."""
    bits: list[Any] = [f"last {limit}"]
    if agent:
        bits.append(f"agent={agent}")
    if status:
        bits.append(f"status={status}")
    suffix = " · ".join(bits)
    return f"mdk monitor  [dim]({suffix})[/dim]"
