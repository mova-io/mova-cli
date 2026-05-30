"""``movate watch <agent>`` — TDD-style hot-reload for the dev inner loop.

Watches an agent directory's key files (``agent.yaml``, the prompt,
both schemas, the eval dataset, the judge config, and project- and
agent-level ``contexts/*.md``) and, on every change, either:

* re-runs ``movate validate`` (the default — lint + forecast in <1s), or
* with ``--run`` (ADR 027), **re-executes** the agent against a test input
  and prints the new output plus a diff vs. the previous run — the
  live-reload test loop. ``mdk dev`` drives this same loop (:func:`run_loop`)
  as one phase, so the loop has a single home.

Implementation
--------------

Pure stdlib polling. We considered ``watchdog`` and ``watchfiles``
but adding a runtime dep for a single dev-loop command isn't worth
it — a 0.5-second mtime poll loop is fast enough for human
keystrokes, has no platform-specific quirks (macOS FSEvents vs
inotify), and pulls zero extra deps.

The watcher itself is split from the dispatcher so tests can drive
``dispatch_once`` / ``dispatch_run_once`` deterministically without
spinning up a real loop.

Concurrency (ADR 027 D4): the live-reload loop is a **single foreground
loop** — no background thread. It polls mtimes *between* interactive
prompts and runs each dispatch with ``asyncio.run`` (one event loop per
dispatch, fully torn down before the next), which avoids event-loop
reentrancy and terminal-input races. See :func:`run_loop`.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.prompt import Prompt

from movate.cli._completion import complete_agent_path
from movate.cli._console import hint, warn
from movate.core.loader import AgentLoadError, _resolve_project_root, load_agent

stdout = Console()
err = Console(stderr=True)


def watch(
    path: Path = typer.Argument(
        ...,
        help="Path to an agent directory.",
        shell_complete=complete_agent_path,
    ),
    run: bool = typer.Option(
        False,
        "--run/--no-run",
        "--test/--no-test",
        help=(
            "Re-execute the agent on every save (output + diff vs. the previous run) "
            "instead of only re-validating. Resolves a test input from --input, then "
            "the first evals/dataset.jsonl row, then prompts you once."
        ),
    ),
    input_flag: str | None = typer.Option(
        None,
        "--input",
        "-i",
        help=(
            "Test input for --run (plain string or JSON). Defaults to the first row of "
            "evals/dataset.jsonl, else prompts once. Ignored without --run."
        ),
    ),
    mock: bool = typer.Option(
        False,
        "--mock",
        help="Use the deterministic MockProvider for --run (no API keys needed).",
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

    With ``--run`` it instead re-executes the agent against a test input
    on every save and prints the new output plus a diff vs. the previous
    run — the live-reload test loop (ADR 027). The same loop drives
    ``mdk dev``.

    [bold]Examples:[/bold]

      [dim]# Watch an agent — re-runs validate on every save[/dim]
      $ movate watch ./agents/faq-agent

      [dim]# Live-reload test loop: re-run the agent + diff on every save[/dim]
      $ movate watch ./agents/faq-agent --run --mock

      [dim]# Drive the loop with a specific input[/dim]
      $ movate watch ./agents/faq-agent --run -i '{"text": "hello"}'

      [dim]# Slow shared filesystem? Raise the poll interval.[/dim]
      $ movate watch ./agents/faq-agent --poll-interval 2

      [dim]# CI-strict mode: lint warnings fail the validate[/dim]
      $ movate watch ./agents/faq-agent --strict

    Exit with Ctrl-C. Each re-run prints with a timestamp so you can
    tell at a glance which save triggered which result.
    """
    if run:
        _watch_run(path, input_flag, mock=mock, poll_interval=poll_interval)
        return

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


