"""``movate.memory`` — agent memory layer.

A :class:`MemoryStore` Protocol with two shipped backends:

* ``InMemoryStore`` (default) — JSON-file-backed, persists to
  ``~/.movate/memory.json`` across process restarts. Thread-safe
  via RLock + atomic temp-rename. Suitable for single-node use.
* ``SqliteStore``              — aiosqlite-backed with WAL mode.
  Enabled via ``MOVATE_MEMORY_BACKEND=sqlite``. Better concurrency
  for multi-reader workloads; same Protocol surface as InMemoryStore.
* ``VectorStore``  (deferred) — semantic recall via pgvector
  / Azure AI Search. Follow-up once traffic patterns are clear.

Public API:

    MemoryEntry  — frozen dataclass: ``agent``, ``key``, ``value``,
                   ``created_at``, ``ttl_seconds``
    MemoryStore  — Protocol: ``list``, ``get``, ``set``, ``delete``
    InMemoryStore — default backend for the MVP CLI commands

The CLI commands (``mdk memory list / get / set / evict / summarise /
query``) live in :mod:`movate.cli.memory_cmd` and operate on the
auto-selected store from :func:`build_memory_store`.

[bold]Sprint T scope vs BACKLOG:[/bold] BACKLOG calls for full
engine integration (Executor consults the memory store mid-run +
summarisation policy + 3 backends). MVP ships the storage + CLI
surface so operators can manage memory state today; Executor
integration is a follow-up once the Sprint M-S queue lands.
"""

from __future__ import annotations

from movate.memory.store import (
    InMemoryStore,
    MemoryEntry,
    MemoryStore,
    SqliteStore,
    build_memory_store,
)

__all__ = [
    "InMemoryStore",
    "MemoryEntry",
    "MemoryStore",
    "SqliteStore",
    "build_memory_store",
]
