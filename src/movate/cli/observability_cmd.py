"""``mdk observability`` — the Observability Intelligence CLI (ADR 047).

The CLI parity for the runtime's ``/api/v1/observability/*`` surface. Every
subcommand talks to a deployed runtime over HTTP (``--target``) via
:class:`movate.core.client.MovateClient` — the control plane (cli) stays
⊥ the execution plane (runtime), reaching it only through the public API.

Subcommands:

* ``mdk observability ask "<question>" --target <env>`` — grounded answer + evidence
* ``mdk observability troubleshoot "<symptom>" [--since N] --target <env>``
* ``mdk observability health [--project <id>] --target <env>``
* ``mdk observability digest [--date YYYY-MM-DD] [--project <id>] --target <env>``
* ``mdk observability analyze [--project <id>] --target <env>`` — on-demand trigger

All support ``--json`` for piping (mirrors ``mdk costs report --json``).
"""

from __future__ import annotations

import asyncio
import json

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._console import echo_remote_context, error, get_global_target
from movate.core.client import MovateClient, MovateClientError
from movate.core.user_config import (
    UserConfigError,
    resolve_bearer_token,
    resolve_target,
)
from movate.runtime.schemas import (
    AnalyzeAcceptedView,
    EvidenceView,
    GroundedAnswerView,
    ObservabilityHealthView,
    ObservabilityInsightView,
)

stdout = Console()
err = Console(stderr=True)

# Health-score colour thresholds (display only).
_HEALTH_GOOD = 80.0
_HEALTH_OK = 50.0

