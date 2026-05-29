"""``mdk audit-llm`` — Claude-orchestrated audit endpoint client.

Companion to ``mdk audit`` (which runs *static* scanners locally over a
project / snapshot). This command calls the runtime endpoint
``POST /api/v1/agents/{name}/audit/from-llm`` (or the project-scoped
``POST /api/v1/projects/{project_id}/audit/from-llm``) and surfaces
the structured findings grouped by severity then category.

The endpoint is READ-ONLY: nothing is written to the agent registry,
prompt, contexts, schemas, or eval dataset — it only produces a
findings report.

Usage::

    mdk audit-llm <agent> [--category ...] [--severity-floor warn] \\
        --target <env>
    mdk audit-llm project <project_id> [--categories ...] --target <env>
    mdk audit-llm <agent> --target dev --json

Designed to mirror the existing ``mdk audit`` rendering (grouped table
+ JSON-out for CI), but the *findings* come from a Claude-orchestrated
audit, not local static scanners.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()
err_console = Console(stderr=True)

# CLI surface — matches the spec wire (the seven audit categories that ship
# in v1). Kept in sync with movate.core.auditor.CATEGORIES; if a future
# category is added there, the CLI silently allows it (the runtime
# validates).
_VALID_CATEGORIES: tuple[str, ...] = (
    "ambiguous_prompts",
    "missing_eval_coverage",
    "security_smells",
    "cost_outliers",
    "kb_quality",
    "schema_drift",
    "model_choice",
)

_VALID_SEVERITIES: tuple[str, ...] = ("info", "warn", "error", "critical")


audit_llm_app = typer.Typer(
    no_args_is_help=True,
    help="Run a Claude-orchestrated read-only audit against a runtime target.",
)


def _resolve_runtime(target: str) -> tuple[str, str]:
    """Resolve a ``--target <env>`` flag to a (base_url, api_key) pair.

    Re-uses the same target-resolution as ``mdk auth list-keys``
    (``movate.config.resolve_target``). Errors are surfaced via
    ``typer.Exit(2)`` with a one-line operator pointer.
    """
    try:
        from movate.config import resolve_target  # type: ignore[import-untyped]  # noqa: PLC0415
    except ImportError:
        # Older user_config layout.
        from movate.core.user_config import (  # noqa: PLC0415
            resolve_target,
        )

    try:
        _, target_cfg = resolve_target(target)
    except Exception as exc:
        err_console.print(f"[red]✗[/red] unknown --target {target!r}: {exc}")
        raise typer.Exit(code=2) from None

    api_key = os.environ.get(target_cfg.key_env, "").strip()
    base_url = target_cfg.url.rstrip("/")
    if not api_key:
        err_console.print(
            f"[red]✗[/red] env var ${target_cfg.key_env} is empty. "
            f"Run 'mdk auth refresh-runtime-key {target}'."
        )
        raise typer.Exit(code=2)
    return base_url, api_key


def _poll_until_terminal(
    *,
    client: httpx.Client,
    base_url: str,
    api_key: str,
    job_id: str,
    max_wait_s: float = 120.0,
    interval_s: float = 0.5,
) -> dict[str, Any]:
    """Poll the standard ``GET /api/v1/jobs/{job_id}`` until terminal.

    Returns the job's JSON payload. Operators waiting on a long audit
    can ``Ctrl-C`` to abandon polling (the audit keeps running
    server-side and the result is retrievable via
    ``GET /api/v1/audits/{audit_id}``)."""
    start = time.monotonic()
    headers = {"Authorization": f"Bearer {api_key}"}
    while True:
        resp = client.get(f"{base_url}/api/v1/jobs/{job_id}", headers=headers)
        resp.raise_for_status()
        payload: dict[str, Any] = resp.json()
        status = payload.get("status", "queued")
        if status in ("success", "error", "safety_blocked", "dead_letter", "cancelled"):
            return payload
        if time.monotonic() - start > max_wait_s:
            err_console.print(
                f"[yellow]⚠[/yellow] audit job {job_id} still running after "
                f"{max_wait_s:.0f}s; fetch later with "
                f"'mdk audit-llm get {job_id} --target ...'."
            )
            raise typer.Exit(code=1)
        time.sleep(interval_s)


def _post_audit_and_wait(
    *,
    base_url: str,
    api_key: str,
    route: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """POST the audit request, poll for terminal status, then fetch the
    rich :class:`AuditJobView` payload. Returns the audit view dict.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
        try:
            resp = client.post(f"{base_url}{route}", json=body, headers=headers)
        except httpx.HTTPError as exc:
            err_console.print(f"[red]✗[/red] could not reach {base_url}: {exc}")
            raise typer.Exit(code=2) from None
        if resp.status_code == httpx.codes.UNAUTHORIZED:
            err_console.print("[red]✗[/red] 401 Unauthorized — key invalid or expired.")
            raise typer.Exit(code=2)
        if resp.status_code == httpx.codes.NOT_FOUND:
            err_console.print(f"[red]✗[/red] 404 — {resp.json().get('detail', resp.text[:200])}")
            raise typer.Exit(code=2)
        if resp.status_code not in (httpx.codes.OK, httpx.codes.ACCEPTED):
            err_console.print(f"[red]✗[/red] HTTP {resp.status_code}: {resp.text[:300]!r}")
            raise typer.Exit(code=2)
        accepted = resp.json()
        job_id = accepted.get("job_id")
        if not job_id:
            err_console.print(f"[red]✗[/red] no job_id in response: {accepted!r}")
            raise typer.Exit(code=2)

        terminal = _poll_until_terminal(
            client=client, base_url=base_url, api_key=api_key, job_id=job_id
        )
        if terminal.get("status") != "success":
            err_info = terminal.get("error") or {}
            err_console.print(
                f"[red]✗[/red] audit failed: {err_info.get('message', terminal.get('status'))}"
            )
            raise typer.Exit(code=1)
        audit_id = terminal.get("result_run_id")
        if not audit_id:
            err_console.print(f"[red]✗[/red] no audit_id on terminal job: {terminal!r}")
            raise typer.Exit(code=1)
        fetch = client.get(f"{base_url}/api/v1/audits/{audit_id}", headers=headers)
        fetch.raise_for_status()
        view: dict[str, Any] = fetch.json()
        return view


