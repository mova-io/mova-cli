"""Top-level Typer app. Subcommands live in sibling modules.

The CLI groups commands by intent so a teammate unfamiliar with movate can
navigate by what they want to *do* rather than memorize a flat list.

Shell completion: ``movate --install-completion`` (bash/zsh/fish/PowerShell).
"""

from __future__ import annotations

import logging
import os
import sys

import click
import typer
from dotenv import load_dotenv


def _expand_help_alias() -> None:
    """Treat a trailing ``help`` token as a synonym for ``--help``.

    Operators consistently type ``mdk init help`` (or ``mdk auth help``)
    expecting the help screen — most CLIs accept that as a natural-
    language alias. Typer alone doesn't; without this hook the user
    gets an "agent name required" error or "Missing argument" diagnostic
    for what was really a help request.

    Rules (deliberately narrow to avoid breaking legitimate ``help``
    values):

    * The LAST positional must be exactly ``help`` (case-sensitive).
    * The preceding token must NOT start with ``-`` — otherwise ``help``
      is the value of some flag (e.g. ``--llm help`` for the unlikely
      operator who really wants to describe their agent as "help").
    * ``--help`` / ``-h`` must not already be present.
    * Opt out entirely via ``MDK_NO_HELP_ALIAS=1`` for the edge case
      where ``help`` is genuinely a payload value (``mdk run agent help``
      meaning "send 'help' as input").

    No regex, no Typer introspection — just a sys.argv rewrite that
    runs BEFORE Typer parses anything.
    """
    import os  # noqa: PLC0415
    import sys  # noqa: PLC0415

    if os.environ.get("MDK_NO_HELP_ALIAS", "").strip():
        return
    if len(sys.argv) < 2:  # noqa: PLR2004 - just program name
        return
    last = sys.argv[-1]
    if last not in ("help", "?"):
        return
    # Already a help request — don't double-flag.
    if "--help" in sys.argv or "-h" in sys.argv:
        return
    # The token before `help` is a flag taking a value — `help` is the
    # value, not a help request.
    if len(sys.argv) >= 3 and sys.argv[-2].startswith("-"):  # noqa: PLR2004
        return
    sys.argv[-1] = "--help"


_expand_help_alias()

# Load .env from cwd (or any parent). Existing env vars take precedence.
load_dotenv()

# Machine-global credentials store. After dotenv runs, autoload any
# provider keys that are STILL unset from ~/.movate/credentials. The
# resolution order is therefore: shell env > project .env > active-
# profile secrets > ~/.movate/credentials. Operators set keys once
# via `mdk auth login <provider>` and every project on the machine
# picks them up.
from movate.credentials import autoload_credentials  # noqa: E402
from movate.storage import mark_cli_mode as _storage_mark_cli_mode  # noqa: E402

# Tell ``movate.storage`` we're running in CLI mode so the
# "SqliteProvider not durable across container restarts" warning
# drops to DEBUG. The warning targets production containers; in
# ``mdk ...`` invocations it's noise at the top of every output.
# Server processes (``movate serve`` / FastAPI runtime) don't hit
# this codepath, so they still emit the warning at WARNING level.
_storage_mark_cli_mode()

autoload_credentials()

# Bridge MDK_* ↔ MOVATE_* env vars in both directions. Runs BEFORE any
# other module reads os.environ so legacy and canonical prefixes are
# interchangeable for downstream readers. One-shot deprecation warning
# on first invocation if MOVATE_* vars are in use. See _env_aliases.py.
from movate.cli._env_aliases import sync_env_aliases  # noqa: E402

sync_env_aliases()


