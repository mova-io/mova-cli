"""User-level state paths for the playground (pure logic).

Chainlit's data layer persists conversation threads so the operator gets
a past-conversations sidebar and can resume a chat. The playground keeps
this **local-first + zero-config**: a SQLite file under the user's home
mdk directory, no external service required. If a Postgres URL is
configured, that is used instead (shared / team setups).

These helpers are pure (path computation + env reads only — they do not
touch the network or import Chainlit) so they unit-test in isolation.
The Chainlit app calls :func:`resolve_data_layer_config` once at startup.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: Home-level mdk directory holding cross-project playground state. Distinct
#: from the *project* ``.mdk/`` (movate.core.paths) — this is per-user,
#: machine-global, like the threads sidebar it backs.
HOME_MDK_DIR_NAME = ".mdk"
#: Sub-directory under the home mdk dir for playground state.
PLAYGROUND_SUBDIR = "playground"
#: SQLite filename for Chainlit thread persistence.
THREADS_DB_NAME = "threads.db"


def playground_state_dir(home: Path | None = None) -> Path:
    """Return ``~/.mdk/playground`` (creating nothing — caller owns I/O).

    ``home`` is injectable for tests; defaults to the real home dir.
    """
    base = home if home is not None else Path.home()
    return base / HOME_MDK_DIR_NAME / PLAYGROUND_SUBDIR


def threads_db_path(home: Path | None = None) -> Path:
    """Path to the local SQLite threads database (``threads.db``)."""
    return playground_state_dir(home) / THREADS_DB_NAME


@dataclass(frozen=True)
class DataLayerConfig:
    """Resolved Chainlit data-layer configuration for thread persistence.

    Exactly one of :attr:`sqlite_path` / :attr:`postgres_url` is set
    (Postgres wins when configured). :attr:`enabled` is False only when
    history was explicitly disabled (``--no-history``), in which case the
    playground runs without the past-conversations sidebar (today's
    single-shot feel, no persistence).
    """

    enabled: bool
    sqlite_path: Path | None = None
    postgres_url: str | None = None

    @property
    def backend(self) -> str:
        if not self.enabled:
            return "disabled"
        if self.postgres_url:
            return "postgres"
        return "sqlite"


def resolve_data_layer_config(
    *,
    enabled: bool = True,
    postgres_url: str | None = None,
    home: Path | None = None,
    env: dict[str, str] | None = None,
) -> DataLayerConfig:
    """Decide where Chainlit persists threads.

    Resolution:

    * ``enabled=False`` (operator passed ``--no-history``) → no data
      layer; the playground runs ephemeral, no sidebar.
    * a Postgres URL — passed in or from ``MDK_PLAYGROUND_THREADS_URL`` /
      ``DATABASE_URL`` — → use Postgres (team / shared persistence).
    * otherwise → local SQLite at :func:`threads_db_path` (the
      zero-config default).

    Pure: computes paths + reads env; the caller creates the directory
    and wires Chainlit's ``cl_data`` layer.
    """
    if not enabled:
        return DataLayerConfig(enabled=False)
    environ = env if env is not None else dict(os.environ)
    pg = postgres_url or environ.get("MDK_PLAYGROUND_THREADS_URL") or environ.get("DATABASE_URL")
    if pg:
        return DataLayerConfig(enabled=True, postgres_url=pg)
    return DataLayerConfig(enabled=True, sqlite_path=threads_db_path(home))


# Chainlit's SQLAlchemyDataLayer (the conversation-history backend) does NOT
# create its own schema — it issues raw INSERT/UPDATE/SELECT against tables it
# assumes already exist. On the zero-config SQLite path nothing provisioned them,
# so every persist raised ``no such table: threads`` / ``steps`` and the
# playground's agent picker + chat silently broke. This DDL is the canonical
# Chainlit data-layer schema (users / threads / steps / elements / feedbacks);
# SQLite is dynamically typed, so the Postgres-ish type names (UUID/JSONB/TEXT[])
# are accepted as type-affinity. ``IF NOT EXISTS`` makes it idempotent.
_CHAINLIT_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    "id" UUID PRIMARY KEY,
    "identifier" TEXT NOT NULL UNIQUE,
    "metadata" JSONB NOT NULL,
    "createdAt" TEXT
);
CREATE TABLE IF NOT EXISTS threads (
    "id" UUID PRIMARY KEY,
    "createdAt" TEXT,
    "name" TEXT,
    "userId" UUID,
    "userIdentifier" TEXT,
    "tags" TEXT,
    "metadata" JSONB,
    FOREIGN KEY ("userId") REFERENCES users ("id") ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS steps (
    "id" UUID PRIMARY KEY,
    "name" TEXT NOT NULL,
    "type" TEXT NOT NULL,
    "threadId" UUID NOT NULL,
    "parentId" UUID,
    "streaming" BOOLEAN NOT NULL,
    "waitForAnswer" BOOLEAN,
    "isError" BOOLEAN,
    "metadata" JSONB,
    "tags" TEXT,
    "input" TEXT,
    "output" TEXT,
    "createdAt" TEXT,
    "command" TEXT,
    "start" TEXT,
    "end" TEXT,
    "generation" JSONB,
    "showInput" TEXT,
    "language" TEXT,
    "indent" INT,
    "defaultOpen" BOOLEAN
);
CREATE TABLE IF NOT EXISTS elements (
    "id" UUID PRIMARY KEY,
    "threadId" UUID,
    "type" TEXT,
    "url" TEXT,
    "chainlitKey" TEXT,
    "name" TEXT NOT NULL,
    "display" TEXT,
    "objectKey" TEXT,
    "size" TEXT,
    "page" INT,
    "language" TEXT,
    "forId" UUID,
    "mime" TEXT,
    "props" JSONB
);
CREATE TABLE IF NOT EXISTS feedbacks (
    "id" UUID PRIMARY KEY,
    "forId" UUID NOT NULL,
    "threadId" UUID NOT NULL,
    "value" INT NOT NULL,
    "comment" TEXT
);
"""


def ensure_chainlit_sqlite_schema(db_path: Path) -> None:
    """Create the Chainlit data-layer tables in ``db_path`` if missing.

    Idempotent (``CREATE TABLE IF NOT EXISTS``). Synchronous stdlib ``sqlite3``
    — runs once at data-layer build time, before the async SQLAlchemyDataLayer
    starts issuing queries. Raises on a genuinely broken DB so the caller can
    degrade to no-persistence rather than ship a half-configured data layer.
    """
    import sqlite3  # noqa: PLC0415

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_CHAINLIT_SQLITE_SCHEMA)
        conn.commit()
    finally:
        conn.close()
