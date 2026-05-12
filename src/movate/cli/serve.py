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
from rich.console import Console

# NOTE: ``uvicorn``, ``fastapi``, and the runtime app are imported lazily
# inside ``serve()`` so the rest of the CLI works even when the optional
# ``[runtime]`` extra is not installed. Without lazy imports, ``movate
# --help`` would crash with ``ModuleNotFoundError: uvicorn``.

err = Console(stderr=True)


def _missing_extra_hint() -> str:
    return (
        "[red]✗[/red] the [bold]serve[/bold] command needs the optional `runtime` extra.\n"
        "  install: [dim]uv tool install --editable '.[runtime]' --force[/dim] "
        "(or [dim]pip install 'movate-cli[runtime]'[/dim])"
    )


def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host."),
    port: int = typer.Option(8000, "--port", help="Bind port."),
    agents_path: Path = typer.Option(
        Path("./agents"),
        "--agents-path",
        envvar="MOVATE_AGENTS_PATH",
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
        envvar="MOVATE_RATE_LIMIT_PER_MINUTE",
        help=(
            "Per-API-key token-bucket capacity (steady-state requests/min). "
            "Default 60. Set to 0 to disable rate limiting entirely "
            "(returns ``X-RateLimit-Limit: 0`` headers — operator's signal "
            "that limiting is OFF)."
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
    try:
        # Lazy import — see module-level NOTE. Defers ~uvicorn + fastapi
        # ~0.5s startup and lets the CLI launch even without the extra.
        import uvicorn  # noqa: PLC0415, F401 — imported for early failure detection
        from movate.runtime.app import build_app  # noqa: PLC0415, F401
        from movate.runtime.registry import scan_agents  # noqa: PLC0415, F401
        from movate.storage import build_storage  # noqa: PLC0415, F401
    except ImportError:
        err.print(_missing_extra_hint())
        raise typer.Exit(code=2) from None

    asyncio.run(
        _run_serve(
            host=host,
            port=port,
            agents_path=agents_path,
            log_level=log_level,
            rate_limit_per_minute=rate_limit_per_minute,
        )
    )


async def _run_serve(
    *,
    host: str,
    port: int,
    agents_path: Path,
    log_level: str,
    rate_limit_per_minute: int,
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
    # Imports already validated by ``serve()`` before we get here.
    import uvicorn  # noqa: PLC0415

    from movate.runtime.app import build_app  # noqa: PLC0415
    from movate.runtime.registry import scan_agents  # noqa: PLC0415
    from movate.storage import build_storage  # noqa: PLC0415

    storage = build_storage()
    await storage.init()

    agents = scan_agents(agents_path)
    if not agents:
        err.print(
            f"[yellow]⚠[/yellow] no agents loaded from {agents_path} "
            f"(GET /agents will return empty)"
        )
    else:
        err.print(f"[green]✓[/green] loaded {len(agents)} agent(s) from {agents_path}")
        for b in agents:
            err.print(f"  - {b.spec.name} v{b.spec.version}")

    app = build_app(
        storage,
        agents=agents,
        rate_limit_per_minute=rate_limit_per_minute,
    )
    err.print(f"[bold]movate[/bold] serving on http://{host}:{port}")
    if rate_limit_per_minute > 0:
        err.print(f"[dim]  rate limit: {rate_limit_per_minute} req/min per API key[/dim]")
    else:
        err.print("[dim]  rate limit: [yellow]DISABLED[/yellow][/dim]")

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
