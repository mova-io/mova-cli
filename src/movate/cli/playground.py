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
    mdk playground serve --runtime-url https://movate-prod-api.eastus2...

The bearer token is read from ``MOVATE_API_KEY`` (or
``MDK_PLAYGROUND_API_KEY``) by default — never pass it on the command
line. To point at a different env var, ``export`` it onto
``MDK_PLAYGROUND_API_KEY`` before launching.

The playground is a ChatGPT-like Chainlit app: multi-turn chat with a
deployed agent, a past-conversations sidebar (resume prior chats), file
uploads (text extracted into context, optionally persisted to the
agent's KB), live token streaming, and 👍/👎/comment feedback. It is
**capability-aware** — it asks the runtime what it supports
(``GET /api/v1/capabilities``) and uses server-managed sessions /
streaming / the feedback API when available, falling back to
client-managed history + buffered responses otherwise.
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
        "ChatGPT-like browser UI for testing deployed agents. Powered by "
        "Chainlit. Multi-turn chat, a past-conversations sidebar, file "
        "uploads, live streaming, and 👍/👎/comment feedback persisted to "
        "the runtime's Postgres + Langfuse for analysis + agent tuning."
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
            "[bold]uv pip install 'movate-cli\\[playground]'[/bold]\n\n"
            "Or, if you installed mdk as a uv tool:\n  "
            "[bold]uv tool install 'movate-cli\\[playground]'[/bold]\n\n"
            "Or, for a development install of this repo:\n  "
            "[bold]uv sync --extra playground[/bold]"
        )
        raise typer.Exit(code=2) from None


def _warn_if_unstable_python() -> None:
    """Soft-warn on Python 3.14, where the chat UI hits NoEventLoopError.

    On CPython 3.14 chainlit's async stack (starlette FileResponse →
    anyio.to_thread.run_sync → sniffio) fails because sniffio 1.3.1
    can't detect the asyncio event loop, raising ``anyio.NoEventLoopError``
    the first time a request hits the UI (e.g. serving static files).
    Startup looks fine, so the failure is baffling without this hint.

    ``uv tool install`` picks the newest interpreter by default, so
    operators land on 3.14 without choosing it. We warn rather than
    hard-exit: a future sniffio/anyio release may add 3.14 support, and
    blocking launch outright would strand them once it does.
    """
    if sys.version_info < (3, 14):
        return
    err.print(
        "[yellow]⚠[/yellow] The playground's chat UI is unstable on "
        "Python 3.14 (chainlit/sniffio cannot detect the asyncio event "
        "loop, causing anyio.NoEventLoopError when serving the UI). "
        "Reinstall mdk on Python 3.13:\n  "
        "[bold]uv tool install --reinstall --python 3.13 "
        "'movate-cli\\[playground]'[/bold]"
    )


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
    no_history: bool = typer.Option(
        False,
        "--no-history",
        help=(
            "Disable thread persistence (the past-conversations sidebar / "
            "resume). The playground runs ephemeral — each refresh starts "
            "a fresh chat. By default threads persist to a local SQLite "
            "file at ``~/.mdk/playground/threads.db`` (or a Postgres URL "
            "from ``MDK_PLAYGROUND_THREADS_URL`` / ``DATABASE_URL``)."
        ),
    ),
    persist_uploads: bool = typer.Option(
        False,
        "--persist-uploads",
        help=(
            "Auto-ingest uploaded files into the agent's KB (instead of "
            "only holding their text as conversation context). Off by "
            "default — uploads stay session-scoped and you opt in per "
            "file via the 'Add to agent's KB permanently' button in the "
            "chat. ``--persist-uploads`` flips the default to always-ingest."
        ),
    ),
) -> None:
    """Launch the ChatGPT-like Chainlit playground for testing agents.

    The browser UI lists agents from the configured runtime; pick one and
    chat with it multi-turn. Attach files (text extracted into context,
    optionally persisted to the agent's KB), resume past conversations
    from the sidebar, watch tokens stream live (when the runtime supports
    it), and capture 👍/👎/comment feedback that the runtime persists to
    Postgres (and pushes to Langfuse if configured).

    Capability-aware: the playground asks the runtime what it supports
    and auto-upgrades to server-managed sessions / streaming / the
    feedback API when available, falling back to client-managed history +
    buffered responses otherwise.
    """
    _ensure_chainlit_installed()
    _warn_if_unstable_python()

    # Chainlit reads its config from env vars + CLI flags. We export
    # the runtime config under MDK_PLAYGROUND_* so the app module
    # (``movate.playground.app``) can pick them up at start time. The
    # bearer token is set on the SERVER process env only — it never
    # reaches browser JS and is never logged.
    env = os.environ.copy()
    env["MDK_PLAYGROUND_RUNTIME_URL"] = runtime_url
    if api_key:
        env["MDK_PLAYGROUND_API_KEY"] = api_key
    if no_history:
        env["MDK_PLAYGROUND_NO_HISTORY"] = "1"
    if persist_uploads:
        env["MDK_PLAYGROUND_PERSIST_UPLOADS"] = "1"

    # ``chainlit run`` takes a path to a Python module file. We resolve
    # the app module's file via importlib *without executing it* — the app
    # module imports chainlit at top level (by design, for a clear error),
    # and the actual chainlit import belongs in the child ``chainlit run``
    # process, not this parent. ``find_spec`` reads the path off the module
    # spec without running the body.
    import importlib.util  # noqa: PLC0415

    spec = importlib.util.find_spec("movate.playground.app")
    if spec is None or spec.origin is None:
        err.print("[red]✗[/red] could not locate the playground app module.")
        raise typer.Exit(code=1)
    app_module_file = spec.origin

    chainlit_cmd = [
        sys.executable,
        "-m",
        "chainlit",
        "run",
        app_module_file,
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
