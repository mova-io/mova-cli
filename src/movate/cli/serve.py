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
from pathlib import Path

import typer
import uvicorn
from rich.console import Console

from movate.cli._console import hint, success
from movate.runtime.app import build_app
from movate.runtime.registry import scan_agents
from movate.storage import build_storage

err = Console(stderr=True)


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
