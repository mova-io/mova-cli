"""``movate serve`` — run the FastAPI runtime.

Builds the app via :func:`build_app`, scanning ``agents_path`` for
agent definitions at startup, and binds uvicorn to the requested
host/port. Storage init runs on the same loop uvicorn drives so
``aiosqlite`` connections aren't bound to a dead loop.

Workers (stage 4) consume the queue this populates. Without a
worker running, jobs land in the ``QUEUED`` state and stay there —
``GET /jobs/{id}`` returns the queued state, ``/run`` still works.
For end-to-end execution, run ``movate worker`` in a sibling
process.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import typer
import uvicorn
from rich.console import Console

from movate.cli._console import hint, success
from movate.runtime.app import build_app
from movate.runtime.registry import scan_agents
from movate.storage import build_storage

err = Console(stderr=True)


# Optional deps `mdk serve` needs at startup that aren't part of the
# core install. Each entry is (import_names, pip_name, why):
#
# * ``import_names`` is a tuple of module names we'll try in order —
#   the dep is considered present if ANY of them imports. Tuple-form
#   handles deps that renamed their canonical import (e.g.
#   ``python-multipart`` ≥0.0.12 exports ``python_multipart``, older
#   versions export ``multipart``).
# * ``pip_name`` is what we tell the operator to install if missing.
# * ``why`` is a short reason that goes in the error message.
#
# Keep the list tight; only add entries for deps that fail loudly +
# late inside uvicorn / FastAPI when actually missing.
_SERVE_REQUIRED_OPTIONAL_DEPS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        ("python_multipart", "multipart"),
        "python-multipart",
        "POST /api/v1/agents bundle upload",
    ),
)


def _preflight_optional_deps() -> None:
    """Fail fast with a copy-paste install hint when ``mdk serve`` is
    missing an optional dep it would otherwise crash on deep inside
    FastAPI's route registration.

    FastAPI doesn't pull ``python-multipart`` in transitively — it's
    only required when a route uses ``UploadFile`` / ``Form()``. We
    use ``UploadFile`` on POST /api/v1/agents (item 76). Without
    multipart, ``build_app()`` raises a ``RuntimeError`` mid-route-
    registration with a stack trace 20 frames deep. This preflight
    catches the same condition AT startup with a clean message that
    points operators at the right install command.

    Same pattern applies to any future serve-only dep — add to
    :data:`_SERVE_REQUIRED_OPTIONAL_DEPS` and it gets caught.
    """
    missing: list[tuple[str, str]] = []  # (pip_name, why)
    for import_names, pip_name, why in _SERVE_REQUIRED_OPTIONAL_DEPS:
        if not any(_try_import(name) for name in import_names):
            missing.append((pip_name, why))

    if not missing:
        return

    err.print(
        "[red]✗ mdk serve: missing optional dependencies[/red]\n"
        "[dim]These come with the [bold]runtime[/bold] extra and are "
        "required for the FastAPI surface:[/dim]"
    )
    for pip_name, why in missing:
        err.print(f"  [red]•[/red] [bold]{pip_name}[/bold]   [dim]({why})[/dim]")
    err.print(
        "\n[bold]Fix:[/bold]\n"
        "  [dim]$[/dim] [bold]uv tool install --force movate-cli[runtime][/bold]"
        "   [dim]# if installed as a uv tool[/dim]\n"
        "  [dim]$[/dim] [bold]pip install 'movate-cli[runtime]'[/bold]"
        "   [dim]# if installed via pip[/dim]\n"
        "\n[dim]Or install just the missing deps directly:[/dim]\n"
        f"  [dim]$[/dim] [bold]pip install {' '.join(p for p, _ in missing)}[/bold]"
    )
    raise typer.Exit(code=2)


def _try_import(name: str) -> bool:
    """True if ``name`` imports cleanly; False on ImportError.

    Wraps ``importlib.import_module`` so the preflight stays a flat
    list of tries — readable + easy to test against multiple module
    aliases for the same dep (e.g. python_multipart vs multipart).
    """
    try:
        importlib.import_module(name)
    except ImportError:
        return False
    return True


def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host."),
    port: int = typer.Option(8000, "--port", help="Bind port."),
    agents_path: Path = typer.Option(
        Path("./agents"),
        "--agents-path",
        envvar=["MDK_AGENTS_PATH", "MOVATE_AGENTS_PATH"],
        help="Directory to scan for agent.yaml files. Falls back to empty catalog if missing.",
    ),
    log_level: str = typer.Option(
        "info",
        "--log-level",
        help="uvicorn log level (debug | info | warning | error).",
    ),
    rate_limit_per_minute: int = typer.Option(
        60,
        "--rate-limit-per-minute",
        envvar=["MDK_RATE_LIMIT_PER_MINUTE", "MOVATE_RATE_LIMIT_PER_MINUTE"],
        help=(
            "Per-API-key token-bucket capacity (steady-state requests/min). "
            "Default 60. Set to 0 to disable rate limiting entirely "
            "(returns ``X-RateLimit-Limit: 0`` headers — operator's signal "
            "that limiting is OFF)."
        ),
    ),
    cors_origins: str = typer.Option(
        "",
        "--cors-origins",
        envvar=["MDK_CORS_ALLOWED_ORIGINS", "MOVATE_CORS_ALLOWED_ORIGINS"],
        help=(
            "Comma-separated list of allowed origins for the CORS "
            "middleware (e.g. 'http://localhost:4200,https://mova-io.movate.com'). "
            "Empty string (default) leaves the middleware off — fine for "
            "server-to-server use cases; required for the Mova iO Angular "
            "front end to call this runtime from the browser. Use '*' for "
            "fully-permissive local dev only."
        ),
    ),
) -> None:
    """Start the movate FastAPI runtime.

    [bold]Examples:[/bold]

      [dim]# Default — binds 127.0.0.1:8000, scans ./agents/, 60 req/min/key[/dim]
      $ movate serve

      [dim]# Custom port + remote-accessible[/dim]
      $ movate serve --host 0.0.0.0 --port 8080

      [dim]# Higher rate limit for a high-traffic prod deploy[/dim]
      $ movate serve --rate-limit-per-minute 600

      [dim]# Disable rate limiting (single-tenant dev)[/dim]
      $ movate serve --rate-limit-per-minute 0
    """
    # Catch missing optional deps (python-multipart etc.) BEFORE
    # uvicorn boots — operators who installed the CLI without the
    # `[runtime]` extra get a clean error pointing at the right
    # install command instead of a 20-frame FastAPI traceback.
    _preflight_optional_deps()

    asyncio.run(
        _run_serve(
            host=host,
            port=port,
            agents_path=agents_path,
            log_level=log_level,
            rate_limit_per_minute=rate_limit_per_minute,
            cors_origins=cors_origins,
        )
    )


async def _run_serve(
    *,
    host: str,
    port: int,
    agents_path: Path,
    log_level: str,
    rate_limit_per_minute: int,
    cors_origins: str,
) -> None:
    """Async entry that owns the loop end-to-end.

    Critical: ``storage.init()`` and ``server.serve()`` MUST run on
    the **same** event loop. asyncpg connections are bound to the
    loop they're created on; if init runs in ``asyncio.run()`` and
    uvicorn then creates its own loop, the pool's connections
    silently break with "another operation is in progress" on the
    first request. Running uvicorn via ``Server.serve()`` inside
    this async function keeps everything on one loop.
    """
    storage = build_storage()
    await storage.init()

    agents = scan_agents(agents_path)
    if not agents:
        err.print(
            f"[yellow]⚠[/yellow] no agents loaded from {agents_path} "
            f"(GET /agents will return empty)"
        )
    else:
        success(f"loaded {len(agents)} agent(s) from {agents_path}")
        for b in agents:
            err.print(f"  - {b.spec.name} v{b.spec.version}")

    # Pass the cors_origins kwarg as a parsed list (or None when empty,
    # which lets build_app's env-fallback path also kick in if the user
    # set the env var via a wrapper script). Empty string → None.
    parsed_origins = (
        [o.strip() for o in cors_origins.split(",") if o.strip()] if cors_origins else None
    )
    app = build_app(
        storage,
        agents=agents,
        # Plumb the agents_path so POST /api/v1/agents (item 76) knows
        # where to persist new bundles. Same path that scan_agents()
        # already walked above, so creates land in the same registry
        # GET /agents reads from.
        agents_path=agents_path,
        rate_limit_per_minute=rate_limit_per_minute,
        cors_allowed_origins=parsed_origins,
    )
    err.print(f"[bold]movate[/bold] serving on http://{host}:{port}")
    if rate_limit_per_minute > 0:
        hint(f"[dim]  rate limit: {rate_limit_per_minute} req/min per API key[/dim]")
    else:
        hint("[dim]  rate limit: [yellow]DISABLED[/yellow][/dim]")
    if parsed_origins:
        hint(f"[dim]  CORS allowed origins: {', '.join(parsed_origins)}[/dim]")
    else:
        hint("[dim]  CORS: [yellow]OFF[/yellow] (set --cors-origins for browser callers)[/dim]")

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        # Don't let uvicorn install its own signal handlers — typer's
        # default Ctrl-C handling is fine and uvicorn's interferes
        # with our existing CLI patterns.
        lifespan="off",
    )
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        await storage.close()
