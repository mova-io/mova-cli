"""``mdk canary`` — champion/challenger rollout (ADR 016 D3).

Closes the ADR 016 improvement loop (harvest → continuous-eval/drift →
**canary**): with versioned agents (ADR 014 registry), route a configurable
slice of prod traffic to a *challenger* version, compare it live against the
champion (feedback + run/error counts sliced by ``agent_version``), then
promote the winner — assisted by default, opt-in auto-promote behind an
eval-gate.

It is **additive + default-off**: until ``mdk canary set`` runs, an agent has
no canary and routes 100% to its champion. ``mdk canary off`` is the kill
switch (weight → 0, or ``--delete`` removes the row).

Subcommands:

* ``set <agent> --challenger <ver> --weight <0-100>`` — create / update.
* ``status <agent>`` — show the current canary config.
* ``compare <agent>`` — champion-vs-challenger live quality + deltas.
* ``promote <agent> [--to <ver>]`` — promote a version to champion.
* ``off <agent> [--delete]`` — kill switch (weight 0) or remove.

Mirrors ``mdk schedule`` / ``mdk trigger``: operates against the local
runtime storage under the ``local`` tenant.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._completion import complete_agent_path
from movate.cli._output import Report
from movate.core.canary import SideStats, aggregate_side
from movate.core.models import CanaryConfig
from movate.storage.base import StorageProvider

console = Console()
err_console = Console(stderr=True)

# Local CLI storage scopes records under the "local" tenant — matches
# schedule_cmd / trigger_cmd / build_local_runtime's Executor tenant_id.
_LOCAL_TENANT = "local"

canary_app = typer.Typer(
    name="canary",
    help="Manage champion/challenger canary rollouts (ADR 016 D3).",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@canary_app.command("set")
def set_canary(
    agent: str = typer.Argument(
        ...,
        help="Agent to roll out a canary for.",
        shell_complete=complete_agent_path,
    ),
    challenger: str = typer.Option(
        ...,
        "--challenger",
        help="Challenger version to receive canary traffic.",
    ),
    weight: int = typer.Option(
        0,
        "--weight",
        "-w",
        min=0,
        max=100,
        help="Percent of traffic (0-100) routed to the challenger. 0 = kill switch.",
    ),
    sticky: bool = typer.Option(
        True,
        "--sticky/--no-sticky",
        help="Consistent routing per thread (no champion↔challenger flip mid-conversation).",
    ),
    champion: str | None = typer.Option(
        None,
        "--champion",
        help="Pin the champion to a specific version. Default: registry latest.",
    ),
    auto_promote: bool = typer.Option(
        False,
        "--auto-promote",
        help="Opt-in: auto-promote the challenger once it clears --eval-gate.",
    ),
    eval_gate: float | None = typer.Option(
        None,
        "--eval-gate",
        help="Min challenger quality (0-1) for auto-promote. Required with --auto-promote.",
    ),
    auto_rollback: bool = typer.Option(
        False,
        "--auto-rollback/--no-auto-rollback",
        help="Opt-in: a drift regression on the challenger auto-trips the kill "
        "switch (weight → 0). Default off = alert-only (ADR 016 D5).",
    ),
    disabled: bool = typer.Option(
        False, "--disabled", help="Create the canary but leave it dormant (routes to champion)."
    ),
    output_format: Report = typer.Option(Report.TABLE, "--format", case_sensitive=False),
) -> None:
    """Set (or update) an agent's canary rollout.

    [bold]Examples:[/bold]

      [dim]# Send 10% of traffic to version 2026.5.23.1[/dim]
      $ mdk canary set faq-agent --challenger 2026.5.23.1 --weight 10

      [dim]# Auto-promote once the challenger clears a 0.9 thumbs-up rate[/dim]
      $ mdk canary set faq-agent --challenger 2026.5.23.1 --weight 25 \\
          --auto-promote --eval-gate 0.9

      [dim]# Kill switch — back to 100% champion instantly[/dim]
      $ mdk canary off faq-agent
    """
    if auto_promote and eval_gate is None:
        err_console.print(
            "[red]✗[/red] --auto-promote requires --eval-gate (the bar a challenger must clear)"
        )
        raise typer.Exit(code=2)

    now = datetime.now(UTC)
    existing = asyncio.run(_get(agent))
    config = CanaryConfig(
        tenant_id=_LOCAL_TENANT,
        agent=agent,
        challenger_version=challenger,
        champion_version=champion,
        weight=weight,
        sticky=sticky,
        enabled=not disabled,
        auto_promote=auto_promote,
        eval_gate=eval_gate,
        auto_rollback=auto_rollback,
        created_at=existing.created_at if existing else now,
        updated_at=now,
    )
    asyncio.run(_save(config))

    if output_format == Report.JSON:
        console.print_json(config.model_dump_json())
        return
    state = "enabled" if config.enabled else "disabled (dormant)"
    kill = " [yellow](kill switch — 100% champion)[/yellow]" if weight == 0 else ""
    console.print(
        f"[green]✓[/green] canary for [bold]{agent}[/bold] set: "
        f"challenger [bold]{challenger}[/bold] at [bold]{weight}%[/bold] ({state}){kill}"
    )


@canary_app.command("status")
def canary_status(
    agent: str = typer.Argument(..., help="Agent to show the canary for."),
    output_format: Report = typer.Option(Report.TABLE, "--format", case_sensitive=False),
) -> None:
    """Show an agent's current canary config."""
    config = asyncio.run(_get(agent))
    if config is None:
        if output_format == Report.JSON:
            console.print_json(data=None)
            return
        console.print(
            f"[dim]no canary for[/dim] {agent} "
            f"[dim]— set one with[/dim] mdk canary set {agent} --challenger <ver> --weight <n>"
        )
        return
    if output_format == Report.JSON:
        console.print_json(config.model_dump_json())
        return
    table = Table(title=f"Canary — {agent}")
    table.add_column("field", style="bold")
    table.add_column("value")
    table.add_row("challenger", config.challenger_version)
    table.add_row("champion", config.champion_version or "<latest>")
    table.add_row("weight", f"{config.weight}%")
    table.add_row("sticky", "yes" if config.sticky else "no")
    table.add_row("enabled", "yes" if config.enabled else "no")
    table.add_row("auto-promote", "yes" if config.auto_promote else "no")
    table.add_row("eval-gate", "—" if config.eval_gate is None else f"{config.eval_gate:.3f}")
    table.add_row("auto-rollback", "yes" if config.auto_rollback else "no")
    table.add_row("updated", config.updated_at.isoformat(timespec="seconds"))
    console.print(table)


