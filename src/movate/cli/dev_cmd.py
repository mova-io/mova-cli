"""``mdk dev <agent>`` — one guided loop from scaffold to Azure deploy.

The front door for authoring an agent. It ties the scattered dev verbs
(``init`` → edit ``prompt.md`` → ``run`` / ``watch`` → ``eval`` →
``deploy``) into a single resident session:

* **Scaffold on the fly** when the named agent doesn't exist yet.
* **Live test loop** — edit ``prompt.md`` or a context and the agent
  re-runs against your test input automatically, no restart. The loader
  reads the prompt + contexts fresh from disk on every run, so there's
  no cache to invalidate.
* **Actions menu** (Ctrl-C out of the live loop) to change the test
  input, create-and-attach a context, run evals, open the prompt in an
  editor, or deploy to Azure.

Non-TTY (CI / piped stdout) degrades to printing the recommended command
sequence and exiting — the same gating :mod:`movate.cli._next_steps` uses
so scripts never block on a prompt.

This command is pure orchestration: scaffolding, execution, eval, and
deploy all reuse the existing primitives. The only command-specific
logic is the watch⇄menu loop and :func:`_attach_context_to_agent`.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.markup import escape
from rich.prompt import Prompt

from movate.cli import _console
from movate.cli._completion import complete_agent_path
from movate.cli._next_steps import mdk_bin_name
from movate.cli._resolve import resolve_agent_or_workflow_arg, walk_up_for_project_root
from movate.cli.contexts_cmd import _CONTEXT_TEMPLATE, attach_context_to_agent
from movate.cli.watch import _compute_watched_paths, _snapshot_mtimes, dispatch_run_once
from movate.core.loader import AgentLoadError, load_agent

stdout = Console()
err = Console(stderr=True)


def dev(  # noqa: PLR0912 — menu dispatch is inherently branchy; flat reads clearer
    agent: str = typer.Argument(
        None,
        help="Agent name or path. If it doesn't exist yet, dev offers to scaffold it.",
        shell_complete=complete_agent_path,
    ),
    template: str = typer.Option(
        None,
        "--template",
        "-t",
        help="Template to scaffold from when the agent doesn't exist. Defaults to 'default'.",
    ),
    llm: str = typer.Option(
        None,
        "--llm",
        help="Natural-language description — scaffold a new agent from it (LLM-generated).",
    ),
    input_flag: str = typer.Option(
        None,
        "--input",
        "-i",
        help=(
            "Test input for the live loop (plain string or JSON). Defaults to the first "
            "row of evals/dataset.jsonl, or prompts you once if there's no dataset."
        ),
    ),
    mock: bool = typer.Option(
        False,
        "--mock",
        help="Use the deterministic MockProvider for runs (no API keys needed).",
    ),
    target: str = typer.Option(
        None,
        "--target",
        help="Azure deploy target (from ~/.movate/config.yaml). Used by the deploy action.",
    ),
    poll_interval: float = typer.Option(
        0.5,
        "--poll-interval",
        help="Seconds between filesystem polls in the live loop.",
    ),
) -> None:
    """Guided agent authoring: scaffold → edit → live-test → deploy.

    [bold]Examples:[/bold]

      [dim]# Resume work on an existing agent, live-testing on every save[/dim]
      $ mdk dev rag-qa

      [dim]# Scaffold a brand-new agent, then drop straight into the loop[/dim]
      $ mdk dev support-bot --template faq

      [dim]# No API keys handy? Drive the loop with the mock provider[/dim]
      $ mdk dev rag-qa --mock
    """
    agent_dir = _resolve_or_scaffold(agent, template=template, llm=llm, mock=mock)
    if agent_dir is None:
        raise typer.Exit(code=2)
    agent_name = agent_dir.name

    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive:
        _print_noninteractive_guide(agent_dir, agent_name, target)
        return

    test_input = _resolve_test_input(input_flag, agent_dir)
    if test_input is None:
        test_input = _prompt_for_input(agent_dir)

    _print_intro(agent_dir)

    while True:
        if test_input is not None:
            # Ctrl-C breaks the live loop and opens the actions menu.
            with contextlib.suppress(KeyboardInterrupt):
                _live_loop(agent_dir, test_input, mock=mock, poll_interval=poll_interval)

        action = _actions_menu()
        if action == "quit":
            break
        if action == "resume":
            if test_input is None:
                test_input = _prompt_for_input(agent_dir)
            continue
        if action == "input":
            test_input = _prompt_for_input(agent_dir)
        elif action == "context":
            _add_context_action(agent_dir)
        elif action == "edit":
            _open_in_editor(agent_dir / "prompt.md")
        elif action == "eval":
            _run_subcommand([mdk_bin_name(), "eval", str(agent_dir)])
        elif action == "deploy":
            target = _deploy_action(agent_name, target)

    _console.success("dev session ended")


# ---------------------------------------------------------------------------
# Phase 0 — resolve / scaffold
# ---------------------------------------------------------------------------


def _resolve_or_scaffold(
    agent: str | None,
    *,
    template: str | None,
    llm: str | None,
    mock: bool,
) -> Path | None:
    """Resolve ``agent`` to an existing directory, or scaffold a new one.

    Returns the resolved agent directory, or ``None`` after printing a
    actionable error (caller exits 2).
    """
    if not agent:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            _console.error("provide an agent name: mdk dev <name>")
            return None
        agent = Prompt.ask("[bold]Agent name[/bold]").strip()
        if not agent:
            _console.error("no agent name given")
            return None

    resolved = Path(resolve_agent_or_workflow_arg(agent))
    if (resolved / "agent.yaml").is_file():
        return resolved.resolve()

    # Doesn't exist — scaffold it. We need a project to scaffold into.
    project_root = walk_up_for_project_root()
    if project_root is None:
        _console.error(
            "not inside a movate project",
            context="run `mdk init <project>` first, then `mdk dev <agent>` inside it.",
        )
        return None

    agents_dir = project_root / "agents"
    name = Path(agent).name
    _console.hint(f"[dim]no agent '{name}' yet — scaffolding into {agents_dir / name}[/dim]")

    # Lazy import: init pulls in the scaffold + LLM stack we only need here.
    from movate.cli.init import (  # noqa: PLC0415
        _DEFAULT_LLM_MODEL,
        _init_agent,
        _init_agent_from_llm,
    )

    if llm:
        _init_agent_from_llm(
            name=name,
            description=llm,
            llm_model=_DEFAULT_LLM_MODEL,
            target=agents_dir,
            force=False,
            dry_run=False,
            starting_template=template or "default",
            mock=mock,
        )
    else:
        _init_agent(
            name=name,
            template=template or "default",
            target=agents_dir,
            force=False,
            quiet=True,
        )
        _console.success(f"scaffolded agent at {agents_dir / name}")

    return (agents_dir / name).resolve()


# ---------------------------------------------------------------------------
# Test input
# ---------------------------------------------------------------------------


def _resolve_test_input(input_flag: str | None, agent_dir: Path) -> str | None:
    """Pick the input the live loop runs on: explicit flag, else the first
    row of ``evals/dataset.jsonl``, else ``None`` (caller prompts)."""
    if input_flag:
        return input_flag
    try:
        bundle = load_agent(agent_dir)
        ds_path = (bundle.agent_dir / bundle.spec.evals.dataset).resolve()
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
    """Ask the operator for a test input (plain string or JSON)."""
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


# ---------------------------------------------------------------------------
# Live loop
# ---------------------------------------------------------------------------


def _live_loop(agent_dir: Path, test_input: str, *, mock: bool, poll_interval: float) -> None:
    """Re-run the agent on every change to its files until Ctrl-C.

    Mirrors the poll loop in :func:`movate.cli.watch.watch`, but dispatches
    a run (via :func:`dispatch_run_once`) instead of a validate. Lets
    ``KeyboardInterrupt`` propagate so the caller can open the actions menu.
    """
    try:
        watched = _compute_watched_paths(agent_dir)
        paths = watched.paths
    except AgentLoadError as exc:
        _console.warn(f"couldn't read agent: {exc}")
        paths = ()

    err.print(
        f"[bold]live[/bold] {agent_dir}\n"
        f"[dim]  edit prompt.md or a context — re-runs on save. Ctrl-C for the menu.[/dim]"
    )
    _, previous = dispatch_run_once(agent_dir, test_input, mock=mock)

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
            _, current = dispatch_run_once(agent_dir, test_input, mock=mock)
            _print_output_diff(previous, current)
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


# ---------------------------------------------------------------------------
# Actions menu
# ---------------------------------------------------------------------------


def _actions_menu() -> str:
    stdout.print()
    stdout.print("[bold]Actions:[/bold]")
    stdout.print(
        "  [bold cyan]r[/bold cyan] resume live test    "
        "[bold cyan]i[/bold cyan] change test input    "
        "[bold cyan]c[/bold cyan] add a context"
    )
    stdout.print(
        "  [bold cyan]e[/bold cyan] run evals           "
        "[bold cyan]d[/bold cyan] deploy to Azure       "
        "[bold cyan]o[/bold cyan] open prompt in editor"
    )
    stdout.print("  [bold cyan]q[/bold cyan] quit")
    try:
        choice = Prompt.ask(
            "[bold]Pick[/bold]",
            choices=["r", "i", "c", "e", "d", "o", "q"],
            default="r",
            show_choices=False,
        )
    except (KeyboardInterrupt, EOFError):
        return "quit"
    return {
        "r": "resume",
        "i": "input",
        "c": "context",
        "e": "eval",
        "d": "deploy",
        "o": "edit",
        "q": "quit",
    }[choice]


def _add_context_action(agent_dir: Path) -> None:
    """Create an agent-local context file and wire it into agent.yaml."""
    try:
        name = Prompt.ask("[bold]Context name[/bold] (no extension)").strip()
    except (KeyboardInterrupt, EOFError):
        return
    if not name:
        return

    ctx_dir = agent_dir / "contexts"
    ctx_dir.mkdir(parents=True, exist_ok=True)
    dest = ctx_dir / f"{name}.md"
    if not dest.exists():
        dest.write_text(_CONTEXT_TEMPLATE.format(name=name), encoding="utf-8")
        _console.success(f"created {dest}")
    else:
        _console.hint(f"[dim]{dest} already exists — reusing it[/dim]")

    try:
        added = attach_context_to_agent(agent_dir / "agent.yaml", name)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        _console.warn(f"couldn't update agent.yaml: {exc}")
        _console.hint(f"[dim]add it manually under contexts: [{name}][/dim]")
        return

    if added:
        _console.success(f"wired '{name}' into agent.yaml contexts")
    else:
        _console.hint(f"[dim]'{name}' was already listed in contexts[/dim]")
    err.print(f"[dim]edit {dest}, then resume the live loop to see it take effect[/dim]")


def _deploy_action(agent_name: str, target: str | None) -> str | None:
    """Deploy the project's agents to Azure, then hint at KB sync."""
    if not target:
        try:
            target = Prompt.ask("[bold]Deploy target[/bold] (from ~/.movate/config.yaml)").strip()
        except (KeyboardInterrupt, EOFError):
            return None
    if not target:
        _console.warn("no target — skipping deploy")
        return None

    _run_subcommand([mdk_bin_name(), "deploy", "--target", target, "--mode", "agents"])
    # KB isn't auto-synced on deploy — surface the follow-up explicitly.
    err.print(
        "\n[dim]Note: knowledge base is not synced by deploy. If this agent uses a KB:\n"
        f"  {mdk_bin_name()} kb ingest {agent_name} <path> --target {target}[/dim]"
    )
    return target


