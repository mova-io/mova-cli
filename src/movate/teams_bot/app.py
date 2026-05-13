"""FastAPI app exposing the Bot Framework webhook.

Two routes:

* ``POST /api/messages`` — Bot Framework webhook. Parses the inbound
  Activity, dispatches to :func:`handle_activity`, and returns the
  reply Activity as the response body (inline-reply mode).
* ``GET /health`` — liveness probe for ACA / `mdk doctor`. Returns
  200 with a fixed JSON body; never touches storage.

The app deliberately does NOT validate the Bot Framework JWT yet —
that's a hardening PR. For 3.1.a, anyone who knows the URL can post;
acceptable for local dev + alpha pilot, NOT for production exposure.
``MOVATE_TEAMS_FLEET_API_KEY`` (read by later slices) is the only
secret the bot needs.

The FastAPI app construction is gated behind a function rather than
a module-level ``app`` so importing the module under
``movate-cli[teams]`` doesn't blow up when ``fastapi`` isn't
installed. The CLI command imports + calls :func:`build_app` only
after the optional extras have been resolved.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from movate.teams_bot.activity import Activity
from movate.teams_bot.handler import HandlerContext, handle_activity

if TYPE_CHECKING:
    from fastapi import FastAPI

    from movate.core.client import MovateClient


# Env-var names the bot reads at startup. Centralised here so the CLI
# (cli/teams_bot.py) and the app share one source of truth.
ENV_RUNTIME_URL = "MOVATE_RUNTIME_URL"
ENV_FLEET_API_KEY = "MOVATE_TEAMS_FLEET_API_KEY"
ENV_LANGFUSE_PUBLIC_HOST = "MOVATE_TEAMS_LANGFUSE_PUBLIC_HOST"
ENV_REQUIRE_BINDING = "MOVATE_TEAMS_REQUIRE_BINDING"


def build_app(
    *,
    runtime_url: str | None = None,
    fleet_api_key: str | None = None,
    langfuse_public_host: str | None = None,
    runtime_client: MovateClient | None = None,
    enable_identity: bool = True,
    teams_db_path: Path | None = None,
    require_binding: bool | None = None,
) -> FastAPI:
    """Construct the Teams-bot FastAPI app.

    Args:
        runtime_url: Base URL of the Movate runtime to forward commands
            to. Falls back to ``MOVATE_RUNTIME_URL`` env, else ``None``
            (commands that need the runtime will reply with a config-
            error card).
        fleet_api_key: The bot's API key for the runtime. Falls back to
            ``MOVATE_TEAMS_FLEET_API_KEY``. Required when ``runtime_url``
            is set (otherwise reqs will 401).
        langfuse_public_host: Optional public Langfuse base URL. When
            set, successful run cards include a "View trace" button.
        runtime_client: Tests inject a pre-built :class:`MovateClient`
            (often backed by ``ASGITransport(app=<runtime FastAPI>)``)
            to avoid spinning up a real HTTP loop. Takes precedence
            over ``runtime_url`` + ``fleet_api_key``.
        enable_identity: When True (default), wires up the per-user
            identity-binding store + resolver. Requires
            ``MOVATE_TEAMS_ENCRYPTION_KEY`` to be set; raises at boot
            if not. Set False for the alpha smoke-test path that uses
            only the fleet key.
        teams_db_path: Tests pass ``Path(":memory:")``; production
            leaves None and the store reads ``MOVATE_TEAMS_DB`` or
            falls back to ``~/.movate/teams.db``.
        require_binding: Strict mode for ``run``. Falls back to the
            ``MOVATE_TEAMS_REQUIRE_BINDING`` env (truthy → strict).
            Default False — alpha allows fallback to the fleet client.

    Importing FastAPI inline means a dev install without the ``[teams]``
    extra (which pulls in fastapi/uvicorn) can still import the rest of
    the package — only this function fails.
    """
    try:
        from fastapi import FastAPI  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "the 'fastapi' package is required for the Teams bot. "
            "Install with: uv add 'movate-cli[teams]'"
        ) from exc

    # Resolve config from args → env → defaults. Explicit arg wins so
    # tests can pass a fully-formed context without exporting env vars.
    resolved_runtime_url = runtime_url or os.environ.get(ENV_RUNTIME_URL)
    resolved_api_key = fleet_api_key or os.environ.get(ENV_FLEET_API_KEY)
    resolved_langfuse = langfuse_public_host or os.environ.get(ENV_LANGFUSE_PUBLIC_HOST)
    resolved_require_binding = (
        require_binding
        if require_binding is not None
        else os.environ.get(ENV_REQUIRE_BINDING, "").lower() in {"1", "true", "yes"}
    )

    # Build a long-lived MovateClient if we have everything we need.
    # The connection pool stays warm across requests — important since
    # Teams sends one Activity per HTTP request and a cold-pool TLS
    # handshake per request adds 100s of ms.
    client: MovateClient | None = runtime_client
    if client is None and resolved_runtime_url and resolved_api_key:
        from movate.core.client import MovateClient as _MovateClient  # noqa: PLC0415

        client = _MovateClient(
            base_url=resolved_runtime_url,
            api_key=resolved_api_key,
        )

    # Identity binding (3.1.c). Wired in two pieces:
    #   - users_store: persistent Fernet-encrypted SQLite table
    #   - identity_resolver: in-memory LRU of per-user MovateClients
    # Both are optional — `enable_identity=False` falls back to the
    # 3.1.b fleet-only behavior. The encryption key is REQUIRED when
    # identity is enabled — we fail loud at boot rather than silently
    # let the first `connect` crash.
    users_store = None
    identity_resolver = None
    if enable_identity:
        from movate.teams_bot.identity import IdentityResolver  # noqa: PLC0415
        from movate.teams_bot.storage import TeamsUsersStore  # noqa: PLC0415

        users_store = TeamsUsersStore(db_path=teams_db_path)
        # init() runs in an async context inside the startup hook below
        # — we can't call it here because build_app is sync.
        if resolved_runtime_url:
            identity_resolver = IdentityResolver(
                store=users_store,
                runtime_base_url=resolved_runtime_url,
            )

    handler_ctx = HandlerContext(
        runtime_client=client,
        langfuse_public_host=resolved_langfuse,
        users_store=users_store,
        identity_resolver=identity_resolver,
        require_binding=resolved_require_binding,
    )

    app = FastAPI(
        title="movate teams-bot",
        description="Bot Framework webhook bridging Teams to the Movate runtime.",
        version="0.7.0c",
    )
    # Expose state on the app so the CLI ``serve`` command (and ops
    # tooling like ``mdk doctor``) can inspect the resolved config
    # without re-reading env vars from another process.
    app.state.handler_ctx = handler_ctx
    app.state.runtime_url = resolved_runtime_url

    @app.on_event("startup")
    async def _startup() -> None:
        """Initialise the teams_users sqlite schema. Idempotent (CREATE
        TABLE IF NOT EXISTS) so it's safe across restarts."""
        if users_store is not None:
            await users_store.init()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        """Close every long-lived pool: fleet client, per-user cached
        clients, sqlite. Order matters — close clients before the store
        in case the store's close blocks on something the clients hold."""
        if handler_ctx.runtime_client is not None:
            await handler_ctx.runtime_client.aclose()
        if handler_ctx.identity_resolver is not None:
            await handler_ctx.identity_resolver.aclose()
        if users_store is not None:
            await users_store.close()

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness probe. Never touches storage or external services.

        Deliberately separate from a deeper ``/ready`` (TBD) because
        ACA's liveness probe fires every few seconds and shouldn't
        depend on anything that can be slow or flaky."""
        return {"status": "ok", "service": "movate-teams-bot"}

    # Declared with ``Activity`` directly as the body parameter so
    # FastAPI runs Pydantic validation for us — malformed JSON and
    # bad Activity shape both surface as HTTP 422 with the same
    # validation-error envelope.
    #
    # Pydantic accepts both the field name and the alias by default,
    # so the wire's ``"from": {...}`` matches the ``from_`` field
    # without any extra config.

    @app.post("/api/messages")
    async def on_message(activity: Activity) -> dict[str, Any]:
        """Bot Framework webhook.

        Bot Framework posts an Activity JSON object; we dispatch on
        the parsed command and return the reply Activity inline.
        Teams (and the Bot Framework Emulator) accept inline replies
        — no callback to the Bot Framework connector needed.

        Errors:

        * Malformed JSON / bad Activity shape → 422 via FastAPI's
          Pydantic validation envelope.
        * Handler raised → 500. Returning a 5xx tells Teams to retry,
          which is the right behaviour for transient errors. For
          deterministic failures (bad command, missing agent), the
          handler returns a 200 with an error card instead.
        """
        reply = await handle_activity(activity, app.state.handler_ctx)
        if reply is None:
            # Teams accepts an empty 200 as "no reply, OK".
            return {}
        return reply.to_wire()

    return app