def _eager_load_project_config() -> None:
    """Trigger any project-config deprecation warnings at CLI startup,
    BEFORE any Rich panel / prompt / wizard renders.

    Pre-2026-05-19 the project config was loaded lazily — first by the
    bundle loader or runtime, well into a command's execution. For
    ``mdk eval`` specifically that meant the legacy-yaml warning
    (``⚠ movate.yaml is deprecated``) fired mid-wizard, between the
    operator's ``Pick (1):`` answer and the spinner — visually jarring
    and easy to miss.

    Eager-loading here pushes the warning to the very first stderr
    line. Subsequent loads find the warning's one-shot flag already
    set and stay silent. Skipped when cwd isn't a project root so
    one-shot commands (``mdk --version``, ``mdk init``) don't pay
    a YAML-read cost.
    """
    import contextlib  # noqa: PLC0415
    import sys  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from movate.core.config import is_project_root, load_project_config  # noqa: PLC0415

    # Config-free commands must not load project config (and so must not
    # emit the legacy-yaml deprecation warning). `--version` / `--help`
    # answer without touching the project, so eager-loading there is both
    # wasted work and spurious noise — `mdk --version` from inside a
    # project dir was printing "movate.yaml is deprecated" on every call.
    # argv is inspected directly: this runs at import, before Typer parses.
    _config_free_flags = {"--version", "-V", "--help", "-h"}
    if _config_free_flags.intersection(sys.argv[1:]):
        return

    if not is_project_root(Path.cwd()):
        return
    # Defensive: a malformed project.yaml would otherwise abort the
    # CLI before any command-specific error handling fires. Swallow
    # exceptions; the bundle/runtime path will surface the load
    # error later with a friendlier context-aware message.
    with contextlib.suppress(Exception):
        load_project_config()


_eager_load_project_config()

from movate import __version__  # noqa: E402
from movate.cli import (  # noqa: E402
    _console,
    add_cmd,
    audit_cmd,
    compose_cmd,
    demo_cmd,
    dev_cmd,
    diff_cmd,
    eval_gen_cmd,
    eval_harvest_cmd,
    eval_schedule_cmd,
    eval_scorecard_cmd,
    fix_cmd,
    fmt_cmd,
    menu_cmd,
    migrate_cmd,
    migrate_state_cmd,
    monitor_cmd,
    plan_cmd,
    promote_cmd,
    replay_cmd,
    report_cmd,
    rollback_cmd,
    schedule_cmd,
    simulate_cmd,
    trigger_cmd,
    tune_cmd,
)
from movate.cli import bench as bench_cmd  # noqa: E402
from movate.cli import chat as chat_cmd  # noqa: E402
from movate.cli import deploy as deploy_cmd  # noqa: E402
from movate.cli import doctor as doctor_cmd  # noqa: E402
from movate.cli import eval as eval_cmd  # noqa: E402
from movate.cli import explain as explain_cmd  # noqa: E402
from movate.cli import (  # noqa: E402
    import_json as _import_json,  # noqa: F401  -- registers `json` on import_app
)
from movate.cli import (  # noqa: E402
    import_openapi as _import_openapi,  # noqa: F401  -- registers `openapi` on import_app
)
from movate.cli import init as init_cmd  # noqa: E402
from movate.cli import logs as logs_cmd  # noqa: E402
from movate.cli import pricing as pricing_cmd  # noqa: E402
from movate.cli import run as run_cmd  # noqa: E402
from movate.cli import serve as serve_cmd  # noqa: E402
from movate.cli import show as show_cmd  # noqa: E402
from movate.cli import submit as submit_cmd  # noqa: E402
from movate.cli import validate as validate_cmd  # noqa: E402
from movate.cli import watch as watch_cmd  # noqa: E402
from movate.cli import worker as worker_cmd  # noqa: E402
from movate.cli.agent_cmd import agent_app  # noqa: E402
from movate.cli.auth import auth_app  # noqa: E402
from movate.cli.authoring_cmd import authoring_app  # noqa: E402
from movate.cli.backup_cmd import export_state_cmd, import_state_cmd  # noqa: E402
from movate.cli.batch_cmd import batch_app  # noqa: E402
from movate.cli.benchmark_cmd import benchmark_app  # noqa: E402
from movate.cli.canary_cmd import canary_app  # noqa: E402
from movate.cli.ci import ci_app  # noqa: E402
from movate.cli.config_cmd import config_app  # noqa: E402
from movate.cli.contexts_cmd import contexts_app  # noqa: E402
from movate.cli.costs_cmd import costs_app  # noqa: E402
from movate.cli.docs_cmd import docs_app  # noqa: E402
from movate.cli.doctor import doctor_app  # noqa: E402
from movate.cli.export_oci_cmd import export_app  # noqa: E402
from movate.cli.fleet_cmd import fleet_app  # noqa: E402
from movate.cli.guardrails_cmd import guardrails_app  # noqa: E402
from movate.cli.import_lyzr import import_app  # noqa: E402
from movate.cli.infra_cmd import infra_app  # noqa: E402
from movate.cli.inspect_cmd import inspect_app  # noqa: E402
from movate.cli.jobs import jobs_app  # noqa: E402
from movate.cli.kb_cmd import kb_app  # noqa: E402
from movate.cli.keys_cmd import keys_app  # noqa: E402
from movate.cli.knowledge_cmd import knowledge_app  # noqa: E402
from movate.cli.mcp_cmd import mcp_app  # noqa: E402
from movate.cli.memory_cmd import memory_app  # noqa: E402
from movate.cli.models_cmd import models_app  # noqa: E402
from movate.cli.patterns_cmd import app as patterns_app  # noqa: E402
from movate.cli.playground import playground_app  # noqa: E402
from movate.cli.policy_cmd import policy_app  # noqa: E402
from movate.cli.profiles_cmd import profiles_app  # noqa: E402
from movate.cli.runs import runs_app  # noqa: E402
from movate.cli.scaffold import scaffold_app  # noqa: E402
from movate.cli.schema_cmd import schema_app  # noqa: E402
from movate.cli.secrets_cmd import secrets_app  # noqa: E402
from movate.cli.skills_cmd import skills_app  # noqa: E402
from movate.cli.snapshot_cmd import snapshot_app  # noqa: E402
from movate.cli.teams_bot import teams_bot_app  # noqa: E402
from movate.cli.templates_cmd import app as templates_app  # noqa: E402
from movate.cli.tenants import tenants_app  # noqa: E402
from movate.cli.trace import trace_app  # noqa: E402
from movate.cli.workflow_cmd import workflow_app  # noqa: E402
from movate.tracing import install_log_correlation  # noqa: E402

