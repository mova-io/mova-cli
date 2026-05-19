"""``mdk playground`` — Chainlit-based UI for testing deployed agents.

Quickstart::

    # Install the optional extra (Chainlit + httpx)
    uv pip install 'movate-cli[playground]'

    # Local dev: point at the local runtime
    mdk serve --port 8000           # terminal 1
    mdk worker --mock                # terminal 2
    mdk playground serve             # terminal 3 → browser

    # Against a deployed runtime
    export MOVATE_API_KEY=mvt_live_...
    mdk playground serve --runtime-url https://movate-prod-api.eastus2... \\
        --api-key-env MOVATE_API_KEY

The playground is a Chainlit app under the hood — every feature
Chainlit ships (👍/👎 widgets, thread persistence, chat history,
file uploads, OAuth) is available. The wrapper here is just config
plumbing + a `chainlit run` shell-out.
"""

from __future__ import annotations

import os
import subprocess
import sys

import typer
from rich.console import Console

err = Console(stderr=True)


playground_app = typer.Typer(
    name="playground",
    help=(
        "Browser UI for testing deployed agents. Powered by Chainlit. "
        "Captures 👍/👎/comment feedback and persists to the runtime's "
        "Postgres + Langfuse for analysis + agent tuning."
    ),
    no_args_is_help=True,
)


def _ensure_chainlit_installed() -> None:
    """Surface a friendly error when the optional extra is missing.

    Chainlit isn't a runtime dependency — only operators running the
    playground need it. Default ``mdk install`` doesn't pull it in
    so a clear hint here beats the cryptic ImportError downstream.
    """
    try:
        import chainlit  # noqa: F401, PLC0415
    except ImportError:
        err.print(
            "[red]✗[/red] [bold]chainlit[/bold] not installed. "
            "The playground is gated behind an optional extra to keep "
            "the default install size down.\n\n"
            "Install with:\n  "
            "[bold]uv pip install 'movate-cli[playground]'[/bold]\n\n"
            "Or, for a development install of this repo:\n  "
            "[bold]uv sync --extra playground[/bold]"
        )
        raise typer.Exit(code=2) from None


@playground_app.command("serve")
def serve(
    runtime_url: str = typer.Option(
        "http://127.0.0.1:8000",
        "--runtime-url",
        envvar=["MDK_PLAYGROUND_RUNTIME_URL", "MOVATE_RUNTIME_URL"],
        help=(
            "Base URL of the runtime to test against. Defaults to "
            "the local ``mdk serve`` default. For a deployed runtime, "
            "pass the ACA URL (e.g. https://...azurecontainerapps.io)."
        ),
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        envvar=["MDK_PLAYGROUND_API_KEY", "MOVATE_API_KEY"],
        help=(
            "Bearer token for the runtime. Required when the runtime "
            "enforces auth (production); optional for local dev. Read "
            "from ``MDK_PLAYGROUND_API_KEY`` or ``MOVATE_API_KEY`` env "
            "vars by default — never paste keys on the command line."
        ),
    ),
    port: int = typer.Option(
        8765,
        "--port",
        help=(
            "Bind port for the Chainlit UI. Defaults to 8765 — leaves "
            "Chainlit's docs default (8000) free for the runtime if "
            "they run side-by-side."
        ),
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help=(
            "Bind host. ``127.0.0.1`` (default) restricts to localhost; "
            "use ``0.0.0.0`` for container deployments behind a "
            "trusted reverse proxy."
        ),
    ),
    headless: bool = typer.Option(
        False,
        "--headless",
        help=(
            "Don't auto-open the browser. Default behavior opens "
            "``http://<host>:<port>`` on serve; ``--headless`` is "
            "useful for CI / container deploys / SSH tunnels."
        ),
    ),
) -> None:
    """Launch the Chainlit playground UI for testing deployed agents.

    The browser UI lists agents from the configured runtime, lets you
    submit JSON input for each, displays the structured output, and
    captures 👍/👎/comment feedback that the runtime persists to
    Postgres (and pushes to Langfuse if configured).
    """
    _ensure_chainlit_installed()

    # Chainlit reads its config from env vars + CLI flags. We export
    # the runtime config under MDK_PLAYGROUND_* so the app module
    # (``movate.playground.app``) can pick them up at start time.
    env = os.environ.copy()
    env["MDK_PLAYGROUND_RUNTIME_URL"] = runtime_url
    if api_key:
        env["MDK_PLAYGROUND_API_KEY"] = api_key

    # ``chainlit run`` takes a path to a Python module file. We point
    # it at our app module's __file__ — works whether installed via
    # editable install or wheel. The ``-h`` flag suppresses Chainlit's
    # auto-browser-open (we handle that ourselves to respect
    # ``--headless``).
    import movate.playground.app as app_module  # noqa: PLC0415

    chainlit_cmd = [
        sys.executable,
        "-m",
        "chainlit",
        "run",
        app_module.__file__,
        "--host",
        host,
        "--port",
        str(port),
    ]
    if headless:
        chainlit_cmd.append("--headless")

    err.print(
        f"[bold cyan]MDK playground[/bold cyan] → "
        f"runtime [bold]{runtime_url}[/bold], "
        f"UI on [bold]http://{host}:{port}[/bold]"
    )
    if api_key:
        err.print(
            "[dim]Auth: using bearer token from env. "
            "Operators sign in via the runtime's standard auth flow.[/dim]"
        )
    else:
        err.print(
            "[yellow]⚠[/yellow] no API key set — only works for runtimes "
            "started without auth (local dev / staging). For production "
            "set [bold]MOVATE_API_KEY[/bold] or pass [bold]--api-key[/bold]."
        )

    try:
        subprocess.run(chainlit_cmd, env=env, check=True)
    except subprocess.CalledProcessError as exc:
        err.print(f"[red]✗[/red] chainlit exited with code {exc.returncode}")
        raise typer.Exit(code=exc.returncode) from None
    except KeyboardInterrupt:
        # Clean Ctrl-C exit — chainlit handles SIGINT internally.
        err.print("\n[dim]playground stopped.[/dim]")
