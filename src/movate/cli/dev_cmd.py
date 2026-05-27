"""``mdk dev <agent>`` — one guided loop from scaffold to Azure deploy.

The front door for authoring an agent. It ties the scattered dev verbs
(``init`` → edit ``prompt.md`` → ``run`` / ``watch`` → ``eval`` →
``deploy``) into a single resident session:

* **Scaffold on the fly** when the named agent doesn't exist yet.
* **Live test loop** — edit ``prompt.md`` or a context and the agent
  re-runs against your test input automatically, no restart. The loader
  reads the prompt + contexts fresh from disk on every run, so there's
  no cache to invalidate.
* **Actions menu** (Ctrl-C out of the live loop) to ask the conversational
  copilot, change the test input, create-and-attach a context, run evals,
  open the prompt in an editor, or deploy to Azure.
* **Ask the copilot** (the ``a`` key, ADR 025 S1 — absorbs F9): type a
  natural-language request ("add a returns-policy context", "make the tone
  formal", "ingest https://…", "add a calculator skill"); a provider-pluggable
  planner maps it to typed authoring **catalog** action(s), and the existing
  :class:`movate.authoring.AuthoringDriver` runs plan → preview → confirm →
  apply → verify for each. The planner never edits files; the driver does,
  through the catalog's validated/reversible/confirm-gated spine. An ambiguous
  request asks one clarifying question and changes nothing.

Non-TTY (CI / piped stdout) degrades to printing the recommended command
sequence and exiting — the same gating :mod:`movate.cli._next_steps` uses
so scripts never block on a prompt.

This command is pure orchestration: scaffolding, execution, eval, deploy, and
the copilot all reuse the existing primitives + the authoring catalog. The only
command-specific logic is the watch⇄menu loop, :func:`_add_context_action`, and
the :func:`_copilot_action` planner↔driver glue.
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
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from movate.authoring import AuthoringDriver
    from movate.authoring.planner import Planner, ProposedAction

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
    target: str | None = typer.Option(
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
        elif action == "ask":
            _copilot_action(agent_dir, mock=mock)
        elif action == "context":
            _add_context_action(agent_dir)
        elif action == "edit":
            _open_in_editor(agent_dir / "prompt.md")
        elif action == "eval":
            _run_subcommand([mdk_bin_name(), "eval", str(agent_dir)])
        elif action == "grounding":
            _grounding_action(agent_dir)
        elif action == "deploy":
            target = _deploy_action(agent_name, target)
        elif action == "ingest_kb":
            target = _ingest_kb_action(agent_name, agent_dir, target)
        elif action == "test_deployed":
            target = _test_deployed_action(agent_dir, test_input, target)

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
        "  [bold cyan]a[/bold cyan] ask the copilot     "
        "[bold cyan]r[/bold cyan] resume live test      "
        "[bold cyan]i[/bold cyan] change test input"
    )
    stdout.print(
        "  [bold cyan]c[/bold cyan] add a context        "
        "[bold cyan]e[/bold cyan] run evals             "
        "[bold cyan]g[/bold cyan] grounding check"
    )
    stdout.print(
        "  [bold cyan]o[/bold cyan] open prompt in editor "
        "[bold cyan]d[/bold cyan] deploy to Azure       "
        "[bold cyan]k[/bold cyan] ingest knowledge base"
    )
    stdout.print("  [bold cyan]x[/bold cyan] test deployed agent  [bold cyan]q[/bold cyan] quit")
    try:
        choice = Prompt.ask(
            "[bold]Pick[/bold]",
            choices=["a", "r", "i", "c", "e", "g", "d", "k", "x", "o", "q"],
            default="r",
            show_choices=False,
        )
    except (KeyboardInterrupt, EOFError):
        return "quit"
    return {
        "a": "ask",
        "r": "resume",
        "i": "input",
        "c": "context",
        "e": "eval",
        "g": "grounding",
        "d": "deploy",
        "k": "ingest_kb",
        "x": "test_deployed",
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


# ---------------------------------------------------------------------------
# Copilot (the `a` key) — NL → catalog action(s) via the planner (ADR 025 S1)
# ---------------------------------------------------------------------------


def _project_root_for_agent(agent_dir: Path) -> Path:
    """Resolve the project root the authoring driver should operate against.

    The catalog driver resolves an agent at ``<project>/agents/<name>``, so the
    project root must be the dir two levels above a canonical agent dir. We
    derive it from the agent path (``agents/<name>`` → its grandparent) rather
    than the CWD walk-up, so the copilot works regardless of where ``mdk dev``
    was launched from. Falls back to the CWD project marker, then the agent's
    grandparent, for non-canonical layouts.
    """
    agent_dir = agent_dir.resolve()
    if agent_dir.parent.name == "agents":
        return agent_dir.parent.parent
    return walk_up_for_project_root() or agent_dir.parent


def _build_planner(agent_dir: Path, project_root: Path, *, mock: bool) -> Planner:
    """Construct the NL→catalog planner backing the copilot (ADR 025 D6).

    Under ``--mock`` (or any non-keyed/offline run) this returns the
    deterministic :class:`MockPlanner` so the copilot works with no API keys —
    the same hermetic path the tests drive. Otherwise it wraps a real
    :class:`BaseLLMProvider` (the existing model seam) in an
    :class:`LLMPlanner`. No new dependency: the provider is the one ``mdk
    init --llm`` already uses.
    """
    from movate.authoring.planner import LLMPlanner, MockPlanner  # noqa: PLC0415

    if mock:
        return MockPlanner()
    from movate.cli.init import _DEFAULT_LLM_MODEL  # noqa: PLC0415
    from movate.providers.litellm import LiteLLMProvider  # noqa: PLC0415

    return LLMPlanner(LiteLLMProvider(), project=project_root, model=_DEFAULT_LLM_MODEL)


def _copilot_action(agent_dir: Path, *, mock: bool) -> None:
    """Ask the copilot: NL request → catalog action(s) → preview → confirm → apply.

    The native conversational surface (ADR 025 S1, F9). Maps the operator's
    free-text request to typed catalog action(s) via the planner, then drives
    each through the **existing** :class:`AuthoringDriver` spine — plan →
    preview (diff + cost/side-effects) → confirm → apply → verify. The planner
    never edits files; the driver does, behind the catalog's D8 boundaries and
    the D2 confirmation gates. An ambiguous request yields a single clarifying
    question and mutates nothing (D6).
    """
    from movate.authoring import AuthoringContext, AuthoringDriver  # noqa: PLC0415
    from movate.authoring.planner import PlannerError  # noqa: PLC0415

    try:
        request = Prompt.ask("[bold]Ask the copilot[/bold] (what should change?)").strip()
    except (KeyboardInterrupt, EOFError):
        return
    if not request:
        return

    project_root = _project_root_for_agent(agent_dir)
    planner = _build_planner(agent_dir, project_root, mock=mock)

    try:
        outcome = planner.plan(request, agent=agent_dir.name)
    except PlannerError as exc:
        _console.warn(f"copilot couldn't plan that: {exc}")
        return

    # Ambiguous request → ask ONE clarifying question, mutate nothing (D6).
    if outcome.is_clarification:
        stdout.print(f"\n[bold yellow]?[/bold yellow] {outcome.needs_clarification}")
        err.print("[dim]nothing changed — re-run 'a' with more detail.[/dim]")
        return

    if outcome.message:
        stdout.print(f"\n[dim]{outcome.message}[/dim]")

    driver = AuthoringDriver(AuthoringContext(project=project_root))
    for proposed in outcome.actions:
        if not _drive_proposed_action(driver, proposed):
            return  # user aborted (Ctrl-C at a confirm prompt)

    err.print("[dim]resume the live loop ('r') to see changes take effect.[/dim]")


def _drive_proposed_action(driver: AuthoringDriver, proposed: ProposedAction) -> bool:
    """Plan → preview → confirm → apply → verify one proposed catalog action.

    Returns ``True`` to continue to the next proposed action (applied, skipped,
    or failed-soft), ``False`` to abort the whole turn (the user Ctrl-C'd the
    confirm prompt). All writes go through the catalog :class:`AuthoringDriver`
    — the D2 confirmation gate + D3 verify (revert-on-failure) hold here too.
    """
    from movate.authoring import ConfirmationRequiredError  # noqa: PLC0415
    from movate.authoring.base import AuthoringActionError  # noqa: PLC0415
    from movate.authoring.catalog import UnknownActionError  # noqa: PLC0415

    try:
        plan = driver.plan(proposed.name, proposed.args)
    except (UnknownActionError, AuthoringActionError, ValueError) as exc:
        _console.warn(f"couldn't plan '{proposed.name}': {exc}")
        return True

    # Preview: diff + cost/side-effect estimate (D2).
    stdout.print(f"\n[bold]plan:[/bold] {plan.summary}")
    stdout.print(
        f"  side effects: {', '.join(s.value for s in plan.side_effects) or '—'}"
        f"   reversible: {'yes' if plan.reversible else '[red]no[/red]'}"
    )
    if plan.estimated_cost_usd is not None:
        stdout.print(f"  estimated cost: ~${plan.estimated_cost_usd:.4f}")
    if plan.diff:
        stdout.print(plan.diff)

    # Confirmation gate (D2): cost/networked/destructive actions default to a NO
    # so the user must explicitly opt in; additive+reversible+free default YES.
    try:
        confirmed = typer.confirm("Apply this change?", default=not plan.requires_confirmation)
    except (KeyboardInterrupt, EOFError):
        err.print("[yellow]aborted[/yellow]")
        return False
    if not confirmed:
        err.print("[dim]skipped.[/dim]")
        return True

    try:
        applied = driver.apply(
            proposed.name,
            proposed.args,
            confirmed=True,
            # Networked actions (ingest-kb) have no meaningful mock-run; the
            # rest run the D3 verify loop (validate → run --mock).
            verify="network" not in [s.value for s in plan.side_effects],
        )
    except ConfirmationRequiredError as exc:
        _console.warn(str(exc))
        return True
    except (AuthoringActionError, ValueError) as exc:
        _console.warn(f"apply failed: {exc}")
        return True

    if applied.verify is not None and not applied.verify.ok:
        if applied.verify.reverted:
            _console.warn(f"verify failed → reverted (project unchanged): {applied.verify.error}")
            return True
        _console.warn(f"applied, but verify warning: {applied.verify.error}")

    result = applied.result
    if result is not None:
        _console.success(result.summary)
        for path in result.changed_paths:
            err.print(f"[dim]  • {path}[/dim]")
    return True


def _ensure_target(target: str | None, *, purpose: str) -> str | None:
    """Return a deploy target — the one passed, or prompt for it."""
    if target:
        return target
    try:
        target = Prompt.ask(
            f"[bold]Target[/bold] for {purpose} (from ~/.movate/config.yaml)"
        ).strip()
    except (KeyboardInterrupt, EOFError):
        return None
    return target or None


def _deploy_action(agent_name: str, target: str | None) -> str | None:
    """Deploy the project's agents to Azure, then hint at KB sync."""
    target = _ensure_target(target, purpose="deploy")
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


