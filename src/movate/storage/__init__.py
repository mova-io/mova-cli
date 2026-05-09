"""Storage providers: pluggable persistence behind a single Protocol."""

from __future__ import annotations

from movate.storage.base import StorageProvider
from movate.storage.sqlite import SqliteProvider

__all__ = ["SqliteProvider", "StorageProvider", "build_storage"]


def build_storage() -> StorageProvider:
    """Auto-select a StorageProvider.

    v0.1: always SQLite at ``~/.movate/local.db``. Postgres lands in v0.5.
    """
    return SqliteProvider()
