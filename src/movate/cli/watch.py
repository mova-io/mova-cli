"""``movate watch <agent>`` — TDD-style hot-reload for the dev inner loop.

Watches an agent directory's key files (``agent.yaml``, the prompt,
both schemas, the eval dataset, the judge config) and re-runs
``movate validate`` whenever any of them changes. The point: tight
feedback while iterating on a prompt — save the file, see lint +
forecast results in <1s.

Implementation
--------------

Pure stdlib polling. We considered ``watchdog`` and ``watchfiles``
but adding a runtime dep for a single dev-loop command isn't worth
it — a 0.5-second mtime poll loop is fast enough for human
keystrokes, has no platform-specific quirks (macOS FSEvents vs
inotify), and pulls zero extra deps.

The watcher itself is split from the dispatcher so tests can drive
``dispatch_once`` deterministically without spinning up a real loop.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console

from movate.cli._completion import complete_agent_path
from movate.cli._console import hint, warn
from movate.core.loader import AgentLoadError, load_agent

stdout = Console()
err = Console(stderr=True)


def watch(
    path: Path = typer.Argument(
        ...,
        help="Path to an agent directory.",
        shell_complete=complete_agent_path,
    ),
    poll_interval: float = typer.Option(
        0.5,
        "--poll-interval",
        help=(
            "Seconds between filesystem polls. 0.5s feels instant for human "
            "edits; raise to 2-5s if you're watching a slow shared filesystem."
        ),
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Pass --strict to the underlying validate (promote lint warnings to errors).",
    ),
) -> None:
    """Re-run ``movate validate`` whenever the agent's files change.

    [bold]Examples:[/bold]

      [dim]# Watch an agent — re-runs validate on every save[/dim]
      $ movate watch ./agents/faq-agent

      [dim]# Slow shared filesystem? Raise the poll interval.[/dim]
      $ movate watch ./agents/faq-agent --poll-interval 2

      [dim]# CI-strict mode: lint warnings fail the validate[/dim]
      $ movate watch ./agents/faq-agent --strict

    Exit with Ctrl-C. Each re-run prints with a timestamp so you can
    tell at a glance which save triggered which result.
    """
    try:
        # First run on entry — gives an initial state + exposes any
        # broken files BEFORE the operator starts editing. Failures
        # here don't exit; the watcher continues so they can fix it.
        watched = _compute_watched_paths(path)
    except AgentLoadError as exc:
        warn(f"couldn't read agent at start: {exc}")
        hint("[dim]watching anyway — fix the file and the watcher will re-run[/dim]")
        watched = _WatchedSet(agent_dir=path)

    err.print(
        f"[bold]movate watch[/bold] {path}\n"
        f"[dim]  watching {len(watched.paths)} files; "
        f"poll every {poll_interval}s; Ctrl-C to exit[/dim]"
    )
    # Initial dispatch so the operator sees current state without
    # having to make a no-op edit.
    dispatch_once(path, strict=strict)

    snapshot = _snapshot_mtimes(watched.paths)
    try:
        while True:
            time.sleep(poll_interval)
            # The agent file might be temporarily broken mid-save
            # (editor truncated then wrote). When that happens
            # ``_compute_watched_paths`` raises ``AgentLoadError``;
            # suppress and re-snapshot the paths we already know
            # about, then we'll re-derive once it's parseable again.
            with contextlib.suppress(AgentLoadError):
                watched = _compute_watched_paths(path)
            new_snapshot = _snapshot_mtimes(watched.paths)
            if new_snapshot != snapshot:
                # Debounce — give an editor that does write-then-rename
                # a few hundred ms to settle so we don't dispatch twice
                # for a single save.
                time.sleep(0.2)
                # Re-snapshot AFTER the debounce so the next iteration
                # doesn't double-fire.
                snapshot = _snapshot_mtimes(_compute_watched_paths(path).paths)
                dispatch_once(path, strict=strict)
    except KeyboardInterrupt:
        err.print("\n[dim]watcher stopped[/dim]")


# ---------------------------------------------------------------------------
# Dispatch + path discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _WatchedSet:
    """The files the watcher polls for change. Computed fresh on every
    loop iteration so a change to ``agent.yaml`` that adds a new
    schema or dataset is picked up automatically."""

    agent_dir: Path
    paths: tuple[Path, ...] = field(default_factory=tuple)


def _compute_watched_paths(agent_dir: Path) -> _WatchedSet:
    """Resolve every file the agent depends on. Includes the YAML,
    prompt, both schemas, the eval dataset, and the judge config —
    if any exist on disk. Best-effort: a file that disappears
    between loads just drops out of the set.

    Raises :class:`AgentLoadError` if ``agent.yaml`` itself can't
    be parsed (the caller handles that gracefully)."""
    paths: list[Path] = []
    agent_yaml = agent_dir / "agent.yaml"
    if agent_yaml.exists():
        paths.append(agent_yaml)

    # Use load_agent's parser so we honor whatever schema/prompt
    # paths the YAML declares. If it raises, the outer try in
    # watch() handles it; if it succeeds, we get authoritative paths.
    bundle = load_agent(agent_dir)
    spec = bundle.spec
    candidates = [(agent_dir / spec.prompt).resolve()]
    # ``schemas.input`` / ``schemas.output`` may be either a path string
    # (legacy, points at a file we want to watch) or an inline shorthand
    # dict (compiled at load — nothing to watch, edits to the YAML
    # itself already retrigger via the agent.yaml watcher below).
    for schema_ref in (spec.schemas.input, spec.schemas.output):
        if isinstance(schema_ref, str):
            candidates.append((agent_dir / schema_ref).resolve())
    paths.extend(p for p in candidates if p.exists())
    # Optional files: eval dataset + judge config.
    if spec.evals.dataset:
        ds = (agent_dir / spec.evals.dataset).resolve()
        if ds.exists():
            paths.append(ds)
    if spec.evals.judge:
        jc = (agent_dir / spec.evals.judge).resolve()
        if jc.exists():
            paths.append(jc)

    return _WatchedSet(agent_dir=agent_dir, paths=tuple(paths))


def _snapshot_mtimes(paths: Iterable[Path]) -> dict[Path, float]:
    """``{path: mtime}`` for every existing path. Missing paths get
    ``-1.0`` so a delete-then-recreate registers as a change."""
    snapshot: dict[Path, float] = {}
    for p in paths:
        try:
            snapshot[p] = p.stat().st_mtime
        except FileNotFoundError:
            snapshot[p] = -1.0
    return snapshot


def dispatch_once(agent_dir: Path, *, strict: bool) -> int:
    """Run one validate pass + print result. Returns the exit code
    that ``movate validate`` would have used (0 = clean, 2 = error).

    Split from the loop so tests can call this directly and assert
    behavior without a real poll cycle.
    """
    # Local import — keep watch's import path light, and the validate
    # module pulls in pricing + linter machinery we only need here.
    from movate.cli.validate import _validate_agent  # noqa: PLC0415

    ts = datetime.now().strftime("%H:%M:%S")
    err.print(f"\n[dim]── {ts} ──[/dim]")
    try:
        _validate_agent(agent_dir, strict=strict, run_linter=True)
    except typer.Exit as exc:
        # validate raises Exit(2) on errors; we catch + report so the
        # watcher keeps going on the next change.
        return int(exc.exit_code or 0)
    return 0


__all__ = ["dispatch_once", "watch"]
