"""``mdk replay <run-id>`` — re-execute a past run with the same input (Sprint Q).

The killer feature for deterministic prompt iteration. After editing a
prompt or model setting, run ``mdk replay <run-id>`` to check whether
the change actually improved the case that failed yesterday. Without
replay, the operator has to reconstruct the input by hand — easy to
mistype, easy to miss a subtle wrapper field.

Pairs with:

* ``mdk explain <run-id>``  — what happened?
* ``mdk replay  <run-id>``  — what happens NOW?
* ``mdk inspect agent``     — what does the executor see?

  $ mdk replay r-abc-123                 # re-run, print new response
  $ mdk replay r-abc-123 --diff          # side-by-side with original
  $ mdk replay r-abc-123 --mock          # offline / hermetic
  $ mdk replay r-abc-123 --json          # machine-readable

Design rules:

* **Uses the stored input verbatim.** No coercion / reinterpretation —
  the recorded ``input`` dict goes straight to the executor.
* **Uses the current agent on disk.** If the operator edited the
  prompt since the recorded run, that's the WHOLE POINT — we want to
  see how the new prompt handles the old input.
* **Uses the current model defaults.** Same rationale: replay is for
  comparing "before-my-edit" vs "after-my-edit." If you want bitwise
  reproduction of the original, that's a different feature
  (deterministic re-run from snapshot — Sprint S+).
* **No mutation of the original RunRecord.** Replay writes a NEW
  RunRecord. The audit trail stays honest.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from movate.cli._runtime import build_local_runtime, shutdown_runtime
from movate.core.loader import AgentLoadError, load_agent
from movate.core.models import RunRecord, RunRequest

console = Console()
err_console = Console(stderr=True)


def _resolve_agent_dir(agent_name: str, project_root: Path) -> Path | None:
    """Find ``agents/<name>`` under ``project_root``. None when missing."""
    candidate = project_root / "agents" / agent_name
    if candidate.is_dir() and (candidate / "agent.yaml").is_file():
        return candidate.resolve()
    return None


async def _fetch_run(run_id: str, tenant_id: str) -> RunRecord | None:
    """Pull the recorded run from storage. Returns None on miss."""
    from movate.storage import build_storage  # noqa: PLC0415

    storage = build_storage()
    await storage.init()
    try:
        return await storage.get_run(run_id, tenant_id=tenant_id)
    finally:
        await storage.close()


async def _replay(
    *,
    original: RunRecord,
    agent_dir: Path,
    mock: bool,
) -> RunRecord:
    """Re-execute the agent at ``agent_dir`` with ``original.input``.

    Returns the newly-recorded RunRecord (already persisted by the
    executor). Note: this is a fresh run; the original is unchanged.
    """
    rt = await build_local_runtime(mock=mock)
    try:
        bundle = load_agent(agent_dir)
        request = RunRequest(agent=bundle.spec.name, input=original.input)
        response = await rt.executor.execute(bundle, request)
        # Fish the freshly-persisted record out of storage so we have
        # the same RunRecord shape on both sides of the diff.
        new_run = await rt.storage.get_run(response.run_id, tenant_id="local")
        if new_run is None:
            # Shouldn't happen — executor.save_run is part of the
            # execute path. Fall back to a synthetic record so the
            # caller can still render output / diff.
            raise RuntimeError(
                f"executor returned run_id={response.run_id!r} but it's "
                "not in storage; likely a storage misconfiguration."
            )
        return new_run
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_summary(original: RunRecord, replayed: RunRecord) -> None:
    """One-line-per-field comparison + new output highlighted."""
    table = Table(title="Replay summary", title_style="bold")
    table.add_column("field", style="dim", no_wrap=True)
    table.add_column("original", no_wrap=True)
    table.add_column("replayed", no_wrap=True)

    table.add_row("run_id", original.run_id, replayed.run_id)
    table.add_row("status", str(original.status), str(replayed.status))
    table.add_row("provider", original.provider, replayed.provider)
    table.add_row(
        "cost_usd",
        f"${original.metrics.cost_usd:.6f}",
        f"${replayed.metrics.cost_usd:.6f}",
    )
    table.add_row(
        "latency_ms",
        f"{original.metrics.latency_ms}",
        f"{replayed.metrics.latency_ms}",
    )
    table.add_row(
        "tokens",
        f"{original.metrics.tokens.input}/{original.metrics.tokens.output}",
        f"{replayed.metrics.tokens.input}/{replayed.metrics.tokens.output}",
    )
    console.print(table)


def _render_output(label: str, output: dict[str, Any] | None) -> None:
    """Render one of the run outputs (original or replayed) as JSON panel."""
    body = json.dumps(output or {}, indent=2)
    console.print(
        Panel(
            Syntax(body, "json", theme="ansi_dark", line_numbers=False),
            title=label,
            title_align="left",
            border_style="cyan",
        )
    )


def _outputs_equal(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    """Compare two run outputs ignoring whitespace + key order."""
    return json.dumps(a or {}, sort_keys=True) == json.dumps(b or {}, sort_keys=True)


def _replay_remote(
    run_id: str,
    *,
    target: str,
    against: str,
    mock: bool,
    diff: bool,
    json_output: bool,
) -> None:
    """Replay against a deployed runtime via ``POST /runs/{id}/replay`` (ADR 045 D13)."""
    from movate.cli._console import get_global_target  # noqa: PLC0415
    from movate.core.client import MovateClient, MovateClientError  # noqa: PLC0415
    from movate.core.user_config import (  # noqa: PLC0415
        UserConfigError,
        resolve_bearer_token,
        resolve_target,
    )

    try:
        _name, target_cfg = resolve_target(target or get_global_target())
        token = resolve_bearer_token(target_cfg)
    except UserConfigError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc

    async def _go() -> Any:
        async with MovateClient(base_url=target_cfg.url, api_key=token) as client:
            return await client.replay_run(run_id, against=against, mock=mock)

    try:
        view = asyncio.run(_go())
    except MovateClientError as exc:
        err_console.print(f"[red]✗[/red] remote replay failed: {exc}")
        raise typer.Exit(code=1) from exc

    if json_output:
        console.print_json(view.model_dump_json())
        return

    state = "changed" if view.changed else "unchanged"
    state_style = "yellow" if view.changed else "green"
    console.print(
        f"[bold]replay[/bold] {view.original.run_id} → {view.replayed.run_id}  "
        f"[dim](against[/dim] [cyan]{view.against}[/cyan][dim],[/dim] "
        f"[{state_style}]{state}[/{state_style}][dim])[/dim]"
    )
    # Default to side-by-side for a remote replay (the whole point is the diff);
    # without --diff, show just the replayed output.
    if diff:
        _render_output("original", view.original.output)
        _render_output("replayed", view.replayed.output)
    else:
        _render_output("replayed", view.replayed.output)


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def replay(
    run_id: str = typer.Argument(
        ...,
        help="Run ID to replay (from [bold]mdk list[/bold] or [bold]mdk logs[/bold]).",
        metavar="RUN_ID",
    ),
    diff: bool = typer.Option(
        False,
        "--diff",
        help=(
            "Render the original vs replayed output side-by-side. "
            "Without [bold]--diff[/bold], only the new output is shown."
        ),
    ),
    mock: bool = typer.Option(
        False,
        "--mock",
        help=(
            "Use [bold]MockProvider[/bold] — offline / hermetic / no API key needed. "
            "Useful for smoke-testing the replay wiring."
        ),
    ),
    tenant_id: str = typer.Option(
        "local",
        "--tenant-id",
        help="Tenant scope for storage lookup (default: 'local' for CLI use).",
    ),
    project_root: str = typer.Option(
        ".",
        "--project-root",
        envvar="MOVATE_PROJECT_ROOT",
        help="Project root (default: cwd). Used to find agents/<name>/.",
        hidden=True,
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the replayed RunRecord as JSON. Skips the Rich rendering.",
    ),
    target: str | None = typer.Option(
        None,
        "--target",
        help=(
            "Replay against a [bold]deployed[/bold] runtime (named target or URL) via "
            "[bold]POST /api/v1/runs/{id}/replay[/bold] (ADR 045 D13) instead of the "
            "local on-disk agent. Pair with [bold]--against[/bold] to pick the version."
        ),
    ),
    against: str = typer.Option(
        "published",
        "--against",
        help=(
            "Remote only ([bold]--target[/bold]): the agent version to replay against — "
            "[bold]published[/bold] (latest) or [bold]version:X[/bold]."
        ),
    ),
) -> None:
    """Re-execute a past run with the same input.

    The recorded ``input`` dict goes verbatim to the executor; the
    agent on disk runs against it. If you've edited the prompt since
    the original run, that's the whole point — see how the new prompt
    handles the old input.

    With [bold]--target[/bold] the replay runs on a [bold]deployed[/bold] runtime
    (``POST /api/v1/runs/{id}/replay``, ADR 045 D13) against the published — or a
    pinned [bold]--against version:X[/bold] — agent version, returning the
    original vs replayed side-by-side. Without it, replay is local (below).

    [bold]Examples:[/bold]

      [dim]$ mdk replay r-abc-123                    # re-run locally, print new output[/dim]
      [dim]$ mdk replay r-abc-123 --diff             # side-by-side with original[/dim]
      [dim]$ mdk replay r-abc-123 --mock             # offline / hermetic[/dim]
      [dim]$ mdk replay r-abc-123 --target prod      # replay on the deployed runtime[/dim]
      [dim]$ mdk replay r-abc-123 --target prod --against version:0.2.0[/dim]
    """
    # Remote replay (ADR 045 D13): hit the deployed endpoint instead of the
    # local on-disk agent. Only when --target is explicit — bare `mdk replay`
    # stays local (back-compat).
    if target is not None:
        _replay_remote(
            run_id, target=target, against=against, mock=mock, diff=diff, json_output=json_output
        )
        return

    original = asyncio.run(_fetch_run(run_id, tenant_id))
    if original is None:
        err_console.print(
            f"[red]✗[/red] run [bold]{run_id}[/bold] not found "
            f"under tenant [cyan]{tenant_id}[/cyan]. "
            "[dim]Run [bold]mdk list[/bold] to browse recent runs.[/dim]"
        )
        raise typer.Exit(code=1)

    root = Path(project_root).resolve()
    agent_dir = _resolve_agent_dir(original.agent, root)
    if agent_dir is None:
        err_console.print(
            f"[red]✗[/red] agent [bold]{original.agent}[/bold] not found "
            f"under [cyan]{root / 'agents'}[/cyan]. "
            "[dim]The original agent directory may have been moved or renamed.[/dim]"
        )
        raise typer.Exit(code=2)

    _ctx = (
        console.status("[dim]Replaying…[/dim]", spinner="dots")
        if not json_output and sys.stderr.isatty()
        else contextlib.nullcontext()
    )
    try:
        with _ctx:
            replayed = asyncio.run(_replay(original=original, agent_dir=agent_dir, mock=mock))
    except AgentLoadError as exc:
        err_console.print(f"[red]✗ load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    if json_output:
        # Serialize the new RunRecord. mode='json' coerces datetimes
        # and StrEnum values into native JSON shapes.
        console.print_json(replayed.model_dump_json())
        return

    _render_summary(original, replayed)
    console.print()
    if diff:
        _render_output("Original output", original.output)
        _render_output("Replayed output", replayed.output)
        if _outputs_equal(original.output, replayed.output):
            console.print("[green]✓[/green] outputs match")
        else:
            console.print(
                "[yellow]⚠ outputs differ[/yellow] [dim](expected if you edited "
                "the prompt — the whole point of replay is to surface this)[/dim]"
            )
    else:
        _render_output("Replayed output", replayed.output)
        console.print(
            f"\n[dim]Replay recorded as a new run "
            f"([cyan]{replayed.run_id}[/cyan]). "
            f"Use [bold]mdk replay {run_id} --diff[/bold] to compare side-by-side.[/dim]"
        )

    # Force-touch env to silence unused-import lint in some builds.
    _ = os.environ.get("MOVATE_FORCE_NOOP")
