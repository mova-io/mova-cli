"""Top-level Typer app. Subcommands live in sibling modules.

The CLI groups commands by intent so a teammate unfamiliar with movate can
navigate by what they want to *do* rather than memorize a flat list.

Shell completion: ``movate --install-completion`` (bash/zsh/fish/PowerShell).
"""

from __future__ import annotations

import logging
import os
import sys

import typer
from dotenv import load_dotenv

# Load .env from cwd (or any parent). Existing env vars take precedence.
load_dotenv()

# Bridge MDK_* ↔ MOVATE_* env vars in both directions. Runs BEFORE any
# other module reads os.environ so legacy and canonical prefixes are
# interchangeable for downstream readers. One-shot deprecation warning
# on first invocation if MOVATE_* vars are in use. See _env_aliases.py.
from movate.cli._env_aliases import sync_env_aliases  # noqa: E402

sync_env_aliases()

from movate import __version__  # noqa: E402
from movate.cli import (  # noqa: E402
    _console,
    audit_cmd,
    demo_cmd,
    diff_cmd,
    eval_gen_cmd,
    fix_cmd,
    fmt_cmd,
    menu_cmd,
    migrate_cmd,
    monitor_cmd,
    promote_cmd,
    replay_cmd,
    rollback_cmd,
    tune_cmd,
)
from movate.cli import bench as bench_cmd  # noqa: E402
from movate.cli import chat as chat_cmd  # noqa: E402
from movate.cli import deploy as deploy_cmd  # noqa: E402
from movate.cli import doctor as doctor_cmd  # noqa: E402
from movate.cli import eval as eval_cmd  # noqa: E402
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
from movate.cli.auth import auth_app  # noqa: E402
from movate.cli.ci import ci_app  # noqa: E402
from movate.cli.config_cmd import config_app  # noqa: E402
from movate.cli.costs_cmd import costs_app  # noqa: E402
from movate.cli.docs_cmd import docs_app  # noqa: E402
from movate.cli.export_oci_cmd import export_app  # noqa: E402
from movate.cli.import_lyzr import import_app  # noqa: E402
from movate.cli.inspect_cmd import inspect_app  # noqa: E402
from movate.cli.jobs import jobs_app  # noqa: E402
from movate.cli.policy_cmd import policy_app  # noqa: E402
from movate.cli.profiles_cmd import profiles_app  # noqa: E402
from movate.cli.scaffold import scaffold_app  # noqa: E402
from movate.cli.secrets_cmd import secrets_app  # noqa: E402
from movate.cli.skills_cmd import skills_app  # noqa: E402
from movate.cli.snapshot_cmd import snapshot_app  # noqa: E402
from movate.cli.teams_bot import teams_bot_app  # noqa: E402
from movate.cli.tenants import tenants_app  # noqa: E402
from movate.cli.trace import trace_app  # noqa: E402

PANEL_DEVELOP = "Develop"
PANEL_RUN = "Run & evaluate"
PANEL_DIAGNOSE = "Diagnose"
PANEL_DEPLOY = "Deploy & operate"
PANEL_MANAGE = "Manage"

