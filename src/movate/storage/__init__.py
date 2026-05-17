"""Storage providers: pluggable persistence behind a single Protocol.

Auto-selection in :func:`build_storage`:

* ``MOVATE_DB_URL`` set and starts with ``postgres://`` /
  ``postgresql://`` → :class:`PostgresProvider` (v0.5+).
* otherwise → :class:`SqliteProvider` at ``MOVATE_DB`` or
  ``~/.movate/local.db``.

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

__all__ = ["SqliteProvider", "StorageProvider", "build_storage", "selected_backend"]

logger = logging.getLogger(__name__)

# Snapshot of the last backend selected by build_storage(). Read by
# /api/v1/health to surface storage durability, and by `mdk doctor
# target` to flag non-durable deployments. None until build_storage()
# has been called.
_LAST_SELECTED: tuple[str, str, bool] | None = None


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


def build_storage() -> StorageProvider:
    """Auto-select a StorageProvider based on environment.

    * ``MOVATE_DB_URL`` (e.g. ``postgresql://user:pw@host/db``) →
      :class:`PostgresProvider`. Production / multi-worker.
    * ``MOVATE_DB`` or default ``~/.movate/local.db`` →
      :class:`SqliteProvider`. Local dev and CI.

    Both implement the same Protocol so application code never
    branches on backend.

    Logs a single line per call describing the selection so an
    operator can spot a misconfigured Container App (Postgres
    intended, SQLite actually picked) from the container logs alone.
    """
    global _LAST_SELECTED  # noqa: PLW0603

    db_url = os.environ.get("MOVATE_DB_URL")
    if db_url and db_url.startswith(("postgres://", "postgresql://")):
        # Lazy import — keeps asyncpg optional for sqlite-only users.
        from movate.storage.postgres import PostgresProvider  # noqa: PLC0415

        parsed = urlparse(db_url)
        host = parsed.hostname or "<unknown>"
        db_name = parsed.path.lstrip("/") or "<default>"
        detail = f"host={host} db={db_name}"
        _LAST_SELECTED = ("postgres", detail, True)
        logger.info("storage: PostgresProvider %s", detail)
        return PostgresProvider(dsn=db_url)

    db_path = os.environ.get("MOVATE_DB", "~/.movate/local.db")
    _LAST_SELECTED = ("sqlite", db_path, False)
    # WARN level because the ApiKeyRecord / RunRecord tables in a
    # SQLite file under the container filesystem vanish on every
    # revision recycle. Fine for dev/CI, broken for production.
    logger.warning(
        "storage: SqliteProvider at %s — NOT durable across container "
        "restarts; set MOVATE_DB_URL=postgresql://... for production.",
        db_path,
    )
    return SqliteProvider(db_path=db_path)
