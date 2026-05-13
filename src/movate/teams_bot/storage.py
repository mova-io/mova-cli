"""Teams-user → Movate-API-key binding storage.

Scoped to the bot's own concerns: a tiny ``teams_users`` table that
maps an Azure AD object id to an encrypted Movate API key + the
user's tenant. Deliberately separate from the main
:class:`movate.storage.StorageProvider` because:

* The runtime doesn't need this table — only the bot reads it.
* The bot's storage lifecycle (encryption key, schema) is independent
  of the runtime's. Easier to evolve in isolation.
* Postgres impl is intentionally not in this PR — alpha pilots run
  on sqlite (one bot instance, one volume). Adding the Postgres
  variant is a tracked follow-up (issue TBD).

Schema (sqlite)
---------------

::

    CREATE TABLE IF NOT EXISTS teams_users (
        aad_object_id   TEXT PRIMARY KEY,
        tenant_prefix   TEXT NOT NULL,   -- from parse_api_key
        encrypted_key   BLOB NOT NULL,   -- Fernet ciphertext
        key_hint        TEXT NOT NULL,   -- last 4 chars, for whoami
        created_at      TIMESTAMP NOT NULL,
        updated_at      TIMESTAMP NOT NULL
    )

The store NEVER returns ciphertext to its caller. ``get_decrypted_key``
returns the plaintext key or raises :class:`MissingBindingError`. The
encryption boundary is at the store, not the resolver — keeps the
crypto blast-radius tiny.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from movate.teams_bot.crypto import (
    decrypt_key,
    encrypt_key,
    hint_from_key,
)

if TYPE_CHECKING:
    import aiosqlite
    from cryptography.fernet import Fernet


# Default DB path — sibling to ``~/.movate/local.db`` so all
# bot-related state lives under the same dir. Operators override via
# ``MOVATE_TEAMS_DB`` env.
ENV_TEAMS_DB = "MOVATE_TEAMS_DB"
_DEFAULT_TEAMS_DB = Path.home() / ".movate" / "teams.db"


class MissingBindingError(Exception):
    """Raised when a lookup returns no row for the given AAD id.

    Distinct from ``None`` returns so callers (the resolver) can
    distinguish "user hasn't bound yet" (None / no-op) from "store
    layer error" (this exception, surfaces in logs).

    Not raised by ``get_binding`` itself — that returns ``None`` for
    the not-found case so it composes cleanly with ``if`` checks.
    Reserved for the decrypt-and-return path which has no obvious
    sentinel for failure.
    """


@dataclass(frozen=True)
class TeamsUserBinding:
    """One row of the ``teams_users`` table — what ``whoami`` reads.

    ``key_hint`` is the last 4 chars of the API key (plaintext, safe
    to display). The actual API key only exists in plaintext inside
    :func:`TeamsUsersStore.get_decrypted_key` — never on this struct.
    """

    aad_object_id: str
    tenant_prefix: str
    key_hint: str
    created_at: datetime
    updated_at: datetime


class TeamsUsersStore:
    """SQLite-backed teams_users store.

    Async API mirrors the main :class:`movate.storage.sqlite.SqliteProvider`
    so a future Postgres variant slots in via duck-typed protocol.

    Thread-safe in the same sense aiosqlite is — concurrent reads/writes
    serialise via the single underlying connection. The bot is single-
    process, so this is fine; if/when we add multi-replica deploys,
    swap to Postgres.
    """

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        fernet: Fernet | None = None,
    ) -> None:
        """Args:

        db_path: where the sqlite file lives. Tests pass an in-memory
            URI (``Path(":memory:")``); production reads
            ``MOVATE_TEAMS_DB`` or falls back to ``~/.movate/teams.db``.
        fernet: pre-built Fernet for encryption. Tests inject one keyed
            off a known value so assertions are stable; production
            leaves it None and the store resolves from env.
        """
        resolved = db_path
        if resolved is None:
            env = os.environ.get(ENV_TEAMS_DB)
            resolved = Path(env) if env else _DEFAULT_TEAMS_DB
        self._db_path = resolved
        self._fernet = fernet
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Idempotent schema setup. Safe to call on every bot startup."""
        import aiosqlite  # noqa: PLC0415

        # Path(":memory:") is sqlite's special-case in-memory store —
        # we don't want mkdir to try to create a "memory" directory.
        if str(self._db_path) != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS teams_users (
                aad_object_id   TEXT PRIMARY KEY,
                tenant_prefix   TEXT NOT NULL,
                encrypted_key   BLOB NOT NULL,
                key_hint        TEXT NOT NULL,
                created_at      TIMESTAMP NOT NULL,
                updated_at      TIMESTAMP NOT NULL
            )
            """
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # --------------------------------------------------------------
    # CRUD
    # --------------------------------------------------------------

    async def upsert_binding(
        self,
        *,
        aad_object_id: str,
        tenant_prefix: str,
        api_key_plaintext: str,
    ) -> TeamsUserBinding:
        """Create or replace the binding for one user.

        Used by ``/movate connect`` (first bind) and ``/movate
        rotate-key`` (replace). The encrypted ciphertext + key hint
        are derived here; callers never see them.

        Returns the resulting :class:`TeamsUserBinding` so the
        confirmation card can render the tenant + hint without a
        second round-trip.
        """
        assert self._db is not None, "store not initialised — call init() first"

        ciphertext = encrypt_key(api_key_plaintext, fernet=self._fernet)
        hint = hint_from_key(api_key_plaintext)
        now = datetime.now(UTC)

        await self._db.execute(
            """
            INSERT INTO teams_users (
                aad_object_id, tenant_prefix, encrypted_key, key_hint,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(aad_object_id) DO UPDATE SET
                tenant_prefix = excluded.tenant_prefix,
                encrypted_key = excluded.encrypted_key,
                key_hint      = excluded.key_hint,
                updated_at    = excluded.updated_at
            """,
            (aad_object_id, tenant_prefix, ciphertext, hint, now, now),
        )
        await self._db.commit()

        # Re-read so the returned binding's created_at reflects the
        # ACTUAL stored value (preserves the original on a rebind).
        binding = await self.get_binding(aad_object_id)
        assert binding is not None, "upsert didn't yield a binding — sqlite bug?"
        return binding

    async def get_binding(self, aad_object_id: str) -> TeamsUserBinding | None:
        """Look up one user's metadata (no plaintext key).

        Returns ``None`` when the user hasn't connected — clean
        ``if binding is None`` check for the resolver.
        """
        assert self._db is not None, "store not initialised — call init() first"
        async with self._db.execute(
            """
            SELECT aad_object_id, tenant_prefix, key_hint,
                   created_at, updated_at
            FROM teams_users
            WHERE aad_object_id = ?
            """,
            (aad_object_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return TeamsUserBinding(
            aad_object_id=row[0],
            tenant_prefix=row[1],
            key_hint=row[2],
            created_at=_parse_dt(row[3]),
            updated_at=_parse_dt(row[4]),
        )

    async def get_decrypted_key(self, aad_object_id: str) -> str:
        """Resolve to the plaintext API key for runtime calls.

        Plaintext is held only briefly — the resolver passes it
        straight to MovateClient and discards it. Raises
        :class:`MissingBindingError` when the user hasn't connected.
        """
        assert self._db is not None, "store not initialised — call init() first"
        async with self._db.execute(
            "SELECT encrypted_key FROM teams_users WHERE aad_object_id = ?",
            (aad_object_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise MissingBindingError(f"no Teams binding for aad_object_id={aad_object_id}")
        return decrypt_key(row[0], fernet=self._fernet)

    async def delete_binding(self, aad_object_id: str) -> bool:
        """Remove a binding (``/movate disconnect``). Returns True if
        a row was actually deleted, False if the user wasn't bound."""
        assert self._db is not None, "store not initialised — call init() first"
        async with self._db.execute(
            "DELETE FROM teams_users WHERE aad_object_id = ?",
            (aad_object_id,),
        ) as cur:
            deleted = cur.rowcount > 0
        await self._db.commit()
        return deleted


def _parse_dt(value: object) -> datetime:
    """sqlite3 stores TIMESTAMP as ISO strings — parse back to datetime.

    Older sqlite versions and PARSE_DECLTYPES configs differ on whether
    we get back a string or a datetime object directly. Handle both
    for portability.
    """
    if isinstance(value, datetime):
        # Some sqlite builds return naive datetimes; tag them as UTC
        # to match how upsert_binding wrote them.
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    if isinstance(value, str):
        # ISO 8601 with or without timezone — fromisoformat handles both.
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt
    raise TypeError(f"unexpected timestamp type: {type(value).__name__}")
