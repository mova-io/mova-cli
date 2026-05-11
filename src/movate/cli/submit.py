"""``movate submit`` — queue a job at a deployed runtime.

Pairs with ``movate config add-target`` (CLI knows which runtime to
talk to and which env var holds the bearer token).

Two modes:

* **Fire-and-forget** (default) — prints the ``job_id`` and exits. Use
  for batch / scripted submission where you'll poll later or check
  ``movate jobs list``.
* **Wait** (``--wait``) — blocks, polls every second, prints the
  terminal state. Adds a desktop notification on completion via
  ``--notify`` (macOS / Linux) so the operator can walk away.

Distinct from ``movate run``: that runs an agent *locally* against
the configured provider. ``movate submit`` queues a job at a *remote*
runtime that may execute on different infra entirely.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._progress import spinner
from movate.core.client import MovateClient, MovateClientError
from movate.core.models import JobKind, JobStatus
from movate.core.user_config import (
    UserConfigError,
    resolve_bearer_token,
    resolve_target,
)
from movate.runtime.schemas import JobView

stdout = Console()
err = Console(stderr=True)


def submit(
    agent: str = typer.Argument(
        ...,
        help=(
            "Agent or workflow name registered on the target runtime "
            "(see `movate jobs list-agents`)."
        ),
    ),
    input_arg: str = typer.Argument(
        None,
        metavar="INPUT",
        help="Input as JSON object, file path, or '-' for stdin. "
        "If the agent has exactly one required string field, a plain string is auto-wrapped.",
    ),
    input_flag: str = typer.Option(None, "--input", "-i", help="Alternative way to pass input."),
    kind: str = typer.Option("agent", "--kind", "-k", help="`agent` or `workflow`."),
    target: str = typer.Option(
        None,
        "--target",
        "-t",
        help=(
            "Deployment target name (from `movate config list-targets`). "
            "Omit to use the active target."
        ),
    ),
    wait: bool = typer.Option(
        False,
        "--wait",
        "-w",
        help="Block until the job reaches a terminal state, then print the result.",
    ),
    timeout: float = typer.Option(
        300.0,
        "--timeout",
        help=(
            "Max seconds to wait when --wait is set. "
            "After this the job continues server-side; CLI exits 124."
        ),
    ),
    poll_interval: float = typer.Option(
        1.0, "--poll-interval", help="Seconds between job-status polls (--wait only)."
    ),
    notify: bool = typer.Option(
        False,
        "--notify",
        help="Desktop notification when --wait completes (macOS terminal-notifier "
        "/ osascript, Linux notify-send). No-op on unsupported platforms.",
    ),
    notify_email: str = typer.Option(
        None,
        "--notify-email",
        help=(
            "Email address the server-side worker emails when the job "
            "reaches a terminal status. Worker must have SMTP configured "
            "(MOVATE_SMTP_HOST + creds) or it falls back to logging only."
        ),
    ),
    output_format: str = typer.Option("table", "--output", "-o", help="table | json"),
) -> None:
    """Queue a job at a deployed runtime and (optionally) wait for completion.

    [bold]Examples:[/bold]

      [dim]# Fire-and-forget against the active target[/dim]
      $ movate submit faq-agent '{"text": "what is movate?"}'
      → prints {"job_id": "...", "status": "queued"} on stdout

      [dim]# Wait for completion + desktop chime when done[/dim]
      $ movate submit faq-agent '{"text": "..."}' --wait --notify

      [dim]# Workflow kind, against prod[/dim]
      $ movate submit returns-pipeline -t prod -k workflow -i initial_state.json
    """
    try:
        kind_enum = JobKind(kind)
    except ValueError as exc:
        err.print(f"[red]✗[/red] kind must be 'agent' or 'workflow'; got {kind!r}")
        raise typer.Exit(code=2) from exc

    raw = input_flag or input_arg
    if raw is None:
        err.print("[red]✗[/red] provide input as a positional arg, --input, or '-' for stdin")
        raise typer.Exit(code=2)
    payload = _coerce_input(raw)

    try:
        target_name, target_cfg = resolve_target(target)
        token = resolve_bearer_token(target_cfg)
    except UserConfigError as exc:
        err.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    asyncio.run(
        _submit(
            target_name=target_name,
            base_url=target_cfg.url,
            token=token,
            kind=kind_enum,
            agent=agent,
            input_payload=payload,
            wait=wait,
            timeout=timeout,
            poll_interval=poll_interval,
            notify=notify,
            notify_email=notify_email,
            output_format=output_format,
        )
    )


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------


async def _submit(
    *,
    target_name: str,
    base_url: str,
    token: str,
    kind: JobKind,
    agent: str,
    input_payload: dict[str, Any],
    wait: bool,
    timeout: float,
    poll_interval: float,
    notify: bool,
    notify_email: str | None,
    output_format: str,
) -> None:
    async with MovateClient(base_url=base_url, api_key=token) as client:
        try:
            with spinner(f"submitting to {target_name}..."):
                accepted = await client.submit_job(
                    kind=kind,
                    target=agent,
                    input=input_payload,
                    notify_email=notify_email,
                )
        except MovateClientError as exc:
            err.print(f"[red]✗ submit failed:[/red] {exc}")
            raise typer.Exit(code=1) from None

        if not wait:
            # Fire-and-forget: bare JSON on stdout (parsable; pipe-friendly).
            stdout.print(accepted.model_dump_json(), soft_wrap=True, highlight=False)
            err.print(
                f"[dim]queued {accepted.job_id} on {target_name}. "
                f"Poll with: movate jobs show {accepted.job_id}"
                + (f" -t {target_name}" if target_name != "local" else "")
                + "[/dim]"
            )
            return

        # --wait mode: block on terminal.
        try:
            with spinner("waiting for terminal state..."):
                final = await client.wait_for_terminal(
                    accepted.job_id,
                    poll_interval_seconds=poll_interval,
                    max_wait_seconds=timeout,
                )
        except TimeoutError as exc:
            err.print(f"[yellow]⏱[/yellow] {exc}")
            # 124 is the conventional `timeout` exit code; reuse it so
            # bash scripts can branch on it.
            raise typer.Exit(code=124) from None
        except MovateClientError as exc:
            err.print(f"[red]✗ poll failed:[/red] {exc}")
            raise typer.Exit(code=1) from None

        _emit_terminal(final, output_format=output_format)

        if notify:
            _desktop_notify(final, target_name=target_name)

        # Exit 1 on terminal-but-failed so CI scripts can branch.
        if final.status != JobStatus.SUCCESS:
            raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _emit_terminal(view: JobView, *, output_format: str) -> None:
    if output_format == "json":
        stdout.print(view.model_dump_json(indent=2), soft_wrap=True, highlight=False)
        return

    icon = {
        JobStatus.SUCCESS: "[green]✓[/green]",
        JobStatus.ERROR: "[red]✗[/red]",
        JobStatus.SAFETY_BLOCKED: "[yellow]⊘[/yellow]",
    }.get(view.status, "?")
    table = Table(title=f"{icon} {view.kind.value}/{view.target}", show_header=False)
    table.add_column("field", style="dim")
    table.add_column("value")
    table.add_row("job_id", view.job_id)
    table.add_row("status", view.status.value)
    if view.result_run_id:
        table.add_row("run_id", view.result_run_id)
    if view.completed_at and view.claimed_at:
        ms = int((view.completed_at - view.claimed_at).total_seconds() * 1000)
        table.add_row("duration", f"{ms}ms (claim → terminal)")
    if view.error:
        table.add_row("error", f"{view.error.type}: {view.error.message}")
    stdout.print(table)


# ---------------------------------------------------------------------------
# Desktop notification (local fallback for the 90% dev-team case until
# server-side SMS/email lands)
# ---------------------------------------------------------------------------


def _desktop_notify(view: JobView, *, target_name: str) -> None:
    """Pop a desktop notification + play a sound. Best-effort.

    Detects platform; uses the most likely tool present. Failures
    log to stderr but don't raise — the notification is a courtesy,
    not load-bearing.
    """
    title = "movate"
    summary = f"{view.kind.value}/{view.target} on {target_name}: {view.status.value}"

    try:
        if sys.platform == "darwin":
            # macOS: osascript is always present. terminal-notifier is
            # nicer (better icons + clickable) but optional.
            if shutil.which("terminal-notifier"):
                subprocess.run(
                    [
                        "terminal-notifier",
                        "-title",
                        title,
                        "-message",
                        summary,
                        "-sound",
                        "Glass",
                    ],
                    check=False,
                    capture_output=True,
                )
            else:
                # AppleScript's display notification: no clicks, but
                # ubiquitous. We escape quotes to avoid breaking the
                # one-liner.
                msg = summary.replace('"', '\\"')
                ttl = title.replace('"', '\\"')
                subprocess.run(
                    [
                        "osascript",
                        "-e",
                        f'display notification "{msg}" with title "{ttl}" sound name "Glass"',
                    ],
                    check=False,
                    capture_output=True,
                )
        elif sys.platform.startswith("linux") and shutil.which("notify-send"):
            subprocess.run(["notify-send", title, summary], check=False, capture_output=True)
        elif sys.platform == "win32":
            # Windows toast notifications need a third-party package
            # (win10toast / windows-toasts). Out of scope for v0.5;
            # fall through to "no-op + hint".
            err.print(
                "[dim]--notify: Windows desktop notifications require "
                "win10toast; install + integrate in a follow-up.[/dim]"
            )
            return
        else:
            err.print("[dim]--notify: unsupported platform; skipping desktop notification.[/dim]")
            return
    except Exception as exc:  # courtesy notification; never fatal
        err.print(f"[dim]--notify: desktop notification failed ({exc}); continuing.[/dim]")


# ---------------------------------------------------------------------------
# Input coercion — same rules as `movate run`
# ---------------------------------------------------------------------------


def _coerce_input(arg: str) -> dict[str, Any]:
    """Stdin / file / JSON-object. No string-auto-wrap here — the
    agent's input schema lives on the server side, not on the client,
    so we can't safely auto-wrap; callers pass explicit JSON.

    Detection order:

    1. ``-`` → stdin
    2. Looks like a JSON literal (starts with ``{`` or ``[``) → parse as JSON
    3. ``Path(arg).is_file()`` → read the file and parse

    The JSON-shape check comes BEFORE the file check because realistic
    inputs (>255 chars) cause ``Path.is_file()`` to raise
    ``OSError: [Errno 63] File name too long`` on macOS/Linux — the OS
    rejects the stat() before is_file can return False. The leading-char
    check is cheap, unambiguous (no filename starts with ``{`` or ``[``
    on any sane FS), and lets us short-circuit before touching the
    filesystem.
    """
    if arg == "-":
        return _ensure_dict(json.loads(sys.stdin.read()))
    stripped = arg.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            return _ensure_dict(json.loads(arg))
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(f"input looks like JSON but failed to parse: {exc}") from exc
    # File-path branch. Wrap is_file() in a try/except because OS-level
    # name-length errors are not the caller's fault and shouldn't crash
    # the CLI — just treat the arg as JSON and let json.loads fail loud
    # if it's actually neither.
    try:
        is_file = Path(arg).is_file()
    except OSError:
        is_file = False
    if is_file:
        return _ensure_dict(json.loads(Path(arg).read_text()))
    try:
        parsed = json.loads(arg)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"input must be JSON object, file path, or '-': {exc}") from exc
    return _ensure_dict(parsed)


def _ensure_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise typer.BadParameter(f"input must be a JSON object, got {type(value).__name__}")
    return value