# ---------------------------------------------------------------------------
# Rendering — mirrors the existing ``mdk audit`` Rich layout (grouped by
# severity then category, with location jump hints).
# ---------------------------------------------------------------------------


_SEVERITY_STYLE: dict[str, str] = {
    "critical": "bright_red",
    "error": "red",
    "warn": "yellow",
    "info": "cyan",
}

_SEVERITY_ICON: dict[str, str] = {
    "critical": "‼",
    "error": "✗",
    "warn": "⚠",
    "info": "i",
}


def _format_location(loc: dict[str, Any] | None) -> str:
    if not loc:
        return ""
    kind: str = str(loc.get("kind", ""))
    line = loc.get("line")
    path = loc.get("path")
    chunk_id = loc.get("chunk_id")
    if kind == "prompt_line" and line is not None:
        return f"prompt.md:{line}"
    if path:
        return str(path) + (f":{line}" if line is not None else "")
    if chunk_id:
        return f"chunk:{chunk_id}"
    return kind


def _render_rich(view: dict[str, Any]) -> None:
    """Render the AuditJobView payload as Rich panel + findings table."""
    scope = view.get("scope", {}) or {}
    summary = view.get("summary", {}) or {}
    by_sev = summary.get("by_severity", {}) or {}
    by_cat = summary.get("by_category", {}) or {}
    total = summary.get("total_findings", 0)
    partial = view.get("partial", False)
    cost = view.get("cost_usd", 0.0)
    tokens = view.get("tokens_used", 0)

    border = "green" if total == 0 else _SEVERITY_STYLE.get(_dominant_severity(by_sev), "yellow")
    title = "✓ Audit clean" if total == 0 else "Audit findings"
    if partial:
        title += " (partial — budget hit)"

    body = (
        f"[bold]scope:[/bold]      {scope.get('type', '?')}:{scope.get('id', '?')}\n"
        f"[bold]model:[/bold]      {view.get('model', '?')}\n"
        f"[bold]findings:[/bold]   {total} "
        f"([red]{by_sev.get('critical', 0)} crit[/red] / "
        f"[red]{by_sev.get('error', 0)} err[/red] / "
        f"[yellow]{by_sev.get('warn', 0)} warn[/yellow] / "
        f"[cyan]{by_sev.get('info', 0)} info[/cyan])\n"
        f"[bold]cost:[/bold]       ${cost:.4f} ({tokens} tokens)"
    )
    console.print(Panel(body, title=title, title_align="left", border_style=border))

    findings = view.get("findings", []) or []
    if not findings:
        return

    table = Table(title="Findings", title_style="bold")
    table.add_column("Severity", no_wrap=True)
    table.add_column("Category", style="cyan", no_wrap=True)
    table.add_column("Agent", style="bold", no_wrap=True)
    table.add_column("Location", style="dim")
    table.add_column("Finding")

    # Group sort: severity desc, then category.
    _sev_order = {"critical": 0, "error": 1, "warn": 2, "info": 3}
    sorted_findings = sorted(
        findings,
        key=lambda f: (_sev_order.get(f.get("severity", "info"), 99), f.get("category", "")),
    )
    for f in sorted_findings:
        sev = f.get("severity", "info")
        style = _SEVERITY_STYLE.get(sev, "white")
        icon = _SEVERITY_ICON.get(sev, "?")
        sev_cell = f"[{style}]{icon} {sev}[/{style}]"
        msg = f.get("title", "")
        desc = f.get("description", "")
        if desc:
            msg += f"\n[dim]{desc}[/dim]"
        suggestion = f.get("suggestion", "")
        if suggestion:
            msg += f"\n[green]→ {suggestion}[/green]"
        table.add_row(
            sev_cell,
            f.get("category", ""),
            f.get("agent_name", ""),
            _format_location(f.get("location")),
            msg,
        )
    console.print(table)
    # Greppable summary line — same pattern as ``mdk audit``.
    console.print(
        f"[dim]mdk_audit_llm_summary: scope={scope.get('type', '?')}:{scope.get('id', '?')} "
        f"total={total} crit={by_sev.get('critical', 0)} err={by_sev.get('error', 0)} "
        f"warn={by_sev.get('warn', 0)} info={by_sev.get('info', 0)} "
        f"cost_usd={cost:.4f} categories={','.join(sorted(by_cat.keys()))}[/dim]"
    )


