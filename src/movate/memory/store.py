"""Memory storage protocol + MVP backends.

``MemoryStore`` is a Protocol — Python-typed, no inheritance required.
Backends just match the shape. Same pattern as :mod:`movate.storage`.

Keys are per-agent (operator-supplied; we don't generate them). Values
are plain dicts so the executor can stash whatever serializable state
makes sense (last-N-messages, distilled summaries, retrieved chunks).
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class MemoryEntry:
    """One stored memory.

    ``ttl_seconds == 0`` means no expiration. ``created_at`` is ISO-8601
    UTC; the CLI renders this for ``mdk memory list``.
    """

    agent: str
    key: str
    value: dict[str, Any]
    created_at: str = ""
    ttl_seconds: int = 0


def _now_iso() -> str:
    """ISO-8601 UTC with millisecond precision (matches snapshot timestamps)."""
    now = datetime.now(UTC)
    millis = now.microsecond // 1000
    return now.strftime(f"%Y-%m-%dT%H:%M:%S.{millis:03d}Z")


def _is_alive(entry: MemoryEntry) -> bool:
    """Return True when the entry has not yet expired.

    ``ttl_seconds == 0`` means no expiration — always alive.
    An unparseable ``created_at`` is treated as alive (we'd rather
    surface a stale entry than silently lose data due to a bad
    timestamp).
    """
    if entry.ttl_seconds == 0:
        return True
    try:
        created = datetime.fromisoformat(entry.created_at.removesuffix("Z")).replace(tzinfo=UTC)
    except (ValueError, AttributeError):
        return True  # malformed timestamp — don't silently evict
    return created + timedelta(seconds=entry.ttl_seconds) > datetime.now(UTC)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class MemoryStore(Protocol):
    """All backends implement this Protocol.

    Async so backends can run against Postgres / vector DBs without
    blocking the executor's event loop. The in-memory backend
    awaits trivially.
    """

    async def list(self, agent: str) -> list[MemoryEntry]:
        """All entries for one agent, sorted by created_at ascending."""

    async def get(self, agent: str, key: str) -> MemoryEntry | None:
        """One entry; None when absent."""

    async def set(
        self,
        agent: str,
        key: str,
        value: dict[str, Any],
        *,
        ttl_seconds: int = 0,
    ) -> MemoryEntry:
        """Insert or replace. Returns the stored entry."""

    async def delete(self, agent: str, key: str) -> bool:
        """True if an entry was removed; False if it didn't exist."""

    async def evict_older_than(self, agent: str, before_iso: str) -> int:
        """Delete entries whose created_at < ``before_iso``. Returns count."""


# ---------------------------------------------------------------------------
# InMemoryStore — MVP default
# ---------------------------------------------------------------------------


