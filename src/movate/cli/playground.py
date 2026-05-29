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

    # Across ALL configured targets (dev/staging/prod) — a chat-profile
    # picker per target; pick a target, then its agents
    mdk config add-target dev   --url http://... --key-env MDK_DEV_KEY
    mdk config add-target prod  --url https://... --key-env MDK_PROD_KEY
    mdk playground serve              # auto: targets configured, no --runtime-url

The bearer token is read from ``MOVATE_API_KEY`` (or
``MDK_PLAYGROUND_API_KEY``) by default — never pass it on the command
line. To point at a different env var, ``export`` it onto
``MDK_PLAYGROUND_API_KEY`` before launching.

**Multi-target mode** activates automatically when deployment targets
are registered in ``~/.movate/config.yaml`` (``mdk config list-targets``)
AND no explicit ``--runtime-url`` / ``--api-key`` is given: the UI shows
one Chainlit chat profile per target (label = name + URL), and selecting
one points that session at the target's runtime + per-target bearer token
(resolved from its ``key_env``). Pass ``--runtime-url`` (or ``--no-targets``)
to force the original single-runtime flow; ``--all-targets`` forces
multi-target explicitly.

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

import click
import typer
from rich.console import Console

from movate.playground.targets import (
    TARGETS_ENV_VAR,
    PlaygroundTarget,
    encode_targets,
    resolve_targets_from_config,
)

err = Console(stderr=True)


def _explicit_runtime_override(ctx: typer.Context) -> bool:
    """Did the operator explicitly pin a single runtime (URL or key)?

    Multi-target mode (chat-profile-per-target) must NEVER pre-empt the
    original single-runtime flow. So we treat ANY explicit ``--runtime-url``
    / ``--api-key`` — whether on the command line OR via the
    ``MDK_PLAYGROUND_*`` / ``MOVATE_*`` env vars — as "the operator wants
    one runtime", and stay in single-runtime mode. Click's
    ``get_parameter_source`` distinguishes an explicit value from the
    option's default, which is exactly the signal we need (a bare
    ``mdk playground serve`` leaves both at DEFAULT).
    """
    explicit = {click.core.ParameterSource.COMMANDLINE, click.core.ParameterSource.ENVIRONMENT}
    return any(ctx.get_parameter_source(param) in explicit for param in ("runtime_url", "api_key"))


def _resolve_target_mode(
    ctx: typer.Context,
    *,
    all_targets: bool,
    no_targets: bool,
) -> tuple[bool, list[PlaygroundTarget]]:
    """Decide single-runtime vs multi-target and resolve the target list.

    Precedence (first match wins):

    1. ``--no-targets`` → always single-runtime.
    2. an explicit ``--runtime-url`` / ``--api-key`` → single-runtime
       (back-compat: the operator pinned one runtime; never surprise them
       with a picker). ``--all-targets`` here is a soft no-op (warned).
    3. ``--all-targets`` → multi-target; errors if none are registered.
    4. auto: targets configured AND no explicit override → multi-target.

    Anything else stays single-runtime (today's behavior). Returns
    ``(multi_target, targets)`` — ``targets`` is empty in single-runtime
    mode. Exits 2 on a contradictory flag combination.
    """
    if all_targets and no_targets:
        err.print("[red]✗[/red] pass at most one of --all-targets / --no-targets.")
        raise typer.Exit(code=2)

    if no_targets:
        return False, []

    if _explicit_runtime_override(ctx):
        if all_targets:
            err.print(
                "[yellow]⚠[/yellow] --all-targets ignored: an explicit "
                "--runtime-url / --api-key pins a single runtime."
            )
        return False, []

    targets = _load_playground_targets()
    if all_targets and not targets:
        err.print(
            "[red]✗[/red] --all-targets given but no targets are "
            "registered. Add one with [bold]mdk config add-target[/bold]."
        )
        raise typer.Exit(code=2)
    # Auto-on when targets exist; --all-targets forces the same path.
    return bool(targets), targets


def _load_playground_targets() -> list[PlaygroundTarget]:
    """Read configured targets from ``~/.movate/config.yaml`` + resolve keys.

    Reuses the SAME loader ``mdk config list-targets`` uses
    (:func:`movate.core.user_config.load_user_config`) — no reinvented
    config parsing. Per-target bearer tokens resolve from each target's
    ``key_env`` env var, which ``movate.credentials.loader`` already
    autoloads from ``~/.movate/credentials`` at CLI startup. Targets with
    a missing key are KEPT (with ``key_available=False``) so the picker can
    show them disabled with a hint rather than silently dropping them.
    """
    from movate.core.user_config import load_user_config  # noqa: PLC0415

    cfg = load_user_config()
    return resolve_targets_from_config(cfg.targets)