observability_app = typer.Typer(
    name="observability",
    help=(
        "Ask questions about your fleet's telemetry. "
        "[bold]mdk observability ask[/bold] answers in natural language with "
        "grounded citations; [bold]troubleshoot[/bold] correlates failures into "
        "a root cause; [bold]health[/bold] / [bold]digest[/bold] show the "
        "overnight analyst's daily summary."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _build_client(target: str | None, *, suppress: bool = False) -> MovateClient:
    """Resolve a target name → MovateClient (mirrors ``mdk jobs``)."""
    try:
        target_name, target_cfg = resolve_target(target or get_global_target())
        token = resolve_bearer_token(target_cfg)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None
    echo_remote_context(target_name, target_cfg, suppress=suppress)
    return MovateClient(base_url=target_cfg.url, api_key=token)


def _render_evidence(evidence: list[EvidenceView]) -> None:
    """Render the evidence[] citations as a compact table."""
    if not evidence:
        err.print("[yellow]⚠ no evidence cited[/yellow]")
        return
    table = Table(title=f"Evidence ({len(evidence)})", title_style="bold", show_lines=False)
    table.add_column("Kind", style="cyan", no_wrap=True)
    table.add_column("Reference", style="green", no_wrap=True)
    table.add_column("Detail", overflow="fold")
    for ev in evidence:
        table.add_row(ev.kind, ev.reference, ev.detail or "—")
    stdout.print(table)


def _render_answer(view: GroundedAnswerView) -> None:
    """Render a GroundedAnswerView: answer, confidence, action, evidence."""
    stdout.print(view.answer)
    stdout.print()
    stdout.print(f"[dim]confidence {view.confidence:.0%} · llm cost ${view.cost_usd:.4f}[/dim]")
    if view.suggested_action:
        stdout.print(f"[bold]Suggested action:[/bold] {view.suggested_action}")
    stdout.print()
    _render_evidence(view.evidence)


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------


@observability_app.command("ask")
def ask(
    question: str = typer.Argument(..., help="Natural-language question about your fleet."),
    project: str = typer.Option("default", "--project", help="Project id to scope the answer."),
    budget: float = typer.Option(0.05, "--budget", help="Max LLM spend (USD) for this query."),
    target: str = typer.Option(None, "--target", "-t", help="Deployment target name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """Ask a natural-language question; get a grounded answer with citations.

    [dim]$ mdk observability ask "why did costs spike yesterday?" -t prod[/dim]
    """
    view = asyncio.run(
        _ask(
            question=question,
            project=project,
            budget=budget,
            target=target,
            suppress=json_output,
        )
    )
    if json_output:
        stdout.print_json(json.dumps(view.model_dump(mode="json")))
    else:
        _render_answer(view)


async def _ask(
    *, question: str, project: str, budget: float, target: str | None, suppress: bool
) -> GroundedAnswerView:
    client = _build_client(target, suppress=suppress)
    try:
        async with client:
            return await client.observability_ask(question, project_id=project, budget_usd=budget)
    except MovateClientError as exc:
        error(str(exc), context="ask")
        raise typer.Exit(code=exc.status_code // 100) from None


# ---------------------------------------------------------------------------
# troubleshoot
# ---------------------------------------------------------------------------


@observability_app.command("troubleshoot")
def troubleshoot(
    symptom: str = typer.Argument(..., help="The symptom to diagnose."),
    since: int = typer.Option(7, "--since", help="Look-back window in days."),
    project: str = typer.Option("default", "--project", help="Project id to scope the analysis."),
    budget: float = typer.Option(0.05, "--budget", help="Max LLM spend (USD) for this query."),
    target: str = typer.Option(None, "--target", "-t", help="Deployment target name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """Correlate failures + drift + deploys into a likely root cause.

    [dim]$ mdk observability troubleshoot "triage agent is timing out" --since 3 -t prod[/dim]
    """
    view = asyncio.run(
        _troubleshoot(
            symptom=symptom,
            since=since,
            project=project,
            budget=budget,
            target=target,
            suppress=json_output,
        )
    )
    if json_output:
        stdout.print_json(json.dumps(view.model_dump(mode="json")))
    else:
        _render_answer(view)


async def _troubleshoot(
    *, symptom: str, since: int, project: str, budget: float, target: str | None, suppress: bool
) -> GroundedAnswerView:
    client = _build_client(target, suppress=suppress)
    try:
        async with client:
            return await client.observability_troubleshoot(
                symptom, time_window_days=since, project_id=project, budget_usd=budget
            )
    except MovateClientError as exc:
        error(str(exc), context="troubleshoot")
        raise typer.Exit(code=exc.status_code // 100) from None


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


@observability_app.command("health")
def health(
    project: str = typer.Option("default", "--project", help="Project id."),
    target: str = typer.Option(None, "--target", "-t", help="Deployment target name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """Show the latest health score + digest for a project."""
    view = asyncio.run(_health(project=project, target=target, suppress=json_output))
    if json_output:
        stdout.print_json(json.dumps(view.model_dump(mode="json")))
        return
    if not view.has_insight:
        err.print(
            f"[yellow]⚠[/yellow] no insights yet for project [bold]{project}[/bold]. "
            "Run [bold]mdk observability analyze[/bold] to populate the store."
        )
        return
    score = view.health_score if view.health_score is not None else 0.0
    colour = "green" if score >= _HEALTH_GOOD else "yellow" if score >= _HEALTH_OK else "red"
    stdout.print(
        f"[bold]Health:[/bold] [{colour}]{score:.0f}/100[/{colour}] "
        f"[dim](as of {view.date}, {view.anomaly_count} anomaly(ies))[/dim]"
    )
    if view.narrative_digest:
        stdout.print()
        stdout.print(view.narrative_digest)


async def _health(*, project: str, target: str | None, suppress: bool) -> ObservabilityHealthView:
    client = _build_client(target, suppress=suppress)
    try:
        async with client:
            return await client.observability_health(project_id=project)
    except MovateClientError as exc:
        error(str(exc), context="health")
        raise typer.Exit(code=exc.status_code // 100) from None


# ---------------------------------------------------------------------------
# digest
# ---------------------------------------------------------------------------


@observability_app.command("digest")
def digest(
    date: str = typer.Option(
        "", "--date", help="ISO YYYY-MM-DD. Omit for the most recent insight."
    ),
    project: str = typer.Option("default", "--project", help="Project id."),
    target: str = typer.Option(None, "--target", "-t", help="Deployment target name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """Show the analyst's daily markdown digest for a date (or the latest)."""
    view = asyncio.run(
        _digest(date=date or None, project=project, target=target, suppress=json_output)
    )
    if view is None:
        err.print(
            f"[yellow]⚠[/yellow] no insight found for project [bold]{project}[/bold]"
            + (f" on {date}" if date else "")
            + "."
        )
        raise typer.Exit(code=1)
    if json_output:
        stdout.print_json(json.dumps(view.model_dump(mode="json")))
        return
    stdout.print(
        f"[bold]Digest — {view.project_id} {view.date}[/bold] "
        f"[dim](health {view.health_score:.0f}/100)[/dim]"
    )
    stdout.print()
    stdout.print(view.narrative_digest or "[dim](no narrative digest generated)[/dim]")


async def _digest(
    *, date: str | None, project: str, target: str | None, suppress: bool
) -> ObservabilityInsightView | None:
    client = _build_client(target, suppress=suppress)
    try:
        async with client:
            listing = await client.observability_insights(
                project_id=project, since=date, until=date, limit=1
            )
    except MovateClientError as exc:
        error(str(exc), context="digest")
        raise typer.Exit(code=exc.status_code // 100) from None
    return listing.insights[0] if listing.insights else None


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------


@observability_app.command("analyze")
def analyze(
    project: str = typer.Option("default", "--project", help="Project id to analyze."),
    date: str = typer.Option("", "--date", help="ISO YYYY-MM-DD. Omit to analyze yesterday."),
    budget: float = typer.Option(0.10, "--budget", help="Max LLM spend (USD) for the digest."),
    target: str = typer.Option(None, "--target", "-t", help="Deployment target name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """Trigger the overnight analyst on demand (requires admin scope).

    Enqueues a job; returns its id. Poll with [bold]mdk jobs show <id>[/bold].
    """
    view = asyncio.run(
        _analyze(
            project=project,
            date=date or None,
            budget=budget,
            target=target,
            suppress=json_output,
        )
    )
    if json_output:
        stdout.print_json(json.dumps(view.model_dump(mode="json")))
    else:
        stdout.print(
            f"[green]✓[/green] analyst enqueued for project [bold]{view.project_id}[/bold] "
            f"— job [cyan]{view.job_id}[/cyan]. "
            f"Poll with [dim]mdk jobs show {view.job_id}[/dim]."
        )


async def _analyze(
    *, project: str, date: str | None, budget: float, target: str | None, suppress: bool
) -> AnalyzeAcceptedView:
    client = _build_client(target, suppress=suppress)
    try:
        async with client:
            return await client.observability_analyze(
                project_id=project, date=date, budget_usd=budget
            )
    except MovateClientError as exc:
        error(str(exc), context="analyze")
        raise typer.Exit(code=exc.status_code // 100) from None


__all__ = ["observability_app"]