# ---------------------------------------------------------------------------
# Editor + subprocess helpers
# ---------------------------------------------------------------------------


def _open_in_editor(path: Path) -> None:
    """Best-effort: open ``path`` in $EDITOR / VS Code / Cursor, else print it."""
    editor = os.environ.get("EDITOR")
    argv: list[str] | None = None
    if editor:
        argv = [*editor.split(), str(path)]
    elif shutil.which("code"):
        argv = ["code", str(path)]
    elif shutil.which("cursor"):
        argv = ["cursor", str(path)]

    if argv is None:
        err.print(f"[dim]no $EDITOR set — open it manually: {path}[/dim]")
        return
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        err.print(f"[dim]opening {path} in your editor…[/dim]")
    except OSError as exc:
        err.print(f"[yellow]⚠[/yellow] couldn't launch editor: {exc} — open {path} manually")


def _run_subcommand(argv: list[str]) -> None:
    stdout.print(f"\n[dim]$ {' '.join(argv)}[/dim]")
    try:
        subprocess.run(argv, check=False)
    except FileNotFoundError:
        err.print(f"[yellow]⚠[/yellow] couldn't run {argv[0]} — try it manually.")


# ---------------------------------------------------------------------------
# Intro / non-interactive guide
# ---------------------------------------------------------------------------


def _print_intro(agent_dir: Path) -> None:
    stdout.print(f"\n[bold]mdk dev[/bold] · [bold]{agent_dir.name}[/bold]")
    stdout.print(f"[dim]prompt: {agent_dir / 'prompt.md'}[/dim]")


def _print_noninteractive_guide(agent_dir: Path, agent_name: str, target: str | None) -> None:
    """Non-TTY: print the command sequence instead of a live session."""
    bin_name = mdk_bin_name()
    deploy_target = target or "<target>"
    stdout.print("[bold]mdk dev[/bold] (non-interactive) — recommended sequence:")
    stdout.print(f"  1. edit   {agent_dir / 'prompt.md'}")
    stdout.print(f"  2. test   {bin_name} run {agent_dir} --mock '<input>'")
    stdout.print(f"  3. watch  {bin_name} watch {agent_dir}")
    stdout.print(f"  4. eval   {bin_name} eval {agent_dir}")
    stdout.print(f"  5. ship   {bin_name} deploy --target {deploy_target} --mode agents")
    stdout.print(f"\nmdk_dev_summary: agent={agent_name} dir={agent_dir} interactive=false")


__all__ = ["dev"]