@canary_app.command("compare")
def canary_compare(
    agent: str = typer.Argument(..., help="Agent to compare champion vs challenger for."),
    challenger: str | None = typer.Option(
        None, "--challenger", help="Override the challenger version (default: from the canary)."
    ),
    champion: str | None = typer.Option(
        None, "--champion", help="Override the champion version (default: from the canary)."
    ),
    output_format: Report = typer.Option(Report.TABLE, "--format", case_sensitive=False),
) -> None:
    """Compare live quality champion-vs-challenger (feedback + run/error counts)."""
    result = asyncio.run(_compare(agent, challenger=challenger, champion=champion))
    if result is None:
        err_console.print(
            "[red]✗[/red] no challenger to compare — set a canary or pass --challenger <version>"
        )
        raise typer.Exit(code=2)
    champ, chall = result
    if output_format == Report.JSON:
        console.print_json(
            data={
                "agent": agent,
                "champion": _side_dict(champ),
                "challenger": _side_dict(chall),
                "success_rate_delta": chall.success_rate - champ.success_rate,
                "thumbs_up_rate_delta": chall.thumbs_up_rate - champ.thumbs_up_rate,
            }
        )
        return
    table = Table(title=f"Canary compare — {agent}")
    table.add_column("metric", style="bold")
    table.add_column("champion", justify="right")
    table.add_column("challenger", justify="right")
    table.add_column("Δ", justify="right")
    table.add_row("version", champ.version or "<latest>", chall.version or "—", "")
    table.add_row("runs", str(champ.run_count), str(chall.run_count), "")
    table.add_row("errors", str(champ.error_count), str(chall.error_count), "")
    table.add_row(
        "success rate",
        f"{champ.success_rate:.2%}",
        f"{chall.success_rate:.2%}",
        f"{chall.success_rate - champ.success_rate:+.2%}",
    )
    table.add_row("👍", str(champ.thumbs_up), str(chall.thumbs_up), "")
    table.add_row("👎", str(champ.thumbs_down), str(chall.thumbs_down), "")
    table.add_row(
        "👍 rate",
        f"{champ.thumbs_up_rate:.2%}",
        f"{chall.thumbs_up_rate:.2%}",
        f"{chall.thumbs_up_rate - champ.thumbs_up_rate:+.2%}",
    )
    console.print(table)


