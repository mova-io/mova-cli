"""``movate tenants`` — operator-only tenant management.

Today: monthly cost budgets per tenant + per-tenant quotas (ADR 036 D2).
Future surfaces (per-tenant rate-limit overrides, per-tenant agent
allowlists, etc.) slot in under the same subcommand parent.

Subcommands:

* ``movate tenants set-budget`` — set or update a tenant's monthly
  USD cap. Inserts a row if missing; updates the limit if not.
* ``movate tenants clear-budget`` — remove the cap for a tenant
  (sets ``monthly_usd_limit = NULL``; row stays for audit history).
* ``movate tenants show`` — current spend + budget + days-remaining
  for one tenant.
* ``movate tenants list`` — every tenant with a configured budget,
  oldest-first.
* ``movate tenants quota show <id>`` — configured + current usage +
  remaining per counter, for one tenant (ADR 036 D2).
* ``movate tenants quota set <id> --daily-tokens N --daily-requests N
  --monthly-cost USD --mode warn|deny`` — write the quota config file
  (CLI side; the runtime picks the change up on the next request
  whose cache entry has expired, or via a fresh ``build_app``).

Local-only — talks straight to the configured ``StorageProvider``.
The HTTP runtime never exposes these operations; budget + quota
management is operator-side, not customer-side.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._console import confirm_destructive, error, hint, success
from movate.core.models import RunRecord, TenantBudget
from movate.core.quotas import (
    DEFAULT_QUOTA_CONFIG_NAME,
    QuotaConfig,
    QuotaMode,
    TenantQuota,
    load_quota_config,
    resolve_config_path,
    save_quota_config,
    upsert_tenant_quota,
)
from movate.core.reporting import Usage, build_usage
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
        error(f"monthly-usd must be >= 0, got {monthly_usd}")
        raise typer.Exit(code=2)

    asyncio.run(_upsert(TenantBudget(tenant_id=tenant_id, monthly_usd_limit=monthly_usd)))
    success(f"budget for {tenant_id} set to ${monthly_usd:.2f}/month")


@tenants_app.command("clear-budget")
def clear_budget(
    tenant_id: str = typer.Argument(..., help="Tenant id."),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirm prompt (use in scripts / CI).",
    ),
) -> None:
    """Remove the budget cap for ``tenant_id`` (unlimited spend).

    The row stays in the table so the audit trail (``created_at`` /
    ``updated_at``) is preserved — the limit just becomes ``NULL``.

    Prompts before clearing — this can cost real money if it's a
    misclick — pass ``-y`` to bypass for scripts."""
    confirm_destructive(
        f"Clear budget for tenant {tenant_id}? This removes the cost ceiling.",
        yes=yes,
    )
    asyncio.run(_upsert(TenantBudget(tenant_id=tenant_id, monthly_usd_limit=None)))
    success(f"cleared budget for {tenant_id} (now unlimited)")


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
        hint("[dim]no tenant budgets configured[/dim]")
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


# ---------------------------------------------------------------------------
# Quota subcommand (ADR 036 D2)
# ---------------------------------------------------------------------------

quota_app = typer.Typer(
    name="quota",
    help="View or update per-tenant quotas (ADR 036 D2).",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
tenants_app.add_typer(quota_app)


def _quota_path_or_error() -> Path:
    """Resolve the quota config path for the CLI's ``quota set``.

    Returns the resolved :class:`pathlib.Path` (which may not yet exist on
    a fresh ``quota set`` call). When no env var is set AND ``./quotas.yaml``
    is absent we fall back to ``Path.cwd() / quotas.yaml`` — the same default
    the resolver uses on read, so the round-trip is consistent.
    """
    resolved = resolve_config_path()
    if resolved is not None:
        return resolved
    return Path.cwd() / DEFAULT_QUOTA_CONFIG_NAME


@quota_app.command("show")
def quota_show(
    tenant_id: str = typer.Argument(..., help="Tenant id."),
) -> None:
    """Show the configured quota + current usage + remaining for ``tenant_id``.

    Surfaces the values the runtime middleware uses for its admission
    check — so the operator can preview "is this tenant about to be
    blocked / warned?" before it happens.

    Reads the quota config via :func:`load_quota_config` and the
    persisted runs via the configured ``StorageProvider`` (same as the
    runtime; consistent answer).
    """
    config = load_quota_config()
    if config is None:
        hint(
            "[dim]no quota config loaded — set "
            "[bold]MDK_QUOTA_CONFIG[/bold] or create "
            f"[bold]./{DEFAULT_QUOTA_CONFIG_NAME}[/bold][/dim]"
        )
        raise typer.Exit(code=0)

    row = config.get(tenant_id)
    daily, monthly = asyncio.run(_load_quota_usage(tenant_id))

    table = Table(title=f"tenant {tenant_id} — quota (ADR 036 D2)", show_header=False)
    table.add_column("field", style="dim")
    table.add_column("value")
    table.add_row("admin bypass", "yes" if config.is_admin(tenant_id) else "no")
    if row is None:
        table.add_row("configured", "[dim]no row (unlimited)[/dim]")
    else:
        table.add_row("mode", row.mode.value)
        table.add_row(
            "daily_token_limit",
            "unlimited" if row.daily_token_limit is None else str(row.daily_token_limit),
        )
        table.add_row(
            "daily_request_limit",
            "unlimited" if row.daily_request_limit is None else str(row.daily_request_limit),
        )
        table.add_row(
            "monthly_cost_usd_limit",
            "unlimited"
            if row.monthly_cost_usd_limit is None
            else f"${row.monthly_cost_usd_limit:.2f}",
        )
    daily_tokens_used = daily.totals.tokens_in + daily.totals.tokens_out
    table.add_row("usage daily_tokens", str(daily_tokens_used))
    table.add_row("usage daily_requests", str(daily.totals.requests))
    table.add_row("usage monthly_cost_usd", f"${monthly.totals.cost_usd:.4f}")
    if row is not None:
        if row.daily_token_limit is not None:
            table.add_row(
                "remaining daily_tokens",
                str(max(0, row.daily_token_limit - daily_tokens_used)),
            )
        if row.daily_request_limit is not None:
            table.add_row(
                "remaining daily_requests",
                str(max(0, row.daily_request_limit - daily.totals.requests)),
            )
        if row.monthly_cost_usd_limit is not None:
            table.add_row(
                "remaining monthly_cost_usd",
                f"${max(0.0, row.monthly_cost_usd_limit - monthly.totals.cost_usd):.4f}",
            )
    stdout.print(table)


@quota_app.command("set")
def quota_set(
    tenant_id: str = typer.Argument(..., help="Tenant id."),
    daily_tokens: int | None = typer.Option(
        None,
        "--daily-tokens",
        help="Daily token ceiling (input + output). Pass 0 / omit to leave unset.",
    ),
    daily_requests: int | None = typer.Option(
        None,
        "--daily-requests",
        help="Daily request-count ceiling. Pass 0 / omit to leave unset.",
    ),
    monthly_cost: float | None = typer.Option(
        None,
        "--monthly-cost",
        help="Monthly cost ceiling in USD. Pass 0 / omit to leave unset.",
    ),
    mode: str = typer.Option(
        "warn",
        "--mode",
        help="warn (log + header + allow) or deny (429 over ceiling).",
    ),
) -> None:
    """Write the per-tenant quota row to the config file.

    CLI side only — the runtime reads the file at app build / on the next
    cache miss. Existing rows for ``tenant_id`` are replaced; new rows are
    appended. Unset limits stay unset (no ceiling for that counter).

    [bold]Examples:[/bold]

      [dim]# Warn-only ceiling at $500/month (rollout posture)[/dim]
      $ movate tenants quota set 4f1a7c... --monthly-cost 500 --mode warn

      [dim]# Hard 50k-tokens/day ceiling on a free-tier tenant[/dim]
      $ movate tenants quota set 4f1a7c... --daily-tokens 50000 --mode deny
    """
    parsed_mode = QuotaMode.parse(mode)
    if mode.strip().lower() not in ("warn", "deny"):
        error(f"--mode must be 'warn' or 'deny', got {mode!r}")
        raise typer.Exit(code=2)

    path = _quota_path_or_error()
    # Load existing or start fresh — load returns ``None`` when the file
    # doesn't exist yet, which is the expected first-set state.
    try:
        existing = load_quota_config(path)
    except Exception as exc:
        error(f"failed to read existing quota config at {path}: {exc}")
        raise typer.Exit(code=2) from None
    if existing is None:
        existing = QuotaConfig()

    quota = TenantQuota(
        tenant_id=tenant_id,
        daily_token_limit=daily_tokens if daily_tokens and daily_tokens > 0 else None,
        daily_request_limit=daily_requests if daily_requests and daily_requests > 0 else None,
        monthly_cost_usd_limit=monthly_cost if monthly_cost and monthly_cost > 0 else None,
        mode=parsed_mode,
    )
    updated = upsert_tenant_quota(existing, quota)
    save_quota_config(updated, path)
    success(f"wrote quota for {tenant_id} → {path}")
    hint(
        "[dim]The runtime picks the change up on the next cache miss "
        "(default TTL 60s) or a fresh build_app.[/dim]"
    )


async def _load_quota_usage(tenant_id: str) -> tuple[Usage, Usage]:
    """Compute the 1-day + 30-day usage rollups for ``tenant_id``.

    Mirrors what the runtime middleware does on a cache miss, so the
    ``quota show`` table matches what the admission gate will see. Local
    helper (not exported) — keeps the CLI table renderer free of the
    storage / aggregation imports.
    """
    storage = build_storage()
    await storage.init()
    try:
        runs = await storage.list_runs(tenant_id=tenant_id, limit=10_000)
    finally:
        await storage.close()
    cutoff_daily = datetime.now(UTC) - timedelta(days=1)
    cutoff_monthly = datetime.now(UTC) - timedelta(days=30)

    def _in_window(record: RunRecord, cutoff: datetime) -> bool:
        ts = record.created_at
        if ts is None:
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return ts >= cutoff

    daily_runs = [r for r in runs if _in_window(r, cutoff_daily)]
    monthly_runs = [r for r in runs if _in_window(r, cutoff_monthly)]
    daily = build_usage(
        daily_runs,
        tenant_id=tenant_id,
        window_days=1,
        include_by_agent=False,
        include_by_provider=False,
    )
    monthly = build_usage(
        monthly_runs,
        tenant_id=tenant_id,
        window_days=30,
        include_by_agent=False,
        include_by_provider=False,
    )
    return daily, monthly


__all__ = ["tenants_app"]
