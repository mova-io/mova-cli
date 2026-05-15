"""``mdk audit`` — production-readiness scanner (Sprint N Day 8-10).

Final member of the K-state cluster. Scans a snapshot (or current
project state) for production-readiness issues:

  $ mdk audit current                   # scan live state
  $ mdk audit abc12345                  # scan a snapshot
  $ mdk audit current --strict          # warnings become errors (CI gate)
  $ mdk audit current --json            # for CI annotations
  $ mdk audit current --category exposed-secret   # one scanner only

Designed for CI gating: exit 0 = clean, exit 1 = findings. With
``--strict``, warnings also fail the build. Pairs with PR #6
(``mdk validate --project``) and ``mdk promote`` (Sprint O) —
audit a snapshot BEFORE promoting it to staging.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from movate.audit import (
    AuditReport,
    audit_current,
    audit_snapshot,
)
from movate.audit.report import Severity, sorted_findings
from movate.audit.scanners import SCANNERS
from movate.snapshot import SnapshotNotFoundError, SnapshotStoreError

console = Console()
err_console = Console(stderr=True)


def _resolve_project_root(explicit: Path | None) -> Path:
    """Walk-up resolution — same convention as snapshot_cmd / diff_cmd."""
    if explicit is not None:
        if not explicit.is_dir():
            err_console.print(f"[red]✗[/red] --project path is not a directory: {explicit}")
            raise typer.Exit(code=2)
        return explicit.resolve()
    current = Path.cwd().resolve()
    while True:
        if (current / "movate.yaml").is_file():
            return current
        if current.parent == current:
            break
        current = current.parent
    return Path.cwd().resolve()


def audit(
    target: str = typer.Argument(
        "current",
        help=(
            "What to audit: ``current`` (the live project state) or "
            "a snapshot hash / prefix. Defaults to ``current``."
        ),
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help=(
            "Promote warnings to errors. CI-friendly: require a clean "
            "bill of health before merging."
        ),
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON instead of a Rich table — pipe-friendly for CI annotations.",
    ),
    categories: list[str] = typer.Option(
        [],
        "--category",
        "-c",
        help=(
            f"Limit to specific scanner categories. Repeatable. "
            f"Valid: {', '.join(sorted(SCANNERS.keys()))}"
        ),
    ),
    project: Path | None = typer.Option(
        None,
        "--project",
        "-p",
        help="Project root. Defaults to walking up from cwd for movate.yaml.",
    ),
) -> None:
    """Run production-readiness scanners over the project or a snapshot.

    [bold]Categories shipped this sprint:[/bold]

      [dim]missing-evals      — agent has no evals/dataset.jsonl[/dim]
      [dim]missing-description — agent.yaml lacks `description:`[/dim]
      [dim]missing-owner       — agent.yaml lacks `owner:`[/dim]
      [dim]exposed-secret      — regex scan for committed credentials[/dim]
      [dim]empty-prompt        — prompt.md is empty / whitespace-only[/dim]
      [dim]no-test-signal      — no examples AND no dataset[/dim]

    [bold]Examples:[/bold]

      [dim]# Scan current project, default-mode (errors fail; warnings don't)[/dim]
      $ mdk audit

      [dim]# Strict CI gate — fail on warnings too[/dim]
      $ mdk audit current --strict

      [dim]# Audit a snapshot before promoting it[/dim]
      $ mdk audit abc12345

      [dim]# Limit to one scanner category[/dim]
      $ mdk audit current --category exposed-secret

      [dim]# JSON for CI annotations[/dim]
      $ mdk audit current --json | jq '.findings[] | select(.severity == "error")'
    """
    project_root = _resolve_project_root(project)

    # Validate scanner category names upfront so a typo doesn't
    # silently filter to "no scanners" and produce an empty report.
    if categories:
        invalid = [c for c in categories if c not in SCANNERS]
        if invalid:
            err_console.print(
                f"[red]✗[/red] unknown scanner category(ies): {invalid}. "
                f"Valid: {sorted(SCANNERS.keys())}"
            )
            raise typer.Exit(code=2)

    try:
        if target == "current":
            report = audit_current(project_root, categories=categories or None)
        else:
            report = audit_snapshot(project_root, target, categories=categories or None)
    except SnapshotNotFoundError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from None
    except SnapshotStoreError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    if json_output:
        # Write via stdout (not Rich) so piping to jq / CI annotation
        # parsers works cleanly — Rich injects ANSI escape codes that
        # break downstream consumers.
        import sys  # noqa: PLC0415

        sys.stdout.write(report.to_json() + "\n")
    else:
        _render_rich(report, target=target, strict=strict)

    # Gate semantics: errors always fail; warnings fail only with --strict.
    if report.gate_fails(strict=strict):
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


_SEVERITY_STYLE: dict[Severity, str] = {
    Severity.ERROR: "red",
    Severity.WARNING: "yellow",
    Severity.INFO: "cyan",
}

_SEVERITY_ICON: dict[Severity, str] = {
    Severity.ERROR: "✗",
    Severity.WARNING: "⚠",
    Severity.INFO: "i",  # plain lowercase i (ruff RUF001 — no ambiguous unicode)
}

# Map audit scanner categories to the `mdk fix --only <id>` command
# that would remediate them. Keeps the diagnose→fix loop tight:
# operators don't need to remember the fix id, the audit row carries
# it. Only listed for findings that have a 1:1 fix today; other
# categories render without a command line (operator fixes manually).
_CATEGORY_TO_FIX_COMMAND: dict[str, str] = {
    # v1 audit scanners (Sprint N) → fixes (Sprint P)
    "missing-evals": "mdk eval-gen <agent> --num 10",
    # v2 audit scanners (Sprint S) → fixes (Sprint P)
    # Note: `floating-model-tag` / `missing-version` / `missing-fallback`
    # / `prompt-too-long` / `schema-no-required` are operator-edits in
    # agent.yaml — no auto-fix maps to them. Hint stays the guide.
}


def _render_rich(report: AuditReport, *, target: str, strict: bool) -> None:
    """Render the audit report as a Rich panel + findings table."""
    if report.scanned_agents == 0:
        console.print(
            "[yellow]⚠[/yellow] no agents found to audit. Run [bold]mdk init[/bold] to create one."
        )
        return

    # Summary panel
    error_n = len(report.errors)
    warn_n = len(report.warnings)
    info_n = len(report.infos)

    if report.is_clean:
        title = "✓ Audit clean"
        border = "green"
        body = (
            f"[bold]target:[/bold]  {target}\n"
            f"[bold]agents:[/bold]  {report.scanned_agents}\n"
            f"[green]No production-readiness issues found.[/green]"
        )
    else:
        gate = "✗ blocks deploy" if report.gate_fails(strict=strict) else "⚠ non-blocking"
        title = f"Audit findings — {gate}"
        border = "red" if report.gate_fails(strict=strict) else "yellow"
        body = (
            f"[bold]target:[/bold]    {target}\n"
            f"[bold]agents:[/bold]    {report.scanned_agents}\n"
            f"[bold]findings:[/bold]  "
            f"[red]{error_n} error(s)[/red]  "
            f"[yellow]{warn_n} warning(s)[/yellow]  "
            f"[cyan]{info_n} info[/cyan]\n"
        )
        mode_str = "strict (warnings fail)" if strict else "default (warnings allowed)"
        body += f"[bold]mode:[/bold]      {mode_str}"

    console.print(Panel(body, title=title, title_align="left", border_style=border))

    if report.findings:
        console.print()
        table = Table(title="Findings", title_style="bold")
        table.add_column("Severity", no_wrap=True)
        table.add_column("Category", style="cyan", no_wrap=True)
        table.add_column("Target", style="bold", no_wrap=True)
        table.add_column("Message + hint", style="white")

        for finding in sorted_findings(report):
            style = _SEVERITY_STYLE[finding.severity]
            icon = _SEVERITY_ICON[finding.severity]
            sev_cell = f"[{style}]{icon} {finding.severity.value}[/{style}]"
            msg_cell = finding.message
            if finding.hint:
                msg_cell += f"\n[dim]hint: {finding.hint}[/dim]"
            # Close the diagnose→fix loop: if this finding maps to an
            # `mdk fix` scanner, surface the exact command operators
            # would otherwise have to look up + retype. Today's map
            # covers the categories that have a 1:1 fix; others render
            # without a command line.
            fix_cmd = _CATEGORY_TO_FIX_COMMAND.get(finding.category)
            if fix_cmd:
                msg_cell += f"\n[cyan]→ {fix_cmd}[/cyan]"
            table.add_row(sev_cell, finding.category, finding.target, msg_cell)

        console.print(table)

    # Greppable single-line summary at the very end. Operators piping
    # CI logs through `grep mdk_audit_summary` get one key=value line
    # they can parse without dealing with Rich panel borders.
    _print_summary_line(report, strict=strict)


def _print_summary_line(report: AuditReport, *, strict: bool) -> None:
    """Emit ``mdk_audit_summary: agents=N errors=N warnings=N ...`` line.

    Greppable + parseable. Stays distinct from the Rich panel above
    so machine-reading tools can pick this line without parsing
    surrounding decoration.
    """
    blocks = "true" if report.gate_fails(strict=strict) else "false"
    console.print(
        f"[dim]mdk_audit_summary: agents={report.scanned_agents} "
        f"errors={len(report.errors)} warnings={len(report.warnings)} "
        f"info={len(report.infos)} strict={str(strict).lower()} "
        f"blocks_deploy={blocks}[/dim]"
    )
