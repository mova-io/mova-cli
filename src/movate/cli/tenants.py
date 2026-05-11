"""``movate tenants`` — operator-only tenant management.

Today: monthly cost budgets per tenant. Future surfaces (per-tenant
rate-limit overrides, per-tenant agent allowlists, etc.) slot in
under the same subcommand parent.

Four subcommands:

* ``movate tenants set-budget`` — set or update a tenant's monthly
  USD cap. Inserts a row if missing; updates the limit if not.
* ``movate tenants clear-budget`` — remove the cap for a tenant
  (sets ``monthly_usd_limit = NULL``; row stays for audit history).
* ``movate tenants show`` — current spend + budget + days-remaining
  for one tenant.
* ``movate tenants list`` — every tenant with a configured budget,
  oldest-first.

Local-only — talks straight to the configured ``StorageProvider``.
The HTTP runtime never exposes these operations; budget management
is operator-side, not customer-side.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import typer
from rich.console import Console
from rich.table import Table

from movate.core.models import TenantBudget
from movate.storage import build_storage


@dataclass(frozen=True)
class _TenantSummary:
    """Pre-formatted strings for ``tenants show``. Keeps the Rich
    rendering layer trivial — every cell is already a string."""

    budget: str
    spent_usd: float
    remaining: str
    status: str
    created_at: str | None  # ISO timestamp or None when no budget row
    updated_at: str | None


stdout = Console()
err = Console(stderr=True)

tenants_app = typer.Typer(
    name="tenants",
    help="Manage tenant budgets + (future) per-tenant overrides.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@tenants_app.command("set-budget")
def set_budget(
    tenant_id: str = typer.Argument(..., help="Tenant id (uuid hex)."),
    monthly_usd: float = typer.Option(
        ...,
        "--monthly-usd",
        help=(
            "Monthly cost ceiling in USD. The Executor aborts runs for "
            "this tenant once current-month spend hits this value."
        ),
    ),
) -> None:
    """Set or update the monthly cost budget for ``tenant_id``.

    [bold]Examples:[/bold]

      [dim]# Cap a customer at $500/mo[/dim]
      $ movate tenants set-budget 4f1a7c... --monthly-usd 500

      [dim]# Raise their cap after the budget was breached[/dim]
      $ movate tenants set-budget 4f1a7c... --monthly-usd 1000
    """
    if monthly_usd < 0:
        err.print(f"[red]✗[/red] monthly-usd must be >= 0, got {monthly_usd}")
        raise typer.Exit(code=2)

    asyncio.run(_upsert(TenantBudget(tenant_id=tenant_id, monthly_usd_limit=monthly_usd)))
    err.print(f"[green]✓[/green] budget for {tenant_id} set to ${monthly_usd:.2f}/month")


@tenants_app.command("clear-budget")
def clear_budget(
    tenant_id: str = typer.Argument(..., help="Tenant id."),
) -> None:
    """Remove the budget cap for ``tenant_id`` (unlimited spend).

    The row stays in the table so the audit trail (``created_at`` /
    ``updated_at``) is preserved — the limit just becomes ``NULL``.
    """
    asyncio.run(_upsert(TenantBudget(tenant_id=tenant_id, monthly_usd_limit=None)))
    err.print(f"[green]✓[/green] cleared budget for {tenant_id} (now unlimited)")


@tenants_app.command("show")
def show(
    tenant_id: str = typer.Argument(..., help="Tenant id."),
) -> None:
    """Show current-month spend vs budget for ``tenant_id``.

    Surfaces the values the Executor uses for its cap check — so the
    operator can see "is this tenant about to get auto-paused?"
    before it happens.
    """
    summary = asyncio.run(_load_summary(tenant_id))

    table = Table(title=f"tenant {tenant_id}", show_header=False)
    table.add_column("field", style="dim")
    table.add_column("value")
    table.add_row("budget", summary.budget)
    table.add_row("spent this month", f"${summary.spent_usd:.4f}")
    table.add_row("remaining", summary.remaining)
    table.add_row("status", summary.status)
    if summary.created_at is not None:
        table.add_row("budget set at", summary.created_at)
        assert summary.updated_at is not None
        table.add_row("last updated", summary.updated_at)
    stdout.print(table)


@tenants_app.command("list")
def list_tenants() -> None:
    """List every tenant with a configured budget row."""
    budgets, spends = asyncio.run(_load_all())

    if not budgets:
        err.print("[dim]no tenant budgets configured[/dim]")
        return

    table = Table(title="tenant budgets")
    table.add_column("tenant_id", style="bold")
    table.add_column("monthly cap")
    table.add_column("spent this month")
    table.add_column("status")
    table.add_column("set at", style="dim")

    for b in budgets:
        cap_str = "unlimited" if b.monthly_usd_limit is None else f"${b.monthly_usd_limit:.2f}"
        spent = spends.get(b.tenant_id, 0.0)
        status = _status_label(b, spent)
        table.add_row(
            b.tenant_id,
            cap_str,
            f"${spent:.4f}",
            status,
            b.created_at.date().isoformat(),
        )
    stdout.print(table)


# ---------------------------------------------------------------------------
# Async helpers — keep the Typer commands synchronous
# ---------------------------------------------------------------------------


async def _upsert(budget: TenantBudget) -> None:
    storage = build_storage()
    await storage.init()
    try:
        await storage.upsert_tenant_budget(budget)
    finally:
        await storage.close()


async def _load_summary(tenant_id: str) -> _TenantSummary:
    """Compose the values shown by ``tenants show`` in one async block.

    Done at the storage layer (not the Rich layer) so unit tests can
    assert the summary independently of the CLI rendering.
    """
    storage = build_storage()
    await storage.init()
    try:
        budget = await storage.get_tenant_budget(tenant_id)
        spent = await storage.sum_tenant_cost_current_month(tenant_id)
    finally:
        await storage.close()

    if budget is None:
        return _TenantSummary(
            budget="unlimited (no row)",
            spent_usd=spent,
            remaining="∞",
            status="[green]ok[/green]",
            created_at=None,
            updated_at=None,
        )
    cap = budget.monthly_usd_limit
    if cap is None:
        return _TenantSummary(
            budget="unlimited (cleared)",
            spent_usd=spent,
            remaining="∞",
            status="[green]ok[/green]",
            created_at=budget.created_at.isoformat(),
            updated_at=budget.updated_at.isoformat(),
        )
    remaining = max(0.0, cap - spent)
    return _TenantSummary(
        budget=f"${cap:.2f}",
        spent_usd=spent,
        remaining=f"${remaining:.4f}",
        status=_status_label(budget, spent),
        created_at=budget.created_at.isoformat(),
        updated_at=budget.updated_at.isoformat(),
    )


async def _load_all() -> tuple[list[TenantBudget], dict[str, float]]:
    """Load every budget + the current-month spend for each."""
    storage = build_storage()
    await storage.init()
    try:
        budgets = await storage.list_tenant_budgets()
        spends = {
            b.tenant_id: await storage.sum_tenant_cost_current_month(b.tenant_id) for b in budgets
        }
    finally:
        await storage.close()
    return budgets, spends


# Threshold for the yellow "approaching budget" warning. 80% is the
# stock SRE rule for "wake the operator up so they can decide whether
# to raise the cap before customer traffic is impacted."
_WARNING_PCT = 0.80


def _status_label(budget: TenantBudget, spent: float) -> str:
    """Color-coded Rich markup. Red when over, yellow when ≥80%, green otherwise."""
    if budget.monthly_usd_limit is None:
        return "[green]ok[/green]"
    cap = budget.monthly_usd_limit
    if cap <= 0:
        return "[red]paused (cap=0)[/red]"
    pct = spent / cap
    if pct >= 1.0:
        return "[red]paused (over budget)[/red]"
    if pct >= _WARNING_PCT:
        return f"[yellow]warning ({pct * 100:.0f}%)[/yellow]"
    return f"[green]ok ({pct * 100:.0f}%)[/green]"


__all__ = ["tenants_app"]