def _ingest_kb_action(agent_name: str, agent_dir: Path, target: str | None) -> str | None:
    """Ingest a knowledge-base path for this agent — locally or to a target.

    Closes the deploy→KB gap: ``deploy`` ships the prompt + contexts, but the
    knowledge base is a separate ingest step. This runs it without leaving the
    session, paralleling the ``c`` (add-context) action.

    The KB path defaults to ``agents/<name>/kb/`` (the convention ``kb ingest``
    itself uses). Target is optional: a remembered/entered target ingests to the
    deployed runtime so the live agent can retrieve it; blank ingests into the
    local store (handy for the grounding check before you ship). Returns the
    (possibly newly-prompted) target so the caller remembers it.
    """
    default_kb = agent_dir / "kb"
    default_hint = str(default_kb) if default_kb.is_dir() else ""
    try:
        raw = Prompt.ask(
            "[bold]KB path[/bold] to ingest (file or dir)",
            default=default_hint,
            show_default=bool(default_hint),
        ).strip()
    except (KeyboardInterrupt, EOFError):
        return target
    if not raw:
        _console.warn("no path — skipping KB ingest")
        return target
    kb_path = Path(raw).expanduser()
    if not kb_path.exists():
        _console.warn(f"{kb_path} does not exist — skipping KB ingest")
        return target

    target = _ensure_target(target, purpose="KB ingest (leave blank = local store)")
    argv = [mdk_bin_name(), "kb", "ingest", agent_name, str(kb_path)]
    if target:
        argv += ["--target", target]
    _run_subcommand(argv)
    return target


