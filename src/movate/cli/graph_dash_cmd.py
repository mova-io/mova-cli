"""``mdk graph serve-dash`` — Dash + dash-cytoscape knowledge-graph viewer.

The Python-native production viewer option in the viz bake-off. Starts a
Dash app (pure Python — no JS authored) that renders the knowledge graph
the runtime's graph API serves, via ``dash-cytoscape``.

Quickstart::

    # Install the optional extra (dash + dash-cytoscape)
    uv pip install 'movate-cli[graph-dash]'

    # Point at a configured target; bearer key is read from the target's
    # key_env and held SERVER-SIDE — never sent to the browser.
    mdk graph serve-dash --target prod --project proj_123

Design notes:

* **Through the API, not the DB.** The viewer fetches graph data over HTTP
  from the runtime's graph API (``GET /api/v1/projects/{id}/graph`` etc.).
  Tenant scoping, auth, and result caps live in the API — this viewer never
  touches Postgres.
* **Bearer key stays server-side.** ``--target`` resolves to a runtime URL +
  a bearer token (from the target's ``key_env``). The token is injected into
  the Dash *server* process only; it is never serialized into a Dash
  component, a ``dcc.Store``, the page, or any log line.
* **Opt-in heavy deps.** ``dash`` / ``dash-cytoscape`` are gated behind the
  ``graph-dash`` extra. Without it, this command prints a friendly install
  hint and exits cleanly (no traceback).

Distinct command name (``serve-dash``) + distinct modules
(``graph_dash_cmd`` / ``graph_dash_app``) so this never collides with the
in-flight sigma viewer (``mdk graph serve``) or other bake-off options.
"""

from __future__ import annotations

import threading
import webbrowser

import typer
from rich.console import Console

from movate.cli._console import echo_remote_context, error, get_global_target
from movate.core.user_config import (
    UserConfigError,
    resolve_bearer_token,
    resolve_target,
)

err = Console(stderr=True)


# A ``graph`` command group. The in-flight sigma viewer also hangs commands
# off ``mdk graph`` (``serve``); registering our own group here is additive
# and idempotent — if that group already exists when both land, the merge is
# a one-liner. ``serve-dash`` is a distinct subcommand either way.
graph_app = typer.Typer(
    name="graph",
    help="Knowledge-graph viewers and tools.",
    no_args_is_help=True,
)


def _ensure_dash_installed() -> None:
    """Friendly error + clean exit when the ``graph-dash`` extra is absent.

    ``dash`` / ``dash-cytoscape`` are heavy (Flask, plotly, a bundled JS
    runtime) so they're gated behind an opt-in extra to keep the default
    install lean. A clear install hint here beats a cryptic ImportError
    from deep inside the app module.
    """
    try:
        import dash  # noqa: F401, PLC0415
        import dash_cytoscape  # noqa: F401, PLC0415
    except ImportError:
        err.print(
            "[red]✗[/red] [bold]dash[/bold] / [bold]dash-cytoscape[/bold] not "
            "installed. The Dash knowledge-graph viewer is gated behind an "
            "optional extra to keep the default install size down.\n\n"
            "Install with:\n  "
            "[bold]uv pip install 'movate-cli\\[graph-dash]'[/bold]\n\n"
            "Or, if you installed mdk as a uv tool:\n  "
            "[bold]uv tool install 'movate-cli\\[graph-dash]'[/bold]\n\n"
            "Or, for a development install of this repo:\n  "
            "[bold]uv sync --extra graph-dash[/bold]"
        )
        raise typer.Exit(code=2) from None


@graph_app.command("serve-dash")
def serve_dash(
    target: str | None = typer.Option(
        None,
        "--target",
        "-t",
        help=(
            "Deployment target to view the graph of. Resolves to the "
            "runtime URL + bearer token (from the target's key_env). The "
            "bearer key is held server-side and never sent to the browser."
        ),
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help=(
            "Project id whose graph to render "
            "(GET /api/v1/projects/{id}/graph). Omit to use the runtime's "
            "default/project-less graph endpoint."
        ),
    ),
    port: int = typer.Option(
        8901,
        "--port",
        help=(
            "Bind port for the Dash UI. Defaults to 8901 — distinct from "
            "the sigma viewer + the runtime so they can run side-by-side."
        ),
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help=(
            "Bind host. ``127.0.0.1`` (default) restricts to localhost; the "
            "bearer key lives in this process, so do NOT bind to 0.0.0.0 "
            "without a trusted reverse proxy in front."
        ),
    ),
    no_open: bool = typer.Option(
        False,
        "--no-open",
        help="Don't auto-open the browser on launch (CI / SSH / headless).",
    ),
    live: bool = typer.Option(
        False,
        "--live",
        help=(
            "Enable the experimental live-growth poll: a 10s dcc.Interval "
            "re-fetches the windowed graph and merges in new nodes. Off by "
            "default (see PR notes — deferred as a follow-up to harden)."
        ),
    ),
) -> None:
    """Launch the Dash + dash-cytoscape knowledge-graph viewer.

    Resolves ``--target`` to a runtime URL + bearer token, builds a Dash app
    that fetches the graph through the runtime API, and serves it on
    ``http://<host>:<port>``. The bearer token is held server-side; the
    browser only ever receives graph data.
    """
    # 1. Optional-extra preflight — friendly hint + clean exit if absent.
    _ensure_dash_installed()

    # 2. Resolve the target → runtime URL + bearer token (server-side only).
    try:
        target_name, target_cfg = resolve_target(target or get_global_target())
        token = resolve_bearer_token(target_cfg)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None

    # Echo target/URL/credential-source (masked) for self-diagnosing 401s.
    # echo_remote_context masks the key — the raw token is never printed.
    echo_remote_context(target_name, target_cfg)

    # 3. Build the Dash app. Imported HERE (after the extra check) so the
    #    module's top-level ``import dash`` only runs once we know it's safe.
    from movate.cli.graph_dash_app import build_app  # noqa: PLC0415

    app = build_app(
        base_url=target_cfg.url,
        bearer_token=token,
        project_id=project,
        poll_live=live,
    )

    url = f"http://{host}:{port}"
    err.print(
        f"[bold cyan]MDK graph viewer (Dash)[/bold cyan] → "
        f"runtime [bold]{target_cfg.url}[/bold], UI on [bold]{url}[/bold]"
    )
    err.print(
        "[dim]Auth: bearer token held server-side, injected from the "
        "target's credentials. The browser never receives it.[/dim]"
    )
    if live:
        err.print("[yellow]⚠[/yellow] live-growth poll enabled (experimental).")

    # 4. Auto-open the browser unless suppressed. Done on a short timer in a
    #    background thread so the page is ready when the tab opens.
    if not no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    # 5. Serve. ``app.run`` blocks until Ctrl-C.
    try:
        app.run(host=host, port=port, debug=False)
    except KeyboardInterrupt:
        err.print("\n[dim]graph viewer stopped.[/dim]")