@dataclass
class InMemoryStore:
    """JSON-file-backed memory store. Default for CLI use.

    Persists to ``MOVATE_MEMORY_FILE`` (default
    ``~/.movate/memory.json``). Reloads on every operation so
    cross-invocation CLI use works — ``mdk memory set`` followed by
    ``mdk memory list`` returns the entry written by the prior call.

    Thread-safe via RLock. Atomic writes via temp + rename so a
    crashed process can't leave a half-written file.

    Name kept as ``InMemoryStore`` (not ``JsonStore``) because the
    in-process behavior matches: dict-backed, no SQL engine, fast.
    The JSON file is just a per-invocation persistence layer.
    """

    _path: Path = field(default_factory=lambda: Path("~/.movate/memory.json").expanduser())
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def _load(self) -> dict[str, dict[str, MemoryEntry]]:
        if not self._path.is_file():
            return {}
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict[str, MemoryEntry]] = {}
        for agent, entries in raw.items():
            if not isinstance(entries, dict):
                continue
            bucket: dict[str, MemoryEntry] = {}
            for key, entry_raw in entries.items():
                if not isinstance(entry_raw, dict):
                    continue
                bucket[str(key)] = MemoryEntry(
                    agent=str(agent),
                    key=str(key),
                    value=entry_raw.get("value") or {},
                    created_at=str(entry_raw.get("created_at") or ""),
                    ttl_seconds=int(entry_raw.get("ttl_seconds") or 0),
                )
            out[str(agent)] = bucket
        return out

    def _save(self, data: dict[str, dict[str, MemoryEntry]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        serializable = {
            agent: {
                key: {
                    "value": entry.value,
                    "created_at": entry.created_at,
                    "ttl_seconds": entry.ttl_seconds,
                }
                for key, entry in entries.items()
            }
            for agent, entries in data.items()
        }
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(serializable, indent=2, ensure_ascii=False))
        tmp.replace(self._path)

    async def list(self, agent: str) -> list[MemoryEntry]:
        with self._lock:
            data = self._load()
        entries = [e for e in data.get(agent, {}).values() if _is_alive(e)]
        return sorted(entries, key=lambda e: e.created_at)

    async def get(self, agent: str, key: str) -> MemoryEntry | None:
        with self._lock:
            data = self._load()
        entry = data.get(agent, {}).get(key)
        if entry is None or not _is_alive(entry):
            return None
        return entry

    async def set(
        self,
        agent: str,
        key: str,
        value: dict[str, Any],
        *,
        ttl_seconds: int = 0,
    ) -> MemoryEntry:
        entry = MemoryEntry(
            agent=agent,
            key=key,
            value=value,
            created_at=_now_iso(),
            ttl_seconds=ttl_seconds,
        )
        with self._lock:
            data = self._load()
            data.setdefault(agent, {})[key] = entry
            self._save(data)
        return entry

    async def delete(self, agent: str, key: str) -> bool:
        with self._lock:
            data = self._load()
            agent_bucket = data.get(agent, {})
            if key in agent_bucket:
                del agent_bucket[key]
                self._save(data)
                return True
        return False

    async def evict_older_than(self, agent: str, before_iso: str) -> int:
        with self._lock:
            data = self._load()
            agent_bucket = data.get(agent, {})
            stale = [k for k, e in agent_bucket.items() if e.created_at < before_iso]
            for k in stale:
                del agent_bucket[k]
            if stale:
                self._save(data)
        return len(stale)


# ---------------------------------------------------------------------------
# SqliteStore — aiosqlite-backed persistent memory
# ---------------------------------------------------------------------------

_CREATE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS memory_entries (
    agent       TEXT NOT NULL,
    key         TEXT NOT NULL,
    value_json  TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    ttl_seconds INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (agent, key)
);
CREATE INDEX IF NOT EXISTS idx_memory_agent ON memory_entries (agent);
"""


class SqliteStore:
    """Persistent memory backed by SQLite via ``aiosqlite``.

    Enabled by setting ``MOVATE_MEMORY_BACKEND=sqlite`` (or by
    constructing directly). Uses ``~/.movate/memory.db`` by default;
    override with the ``db_path`` constructor arg.

    Design choices:
    * Schema is a single ``memory_entries`` table with ``(agent, key)``
      as the composite primary key — upsert via ``INSERT OR REPLACE``.
    * ``value`` is stored as JSON text; round-trips cleanly for any
      JSON-serializable dict.
    * WAL mode is enabled on first connection for better concurrent
      read throughput (multiple readers, one writer).
    * Each async method opens + closes its own connection so the store
      is safe to share across async tasks without connection-pool
      overhead (writes are infrequent; connections are lightweight).
    * ``aiosqlite`` is already a core dependency (used by the storage
      layer); no new requirements.
    """

    def __init__(self, db_path: str | Path = "~/.movate/memory.db") -> None:
        self._path = Path(str(db_path)).expanduser()

    async def _conn(self) -> Any:
        """Open a connection, enable WAL, ensure schema, return connection.

        Caller is responsible for closing (used as async context manager
        by each method).
        """
        import aiosqlite  # noqa: PLC0415 — optional at module level; always installed

        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self._path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript(_CREATE_TABLE_SQL)
        await conn.commit()
        return conn

    # SQL fragment that filters out rows whose TTL has passed.
    # ``ttl_seconds = 0`` means immortal — never filtered.
    # ``replace(created_at, 'Z', '')`` strips the trailing 'Z' so
    # SQLite's datetime() receives a bare ISO-8601 string it can
    # parse on all supported SQLite versions (3.8+). The 'Z' suffix
    # is only accepted by SQLite >= 3.38.0.
    _TTL_ALIVE_CLAUSE = (
        "AND (ttl_seconds = 0 "
        "OR datetime(replace(created_at, 'Z', ''), '+' || ttl_seconds || ' seconds')"
        " > datetime('now'))"
    )

    async def list(self, agent: str) -> list[MemoryEntry]:
        """All non-expired entries for ``agent``, sorted by created_at ascending."""
        conn = await self._conn()
        try:
            async with conn.execute(
                "SELECT agent, key, value_json, created_at, ttl_seconds "
                "FROM memory_entries WHERE agent = ? "
                + self._TTL_ALIVE_CLAUSE
                + " ORDER BY created_at ASC",
                (agent,),
            ) as cursor:
                rows = await cursor.fetchall()
        finally:
            await conn.close()
        return [_row_to_entry(row) for row in rows]

    async def get(self, agent: str, key: str) -> MemoryEntry | None:
        """One non-expired entry; ``None`` when absent or expired."""
        conn = await self._conn()
        try:
            async with conn.execute(
                "SELECT agent, key, value_json, created_at, ttl_seconds "
                "FROM memory_entries WHERE agent = ? AND key = ? " + self._TTL_ALIVE_CLAUSE,
                (agent, key),
            ) as cursor:
                row = await cursor.fetchone()
        finally:
            await conn.close()
        return _row_to_entry(row) if row else None

    async def set(
        self,
        agent: str,
        key: str,
        value: dict[str, Any],
        *,
        ttl_seconds: int = 0,
    ) -> MemoryEntry:
        """Insert or replace. Returns the stored entry."""
        entry = MemoryEntry(
            agent=agent,
            key=key,
            value=value,
            created_at=_now_iso(),
            ttl_seconds=ttl_seconds,
        )
        conn = await self._conn()
        try:
            await conn.execute(
                "INSERT OR REPLACE INTO memory_entries "
                "(agent, key, value_json, created_at, ttl_seconds) "
                "VALUES (?, ?, ?, ?, ?)",
                (agent, key, json.dumps(value, ensure_ascii=False), entry.created_at, ttl_seconds),
            )
            await conn.commit()
        finally:
            await conn.close()
        return entry

    async def delete(self, agent: str, key: str) -> bool:
        """Delete one entry. Returns ``True`` if a row was removed."""
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "DELETE FROM memory_entries WHERE agent = ? AND key = ?",
                (agent, key),
            )
            await conn.commit()
            deleted = int(cursor.rowcount) > 0
        finally:
            await conn.close()
        return deleted

    async def evict_older_than(self, agent: str, before_iso: str) -> int:
        """Delete entries whose created_at < ``before_iso``. Returns count."""
        conn = await self._conn()
        try:
            cursor = await conn.execute(
                "DELETE FROM memory_entries WHERE agent = ? AND created_at < ?",
                (agent, before_iso),
            )
            await conn.commit()
            return int(cursor.rowcount)
        finally:
            await conn.close()


def _row_to_entry(row: Any) -> MemoryEntry:
    """Convert an ``aiosqlite.Row`` to a :class:`MemoryEntry`."""
    try:
        value = json.loads(row["value_json"])
    except (TypeError, ValueError):
        value = {}
    return MemoryEntry(
        agent=row["agent"],
        key=row["key"],
        value=value if isinstance(value, dict) else {},
        created_at=row["created_at"],
        ttl_seconds=int(row["ttl_seconds"]),
    )


# ---------------------------------------------------------------------------
# PostgresStore — asyncpg-backed multi-worker production memory
# ---------------------------------------------------------------------------

_CREATE_PG_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS agent_memory (
    agent       TEXT NOT NULL,
    key         TEXT NOT NULL,
    value_json  TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    ttl_seconds INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (agent, key)
);
CREATE INDEX IF NOT EXISTS idx_agent_memory_agent ON agent_memory (agent);
"""

# TTL filter using Postgres date arithmetic.
# ``ttl_seconds = 0`` means immortal — never filtered.
# ``created_at::timestamptz`` casts the stored ISO-8601 string to a
# timestamptz so Postgres can compute the interval offset natively.
_PG_TTL_ALIVE_CLAUSE = (
    "AND (ttl_seconds = 0 "
    "OR created_at::timestamptz + (ttl_seconds || ' seconds')::interval "
    "> now() AT TIME ZONE 'UTC')"
)


class PostgresStore:
    """Persistent memory backed by PostgreSQL via ``asyncpg``.

    Enabled by setting ``MOVATE_MEMORY_BACKEND=postgres``. Requires
    ``MOVATE_PG_URL`` to be set to a valid asyncpg DSN (e.g.
    ``postgresql://user:pass@host/dbname``), or pass the DSN directly
    via the ``dsn`` constructor argument.

    Design choices mirror :class:`SqliteStore`:
    * Schema is a single ``agent_memory`` table with ``(agent, key)`` as
      the composite primary key — upsert via ``INSERT ... ON CONFLICT``.
    * ``value`` is stored as JSON text; round-trips cleanly for any
      JSON-serializable dict.
    * Each async method opens + closes its own connection so the store is
      safe to share across async tasks without connection-pool overhead.
      Writes are infrequent and connections are lightweight.
    * ``asyncpg`` is already in the ``[runtime]`` extra — no new deps.
    * Table is named ``agent_memory`` (distinct from the SQLite
      ``memory_entries`` table to avoid confusion when inspecting a
      shared Postgres instance).
    """

    def __init__(self, dsn: str = "") -> None:
        self._dsn = dsn or os.environ.get("MOVATE_PG_URL", "")

    async def _ensure_schema(self, conn: Any) -> None:
        """Create the ``agent_memory`` table and index if they don't exist."""
        await conn.execute(_CREATE_PG_TABLE_SQL)

    async def list(self, agent: str) -> list[MemoryEntry]:
        """All non-expired entries for ``agent``, sorted by created_at ascending."""
        import asyncpg  # noqa: PLC0415 — optional at module level

        conn = await asyncpg.connect(self._dsn)
        try:
            await self._ensure_schema(conn)
            rows = await conn.fetch(
                "SELECT agent, key, value_json, created_at, ttl_seconds "
                "FROM agent_memory WHERE agent = $1 "
                + _PG_TTL_ALIVE_CLAUSE
                + " ORDER BY created_at ASC",
                agent,
            )
        finally:
            await conn.close()
        return [_pg_row_to_entry(row) for row in rows]

    async def get(self, agent: str, key: str) -> MemoryEntry | None:
        """One non-expired entry; ``None`` when absent or expired."""
        import asyncpg  # noqa: PLC0415 — optional at module level

        conn = await asyncpg.connect(self._dsn)
        try:
            await self._ensure_schema(conn)
            row = await conn.fetchrow(
                "SELECT agent, key, value_json, created_at, ttl_seconds "
                "FROM agent_memory WHERE agent = $1 AND key = $2 " + _PG_TTL_ALIVE_CLAUSE,
                agent,
                key,
            )
        finally:
            await conn.close()
        return _pg_row_to_entry(row) if row else None

    async def set(
        self,
        agent: str,
        key: str,
        value: dict[str, Any],
        *,
        ttl_seconds: int = 0,
    ) -> MemoryEntry:
        """Insert or replace. Returns the stored entry."""
        import asyncpg  # noqa: PLC0415 — optional at module level

        entry = MemoryEntry(
            agent=agent,
            key=key,
            value=value,
            created_at=_now_iso(),
            ttl_seconds=ttl_seconds,
        )
        conn = await asyncpg.connect(self._dsn)
        try:
            await self._ensure_schema(conn)
            await conn.execute(
                "INSERT INTO agent_memory (agent, key, value_json, created_at, ttl_seconds) "
                "VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT (agent, key) DO UPDATE SET "
                "value_json = EXCLUDED.value_json, "
                "created_at = EXCLUDED.created_at, "
                "ttl_seconds = EXCLUDED.ttl_seconds",
                agent,
                key,
                json.dumps(value, ensure_ascii=False),
                entry.created_at,
                ttl_seconds,
            )
        finally:
            await conn.close()
        return entry

    async def delete(self, agent: str, key: str) -> bool:
        """Delete one entry. Returns ``True`` if a row was removed."""
        import asyncpg  # noqa: PLC0415 — optional at module level

        conn = await asyncpg.connect(self._dsn)
        try:
            await self._ensure_schema(conn)
            result = await conn.execute(
                "DELETE FROM agent_memory WHERE agent = $1 AND key = $2",
                agent,
                key,
            )
        finally:
            await conn.close()
        # asyncpg returns a tag string like "DELETE 1" or "DELETE 0"
        return bool(str(result).endswith(" 1"))

    async def evict_older_than(self, agent: str, before_iso: str) -> int:
        """Delete entries whose created_at < ``before_iso``. Returns count."""
        import asyncpg  # noqa: PLC0415 — optional at module level

        conn = await asyncpg.connect(self._dsn)
        try:
            await self._ensure_schema(conn)
            result = await conn.execute(
                "DELETE FROM agent_memory WHERE agent = $1 AND created_at < $2",
                agent,
                before_iso,
            )
        finally:
            await conn.close()
        # asyncpg returns a tag string like "DELETE 3"; parse the count.
        try:
            return int(result.rsplit(" ", 1)[-1])
        except (ValueError, AttributeError):
            return 0


def _pg_row_to_entry(row: Any) -> MemoryEntry:
    """Convert an ``asyncpg.Record`` to a :class:`MemoryEntry`."""
    try:
        value = json.loads(row["value_json"])
    except (TypeError, ValueError):
        value = {}
    return MemoryEntry(
        agent=row["agent"],
        key=row["key"],
        value=value if isinstance(value, dict) else {},
        created_at=row["created_at"],
        ttl_seconds=int(row["ttl_seconds"]),
    )


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


def build_memory_store() -> MemoryStore:
    """Auto-select a backend based on ``MOVATE_MEMORY_BACKEND`` env var.

    Values:
      * ``memory``   (default) — :class:`InMemoryStore`
      * ``sqlite``             — :class:`SqliteStore`
      * ``postgres``           — :class:`PostgresStore`

    Future:
      * ``vector`` — semantic recall via pgvector / Azure AI Search
    """
    backend = os.environ.get("MOVATE_MEMORY_BACKEND", "memory").lower()
    if backend == "sqlite":
        return SqliteStore()
    if backend == "postgres":
        dsn = os.environ.get("MOVATE_PG_URL", "")
        if not dsn:
            raise RuntimeError("MOVATE_PG_URL must be set to use MOVATE_MEMORY_BACKEND=postgres")
        return PostgresStore(dsn=dsn)
    # Default = in-memory + JSON-file persistence so CLI invocations
    # see each other's writes. Honors MOVATE_MEMORY_FILE for tests +
    # operators who want a non-default location.
    path = os.environ.get("MOVATE_MEMORY_FILE")
    if path:
        return InMemoryStore(_path=Path(path).expanduser())
    return InMemoryStore()