def _test_deployed_action(
    agent_dir: Path, test_input: str | None, target: str | None
) -> str | None:
    """Run the *deployed* agent on the test input — confirms the prompt +
    contexts you've been editing actually shipped and behave the same.

    Returns the (possibly newly-prompted) target so the caller remembers it.
    """
    target = _ensure_target(target, purpose="the deployed runtime")
    if not target:
        _console.warn("no target — skipping deployed test")
        return None
    if not test_input:
        test_input = _prompt_for_input(agent_dir)
    if not test_input:
        return target
    err.print(
        f"[dim]running {agent_dir.name} on target '{target}' "
        f"— compare against the local output above[/dim]"
    )
    _run_subcommand([mdk_bin_name(), "run", str(agent_dir), "--target", target, "-i", test_input])
    return target


def _grounding_action(agent_dir: Path) -> None:
    """Score the agent for hallucination / faithfulness / context-obedience.

    Runs `mdk eval-scorecard` locally — generates test cases on the fly and
    scores them, so "does it obey my context and not make things up?" is one
    keystroke. (For the deployed agent, add `--target` to the command.)
    """
    err.print(
        "[dim]generating test cases + scoring (hallucination, faithfulness, "
        "instruction-following)…[/dim]"
    )
    _run_subcommand([mdk_bin_name(), "eval-scorecard", str(agent_dir)])


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
    stdout.print(
        f"  6. kb     {bin_name} kb ingest {agent_name} {agent_dir / 'kb'} "
        f"--target {deploy_target}   [dim](if the agent uses a knowledge base)[/dim]"
    )
    stdout.print(f"\nmdk_dev_summary: agent={agent_name} dir={agent_dir} interactive=false")


__all__ = ["dev"]