@canary_app.command("promote")
def promote_canary(
    agent: str = typer.Argument(..., help="Agent to promote a version for."),
    to_version: str | None = typer.Option(
        None, "--to", help="Version to promote (default: the configured challenger)."
    ),
    output_format: Report = typer.Option(Report.TABLE, "--format", case_sensitive=False),
) -> None:
    """Promote a version to champion (assisted — concludes the canary)."""
    updated = asyncio.run(_promote(agent, to_version=to_version))
    if updated is None:
        err_console.print(f"[red]✗[/red] no canary for {agent} — nothing to promote")
        raise typer.Exit(code=2)
    if output_format == Report.JSON:
        console.print_json(updated.model_dump_json())
        return
    console.print(
        f"[green]✓[/green] promoted [bold]{updated.champion_version}[/bold] to champion for "
        f"[bold]{agent}[/bold]; canary weight → 0 (concluded)"
    )


@canary_app.command("off")
def canary_off(
    agent: str = typer.Argument(..., help="Agent to turn the canary off for."),
    delete: bool = typer.Option(
        False, "--delete", help="Remove the canary row entirely (default: weight → 0)."
    ),
) -> None:
    """Kill switch: route 100% to champion (weight 0), or --delete the canary."""
    if delete:
        removed = asyncio.run(_delete(agent))
        if removed:
            console.print(f"[green]✓[/green] deleted canary for [bold]{agent}[/bold]")
        else:
            console.print(f"[dim]no canary for[/dim] {agent} [dim]— nothing to delete[/dim]")
        return
    config = asyncio.run(_get(agent))
    if config is None:
        console.print(f"[dim]no canary for[/dim] {agent} [dim]— already 100% champion[/dim]")
        return
    killed = config.model_copy(update={"weight": 0, "updated_at": datetime.now(UTC)})
    asyncio.run(_save(killed))
    console.print(
        f"[green]✓[/green] kill switch on for [bold]{agent}[/bold]: weight → 0 (100% champion)"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _side_dict(s: SideStats) -> dict[str, object]:
    return {
        "version": s.version,
        "run_count": s.run_count,
        "success_count": s.success_count,
        "error_count": s.error_count,
        "thumbs_up": s.thumbs_up,
        "thumbs_down": s.thumbs_down,
        "feedback_count": s.feedback_count,
        "success_rate": s.success_rate,
        "thumbs_up_rate": s.thumbs_up_rate,
    }


@asynccontextmanager
async def _local_storage() -> AsyncIterator[StorageProvider]:
    """Build the local runtime, yield its storage, tear down cleanly."""
    from movate.cli._runtime import build_local_runtime, shutdown_runtime  # noqa: PLC0415

    runtime = await build_local_runtime(mock=True)
    try:
        yield runtime.storage
    finally:
        await shutdown_runtime(runtime.storage, runtime.tracer)


async def _save(config: CanaryConfig) -> None:
    async with _local_storage() as storage:
        await storage.save_canary_config(config)


async def _get(agent: str) -> CanaryConfig | None:
    async with _local_storage() as storage:
        return await storage.get_canary_config(agent, tenant_id=_LOCAL_TENANT)


async def _delete(agent: str) -> bool:
    async with _local_storage() as storage:
        return await storage.delete_canary_config(agent, tenant_id=_LOCAL_TENANT)


async def _compare(
    agent: str, *, challenger: str | None, champion: str | None
) -> tuple[SideStats, SideStats] | None:
    async with _local_storage() as storage:
        config = await storage.get_canary_config(agent, tenant_id=_LOCAL_TENANT)
        challenger_version = challenger or (config.challenger_version if config else None)
        if challenger_version is None:
            return None
        champion_version = champion or (config.champion_version if config else None)
        if champion_version is None:
            latest = await storage.get_agent_bundle(agent, tenant_id=_LOCAL_TENANT)
            champion_version = latest.version if latest is not None else None
        champ = await aggregate_side(
            storage, agent=agent, tenant_id=_LOCAL_TENANT, version=champion_version
        )
        chall = await aggregate_side(
            storage, agent=agent, tenant_id=_LOCAL_TENANT, version=challenger_version
        )
        return champ, chall


async def _promote(agent: str, *, to_version: str | None) -> CanaryConfig | None:
    async with _local_storage() as storage:
        config = await storage.get_canary_config(agent, tenant_id=_LOCAL_TENANT)
        if config is None:
            return None
        target = to_version or config.challenger_version
        updated = config.model_copy(
            update={
                "champion_version": target,
                "weight": 0,
                "updated_at": datetime.now(UTC),
            }
        )
        await storage.save_canary_config(updated)
        return updated


__all__ = ["canary_app"]
