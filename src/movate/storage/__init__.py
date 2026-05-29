"""Storage providers: pluggable persistence behind a single Protocol.

Auto-selection in :func:`build_storage`:

* ``MOVATE_DB_URL`` (or, as a fallback, ``MOVATE_PG_URL``) set and
  starting with ``postgres://`` / ``postgresql://`` →
  :class:`PostgresProvider` (v0.5+). Durable across container restarts.
* otherwise → :class:`SqliteProvider` at ``MOVATE_DB`` or
  ``~/.movate/local.db``.

The ``MOVATE_PG_URL`` fallback (#122) means a deployment that already
configures Postgres for the memory store — which reads ``MOVATE_PG_URL``
(see :mod:`movate.memory.store`) — no longer silently lands on
ephemeral in-pod SQLite for the api-key store, the recurring source of
401s after a revision recycle. ``MOVATE_DB_URL`` stays the primary,
documented variable and wins when both are set.

Postgres dependency is in the ``[runtime]`` extra; importing the
provider only happens when the env points at it, so users on the
sqlite path never need ``asyncpg``.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

from movate.storage.base import StorageProvider
from movate.storage.sqlite import SqliteProvider

__all__ = [
    "SqliteProvider",
    "StorageProvider",
    "build_storage",
    "mark_cli_mode",
    "selected_backend",
]

logger = logging.getLogger(__name__)

# Snapshot of the last backend selected by build_storage(). Read by
# /api/v1/health to surface storage durability, and by `mdk doctor
# target` to flag non-durable deployments. None until build_storage()
# has been called.
_LAST_SELECTED: tuple[str, str, bool] | None = None

# Has the SQLite-not-durable WARNING been emitted once already in
# this process? ``mdk eval-scorecard`` invokes ``build_storage`` once
# per agent + once for the preflight; without this guard the warning
# would spam stderr 10+ times per sweep. Emit-once keeps the signal
# without the noise.
_DURABILITY_WARNING_EMITTED = False

# Is the current process the CLI (``mdk`` / ``movate``) rather than
# a server / container runtime? CLI invocations use SQLite by design
# — the durability warning is target-audience-irrelevant noise. The
# CLI entry point flips this flag at startup via :func:`mark_cli_mode`;
# the runtime / FastAPI app leaves it ``False`` so production
# misconfiguration still surfaces loudly.
_CLI_MODE = False


def mark_cli_mode() -> None:
    """Mark this process as a CLI invocation.

    Suppresses the SQLite-not-durable WARNING from
    :func:`build_storage` — the warning is aimed at production
    containers where SQLite under ``/var/lib`` vanishes on every
    revision recycle. In CLI / dev / CI runs the warning is noise
    that lives at the top of every ``mdk ...`` invocation.

    Called by :mod:`movate.cli.main` at module-import time; never
    called by the FastAPI runtime so server processes still emit
    the warning at WARNING level."""
    global _CLI_MODE  # noqa: PLW0603
    _CLI_MODE = True


def _reset_state_for_tests() -> None:
    """Reset the once-per-process suppression flags so tests can
    assert on the warning's presence + absence in isolation.

    Module-level globals (the durability-warning flag, the CLI-mode
    flag) survive across tests in the same Python process by design
    (the production code path runs once per CLI invocation). Tests
    that probe the warning need to start from a clean slate; call
    this from an autouse fixture in the relevant test files."""
    global _DURABILITY_WARNING_EMITTED, _CLI_MODE  # noqa: PLW0603
    _DURABILITY_WARNING_EMITTED = False
    _CLI_MODE = False


def selected_backend() -> tuple[str, str, bool] | None:
    """Return the last backend chosen by :func:`build_storage`.

    Tuple: ``(backend, detail, durable)``.

    * ``backend`` — ``"postgres"`` or ``"sqlite"``.
    * ``detail`` — for postgres, ``"host=<host> db=<db>"`` (no password);
      for sqlite, the resolved on-disk path.
    * ``durable`` — ``True`` for postgres, ``False`` for sqlite. SQLite
      in a container is ephemeral; key records vanish on revision recycle.
    """
    return _LAST_SELECTED


def _resolve_pg_url() -> str | None:
    """Resolve a Postgres DSN from the environment, or ``None``.

    Recognizes (in precedence order):

    1. ``MOVATE_DB_URL`` — the canonical, documented storage DSN.
    2. ``MOVATE_PG_URL`` — the variable the Postgres memory store
       (:mod:`movate.memory.store`) already reads. Honored as a
       fallback (#122) so a deployment that wires Postgres for memory
       doesn't silently leave the api-key store on ephemeral in-pod
       SQLite — the recurring cause of 401s after a revision recycle.

    Only values that look like an asyncpg DSN (``postgres://`` /
    ``postgresql://``) qualify; anything else (or unset) yields
    ``None``, leaving the SQLite path to take over.
    """
    for var in ("MOVATE_DB_URL", "MOVATE_PG_URL"):
        value = os.environ.get(var, "").strip()
        if value.startswith(("postgres://", "postgresql://")):
            return value
    return None


def build_storage() -> StorageProvider:
    """Auto-select a StorageProvider based on environment.

    * ``MOVATE_DB_URL`` (e.g. ``postgresql://user:pw@host/db``), or
      ``MOVATE_PG_URL`` as a fallback → :class:`PostgresProvider`.
      Production / multi-worker; durable across container restarts.
    * ``MOVATE_DB`` or default ``~/.movate/local.db`` →
      :class:`SqliteProvider`. Local dev and CI.

    Both implement the same Protocol so application code never
    branches on backend.

    Logs a single line per call describing the selection so an
    operator can spot a misconfigured Container App (Postgres
    intended, SQLite actually picked) from the container logs alone.
    """
    global _LAST_SELECTED  # noqa: PLW0603

    db_url = _resolve_pg_url()
    if db_url:
        # Lazy import — keeps asyncpg optional for sqlite-only users.
        from movate.storage.postgres import PostgresProvider  # noqa: PLC0415

        parsed = urlparse(db_url)
        host = parsed.hostname or "<unknown>"
        db_name = parsed.path.lstrip("/") or "<default>"
        detail = f"host={host} db={db_name}"
        _LAST_SELECTED = ("postgres", detail, True)
        logger.info("storage: PostgresProvider %s", detail)
        return PostgresProvider(dsn=db_url)

    # Persistent-by-default path. ``~/.movate/local.db`` survives process
    # restarts on any host with a stable home dir (local dev, a container
    # with a volume mounted at $HOME/.movate). Trim + empty-guard so a
    # blank ``MOVATE_DB=`` (a common deploy-template footgun) falls back to
    # the durable default rather than being used verbatim as a path. We
    # deliberately never substitute a tempfile here — a temp path would
    # reintroduce the #122 cold-start key loss.
    db_path = os.environ.get("MOVATE_DB", "").strip() or "~/.movate/local.db"
    _LAST_SELECTED = ("sqlite", db_path, False)
    # WARN level because the ApiKeyRecord / RunRecord tables in a
    # SQLite file under the container filesystem vanish on every
    # revision recycle. Fine for dev/CI, broken for production.
    #
    # Two scopes of suppression to avoid stderr spam without losing
    # the signal:
    #   1. Emit at most ONCE per process — eval-scorecard calls
    #      ``build_storage`` 10+ times in one sweep, and the warning
    #      would otherwise appear 10+ times per ``mdk eval-scorecard``
    #      run.
    #   2. Drop to DEBUG when ``mark_cli_mode()`` has been called.
    #      CLI invocations always run SQLite locally by design — the
    #      warning is targeted at production servers, not laptops.
    global _DURABILITY_WARNING_EMITTED  # noqa: PLW0603
    if not _DURABILITY_WARNING_EMITTED:
        log_level = logging.DEBUG if _CLI_MODE else logging.WARNING
        logger.log(
            log_level,
            "storage: SqliteProvider at %s — NOT durable across container "
            "restarts; set MOVATE_DB_URL=postgresql://... for production.",
            db_path,
        )
        _DURABILITY_WARNING_EMITTED = True
    return SqliteProvider(db_path=db_path)
