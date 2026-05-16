"""Shared 'what next?' interactive menu helper.

Several commands (``mdk init``, ``mdk add``, ``mdk validate --all``,
``mdk eval --all``) end with a list of recommended next-step
commands. Pre-PR-#101 each command printed a static block. Now,
in TTY mode, the static block is replaced with an interactive
numbered picker that shells out to the selected command — same
visual language across every command in the flow.

This module exposes :func:`prompt_next_step`: given a list of
:class:`NextStep` actions, render the picker and execute the
operator's choice (or quit). Skipped under non-TTY (CI / piped
stdout / pytest) so scripted use sees only the static surface
each command already prints.

Why centralized: every call-site needs the same gating (TTY-only),
same Rich rendering (numbered cyan brackets, `[s] Skip`, `Pick (s):`
prompt with `s` default), same argv-list execution path (subprocess
to inherit stdio). DRY-up so future flows (``mdk deploy`` success
panel, ``mdk doctor`` follow-up) drop in with one helper call.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass

from rich.console import Console
from rich.prompt import Prompt


@dataclass
class NextStep:
    """One row in a 'what next?' menu.

    ``label`` is the short human-readable description ("Run with a
    sample input"). ``command`` is the resolved CLI string shown to
    the operator as documentation ("mdk run ./agents/faq --mock
    '...'"). ``argv`` is what gets executed when the operator picks
    this entry — passed straight to :func:`subprocess.run` so shell
    quoting is never an issue.
    """

    label: str
    command: str
    argv: list[str]


def prompt_next_step(
    *,
    steps: list[NextStep],
    console: Console | None = None,
) -> None:
    """Render the 'What next?' menu + run the operator's choice.

    Two modes:

    * **TTY (interactive)**: render the numbered picker, prompt the
      operator to pick, shell out to the chosen ``argv``.
    * **Non-TTY (CI / scripts / pytest)**: render the SAME numbered
      list as static output, then return without prompting. Scripts
      grepping for `Next:` / a specific command still see it; the
      operator's terminal isn't blocked waiting for input.

    The operator's choice (TTY mode) is executed via ``subprocess.run``
    with inherited stdio so the child command's output streams as if
    they'd typed it directly. Exit code is swallowed.
    """
    if not steps:
        return

    # No-op under non-TTY: each call site is responsible for any
    # static "Next steps" fallback (typically already in the Panel
    # body above this call). Avoids double-printing for tests + CI.
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return

    c = console or Console()
    c.print()
    c.print("[bold]Next:[/bold]")
    for i, step in enumerate(steps, start=1):
        c.print(f"  [bold cyan][{i}][/bold cyan] {step.label}   [dim]{step.command}[/dim]")
    # Escape the `[s]` to keep Rich from rendering it as strikethrough.
    c.print(r"  [bold cyan]\[s][/bold cyan] Skip   [dim]exit menu[/dim]")
    try:
        choice = Prompt.ask(
            "\n[bold]Pick[/bold]",
            choices=[str(i) for i in range(1, len(steps) + 1)] + ["s"],
            default="s",
            show_choices=False,
        )
    except (KeyboardInterrupt, EOFError):
        return
    if choice == "s":
        return

    chosen = steps[int(choice) - 1]
    c.print(f"\n[dim]$ {chosen.command}[/dim]")
    try:
        subprocess.run(chosen.argv, check=False)
    except FileNotFoundError:
        err = Console(stderr=True)
        err.print(
            f"[yellow]⚠[/yellow] couldn't run [bold]{chosen.argv[0]}[/bold] "
            "— try running the command manually."
        )


def mdk_bin_name() -> str:
    """Resolve which binary the operator invoked us as (``mdk`` or
    the legacy ``movate`` alias) so shelled-out subcommands match.

    Centralized here so every menu builder uses the same rule.
    """
    basename = os.path.basename(sys.argv[0]) if sys.argv else "mdk"
    return "movate" if basename == "movate" else "mdk"
