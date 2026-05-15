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
from datetime import UTC, datetime
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
        entries = list(data.get(agent, {}).values())
        return sorted(entries, key=lambda e: e.created_at)

    async def get(self, agent: str, key: str) -> MemoryEntry | None:
        with self._lock:
            data = self._load()
        return data.get(agent, {}).get(key)

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
# SqliteStore — MVP scaffold (raises on writes; shape is here)
# ---------------------------------------------------------------------------


class SqliteStore:
    """Persistent memory backed by SQLite.

    [bold]MVP scaffold[/bold] — the Protocol surface is implemented
    but writes raise ``NotImplementedError``. Lets the Executor
    wiring + CLI dispatch land against the right interface; the
    actual DDL + adapter lands in a follow-up.
    """

    def __init__(self, db_path: str | Path = "~/.movate/memory.db") -> None:
        self._path = Path(str(db_path)).expanduser()

    async def list(self, agent: str) -> list[MemoryEntry]:
        _ = agent
        return []  # Scaffold: empty store until SQLite adapter lands.

    async def get(self, agent: str, key: str) -> MemoryEntry | None:
        _ = (agent, key)
        return None

    async def set(
        self,
        agent: str,
        key: str,
        value: dict[str, Any],
        *,
        ttl_seconds: int = 0,
    ) -> MemoryEntry:
        _ = (agent, key, value, ttl_seconds)
        raise NotImplementedError(
            "SqliteStore.set is a scaffold — use InMemoryStore for MVP "
            "(set MOVATE_MEMORY_BACKEND=memory) or wait for the SQLite "
            "adapter (Sprint T follow-up)"
        )

    async def delete(self, agent: str, key: str) -> bool:
        _ = (agent, key)
        return False

    async def evict_older_than(self, agent: str, before_iso: str) -> int:
        _ = (agent, before_iso)
        return 0


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


def build_memory_store() -> MemoryStore:
    """Auto-select a backend based on ``MOVATE_MEMORY_BACKEND`` env var.

    Values:
      * ``memory`` (default) — :class:`InMemoryStore`
      * ``sqlite``           — :class:`SqliteStore` (scaffold)

    Future:
      * ``postgres`` — multi-worker production backend
      * ``vector``   — semantic recall via pgvector / Azure AI Search
    """
    backend = os.environ.get("MOVATE_MEMORY_BACKEND", "memory").lower()
    if backend == "sqlite":
        return SqliteStore()
    # Default = in-memory + JSON-file persistence so CLI invocations
    # see each other's writes. Honors MOVATE_MEMORY_FILE for tests +
    # operators who want a non-default location.
    path = os.environ.get("MOVATE_MEMORY_FILE")
    if path:
        return InMemoryStore(_path=Path(path).expanduser())
    return InMemoryStore()