PANEL_DEVELOP = "Develop"
PANEL_RUN = "Run & evaluate"
PANEL_DIAGNOSE = "Diagnose"
PANEL_DEPLOY = "Deploy & operate"
PANEL_MANAGE = "Manage"

from typer.core import TyperGroup  # noqa: E402


class FuzzySuggestionGroup(TyperGroup):
    """Subgroup that suggests close matches when an unknown command runs.

    Typer/Click's default for ``mdk rag-qa`` (operator meant ``mdk add
    rag-qa``) is a bare "No such command 'rag-qa'." With ~50
    subcommands and ~15 templates that get muddled together in muscle
    memory, that's friction. Override ``resolve_command`` to inspect
    the failure and emit a "Did you mean: X, Y?" hint via difflib's
    edit-distance fuzzy match before re-raising.

    Uses ``cutoff=0.5`` for a permissive match — false positives
    ("did you mean: serve?" when you meant "deploy" by typing "delpoy")
    are recoverable; missed matches force the operator back to
    ``--help``.
    """

    def resolve_command(
        self,
        ctx: click.Context,
        args: list[str],
    ) -> tuple[str | None, click.Command | None, list[str]]:
        from difflib import get_close_matches  # noqa: PLC0415

        try:
            return super().resolve_command(ctx, args)
        except click.UsageError as exc:
            # Click 8.1+ ships its own "Did you mean" for high-confidence
            # matches. Don't double up — only inject our suggestion when
            # Click stayed silent (catches typos Click's tighter threshold
            # missed, like `init-stuff` → `init`).
            if "Did you mean" in (exc.message or ""):
                raise
            attempted = args[0] if args else ""
            known = sorted(self.list_commands(ctx))
            close = get_close_matches(attempted, known, n=3, cutoff=0.5)
            if not close:
                raise
            hint = f"Did you mean: {', '.join(close)}?"
            raise click.UsageError(f"{exc.message}\n  {hint}", ctx) from None


