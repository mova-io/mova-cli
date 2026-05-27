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
import os
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.panel import Panel

from movate.cli._console import success
from movate.storage import build_storage

if TYPE_CHECKING:
    from movate.storage.base import StorageProvider

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
    skills_path: Path | None = typer.Option(
        None,
        "--skills-path",
        envvar=["MDK_SKILLS_PATH", "MOVATE_SKILLS_PATH"],
        help=(
            "Directory where POST /api/v1/skills persists uploaded skill "
            "bundles, and where the agent loader looks for skill registry "
            "entries. Defaults to <agents-path>/skills/ to match the "
            "loader's project-root fallback."
        ),
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
            skills_path=skills_path,
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
    skills_path: Path | None,
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
    # Lazy imports — uvicorn, build_app, and scan_agents all require
    # optional [runtime] deps (uvicorn, fastapi). Deferring to here
    # means ``mdk kb ingest``, ``mdk eval``, and other non-serve
    # commands don't crash when the [runtime] extra isn't installed.
    import uvicorn  # noqa: PLC0415

    from movate.cli._runtime import register_pool_observability  # noqa: PLC0415
    from movate.runtime.app import build_app  # noqa: PLC0415
    from movate.runtime.registry import scan_agents  # noqa: PLC0415
    from movate.tracing import init_metrics  # noqa: PLC0415

    # Initialize OTel metrics once at process startup (R3 / item 33), after
    # dotenv + MDK_*→MOVATE_* alias sync (both run at CLI import in main.py).
    # Mirrors the tracer wiring; a complete no-op when the otel extra is absent
    # or the OTLP sink/endpoint isn't configured. Never raises.
    init_metrics()

    storage = build_storage()
    await storage.init()
    # ADR 034 D3 — wire the asyncpg pool's saturation gauges AFTER init (the
    # pool now exists). No-op on SQLite / when metrics are off. Never raises.
    register_pool_observability(storage)
    await _seed_bootstrap_key(storage)

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
        # skills_path defaults to <agents_path>/skills inside build_app
        # when None — matches the agent loader's project-root fallback.
        skills_path=skills_path,
        rate_limit_per_minute=rate_limit_per_minute,
        cors_allowed_origins=parsed_origins,
    )
    _rate_line = (
        f"rate limit:  {rate_limit_per_minute} req/min per API key"
        if rate_limit_per_minute > 0
        else "rate limit:  [yellow]DISABLED[/yellow]"
    )
    _cors_line = (
        f"CORS:        {', '.join(parsed_origins)}"
        if parsed_origins
        else "CORS:        [yellow]OFF[/yellow] [dim](pass --cors-origins for browsers)[/dim]"
    )
    _agent_line = (
        f"agents:      {len(agents)} loaded"
        if agents
        else "agents:      [yellow]none loaded[/yellow] [dim](GET /agents returns empty)[/dim]"
    )
    _body_lines = [
        f"[bold cyan]http://{host}:{port}[/bold cyan]",
        "",
        _agent_line,
        _rate_line,
        _cors_line,
        "",
        "[dim]mdk run <agent> '<input>'   →  invoke an agent[/dim]",
        "[dim]mdk logs --last             →  inspect last run[/dim]",
    ]
    err.print(
        Panel(
            "\n".join(_body_lines),
            title="[green]✓[/green] movate runtime ready",
            title_align="left",
            border_style="green",
        )
    )

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


async def _seed_bootstrap_key(storage: StorageProvider) -> None:
    """Seed a known API key on startup if ``MOVATE_SEED_API_KEY`` is set.

    Solves the chicken-and-egg bootstrap problem on fresh deployments
    where no keys exist in the DB yet (e.g. ephemeral SQLite in a
    container that has no persistent volume, or a Postgres DB on first
    boot). The key is inserted exactly once.

    On a redeploy the row already exists. If it already grants
    ``fleet-admin`` we leave it untouched. But a bootstrap key seeded by
    an OLDER image — before fleet-admin seeding, or before the #61 scope
    fix — keeps a stale/narrow scope (e.g. ``["admin"]`` with no ``read``)
    forever, because the seed was historically insert-only. Such a key
    then 403s on ``read``-scoped endpoints even on a fixed image. We
    therefore **self-heal** a stale bootstrap-key scope here: rewrite it
    in place to ``["fleet-admin"]``, preserving the existing
    ``secret_hash`` / ``salt`` / ``tenant_id`` / ``env`` / ``created_at``
    (the key value is unchanged, so we never re-hash or re-salt).

    The env var value must be a valid movate key string:
        mvt_<env>_<tenant_prefix>_<key_id>_<secret>
    """
    seed_key = os.environ.get("MOVATE_SEED_API_KEY", "").strip()
    if not seed_key:
        return

    from movate.core.auth import (  # noqa: PLC0415
        SCOPE_FLEET_ADMIN,
        ApiKeyParseError,
        hash_secret,
        parse_api_key,
    )
    from movate.core.models import ApiKeyRecord  # noqa: PLC0415

    try:
        parsed = parse_api_key(seed_key)
    except ApiKeyParseError as exc:
        err.print(f"[yellow]⚠[/yellow] MOVATE_SEED_API_KEY is malformed, skipping: {exc}")
        return

    existing = await storage.get_api_key(parsed.key_id)
    if existing is not None:
        # Already a fleet-admin grant → nothing to do (idempotent on redeploy).
        # ``fleet-admin`` in the scopes list is the all-powerful grant that
        # ``effective_scopes`` expands to the full set, so checking membership
        # here is representation-agnostic and matches the seed target below.
        if SCOPE_FLEET_ADMIN in existing.scopes:
            err.print(
                f"[dim]bootstrap key {parsed.key_id} already present "
                f"(scope: fleet-admin) — skipping seed[/dim]"
            )
            return
        # Stale/narrow scope from an older image (e.g. ``["admin"]`` with no
        # ``read``) → heal in place to fleet-admin. PRESERVE the secret_hash /
        # salt / tenant_id / env / created_at (the key value is unchanged).
        old_scopes = list(existing.scopes)
        await storage.update_api_key_scopes(existing.key_id, scopes=[SCOPE_FLEET_ADMIN])
        err.print(
            f"[dim]healed bootstrap key {parsed.key_id} scope → fleet-admin "
            f"(was {old_scopes})[/dim]"
        )
        return

    import base64  # noqa: PLC0415
    import secrets as _secrets  # noqa: PLC0415
    from datetime import UTC, datetime  # noqa: PLC0415

    salt = base64.urlsafe_b64encode(_secrets.token_bytes(16)).rstrip(b"=").decode("ascii")
    record = ApiKeyRecord(
        key_id=parsed.key_id,
        tenant_id=parsed.tenant_prefix,
        env=parsed.env,
        secret_hash=hash_secret(parsed.secret, salt),
        salt=salt,
        label="seed",
        created_at=datetime.now(UTC),
        # The bootstrap key is the first-admin that breaks the
        # chicken-and-egg: with no other keys yet, it must be able to mint
        # scoped keys + manage the fleet. Grant it ``fleet-admin`` (ADR 013
        # L2 → resolves to the full scope set incl. ``admin`` via
        # effective_scopes). Operators rotate to narrowly-scoped keys ASAP.
        scopes=["fleet-admin"],
    )
    await storage.save_api_key(record)
    err.print(
        f"[dim]seeded bootstrap key {parsed.key_id} (scope: fleet-admin) "
        f"from MOVATE_SEED_API_KEY[/dim]"
    )