app = typer.Typer(
    name="mdk",
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
        "-v",
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
app.command("demo", rich_help_panel=PANEL_DEVELOP)(demo_cmd.demo)
app.command("init", rich_help_panel=PANEL_DEVELOP)(init_cmd.init)
app.add_typer(import_app, name="import", rich_help_panel=PANEL_DEVELOP)
app.add_typer(scaffold_app, name="scaffold", rich_help_panel=PANEL_DEVELOP)
app.add_typer(skills_app, name="skills", rich_help_panel=PANEL_DEVELOP)
app.command("validate", rich_help_panel=PANEL_DEVELOP)(validate_cmd.validate)
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
app.command("chat", rich_help_panel=PANEL_RUN)(chat_cmd.chat)
app.command("bench", rich_help_panel=PANEL_RUN)(bench_cmd.bench)
app.command("eval", rich_help_panel=PANEL_RUN)(eval_cmd.eval_)
# `eval-gen` is the sibling that creates a dataset; `eval` runs it.
# Sibling (not subcommand) because restructuring `eval` to a Typer
# sub-app would break ~30 test callsites. See eval_gen_cmd docstring.
app.command("eval-gen", rich_help_panel=PANEL_RUN)(eval_gen_cmd.eval_gen)
app.add_typer(ci_app, name="ci", rich_help_panel=PANEL_RUN)
app.command("logs", rich_help_panel=PANEL_RUN)(logs_cmd.logs)
# `monitor` is the live counterpart to the historical `costs report` /
# `logs`. Same panel since it answers an adjacent operator question.
app.command("monitor", rich_help_panel=PANEL_RUN)(monitor_cmd.monitor)
app.add_typer(trace_app, name="trace", rich_help_panel=PANEL_RUN)

# ----- Remote (talk to a deployed runtime) ----------------------------------

app.command("submit", rich_help_panel=PANEL_RUN)(submit_cmd.submit)
app.add_typer(jobs_app, name="jobs", rich_help_panel=PANEL_RUN)

# ----- Diagnose -------------------------------------------------------------

app.command("doctor", rich_help_panel=PANEL_DIAGNOSE)(doctor_cmd.doctor)
# `fix` is the repair-side companion to `doctor` — same panel.
# Auto-remediates common diagnostic findings. Dry-run by default.
app.command("fix", rich_help_panel=PANEL_DIAGNOSE)(fix_cmd.fix)
app.command("pricing", rich_help_panel=PANEL_DIAGNOSE)(pricing_cmd.pricing)
# `costs` reports on historical spend (different from `pricing` which
# shows the live tariff). Both share the Diagnose panel since they
# answer adjacent operator questions ("what does this cost?" vs
# "what HAVE we spent?").
app.add_typer(costs_app, name="costs", rich_help_panel=PANEL_DIAGNOSE)

# ----- Deploy & operate -----------------------------------------------------

app.command("serve", rich_help_panel=PANEL_DEPLOY)(serve_cmd.serve)
app.command("worker", rich_help_panel=PANEL_DEPLOY)(worker_cmd.worker)
app.command("deploy", rich_help_panel=PANEL_DEPLOY)(deploy_cmd.deploy)
# `export` packages primitives for portability — adjacent to deploy
# (both ship things off-host).
app.add_typer(export_app, name="export", rich_help_panel=PANEL_DEPLOY)
app.add_typer(teams_bot_app, name="teams-bot", rich_help_panel=PANEL_DEPLOY)

# ----- Manage ---------------------------------------------------------------

app.add_typer(auth_app, name="auth", rich_help_panel=PANEL_MANAGE)
app.add_typer(config_app, name="config", rich_help_panel=PANEL_MANAGE)
app.add_typer(policy_app, name="policy", rich_help_panel=PANEL_MANAGE)
app.add_typer(profiles_app, name="profiles", rich_help_panel=PANEL_MANAGE)
app.add_typer(secrets_app, name="secrets", rich_help_panel=PANEL_MANAGE)
app.add_typer(snapshot_app, name="snapshot", rich_help_panel=PANEL_MANAGE)
app.command("diff", rich_help_panel=PANEL_MANAGE)(diff_cmd.diff)
app.command("rollback", rich_help_panel=PANEL_MANAGE)(rollback_cmd.rollback)
app.command("migrate", rich_help_panel=PANEL_MANAGE)(migrate_cmd.migrate)
app.command("promote", rich_help_panel=PANEL_MANAGE)(promote_cmd.promote)
app.command("audit", rich_help_panel=PANEL_MANAGE)(audit_cmd.audit)
app.add_typer(tenants_app, name="tenants", rich_help_panel=PANEL_MANAGE)


if __name__ == "__main__":
    app()