def _watch_run(path: Path, input_flag: str | None, *, mock: bool, poll_interval: float) -> None:
    """``mdk watch --run`` — the live-reload test loop entry point.

    Resolves a test input (D3 precedence), then drives the shared
    :func:`run_loop`. Non-TTY degrades to a documentation-only print —
    the loop needs a tty both to prompt for a missing input and because
    it's a human inner-loop, so on a pipe/CI we print the one-shot
    command instead of blocking (mirrors ``mdk dev``'s non-TTY gate).
    """
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive:
        _print_run_noninteractive_guide(path, input_flag, mock=mock)
        return

    test_input = _resolve_test_input(input_flag, path)
    if test_input is None:
        test_input = _prompt_for_input(path)
    if test_input is None:
        warn("no test input — nothing to run")
        return

    err.print(
        f"[bold]movate watch --run[/bold] {path}\n"
        f"[dim]  re-runs on every save; poll every {poll_interval}s; Ctrl-C to exit[/dim]"
    )
    try:
        run_loop(path, test_input, mock=mock, poll_interval=poll_interval)
    except KeyboardInterrupt:
        err.print("\n[dim]watcher stopped[/dim]")


def _print_run_noninteractive_guide(path: Path, input_flag: str | None, *, mock: bool) -> None:
    """Non-TTY ``--run``: print the one-shot run command instead of looping.

    A live reload loop is a human inner-loop tool; on a pipe/CI there's no
    editor making saves and no tty to prompt on, so we degrade to the
    equivalent single ``mdk run`` invocation (mirrors ``_next_steps`` /
    ``mdk dev``'s non-TTY gate) so scripts never hang waiting for input.
    """
    from movate.cli._next_steps import mdk_bin_name  # noqa: PLC0415

    argv = [mdk_bin_name(), "run", str(path)]
    if mock:
        argv.append("--mock")
    if input_flag:
        argv += ["-i", input_flag]
    else:
        argv += ["-i", "<input>"]
    err.print(
        "[bold]movate watch --run[/bold] (non-interactive) — re-run on save needs a TTY.\n"
        f"[dim]  run it once instead:[/dim] {' '.join(argv)}"
    )


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

    # Contexts the agent renders: project-level
    # (``<project_root>/contexts/*.md``) and agent-local
    # (``<agent_dir>/contexts/*.md``). The loader prepends these to the
    # prompt, so an edit must re-fire the dispatch. We watch the whole
    # dir's markdown — not just the names listed in ``contexts:`` — so a
    # NEWLY added file is caught too (the ``mdk dev`` "add a context"
    # flow creates the file then wires it into agent.yaml; watching the
    # dir means the create alone already registers as a change).
    project_root = _resolve_project_root(agent_dir)
    for ctx_dir in (project_root / "contexts", agent_dir / "contexts"):
        if ctx_dir.is_dir():
            paths.extend(sorted(ctx_dir.glob("*.md")))

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