def _print_launch_banner(
    *,
    multi_target: bool,
    targets: list[PlaygroundTarget],
    runtime_url: str,
    api_key: str | None,
    host: str,
    port: int,
    voice: bool = False,
) -> None:
    """Print the startup banner to stderr (never echoes the bearer token).

    Two shapes: a per-target summary (multi-target mode) listing each
    registered runtime + whether its key resolved, or the original
    single-runtime line + auth hint. Only target NAMES / URLs / key_env
    *names* are printed — never a resolved key value. When ``voice`` is on,
    a one-line note flags that mic input is enabled.
    """
    if voice:
        err.print(
            "[bold magenta]🎙 voice mode[/bold magenta] enabled — talk to "
            "agents with your mic (text still works). The runtime must expose "
            "the voice route, else the UI shows a friendly 'not enabled' note."
        )
    if multi_target and targets:
        ready = sum(1 for t in targets if t.key_available)
        err.print(
            f"[bold cyan]MDK playground[/bold cyan] → multi-target "
            f"([bold]{len(targets)}[/bold] target(s), [bold]{ready}[/bold] "
            f"with a key), UI on [bold]http://{host}:{port}[/bold]"
        )
        for t in targets:
            mark = "[green]●[/green]" if t.key_available else "[yellow]○[/yellow]"
            note = "" if t.key_available else f" [dim](no key — set {t.key_env})[/dim]"
            err.print(f"  {mark} [bold]{t.name}[/bold] → {t.url}{note}")
        err.print(
            "[dim]Pick a target in the chat-profile selector, then an "
            "agent. Pass --runtime-url to pin a single runtime instead.[/dim]"
        )
        return

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
    ctx: typer.Context,
    runtime_url: str = typer.Option(
        "http://127.0.0.1:8000",
        "--runtime-url",
        envvar=["MDK_PLAYGROUND_RUNTIME_URL", "MOVATE_RUNTIME_URL"],
        help=(
            "Base URL of the runtime to test against. Defaults to "
            "the local ``mdk serve`` default. For a deployed runtime, "
            "pass the ACA URL (e.g. https://...azurecontainerapps.io). "
            "Passing this (or its env var) pins a SINGLE runtime and "
            "disables the multi-target chat-profile picker."
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
    all_targets: bool = typer.Option(
        False,
        "--all-targets",
        help=(
            "Force multi-target mode: surface ONE chat profile per "
            "target registered in ``~/.movate/config.yaml`` (the same "
            "targets ``mdk config list-targets`` shows). Pick a target, "
            "then its agents. This is the AUTO default when targets are "
            "configured and no ``--runtime-url`` is given; the flag makes "
            "it explicit (and errors if no targets are registered)."
        ),
    ),
    no_targets: bool = typer.Option(
        False,
        "--no-targets",
        help=(
            "Force single-runtime mode even when targets are configured "
            "— talk only to ``--runtime-url`` (default: the local "
            "runtime). Skips the chat-profile target picker."
        ),
    ),
    voice: bool = typer.Option(
        False,
        "--voice",
        help=(
            "Enable voice mode: talk to the agent with your mic. Captures "
            "audio in the browser, streams it to the runtime's voice "
            "WebSocket (``WS /api/v1/agents/{name}/voice``), renders the live "
            "transcript + the agent's answer, and plays the synthesized reply "
            "back. Off by default — the text playground is unchanged. The "
            "target runtime must expose the voice route (voice Phase 1); if it "
            "doesn't, the UI shows a friendly 'voice not enabled' message and "
            "text still works."
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

    multi_target, targets = _resolve_target_mode(
        ctx, all_targets=all_targets, no_targets=no_targets
    )

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
    # Voice mode (opt-in): the app reads this at import to register the audio
    # callbacks. Absent → text-only (the original flow, byte-for-byte unchanged).
    if voice:
        env["MDK_PLAYGROUND_VOICE"] = "1"
    # Multi-target: hand the resolved target list (URL + key_env + the
    # already-resolved bearer token) to the app via one JSON env var.
    # Its presence is what flips the app into chat-profile mode; absence
    # keeps the original single-runtime path byte-for-byte unchanged.
    if multi_target and targets:
        env[TARGETS_ENV_VAR] = encode_targets(targets)
    else:
        # Defensive: never let a stale value leak from the parent env into
        # a single-runtime launch.
        env.pop(TARGETS_ENV_VAR, None)

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

    _print_launch_banner(
        multi_target=multi_target,
        targets=targets,
        runtime_url=runtime_url,
        api_key=api_key,
        host=host,
        port=port,
        voice=voice,
    )

    try:
        subprocess.run(chainlit_cmd, env=env, check=True)
    except subprocess.CalledProcessError as exc:
        err.print(f"[red]✗[/red] chainlit exited with code {exc.returncode}")
        raise typer.Exit(code=exc.returncode) from None
    except KeyboardInterrupt:
        # Clean Ctrl-C exit — chainlit handles SIGINT internally.
        err.print("\n[dim]playground stopped.[/dim]")
