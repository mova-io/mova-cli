"""``mdk diagnose <agent>`` — kick off the Failure Pattern Diagnoser.

ADR 043 D1, CLI side. The CLI talks to a deployed runtime (the
``--target`` flag picks which one) and never runs the diagnoser
locally — diagnose lives in the execution plane behind the runtime
API (``POST /api/v1/agents/{name}/diagnose``).

Three surfaces:

* ``mdk diagnose <agent>`` — submit a new diagnose job and print the
  returned ``job_id`` + status URL. Use ``--wait`` to poll until
  the result is ready and pretty-print it.
* ``mdk diagnose show <agent> <job_id>`` — fetch + render a
  previously-submitted diagnose.
* ``mdk diagnose <workflow_run_id>`` — when the positional is
  UUID-shaped (workflow run ids are ``str(uuid4())``, ADR 054 D6;
  agent names are human slugs), the callback dispatches to the
  per-run post-mortem instead: evidence-cited "what happened and
  why" over the observability surfaces (facts API + Temporal
  history + Langfuse + sim-ledger + local spec), collected
  client-side with one optional LLM pass — see
  :mod:`movate.cli.diagnose_run_cmd`.

**Read-only with respect to agent state.** The diagnose phase proposes
typed fixes; it never modifies the agent's prompt / KB / context /
model. The apply step is ADR 043's follow-up PR.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from movate.cli._completion import complete_agent_name
from movate.cli._console import error, get_global_target, hint
from movate.cli._output import TableJson
from movate.cli.diagnose_run_cmd import run_postmortem
from movate.core.user_config import (
    UserConfigError,
    resolve_bearer_token,
    resolve_target,
)

stdout = Console()
err = Console(stderr=True)

diagnose_app = typer.Typer(
    help=(
        "Diagnose recent failures for an agent — cluster + propose typed fixes "
        "(ADR 043 D1, read-only with respect to the agent)."
    ),
    no_args_is_help=True,
    # A Click group defaults to allow_interspersed_args=False, which rejects
    # options placed AFTER the positional ("mdk diagnose <id> --no-llm" →
    # "No such command '--no-llm'"). Allow interspersed args so the natural
    # spelling parses; subcommand resolution ("show") is unaffected.
    context_settings={"allow_interspersed_args": True},
)

# A workflow run id is ``str(uuid4())`` (ADR 054 D6 — it doubles as the
# Temporal workflow id); agent names are human slugs. The callback uses this
# to dispatch ``mdk diagnose <workflow_run_id>`` to the per-run post-mortem.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@diagnose_app.callback(invoke_without_command=True)
def diagnose(
    ctx: typer.Context,
    agent: str = typer.Argument(
        None,
        help=(
            "Agent name registered on the target runtime — or a workflow run id "
            "(UUID), which dispatches to the per-run post-mortem instead."
        ),
        shell_complete=complete_agent_name,
    ),
    window_days: int = typer.Option(
        30,
        "--window-days",
        help="Lookback window in days. Failures older than this are ignored.",
    ),
    min_failures: int = typer.Option(
        5,
        "--min-failures",
        help="Cluster floor — clusters smaller than this are dropped from the result.",
    ),
    max_clusters: int = typer.Option(10, "--max-clusters", help="Maximum clusters to return."),
    include_eval_failures: bool = typer.Option(
        True,
        "--eval-failures/--no-eval-failures",
        help="Include failing eval records as failure signal.",
    ),
    include_drift_detections: bool = typer.Option(
        True,
        "--drift/--no-drift",
        help="Include drift detections (computed from eval history) as signal.",
    ),
    include_canary_misses: bool = typer.Option(
        True,
        "--canary/--no-canary",
        help="Include failed runs on the canary challenger as signal.",
    ),
    budget_usd: float = typer.Option(
        1.0,
        "--budget-usd",
        help="Hard spend cap for the diagnoser's LLM call. Estimated > budget = error.",
    ),
    model: str | None = typer.Option(
        None, "--model", help="Optional provider/model override (e.g. openai/gpt-4o-mini)."
    ),
    no_llm: bool = typer.Option(
        False,
        "--no-llm",
        help=(
            "(run post-mortem only) skip the LLM diagnosis pass; print the "
            "structured evidence report."
        ),
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help=(
            "(run post-mortem only) emit the full {workflow_run_id, evidence, "
            "diagnosis} JSON object."
        ),
    ),
    target: str | None = typer.Option(
        None,
        "--target",
        "-t",
        help="Deployment target name (from `mdk config list-targets`). "
        "Omit to use the active target.",
    ),
    wait: bool = typer.Option(
        False,
        "--wait",
        "-w",
        help="Block until the diagnose completes, then pretty-print the result.",
    ),
    poll_interval: float = typer.Option(
        1.0, "--poll-interval", help="Seconds between status polls (--wait only)."
    ),
    timeout: float = typer.Option(
        300.0,
        "--timeout",
        help="Max seconds to wait when --wait is set; after this CLI exits 124.",
    ),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """Diagnose an [bold]<agent>[/bold]'s recent failures — or post-mortem ONE run.

    With an agent name: cluster recent failures and propose typed fixes
    (ADR 043, runs on the target runtime). With a workflow run id (UUID):
    evidence-cited post-mortem of that single run — facts + Temporal history +
    Langfuse + sim-ledger collected client-side, then one LLM pass whose every
    claim cites evidence ids ([bold]--no-llm[/bold] for the evidence report
    only, [bold]--json[/bold] for the machine-readable object).
    """
    # Don't run as a default command if a subcommand was invoked
    # (e.g. ``mdk diagnose show ...``) — Typer's ``invoke_without_command``
    # callback fires either way, so guard explicitly.
    if ctx.invoked_subcommand is not None:
        return
    if agent is None:
        error("missing AGENT argument")
        raise typer.Exit(code=2)

    if _UUID_RE.match(agent):
        # ``mdk diagnose <workflow_run_id>`` — the per-run post-mortem
        # (collected client-side; does not touch the agent diagnoser API).
        run_postmortem(
            agent,
            target=target,
            no_llm=no_llm,
            json_output=json_output or output_format == TableJson.JSON,
            model=model,
        )
        return

    try:
        target_name, target_cfg = resolve_target(target or get_global_target())
        token = resolve_bearer_token(target_cfg)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None

    asyncio.run(
        _submit_diagnose(
            base_url=target_cfg.url,
            token=token,
            target_name=target_name,
            agent=agent,
            body={
                "window_days": window_days,
                "min_failure_count": min_failures,
                "max_clusters": max_clusters,
                "include_eval_failures": include_eval_failures,
                "include_drift_detections": include_drift_detections,
                "include_canary_misses": include_canary_misses,
                "budget_usd": budget_usd,
                **({"model": model} if model else {}),
            },
            wait=wait,
            poll_interval=poll_interval,
            timeout=timeout,
            output_format=output_format,
        )
    )


@diagnose_app.command("show")
def show(
    agent: str = typer.Argument(
        ...,
        help="Agent name (informational; resolution is by job_id).",
        shell_complete=complete_agent_name,
    ),
    job_id: str = typer.Argument(..., help="The diagnose job id (from a prior `mdk diagnose`)."),
    target: str | None = typer.Option(
        None,
        "--target",
        "-t",
        help="Deployment target name. Omit to use the active target.",
    ),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """Fetch + render a previously-submitted diagnose."""
    try:
        _target_name, target_cfg = resolve_target(target or get_global_target())
        token = resolve_bearer_token(target_cfg)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None

    asyncio.run(
        _show_diagnose(
            base_url=target_cfg.url,
            token=token,
            agent=agent,
            job_id=job_id,
            output_format=output_format,
        )
    )


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------

# HTTP status constants — keeps comparisons grep-friendly + lint-clean.
_HTTP_OK = 200
_HTTP_ACCEPTED = 202


async def _submit_diagnose(
    *,
    base_url: str,
    token: str,
    target_name: str,
    agent: str,
    body: dict[str, Any],
    wait: bool,
    poll_interval: float,
    timeout: float,
    output_format: TableJson,
) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"), headers=headers, timeout=30.0
    ) as client:
        try:
            r = await client.post(f"/api/v1/agents/{agent}/diagnose", json=body)
        except httpx.HTTPError as exc:
            error(f"network error: {exc}")
            raise typer.Exit(code=1) from None

        if r.status_code != _HTTP_ACCEPTED:
            _emit_error_from_response(r)
            raise typer.Exit(code=1)

        accepted = r.json()
        job_id = accepted["job_id"]

        if not wait:
            stdout.print(json.dumps(accepted), soft_wrap=True, highlight=False)
            hint(
                f"[dim]submitted diagnose {job_id} on {target_name}. "
                f"Poll with: mdk diagnose show {agent} {job_id}"
                + (f" -t {target_name}" if target_name != "local" else "")
                + "[/dim]"
            )
            return

        # --wait mode: poll the GET endpoint.
        terminal = await _poll_until_terminal(
            client, job_id, poll_interval=poll_interval, timeout=timeout
        )
        _render_diagnose(terminal, output_format=output_format)
        if terminal["status"] != "completed":
            raise typer.Exit(code=1)


async def _show_diagnose(
    *,
    base_url: str,
    token: str,
    agent: str,
    job_id: str,
    output_format: TableJson,
) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"), headers=headers, timeout=30.0
    ) as client:
        try:
            r = await client.get(f"/api/v1/diagnoses/{job_id}")
        except httpx.HTTPError as exc:
            error(f"network error: {exc}")
            raise typer.Exit(code=1) from None
        if r.status_code != _HTTP_OK:
            _emit_error_from_response(r)
            raise typer.Exit(code=1)
        body = r.json()
        # ``agent`` from the CLI argument is informational; the server's
        # ``agent_name`` is authoritative — mention it on a mismatch so
        # the operator notices.
        if body.get("agent_name") and body["agent_name"] != agent:
            hint(
                f"[dim]note: server-reported agent_name="
                f"{body['agent_name']!r} differs from your argument {agent!r}.[/dim]"
            )
        _render_diagnose(body, output_format=output_format)


async def _poll_until_terminal(
    client: httpx.AsyncClient,
    job_id: str,
    *,
    poll_interval: float,
    timeout: float,
) -> dict[str, Any]:
    elapsed = 0.0
    while elapsed < timeout:
        r = await client.get(f"/api/v1/diagnoses/{job_id}")
        if r.status_code != _HTTP_OK:
            _emit_error_from_response(r)
            raise typer.Exit(code=1)
        body: dict[str, Any] = r.json()
        if body["status"] != "running":
            return body
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    error(f"diagnose {job_id} timed out after {timeout:.0f}s")
    raise typer.Exit(code=124)


def _render_diagnose(view: dict[str, Any], *, output_format: TableJson) -> None:
    if output_format == TableJson.JSON:
        stdout.print(json.dumps(view, indent=2), soft_wrap=True, highlight=False)
        return

    status = view.get("status", "?")
    job_id_short = str(view.get("job_id", "?"))[:12]
    header = f"diagnose {job_id_short}  agent={view.get('agent_name')}  status={status}"
    if status == "error":
        err_info = view.get("error") or {}
        stdout.print(
            Panel.fit(
                f"[red]{err_info.get('type', 'error')}[/red]: {err_info.get('message', '')}",
                title=header,
            )
        )
        return

    result = view.get("result") or {}
    summary = result.get("input_summary", {})
    clusters = result.get("clusters", [])

    summary_table = Table(title=header, show_header=False)
    summary_table.add_column("field", style="dim")
    summary_table.add_column("value")
    summary_table.add_row("failures_examined", str(summary.get("total_failures_examined", 0)))
    summary_table.add_row(
        "clusters_identified", str(summary.get("clusters_identified", len(clusters)))
    )
    summary_table.add_row("tokens_used", str(view.get("tokens_used", 0)))
    summary_table.add_row("cost_usd", f"${view.get('cost_usd', 0.0):.4f}")
    summary_table.add_row("model", view.get("model", ""))
    stdout.print(summary_table)

    if not clusters:
        hint("[dim]no clusters returned — nothing to propose.[/dim]")
        return

    for cluster in clusters:
        fix = cluster.get("proposed_fix", {})
        kind = fix.get("kind", "?")
        rationale = fix.get("rationale", "")
        body_lines = [
            f"[bold]{cluster.get('summary', '?')}[/bold]",
            (
                f"  example_count={cluster.get('example_count', 0)}  "
                f"confidence={cluster.get('confidence', '?')}"
            ),
            f"  examples: {', '.join(cluster.get('example_run_ids', []))[:120]}",
            "",
            f"  fix kind: [cyan]{kind}[/cyan]",
            f"  rationale: {rationale}",
        ]
        ei = fix.get("expected_improvement") or {}
        if ei:
            body_lines.append(
                f"  expected: {ei.get('metric', '?')} Δ={ei.get('delta', 0.0):+.4f} "
                f"({ei.get('based_on', '')})"
            )
        stdout.print(Panel("\n".join(body_lines), title=f"cluster {cluster.get('id', '?')}"))


def _emit_error_from_response(r: httpx.Response) -> None:
    try:
        payload = r.json()
        detail = payload.get("detail", {})
        if isinstance(detail, dict):
            err_obj = detail.get("error", {})
            msg = err_obj.get("message") or detail.get("message") or r.text
        else:
            msg = str(detail)
    except Exception:
        msg = r.text
    error(f"HTTP {r.status_code}: {msg}")
