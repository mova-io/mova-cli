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
from movate.cli import _console  # noqa: E402
from movate.cli import bench as bench_cmd  # noqa: E402
from movate.cli import chat as chat_cmd  # noqa: E402
from movate.cli import deploy as deploy_cmd  # noqa: E402
from movate.cli import doctor as doctor_cmd  # noqa: E402
from movate.cli import eval as eval_cmd  # noqa: E402
from movate.cli import (  # noqa: E402
    import_json as _import_json,  # noqa: F401  -- registers `json` on import_app
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
from movate.cli.import_lyzr import import_app  # noqa: E402
from movate.cli.jobs import jobs_app  # noqa: E402
from movate.cli.scaffold import scaffold_app  # noqa: E402
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
        "Common workflows:\n"
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
        envvar="MOVATE_TARGET",
        help=(
            "Default deployment target for remote commands "
            "(submit, jobs *). Overridden by a per-command --target. "
            "Falls back to MOVATE_TARGET env var, then to the active "
            "config target."
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

app.command("init", rich_help_panel=PANEL_DEVELOP)(init_cmd.init)
app.add_typer(import_app, name="import", rich_help_panel=PANEL_DEVELOP)
app.add_typer(scaffold_app, name="scaffold", rich_help_panel=PANEL_DEVELOP)
app.command("validate", rich_help_panel=PANEL_DEVELOP)(validate_cmd.validate)
app.command("show", rich_help_panel=PANEL_DEVELOP)(show_cmd.show)
# NOTE: do NOT pass `help=` here — Typer/Click then ignores the function's
# docstring, which is where each command's [bold]Examples:[/bold] block
# lives. The docstring's first line becomes the panel summary; the full
# docstring becomes `movate <cmd> --help`. Anything you'd put in `help=`
# belongs in the docstring instead.
app.command("watch", rich_help_panel=PANEL_DEVELOP)(watch_cmd.watch)

# ----- Run & evaluate -------------------------------------------------------

app.command("run", rich_help_panel=PANEL_RUN)(run_cmd.run)
app.command("chat", rich_help_panel=PANEL_RUN)(chat_cmd.chat)
app.command("bench", rich_help_panel=PANEL_RUN)(bench_cmd.bench)
app.command("eval", rich_help_panel=PANEL_RUN)(eval_cmd.eval_)
app.add_typer(ci_app, name="ci", rich_help_panel=PANEL_RUN)
app.command("logs", rich_help_panel=PANEL_RUN)(logs_cmd.logs)
app.add_typer(trace_app, name="trace", rich_help_panel=PANEL_RUN)

# ----- Remote (talk to a deployed runtime) ----------------------------------

app.command("submit", rich_help_panel=PANEL_RUN)(submit_cmd.submit)
app.add_typer(jobs_app, name="jobs", rich_help_panel=PANEL_RUN)

# ----- Diagnose -------------------------------------------------------------

app.command("doctor", rich_help_panel=PANEL_DIAGNOSE)(doctor_cmd.doctor)
app.command("pricing", rich_help_panel=PANEL_DIAGNOSE)(pricing_cmd.pricing)

# ----- Deploy & operate -----------------------------------------------------

app.command("serve", rich_help_panel=PANEL_DEPLOY)(serve_cmd.serve)
app.command("worker", rich_help_panel=PANEL_DEPLOY)(worker_cmd.worker)
app.command("deploy", rich_help_panel=PANEL_DEPLOY)(deploy_cmd.deploy)

# ----- Manage ---------------------------------------------------------------

app.add_typer(auth_app, name="auth", rich_help_panel=PANEL_MANAGE)
app.add_typer(config_app, name="config", rich_help_panel=PANEL_MANAGE)
app.add_typer(tenants_app, name="tenants", rich_help_panel=PANEL_MANAGE)


if __name__ == "__main__":
    app()