def dispatch_run_once(agent_dir: Path, test_input: str, *, mock: bool) -> tuple[int, str | None]:
    """Re-run the agent against ``test_input``, print its output, and
    return ``(exit_code, output_text)``.

    The live-reload counterpart to :func:`dispatch_once` (which only
    validates). Reloads the agent fresh from disk — ``load_agent``
    re-reads the prompt + contexts on every call, so an edited prompt or
    a changed context is reflected with no cache to invalidate.

    ``exit_code`` is 0 on a clean run, 2 on any failure (load, input
    coercion, or execution); ``output_text`` is the run's stdout on
    success and ``None`` on failure, so callers can diff successive runs
    without re-deriving it. Never raises except :class:`KeyboardInterrupt`,
    which the caller relies on to break out of the watch loop — a
    transient provider or load error must not kill the dev session.
    """
    import asyncio  # noqa: PLC0415

    # Local imports keep watch's import path light: these pull in the
    # full runtime stack we only need when actually executing.
    from movate.cli._runtime import build_local_runtime, shutdown_runtime  # noqa: PLC0415
    from movate.cli.run import _coerce_agent_input, _configure_mock_for_bundle  # noqa: PLC0415
    from movate.core.models import RunRequest  # noqa: PLC0415

    ts = datetime.now().strftime("%H:%M:%S")
    err.print(f"\n[dim]── {ts} ──[/dim]")
    try:
        bundle = load_agent(agent_dir)
    except AgentLoadError as exc:
        warn(f"load failed: {exc}")
        return 2, None
    try:
        payload = _coerce_agent_input(test_input, bundle)
    except typer.BadParameter as exc:
        warn(f"input error: {exc}")
        return 2, None

    # Run the executor directly and return its text, rather than reusing
    # run._run_local_agent. That helper renders a status spinner (rich
    # Live) when stderr is a tty; capturing its stdout with
    # redirect_stdout to diff successive runs conflicts with the Live
    # display and silently stalls the watch loop. Calling the executor
    # gives us the response text cleanly — no spinner, no stdout capture.
    async def _execute() -> str:
        rt = await build_local_runtime(mock=mock)
        if mock:
            _configure_mock_for_bundle(rt.provider, bundle)
        try:
            request = RunRequest(agent=bundle.spec.name, input=payload)
            response = await rt.executor.execute(bundle, request)
        finally:
            await shutdown_runtime(rt.storage, rt.tracer)
        return response.human_readable

    try:
        output = asyncio.run(_execute())
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        # Broad on purpose: a transient provider / runtime error must not
        # kill the dev session — report it and keep the loop alive.
        warn(f"run failed: {exc}")
        return 2, None
    sys.stdout.write(output + "\n")
    return 0, output


# ---------------------------------------------------------------------------
# Shared live-reload loop (ADR 027) — one home for ``mdk watch --run`` + ``mdk dev``
# ---------------------------------------------------------------------------


def run_loop(
    agent_dir: Path,
    test_input: str,
    *,
    mock: bool,
    poll_interval: float,
    on_iteration: Callable[[int], None] | None = None,
) -> None:
    """Re-run the agent on every change to its files until ``KeyboardInterrupt``.

    The single home for the live-reload test loop (ADR 027 D1): polls
    ``mtime``s with the same architecture as :func:`watch`, but dispatches a
    *run* (:func:`dispatch_run_once`) instead of a validate, then prints a
    diff vs. the previous run (:func:`_print_output_diff`). ``mdk dev`` drives
    this as a phase; ``mdk watch --run`` drives it directly.

    ``on_iteration`` is an optional post-dispatch hook fired with the run's exit
    code after the initial dispatch and after every change-triggered dispatch.
    It's the seam ``mdk dev`` uses for eval-in-the-loop (score a few cases +
    show the delta) without coupling the shared loop to the eval engine. The
    default (``None``) leaves the loop byte-for-byte unchanged — ``mdk watch
    --run`` never passes it. The hook must not raise (the caller wraps its own
    failures); for safety it's still called inside a guard so a buggy hook can't
    kill the loop.

    D4 concurrency model — the one real risk — is enforced here: this is a
    **single foreground loop**. Each dispatch runs via ``asyncio.run`` (inside
    :func:`dispatch_run_once`), which builds *and fully tears down* its own
    event loop before returning, so successive dispatches never reuse a loop
    (no "Event loop is closed" / reentrancy). There is **no background thread**:
    the mtime poll, the per-dispatch ``asyncio.run``, and (in ``mdk dev``) the
    interactive prompt all run on this one thread, never concurrently — so
    there's no terminal-input race. ``KeyboardInterrupt`` propagates so the
    caller can stop the loop (``mdk dev`` opens its actions menu; ``mdk watch``
    exits).
    """

    def _fire_iteration(exit_code: int) -> None:
        if on_iteration is None:
            return
        try:
            on_iteration(exit_code)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # a buggy hook must not sink the loop
            warn(f"dev iteration hook failed: {exc}")

    try:
        paths = _compute_watched_paths(agent_dir).paths
    except AgentLoadError as exc:
        warn(f"couldn't read agent: {exc}")
        paths = ()

    err.print(
        "[dim]  edit prompt.md, agent.yaml, a schema, or a context — re-runs on save."
        " Ctrl-C to stop.[/dim]"
    )
    rc, previous = dispatch_run_once(agent_dir, test_input, mock=mock)
    _fire_iteration(rc)

    snapshot = _snapshot_mtimes(paths)
    while True:
        time.sleep(poll_interval)
        with contextlib.suppress(AgentLoadError):
            paths = _compute_watched_paths(agent_dir).paths
        new_snapshot = _snapshot_mtimes(paths)
        if new_snapshot != snapshot:
            time.sleep(0.2)  # debounce write-then-rename saves.
            with contextlib.suppress(AgentLoadError):
                paths = _compute_watched_paths(agent_dir).paths
            snapshot = _snapshot_mtimes(paths)
            rc, current = dispatch_run_once(agent_dir, test_input, mock=mock)
            _print_output_diff(previous, current)
            _fire_iteration(rc)
            # Keep the last GOOD output as the baseline so a failed run
            # (current is None) doesn't reset the diff reference.
            if current is not None:
                previous = current


