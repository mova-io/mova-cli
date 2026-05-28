"""Reusable progress UI helpers built on Rich.

All progress writes go to **stderr** so the stdout JSON pipe stays
clean — running ``movate eval ./agent -o json | jq .eval_id`` works
whether progress is showing or not. Same for ``movate run ... | tee
result.json`` and friends.

Auto-degrades on non-TTY: Rich's ``Console.is_terminal`` is False for
pipes, redirected streams, and CI environments, so we render a no-op
in those cases. Tests via Typer's ``CliRunner`` see clean stderr.

Four primitives:

* :func:`progress_bar` — known-length loop with a moving bar and
  elapsed time. Use for eval cases, bench models — anything where the
  total is known up front.
* :func:`spinner` — indeterminate-duration single operation. Use for
  one-shot provider calls, agent loads, etc.
* :func:`live_step` — indeterminate-duration step like :func:`spinner`,
  but with a **live elapsed timer** that keeps ticking on its own even
  when no new output arrives, plus a handle to update the message and
  log lines above the live region. Use for long shell-outs (e.g. a
  remote ``az acr build``) where a static spinner reads as "hung".
* :func:`print_event` — one-line event print to stderr. Use for
  worker job feeds, serve startup banners, anywhere a streaming log
  feel beats a progress bar.

None of these are async-context-managers because Rich's progress
machinery is synchronous-friendly and works fine inside ``async``
functions. They're plain ``with`` blocks.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)

# Single shared stderr console so output ordering stays consistent
# across helpers. Callers that already have their own Console can
# pass it via ``console=`` overrides.
_stderr = Console(stderr=True)


@contextmanager
def progress_bar(
    *,
    description: str,
    total: int | None = None,
    transient: bool = True,
    console: Console | None = None,
) -> Iterator[Callable[..., None]]:
    """Context manager yielding an ``advance`` callable.

    Usage::

        with progress_bar(description="cases", total=len(cases)) as advance:
            for case in cases:
                ...
                advance()  # advance by 1
                advance(suffix=" (mean=0.83)")  # add a side-suffix

    ``total`` may be ``None`` for indeterminate-then-known progress —
    the first ``advance(total=N)`` call sets it. Useful when the total
    is known by the engine but not by the CLI until the first callback
    fires.

    ``transient=True`` clears the bar on exit (default; clean output
    after completion). Pass ``transient=False`` to leave it visible —
    handy for long failure post-mortems.
    """
    target = console or _stderr
    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        console=target,
        transient=transient,
        # Rich already disables animation on non-TTY, but being
        # explicit helps in CI logs that capture some control codes.
        disable=not target.is_terminal,
    )
    with progress:
        task_id = progress.add_task(description, total=total)

        def advance(amount: int = 1, *, total: int | None = None, suffix: str = "") -> None:
            if total is not None:
                progress.update(task_id, total=total)
            if suffix:
                progress.update(task_id, description=f"{description}{suffix}")
            progress.advance(task_id, amount)

        yield advance


@contextmanager
def spinner(message: str, *, console: Console | None = None) -> Iterator[None]:
    """Indeterminate-duration spinner for one-shot operations.

    No-op when stderr isn't a TTY — Rich's status uses ANSI escapes
    that can confuse log capture in CI; cleaner to skip entirely.

    Usage::

        with spinner("calling provider..."):
            response = await executor.execute(...)
    """
    target = console or _stderr
    if not target.is_terminal:
        yield
        return
    with target.status(message, spinner="dots"):
        yield


@dataclass
class LiveStep:
    """Handle yielded by :func:`live_step` to drive the live region.

    * :meth:`update` swaps the spinner's description in place.
    * :meth:`log` prints a line **above** the live spinner/timer, so
      streamed sub-process output scrolls naturally while the timer
      stays pinned to the bottom.

    On a non-TTY the no-op variant (``_NULL_LIVE_STEP``) is yielded
    instead, so callers can call ``.update`` / ``.log`` unconditionally.
    """

    _progress: Progress
    _task_id: TaskID

    def update(self, message: str) -> None:
        self._progress.update(self._task_id, description=message)

    def log(self, line: str) -> None:
        # ``console.print`` renders above the Live region. We strip the
        # trailing newline so streamed lines (which keep theirs) don't
        # double-space, and disable markup so log payloads containing
        # ``[`` / ``]`` aren't mis-parsed as Rich tags.
        self._progress.console.print(line.rstrip("\n"), markup=False, highlight=False)


class _NullLiveStep:
    """No-op handle yielded by :func:`live_step` on a non-TTY.

    Keeps the call sites branch-free: ``.update`` / ``.log`` are safe to
    call and simply do nothing, mirroring :func:`spinner`'s non-TTY
    behaviour (no ANSI, clean CI logs / piped output)."""

    def update(self, message: str) -> None:
        return

    def log(self, line: str) -> None:
        return


_NULL_LIVE_STEP = _NullLiveStep()


@contextmanager
def live_step(
    message: str, *, console: Console | None = None
) -> Iterator[LiveStep | _NullLiveStep]:
    """Indeterminate step with a spinner + **live elapsed timer**.

    Like :func:`spinner`, but the timer keeps ticking on its own even
    during long quiet stretches with no new output — Rich's ``Progress``
    auto-refresh advances ``TimeElapsedColumn`` independently, so the
    operator can see the step is still alive (vs. a static spinner that
    reads as "hung"). Use for long shell-outs like a remote
    ``az acr build`` that can run several minutes with no chatter.

    Yields a :class:`LiveStep` handle: ``.update(message)`` changes the
    description, ``.log(line)`` prints a line above the live region (use
    it to stream sub-process output in verbose mode).

    No-op when stderr isn't a TTY — yields a :class:`_NullLiveStep` whose
    ``.update`` / ``.log`` do nothing, so CI logs and piped output stay
    clean (same contract as :func:`spinner`).

    Usage::

        with live_step("building image in ACR…") as step:
            for line in stream:
                step.log(line)  # verbose: echo sub-process output
    """
    target = console or _stderr
    if not target.is_terminal:
        yield _NULL_LIVE_STEP
        return
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=target,
        transient=True,
        disable=not target.is_terminal,
    )
    with progress:
        # total=None → indeterminate task; the spinner animates and the
        # elapsed timer ticks via Progress's own refresh thread.
        task_id = progress.add_task(message, total=None)
        yield LiveStep(progress, task_id)


def print_event(message: str, *, style: str = "", console: Console | None = None) -> None:
    """One-line event print to stderr.

    Style strings are Rich markup (e.g. ``"green"``, ``"bold red"``).
    Empty string = default style. Auto-rendered as plain text when
    stderr isn't a TTY.
    """
    target = console or _stderr
    if style:
        target.print(message, style=style)
    else:
        target.print(message)


__all__ = ["LiveStep", "live_step", "print_event", "progress_bar", "spinner"]