def _dominant_severity(by_sev: dict[str, int]) -> str:
    for sev in ("critical", "error", "warn", "info"):
        if by_sev.get(sev, 0):
            return sev
    return "info"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@audit_llm_app.command("agent")
def audit_agent(
    agent: str = typer.Argument(..., help="Agent name to audit."),
    target: str = typer.Option(
        ...,
        "--target",
        "-t",
        help="Runtime target the audit endpoint lives on (resolved via movate.config).",
    ),
    categories: list[str] = typer.Option(
        [],
        "--category",
        "-c",
        help=(
            f"Limit to specific categories. Repeatable. "
            f"Valid: {', '.join(_VALID_CATEGORIES)}. Default: all."
        ),
    ),
    severity_floor: str = typer.Option(
        "info",
        "--severity-floor",
        help=f"Filter floor. One of: {', '.join(_VALID_SEVERITIES)}.",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Override the audit sub-agents' provider string (defaults to a tenant default).",
    ),
    budget_usd: float = typer.Option(
        1.0,
        "--budget-usd",
        help="Server-side spend cap. 0.0 = no cap. Default: $1.00.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the audit view as JSON (machine-readable, pipe-friendly).",
    ),
) -> None:
    """Run a Claude-orchestrated audit of one agent against a runtime."""
    if severity_floor not in _VALID_SEVERITIES:
        err_console.print(
            f"[red]✗[/red] invalid --severity-floor {severity_floor!r}; "
            f"choose from {_VALID_SEVERITIES}."
        )
        raise typer.Exit(code=2)
    invalid = [c for c in categories if c not in _VALID_CATEGORIES]
    if invalid:
        err_console.print(
            f"[red]✗[/red] unknown --category {invalid!r}; valid: {sorted(_VALID_CATEGORIES)}."
        )
        raise typer.Exit(code=2)

    base_url, api_key = _resolve_runtime(target)
    body: dict[str, Any] = {
        "severity_floor": severity_floor,
        "budget_usd": budget_usd,
    }
    if categories:
        body["categories"] = list(categories)
    if model:
        body["model"] = model

    view = _post_audit_and_wait(
        base_url=base_url,
        api_key=api_key,
        route=f"/api/v1/agents/{agent}/audit/from-llm",
        body=body,
    )

    if json_output:
        # Stdout (not Rich) for clean pipe-to-jq.
        sys.stdout.write(json.dumps(view, indent=2) + "\n")
        return
    _render_rich(view)


@audit_llm_app.command("project")
def audit_project(
    project_id: str = typer.Argument(..., help="Project id to audit."),
    target: str = typer.Option(..., "--target", "-t", help="Runtime target."),
    categories: list[str] = typer.Option(
        [],
        "--category",
        "-c",
        help=f"Repeatable. Valid: {', '.join(_VALID_CATEGORIES)}.",
    ),
    severity_floor: str = typer.Option(
        "info", "--severity-floor", help=f"One of {', '.join(_VALID_SEVERITIES)}."
    ),
    model: str | None = typer.Option(None, "--model"),
    budget_usd: float = typer.Option(2.0, "--budget-usd"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Run a Claude-orchestrated audit across every agent in a project."""
    if severity_floor not in _VALID_SEVERITIES:
        err_console.print(f"[red]✗[/red] invalid --severity-floor {severity_floor!r}.")
        raise typer.Exit(code=2)
    invalid = [c for c in categories if c not in _VALID_CATEGORIES]
    if invalid:
        err_console.print(f"[red]✗[/red] unknown --category {invalid!r}.")
        raise typer.Exit(code=2)

    base_url, api_key = _resolve_runtime(target)
    body: dict[str, Any] = {
        "severity_floor": severity_floor,
        "budget_usd": budget_usd,
    }
    if categories:
        body["categories"] = list(categories)
    if model:
        body["model"] = model

    view = _post_audit_and_wait(
        base_url=base_url,
        api_key=api_key,
        route=f"/api/v1/projects/{project_id}/audit/from-llm",
        body=body,
    )

    if json_output:
        sys.stdout.write(json.dumps(view, indent=2) + "\n")
        return
    _render_rich(view)