def _print_output_diff(previous: str | None, current: str | None) -> None:
    """Show whether the output changed since the last run, and if so, how.

    Answers "did my edit change anything?" at a glance: a one-line marker
    when unchanged, a colorized unified diff when it changed. Skipped when
    there's no baseline yet or the current run failed.
    """
    if previous is None or current is None:
        return
    if previous == current:
        err.print("[dim]· output unchanged since last run[/dim]")
        return
    err.print("[yellow]✎ output changed:[/yellow]")
    diff = difflib.unified_diff(
        previous.splitlines(),
        current.splitlines(),
        fromfile="previous",
        tofile="current",
        lineterm="",
    )
    for line in diff:
        # escape() keeps brackets / markup in the agent's own output from
        # being interpreted as Rich tags; style is applied out-of-band.
        text = escape(line)
        if line.startswith("+") and not line.startswith("+++"):
            err.print(text, style="green")
        elif line.startswith("-") and not line.startswith("---"):
            err.print(text, style="red")
        elif line.startswith("@@"):
            err.print(text, style="cyan")
        else:
            err.print(text, style="dim")


def _resolve_test_input(input_flag: str | None, agent_dir: Path) -> str | None:
    """Pick the input the live loop runs on (ADR 027 D3 precedence).

    Explicit ``--input`` wins; else the first row of ``evals/dataset.jsonl``
    (the same dataset row :func:`movate.cli.run._suggest_dataset_example`
    surfaces); else ``None`` so the caller prompts once and remembers it for
    the session. Best-effort on the dataset path — a broken / missing dataset
    just falls through to the prompt.
    """
    if input_flag:
        return input_flag
    try:
        bundle = load_agent(agent_dir)
        dataset = bundle.spec.evals.dataset
        if not dataset:
            return None
        ds_path = (bundle.agent_dir / dataset).resolve()
        if ds_path.is_file():
            text = ds_path.read_text().strip()
            if text:
                row = json.loads(text.splitlines()[0])
                if isinstance(row, dict) and "input" in row:
                    return json.dumps(row["input"])
    except (AgentLoadError, OSError, json.JSONDecodeError, AttributeError, TypeError):
        pass
    return None


def _prompt_for_input(agent_dir: Path) -> str | None:
    """Ask the operator for a test input (plain string or JSON).

    Surfaces the input schema's required fields as a hint when available.
    Returns ``None`` on an empty answer or Ctrl-C / EOF so the caller can
    bail without a value.
    """
    with contextlib.suppress(AgentLoadError):
        bundle = load_agent(agent_dir)
        required = bundle.input_schema.get("required", [])
        if required:
            err.print(f"[dim]input schema requires: {required}[/dim]")
    try:
        value = Prompt.ask("[bold]Test input[/bold] (plain string or JSON)").strip()
    except (KeyboardInterrupt, EOFError):
        return None
    return value or None


__all__ = [
    "_print_output_diff",
    "_prompt_for_input",
    "_resolve_test_input",
    "dispatch_once",
    "dispatch_run_once",
    "run_loop",
    "watch",
]