app = typer.Typer(
    name="mdk",
    cls=FuzzySuggestionGroup,
    help=(
        "[bold]mdk[/bold] — Movate Development Kit. Declarative platform for "
        "AI agents and workflows.\n\n"
        "[bold green]New here?[/bold green] Run [bold]mdk menu[/bold] for a "
        "guided next-step view.\n\n"
        "Common workflows:\n"
        "  [dim]$ mdk menu                       # status + suggested next step[/dim]\n"
        "  [dim]$ mdk init my-agent              # scaffold[/dim]\n"
        "  [dim]$ mdk run my-agent '{...}'       # one-shot[/dim]\n"
        "  [dim]$ mdk eval my-agent              # score against dataset[/dim]\n"
        "  [dim]$ mdk bench my-agent             # multi-model comparison[/dim]\n"
        "  [dim]$ mdk doctor                     # check environment[/dim]\n\n"
        "[dim]Also installed as [bold]movate[/bold] (transitional alias; dropped "
        "in a future major release).[/dim]\n\n"
        "Run [bold]mdk --install-completion[/bold] to enable shell tab-completion."
    ),
    no_args_is_help=True,
    add_completion=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _version_callback(value: bool) -> None:
    if value:
        # Print the brand name that matches how the user invoked us. ``mdk``
        # is canonical; ``movate`` is the transitional alias.
        binary = os.path.basename(sys.argv[0]) if sys.argv else "mdk"
        # Anything not exactly "movate" is rendered as "mdk" (handles
        # symlinks, "mdk", and the test runner's "pytest" all the same).
        brand = "movate" if binary == "movate" else "mdk"
        typer.echo(f"{brand} {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    verbose: bool = typer.Option(
        False,
        "--verbose",
        # No short form for --verbose at the global level. The `-v`
        # slot is reclaimed for --version below — matches convention
        # (docker -v, npm -v, node -v, python -V all show version,
        # and operators consistently typed `mdk -v` expecting that).
        # Verbose mode is still available via the long form and is
        # mostly used scripted (where explicit names beat shorts).
        # Subcommand-level `-v` short forms (e.g. `mdk trace replay -v`)
        # are unaffected — those don't collide with this top-level slot.
        help="Enable DEBUG-level logging.",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress INFO logging; print only warnings and errors.",
    ),
    target: str = typer.Option(
        None,
        "--target",
        "-t",
        envvar=["MDK_TARGET", "MOVATE_TARGET"],
        help=(
            "Default deployment target for remote commands "
            "(submit, jobs *). Overridden by a per-command --target. "
            "Falls back to MDK_TARGET env var (or legacy MOVATE_TARGET), "
            "then to the active config target."
        ),
    ),
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        "-v",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Global flags applied before any subcommand."""
    if verbose:
        level = logging.DEBUG
    elif quiet:
        level = logging.WARNING
    else:
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # Stamp the active OTel trace context (trace_id/span_id) onto every log
    # record and surface a non-empty trace_id in the deployed log line, so an
    # operator viewing a trace in App Insights can find the correlated logs in
    # Log Analytics by trace_id (item 38). A complete no-op when the `otel`
    # extra is absent or no span is active; local CLI logs stay byte-for-byte
    # unchanged. `serve`/`worker` run in this same process after basicConfig, so
    # they inherit the filter without a separate install call.
    install_log_correlation()
    # Suppress dim "FYI" stderr hints (the kind of "queued j-1 on dev,
    # poll with..." line that's friendly interactively but a nuisance
    # when piping). Error / warning prints stay on regardless.
    _console.set_quiet(quiet)
    # Stash the global --target / MOVATE_TARGET so remote subcommands
    # can fall back to it when their own --target wasn't passed.
    _console.set_global_target(target)


# ----- Develop --------------------------------------------------------------

# `menu` is the suggested first command for new users — shows workspace
# status + contextual next-step suggestions. Lives at the top of the
# Develop panel so it surfaces prominently in `mdk --help`.
app.command("menu", rich_help_panel=PANEL_DEVELOP)(menu_cmd.menu)
app.command("dev", rich_help_panel=PANEL_DEVELOP)(dev_cmd.dev)
app.command("demo", rich_help_panel=PANEL_DEVELOP)(demo_cmd.demo)
app.command("init", rich_help_panel=PANEL_DEVELOP)(init_cmd.init)
# `mdk add` — project-aware ergonomic wrapper around `mdk init -t <template>`.
# Same Develop panel because it's a scaffold command; positioned right after
# `init` so operators see the natural progression.
app.command("add", rich_help_panel=PANEL_DEVELOP)(add_cmd.add)
# `compose` scaffolds a multi-agent workflow.yaml from a list of agents.
# Sibling to `init` (single agent) and `demo` (full populated project).
app.command("compose", rich_help_panel=PANEL_DEVELOP)(compose_cmd.compose)
# `plan` generates a full project plan from a natural-language description.
# Sits next to `compose` (multi-agent) and `init` (single agent) since all
# three answer "how do I scaffold a new thing?"
app.command("plan", rich_help_panel=PANEL_DEVELOP)(plan_cmd.plan)
# `import state` (item 26) restores a `mdk export state` DR backup of
# control-plane state — the escape-hatch counterpart to the export.
import_app.command("state")(import_state_cmd)
app.add_typer(import_app, name="import", rich_help_panel=PANEL_DEVELOP)
app.add_typer(scaffold_app, name="scaffold", rich_help_panel=PANEL_DEVELOP)
app.add_typer(skills_app, name="skills", rich_help_panel=PANEL_DEVELOP)
app.add_typer(templates_app, name="templates", rich_help_panel=PANEL_DEVELOP)
# `patterns` surfaces the governed agent-pattern templates (ADR 038) that
# `mdk init --pattern <name>` scaffolds.
app.add_typer(patterns_app, name="patterns", rich_help_panel=PANEL_DEVELOP)
app.add_typer(schema_app, name="schema", rich_help_panel=PANEL_DEVELOP)
app.command("validate", rich_help_panel=PANEL_DEVELOP)(validate_cmd.validate)
app.add_typer(knowledge_app, name="knowledge", rich_help_panel=PANEL_DEVELOP)
app.add_typer(kb_app, name="kb", rich_help_panel=PANEL_DEVELOP)
# `contexts` lists + inspects shared context files wired into agents —
# the "did my policy.md actually load?" diagnostic. Lives next to `kb`
# since both answer "what supporting content does my agent have?".
app.add_typer(contexts_app, name="contexts", rich_help_panel=PANEL_DEVELOP)
# `authoring` is the typed, reversible action catalog (ADR 025) — the spine the
# conversational copilot + MCP server (later PRs) build on. Develop panel since
# it's how you *evolve* an agent after init (plan → apply → verify → undo).
app.add_typer(authoring_app, name="authoring", rich_help_panel=PANEL_DEVELOP)
# `mcp serve` (ADR 025 PR4) exposes the SAME authoring catalog over MCP — a
# plan_*/apply_*/validate/run tool per action — so an IDE/agent drives the
# plan→apply→verify spine the way `authoring` does. Sits next to `authoring`
# since it's the third surface over the one catalog (D5).
app.add_typer(mcp_app, name="mcp", rich_help_panel=PANEL_DEVELOP)
app.command("fmt", rich_help_panel=PANEL_DEVELOP)(fmt_cmd.fmt)
app.add_typer(docs_app, name="docs", rich_help_panel=PANEL_DEVELOP)
app.command("show", rich_help_panel=PANEL_DEVELOP)(show_cmd.show)
# `inspect` is the resolved-view sibling of `show` (raw view) — same
# Develop panel since both answer "what does this agent look like?".
app.add_typer(inspect_app, name="inspect", rich_help_panel=PANEL_DEVELOP)
# NOTE: do NOT pass `help=` here — Typer/Click then ignores the function's
# docstring, which is where each command's [bold]Examples:[/bold] block
# lives. The docstring's first line becomes the panel summary; the full
# docstring becomes `movate <cmd> --help`. Anything you'd put in `help=`
# belongs in the docstring instead.
app.command("watch", rich_help_panel=PANEL_DEVELOP)(watch_cmd.watch)

# ----- Run & evaluate -------------------------------------------------------

app.command("run", rich_help_panel=PANEL_RUN)(run_cmd.run)
# `replay` re-executes a past run with the same input — pairs with
# `mdk explain` (what happened?) for deterministic prompt iteration.
app.command("replay", rich_help_panel=PANEL_RUN)(replay_cmd.replay)
# `tune` sweeps one model knob (temperature/max_tokens/model) across a
# list of values for the same input. Deterministic helper, NOT
# auto-prompt-engineering — operators read the table + decide.
app.command("tune", rich_help_panel=PANEL_RUN)(tune_cmd.tune)
# `simulate` runs a chatbot through multi-turn scenarios — sibling to
# `eval` (single-turn) and `bench` (multi-model). Same panel.
app.command("simulate", rich_help_panel=PANEL_RUN)(simulate_cmd.simulate)
# `benchmark live` is shadow-traffic replay against a candidate model.
# Pairs with `bench` (multi-model on synthetic input) by using REAL
# recorded inputs from storage. Same panel.
app.add_typer(benchmark_app, name="benchmark", rich_help_panel=PANEL_RUN)
app.command("chat", rich_help_panel=PANEL_RUN)(chat_cmd.chat)
app.command("bench", rich_help_panel=PANEL_RUN)(bench_cmd.bench)
# `eval` is the scoring orchestrator. ``mdk eval harvest <agent>`` (ADR 016
# D1) is reachable as a sub-action: a Typer group whose
# ``invoke_without_command=True`` callback runs the orchestrator can't ALSO
# carry the orchestrator's positional ``path`` without that positional eating
# the ``harvest`` subcommand token (a Click group limitation). So we keep
# ``eval`` a plain command and route the ``harvest`` sub-action inside
# ``eval_`` (when the first positional is literally ``harvest``). Existing
# ``mdk eval …`` callsites are unchanged; ``mdk eval-harvest`` is also wired
# as a discoverable sibling (matches eval-gen / eval-scorecard).
# ``allow_extra_args`` + ``ignore_unknown_options`` let ``mdk eval harvest
# <agent> [harvest-flags]`` flow its trailing tokens into ``ctx.args`` so
# ``eval_`` can forward them to the harvest sub-action (the ``harvest`` token
# can't be a real Typer subcommand without its callback's ``path`` positional
# swallowing it — a Click group limitation). ``eval_`` re-asserts strict
# parsing for the NORMAL path: any leftover token when the first positional
# isn't ``harvest`` is rejected, so flag typos on ``mdk eval`` still error.
app.command(
    "eval",
    rich_help_panel=PANEL_RUN,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)(eval_cmd.eval_)
app.command("eval-harvest", rich_help_panel=PANEL_RUN)(eval_harvest_cmd.harvest)
# `eval-gen` is the sibling that creates a dataset; `eval` runs it.
# Sibling (not subcommand) because restructuring `eval` to a Typer
# sub-app would break ~30 test callsites. See eval_gen_cmd docstring.
app.command("eval-gen", rich_help_panel=PANEL_RUN)(eval_gen_cmd.eval_gen)
# `eval-scorecard` is the Phase 1 of the new eval flow: LLM-generated
# test cases + 10-category scorecard. Sibling pattern like eval-gen.
# Phase 3 will swap bare `mdk eval` to use this flow as the default.
app.command("eval-scorecard", rich_help_panel=PANEL_RUN)(eval_scorecard_cmd.eval_scorecard)
# `eval-schedule` (CRUD) + `eval-scheduler-tick` (cron entrypoint) wire the
# continuous-eval loop (ADR 016 D2). Sibling pattern like eval-gen/eval-scorecard.
# The tick is meant to be driven by an external cron (Azure: a Container Apps
# Job); there is no in-process timer daemon.
app.add_typer(eval_schedule_cmd.eval_schedule_app, name="eval-schedule", rich_help_panel=PANEL_RUN)
app.command("eval-scheduler-tick", rich_help_panel=PANEL_RUN)(eval_schedule_cmd.scheduler_tick)
# `schedule` (CRUD) + `scheduler-tick` (unified cron entrypoint) generalize the
# eval scheduler to arbitrary agent/workflow jobs (ADR 017 D2). The unified tick
# drains BOTH eval and generic schedules; eval-scheduler-tick stays eval-only
# for back-compat. Driven by an external cron (Azure: a Container Apps Job).
app.add_typer(schedule_cmd.schedule_app, name="schedule", rich_help_panel=PANEL_RUN)
app.command("scheduler-tick", rich_help_panel=PANEL_RUN)(schedule_cmd.scheduler_tick)
# `trigger` (CRUD) is the inbound-event sibling of `schedule` (ADR 017 D2): a
# schedule fires on cron, a trigger fires on an external webhook POST. Same
# panel since both register a standing way to enqueue agent/workflow jobs.
app.add_typer(trigger_cmd.trigger_app, name="trigger", rich_help_panel=PANEL_RUN)
# `canary` (ADR 016 D3) closes the improvement loop: route a slice of prod
# traffic to a challenger version, compare champion-vs-challenger live, then
# assisted-promote the winner. Additive + default-off; same panel since it's a
# rollout/lifecycle verb alongside schedule/trigger.
app.add_typer(canary_app, name="canary", rich_help_panel=PANEL_RUN)
app.add_typer(ci_app, name="ci", rich_help_panel=PANEL_RUN)
app.command("logs", rich_help_panel=PANEL_RUN)(logs_cmd.logs)
# `monitor` is the live counterpart to the historical `costs report` /
# `logs`. Same panel since it answers an adjacent operator question.
app.command("monitor", rich_help_panel=PANEL_RUN)(monitor_cmd.monitor)
app.add_typer(trace_app, name="trace", rich_help_panel=PANEL_RUN)

# ----- Remote (talk to a deployed runtime) ----------------------------------

app.command("submit", rich_help_panel=PANEL_RUN)(submit_cmd.submit)
app.add_typer(batch_app, name="batch", rich_help_panel=PANEL_RUN)
app.add_typer(jobs_app, name="jobs", rich_help_panel=PANEL_RUN)
# `runs` is the read-only sibling of `jobs`: look up a PAST run's result by
# id (the run_id a synchronous `mdk run --target` prints). Inline runs persist
# a RunRecord without a queryable JobRecord, so `jobs list` can't surface them
# — `runs show <run_id>` closes that gap via the existing GET /runs/{id}.
app.add_typer(runs_app, name="runs", rich_help_panel=PANEL_RUN)
app.add_typer(workflow_app, name="workflow", rich_help_panel=PANEL_RUN)

# ----- Diagnose -------------------------------------------------------------

# `doctor` is a sub-app: `mdk doctor` runs the default env-check via
# the callback's invoke_without_command path; `mdk doctor agent <name>`
# runs the per-agent doctor (Bundle B). The import of doctor_cmd above
# keeps the module loaded for backward-compat with any code reaching
# in via `from movate.cli import doctor`.
_ = doctor_cmd
app.add_typer(doctor_app, name="doctor", rich_help_panel=PANEL_DIAGNOSE)
# `explain` renders the decision chain behind a completed run — answers
# "what did the agent actually do?" and pairs naturally with `doctor`
# (environment health) and `fix` (remediation).
app.command("explain", rich_help_panel=PANEL_DIAGNOSE)(explain_cmd.explain)
# `fix` is the repair-side companion to `doctor` — same panel.
# Auto-remediates common diagnostic findings. Dry-run by default.
app.command("fix", rich_help_panel=PANEL_DIAGNOSE)(fix_cmd.fix)
app.command("pricing", rich_help_panel=PANEL_DIAGNOSE)(pricing_cmd.pricing)
# `models` is the model catalog — list all known models with pricing,
# context windows, and capability flags, or drill into one model.
# Sits alongside `pricing` (per-1k cost table) since both answer
# "what models can I use and what do they cost?".
app.add_typer(models_app, name="models", rich_help_panel=PANEL_DIAGNOSE)
# `costs` reports on historical spend (different from `pricing` which
# shows the live tariff). Both share the Diagnose panel since they
# answer adjacent operator questions ("what does this cost?" vs
# "what HAVE we spent?").
app.add_typer(costs_app, name="costs", rich_help_panel=PANEL_DIAGNOSE)
# `report` is the offline rollup (ADR 031 D3) — pass-rate / cost / latency /
# top-failure aggregates from the LOCAL store. Sits with `costs` + `explain`
# since all three answer "how are my agents doing?" without remote infra
# (rich dashboards live in Langfuse / Grafana — ADR 031 D1/D2).
app.command("report", rich_help_panel=PANEL_DIAGNOSE)(report_cmd.report)

# ----- Deploy & operate -----------------------------------------------------

app.command("serve", rich_help_panel=PANEL_DEPLOY)(serve_cmd.serve)
app.command("worker", rich_help_panel=PANEL_DEPLOY)(worker_cmd.worker)
app.command("deploy", rich_help_panel=PANEL_DEPLOY)(deploy_cmd.deploy)
# `infra apply` wraps the Bicep deploy + auto-chains into the
# bootstrap-seed flow so first-deploy on a fresh Azure environment is
# one command. Lives next to `deploy` since both target shared infra.
app.add_typer(infra_app, name="infra", rich_help_panel=PANEL_DEPLOY)
# `export` packages primitives for portability — adjacent to deploy
# (both ship things off-host). `export state` (item 26) is the DR escape
# hatch: a logical backup of operator-critical control-plane state.
export_app.command("state")(export_state_cmd)
app.add_typer(export_app, name="export", rich_help_panel=PANEL_DEPLOY)
app.add_typer(teams_bot_app, name="teams-bot", rich_help_panel=PANEL_DEPLOY)
app.add_typer(playground_app, name="playground", rich_help_panel=PANEL_DEPLOY)

# ----- Manage ---------------------------------------------------------------

app.add_typer(auth_app, name="auth", rich_help_panel=PANEL_MANAGE)
app.add_typer(keys_app, name="keys", rich_help_panel=PANEL_MANAGE)
app.add_typer(config_app, name="config", rich_help_panel=PANEL_MANAGE)
app.add_typer(fleet_app, name="fleet", rich_help_panel=PANEL_MANAGE)
app.add_typer(policy_app, name="policy", rich_help_panel=PANEL_MANAGE)
app.add_typer(profiles_app, name="profiles", rich_help_panel=PANEL_MANAGE)
app.add_typer(secrets_app, name="secrets", rich_help_panel=PANEL_MANAGE)
app.add_typer(snapshot_app, name="snapshot", rich_help_panel=PANEL_MANAGE)
app.command("diff", rich_help_panel=PANEL_MANAGE)(diff_cmd.diff)
app.command("rollback", rich_help_panel=PANEL_MANAGE)(rollback_cmd.rollback)
app.command("migrate", rich_help_panel=PANEL_MANAGE)(migrate_cmd.migrate)
app.command("migrate-state", rich_help_panel=PANEL_MANAGE)(migrate_state_cmd.migrate_state)
app.command("promote", rich_help_panel=PANEL_MANAGE)(promote_cmd.promote)
app.command("audit", rich_help_panel=PANEL_MANAGE)(audit_cmd.audit)
app.add_typer(guardrails_app, name="guardrails", rich_help_panel=PANEL_MANAGE)
app.add_typer(tenants_app, name="tenants", rich_help_panel=PANEL_MANAGE)
# `agent` surfaces the durable agent registry's version history + rollback
# (ADR 014 D3). Lives in MANAGE next to `rollback` / `promote` / `tenants`
# since it's a registry-lifecycle operation, not authoring (that's `dev`).
app.add_typer(agent_app, name="agent", rich_help_panel=PANEL_MANAGE)
# `memory` exposes the Sprint T MVP — list/get/set/evict/summarise/
# query against the operator-facing memory store. Lives in MANAGE
# alongside other state-managing commands (secrets, profiles, etc.).
app.add_typer(memory_app, name="memory", rich_help_panel=PANEL_MANAGE)


if __name__ == "__main__":
    app()
