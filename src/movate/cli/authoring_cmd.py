"""``mdk authoring`` — the thin CLI over the authoring action catalog (ADR 025 PR1).

A minimal, scriptable surface so the catalog + plan→apply→verify spine is usable
and testable today. The rich conversational UX is PR3 (``mdk dev`` copilot); the
MCP surface is PR4. This command intentionally stays thin: it parses args,
drives :class:`movate.authoring.AuthoringDriver`, and renders the result.

Subcommands
-----------
``mdk authoring list``
    Show the catalog — every action, its description, side effects, and
    whether it is reversible. ``-o json`` emits the self-describing manifest
    (the same shape PR3's planner / PR4's MCP server consume).
``mdk authoring plan <action> --args '<json>'``
    Dry-run an action: print its diff + cost/side-effect estimate + the
    confirmation gate. NO writes.
``mdk authoring apply <action> --args '<json>' [--yes] [--fast] [--no-verify]``
    Checkpoint → apply → verify. Refuses to apply a confirmation-gated action
    (cost / networked / destructive) without ``--yes``.
``mdk authoring undo``
    Revert the last applied action to its pre-action checkpoint.
``mdk authoring history``
    Print the action log (what the catalog applied, oldest-first).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from movate.authoring import (
    AuthoringContext,
    AuthoringDriver,
    ConfirmationRequiredError,
    describe_catalog,
    list_actions,
)
from movate.authoring.base import AuthoringActionError
from movate.authoring.catalog import UnknownActionError
from movate.cli._resolve import walk_up_for_project_root

console = Console()
err_console = Console(stderr=True)

authoring_app = typer.Typer(
    name="authoring",
    help=(
        "Evolve an agent through the typed, reversible authoring catalog "
        "(ADR 025): add contexts, edit instructions, set models, ingest KB, "
        "add skills/evals — each planned, validated, and undoable. The "
        "conversational copilot lands in a later release; this is the "
        "scriptable spine."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _resolve_project(explicit: Path | None) -> Path:
    if explicit is not None:
        if not explicit.is_dir():
            err_console.print(f"[red]✗[/red] --project path is not a directory: {explicit}")
            raise typer.Exit(code=2)
        return explicit.resolve()
    return walk_up_for_project_root() or Path.cwd().resolve()


def _parse_args_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        err_console.print(f"[red]✗[/red] --args is not valid JSON: {exc}")
        raise typer.Exit(code=2) from None
    if not isinstance(data, dict):
        err_console.print("[red]✗[/red] --args must be a JSON object")
        raise typer.Exit(code=2)
    return data


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@authoring_app.command("list")
def list_catalog(
    output: str = typer.Option(
        "text", "--output", "-o", help="Output format: 'text' (default) or 'json'."
    ),
) -> None:
    """List the authoring action catalog (self-describing)."""
    if output == "json":
        console.print_json(json.dumps(describe_catalog()))
        return
    table = Table(title="Authoring action catalog (ADR 025)")
    table.add_column("name", style="bold cyan")
    table.add_column("side effects", style="dim")
    table.add_column("reversible", justify="center")
    table.add_column("description", overflow="fold")
    for action in list_actions():
        table.add_row(
            action.name,
            ", ".join(s.value for s in action.side_effects) or "—",
            "yes" if action.reversible else "[red]no[/red]",
            action.description,
        )
    console.print(table)
    console.print(f"[dim]{len(list_actions())} action(s).[/dim]")


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


@authoring_app.command("plan")
def plan_action(
    action: str = typer.Argument(..., help="Action name (see `mdk authoring list`)."),
    args: str | None = typer.Option(None, "--args", help="JSON object of the action's args."),
    project: Path | None = typer.Option(None, "--project", "-p", help="Project root."),
    output: str = typer.Option("text", "--output", "-o", help="'text' (default) or 'json'."),
) -> None:
    """Dry-run an action: show its diff + estimate + confirmation gate (NO writes)."""
    root = _resolve_project(project)
    driver = AuthoringDriver(AuthoringContext(project=root))
    try:
        plan = driver.plan(action, _parse_args_json(args))
    except UnknownActionError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None
    except (AuthoringActionError, ValueError) as exc:
        err_console.print(f"[red]✗[/red] plan failed: {exc}")
        raise typer.Exit(code=1) from None

    if output == "json":
        console.print_json(plan.model_dump_json())
        return
    console.print(f"[bold]{plan.action}[/bold] — {plan.summary}")
    console.print(f"  side effects: {', '.join(s.value for s in plan.side_effects) or '—'}")
    console.print(f"  reversible: {'yes' if plan.reversible else 'no'}")
    console.print(
        f"  requires confirmation: {'[yellow]yes[/yellow]' if plan.requires_confirmation else 'no'}"
    )
    if plan.estimated_cost_usd is not None:
        console.print(f"  estimated cost: ~${plan.estimated_cost_usd:.4f}")
    if plan.diff:
        console.print("\n[bold]diff:[/bold]")
        console.print(plan.diff)


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


@authoring_app.command("apply")
def apply_action(
    action: str = typer.Argument(..., help="Action name (see `mdk authoring list`)."),
    args: str | None = typer.Option(None, "--args", help="JSON object of the action's args."),
    project: Path | None = typer.Option(None, "--project", "-p", help="Project root."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Confirm a cost/networked/destructive action."
    ),
    fast: bool = typer.Option(
        False, "--fast", help="Auto-apply additive+reversible+free actions without prompting."
    ),
    verify: bool = typer.Option(
        True, "--verify/--no-verify", help="Run validate + mock-run after apply (D3)."
    ),
) -> None:
    """Checkpoint → apply → verify an action. Confirmation-gated for risky ones."""
    root = _resolve_project(project)
    driver = AuthoringDriver(AuthoringContext(project=root))

    # Show the plan first so the operator sees what's coming.
    try:
        plan = driver.plan(action, _parse_args_json(args))
    except UnknownActionError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None
    except (AuthoringActionError, ValueError) as exc:
        err_console.print(f"[red]✗[/red] plan failed: {exc}")
        raise typer.Exit(code=1) from None

    console.print(f"[bold]plan:[/bold] {plan.summary}")
    if plan.diff:
        console.print(plan.diff)

    # Prompt for confirmation when the plan is gated and --yes wasn't passed.
    confirmed = yes
    if plan.requires_confirmation and not yes:
        confirmed = typer.confirm(
            f"This action has side effects {[s.value for s in plan.side_effects]} "
            f"(reversible={plan.reversible}). Apply?"
        )
        if not confirmed:
            err_console.print("[yellow]aborted[/yellow]")
            raise typer.Exit(code=1)

    try:
        outcome = driver.apply(
            action,
            _parse_args_json(args),
            confirmed=confirmed,
            fast_mode=fast,
            verify=verify,
        )
    except ConfirmationRequiredError as exc:
        err_console.print(f"[yellow]✗[/yellow] {exc}")
        raise typer.Exit(code=1) from None
    except (AuthoringActionError, ValueError) as exc:
        err_console.print(f"[red]✗[/red] apply failed: {exc}")
        raise typer.Exit(code=1) from None

    if outcome.verify is not None and not outcome.verify.ok:
        if outcome.verify.reverted:
            err_console.print(
                f"[red]✗[/red] verify failed → reverted. error: {outcome.verify.error}"
            )
            raise typer.Exit(code=1)
        err_console.print(f"[yellow]⚠[/yellow] applied, but verify warning: {outcome.verify.error}")

    result = outcome.result
    assert result is not None  # apply always returns a result on success
    console.print(f"[green]✓[/green] {result.summary}")
    for path in result.changed_paths:
        console.print(f"  • {path}")


# ---------------------------------------------------------------------------
# undo + history
# ---------------------------------------------------------------------------


@authoring_app.command("undo")
def undo_action(
    project: Path | None = typer.Option(None, "--project", "-p", help="Project root."),
) -> None:
    """Revert the last applied action to its pre-action checkpoint."""
    root = _resolve_project(project)
    driver = AuthoringDriver(AuthoringContext(project=root))
    entry = driver.undo()
    if entry is None:
        console.print("[dim]nothing to undo — the action log is empty.[/dim]")
        return
    console.print(f"[green]✓[/green] undid [bold]{entry.action}[/bold] — {entry.summary}")


@authoring_app.command("history")
def history(
    project: Path | None = typer.Option(None, "--project", "-p", help="Project root."),
    output: str = typer.Option("text", "--output", "-o", help="'text' (default) or 'json'."),
) -> None:
    """Print the authoring action log (oldest-first)."""
    root = _resolve_project(project)
    driver = AuthoringDriver(AuthoringContext(project=root))
    entries = driver.history()
    if output == "json":
        console.print_json(json.dumps([e.model_dump(mode="json") for e in entries]))
        return
    if not entries:
        console.print("[dim]no authoring actions recorded yet.[/dim]")
        return
    table = Table(title="Authoring action history")
    table.add_column("#", justify="right", style="dim")
    table.add_column("action", style="bold cyan")
    table.add_column("agent")
    table.add_column("summary", overflow="fold")
    table.add_column("undone", justify="center")
    for i, e in enumerate(entries):
        table.add_row(
            str(i),
            e.action,
            e.agent or "—",
            e.summary,
            "[dim]yes[/dim]" if e.undone else "",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# audit + replay (D7e, #136)
# ---------------------------------------------------------------------------


@authoring_app.command("audit")
def audit_log(
    project: Path | None = typer.Option(None, "--project", "-p", help="Project root."),
    output: str = typer.Option("text", "--output", "-o", help="'text' (default) or 'json'."),
) -> None:
    """Show the append-only authoring audit log (every plan/apply/undo, D7e).

    Distinct from the live undo stack (``history``): this is the immutable
    record of what the copilot did, when, and at what cost. A corrupt/missing
    log degrades to a warning + whatever could be read — it never crashes.
    """
    root = _resolve_project(project)
    driver = AuthoringDriver(AuthoringContext(project=root))
    records = driver.audit_records()
    if output == "json":
        console.print_json(json.dumps([r.model_dump(mode="json") for r in records]))
        return
    if not records:
        console.print("[dim]no authoring audit records yet.[/dim]")
        return
    table = Table(title="Authoring audit log (D7e)")
    table.add_column("#", justify="right", style="dim")
    table.add_column("when", style="dim", no_wrap=True)
    table.add_column("action", style="bold cyan")
    table.add_column("agent")
    table.add_column("outcome", no_wrap=True)
    table.add_column("cost", justify="right")
    table.add_column("summary", overflow="fold")
    total_cost = 0.0
    for i, r in enumerate(records):
        total_cost += r.cost_usd
        outcome_style = {
            "applied": "green",
            "reverted": "yellow",
            "undone": "yellow",
            "skipped": "dim",
        }.get(r.outcome.value, "white")
        table.add_row(
            str(i),
            r.created_at.split("T", 1)[0] if r.created_at else "—",
            r.action,
            r.agent or "—",
            f"[{outcome_style}]{r.outcome.value}[/{outcome_style}]",
            f"${r.cost_usd:.4f}" if r.cost_usd else "—",
            r.summary,
        )
    console.print(table)
    console.print(f"[dim]{len(records)} record(s); total recorded cost ~${total_cost:.4f}.[/dim]")


@authoring_app.command("replay")
def replay(
    project: Path | None = typer.Option(None, "--project", "-p", help="Project root."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Confirm gated (cost/networked/destructive) steps."
    ),
    fast: bool = typer.Option(
        False, "--fast", help="Auto-apply additive+reversible+free steps without prompting."
    ),
) -> None:
    """Re-apply the recorded *applied* action sequence through the driver (D7e).

    SAFETY: every step is re-driven through the SAME plan → confirm → apply →
    verify spine as a live edit — never a raw re-write — so the D2 confirmation
    gate + D3 verify (revert-on-failure) + D4 checkpoint/undo all hold. Gated
    steps prompt (or require ``--yes``); a failing step is reported and the
    replay continues.
    """
    from movate.authoring import replay_records, replayable  # noqa: PLC0415
    from movate.authoring.models import ActionPlan  # noqa: PLC0415

    root = _resolve_project(project)
    driver = AuthoringDriver(AuthoringContext(project=root))
    records = driver.audit_records()
    pending = replayable(records)
    if not pending:
        console.print("[dim]nothing to replay — no applied actions in the audit log.[/dim]")
        return

    console.print(f"[bold]replaying {len(pending)} recorded action(s):[/bold]")

    def _confirm(record: object, plan: ActionPlan) -> bool:
        action = getattr(record, "action", "?")
        console.print(f"\n[bold]replay:[/bold] {action} — {plan.summary}")
        console.print(
            f"  side effects: {', '.join(s.value for s in plan.side_effects) or '—'}"
            f"   reversible: {'yes' if plan.reversible else '[red]no[/red]'}"
        )
        if plan.diff:
            console.print(plan.diff)
        if plan.requires_confirmation and not yes:
            return typer.confirm("Re-apply this step?", default=False)
        return True

    steps = replay_records(driver, records, confirm=_confirm, fast_mode=fast)
    applied = sum(1 for s in steps if s.applied)
    skipped = sum(1 for s in steps if s.skipped)
    failed = sum(1 for s in steps if s.error and not s.skipped)
    for s in steps:
        if s.applied:
            console.print(f"[green]✓[/green] {s.action} — {s.summary}")
        elif s.error:
            console.print(f"[yellow]⚠[/yellow] {s.action} — {s.error}")
        else:
            console.print(f"[dim]· {s.action} — skipped[/dim]")
    console.print(f"[dim]replay done: {applied} applied, {skipped} skipped, {failed} failed.[/dim]")


__all__ = ["authoring_app"]
