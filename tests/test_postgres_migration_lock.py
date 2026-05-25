"""Item 39: ``_run_migrations`` brackets its body with a session-level
``pg_advisory_lock`` so concurrent-startup pods serialize migrations.

These are pure unit tests against a fake asyncpg connection that records the
SQL it's asked to execute — no real Postgres needed (the integration coverage
in ``test_pgvector_search.py`` is postgres-gated and skips locally). We assert:

* the *first* statement issued is ``pg_advisory_lock`` (before
  ``schema_migrations`` is touched);
* a matching ``pg_advisory_unlock`` is issued;
* both use the same constant key, ``_MIGRATION_LOCK_KEY``;
* the unlock still fires when a migration step raises (the ``finally`` releases
  the lock so it can't leak).
"""

from __future__ import annotations

from typing import Any

import pytest

from movate.storage.postgres import _MIGRATION_LOCK_KEY, PostgresProvider


class _FakeTransaction:
    """Async context manager standing in for ``conn.transaction()``."""

    def __init__(self, fail: bool) -> None:
        self._fail = fail

    async def __aenter__(self) -> _FakeTransaction:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeConn:
    """Records every ``execute`` SQL + args; ``fetch`` returns no applied rows.

    Optionally raises from a migration body (a statement whose SQL contains
    ``raise_marker``) to exercise the ``finally`` release path.
    """

    def __init__(self, *, raise_on: str | None = None, fetchval: Any = "vector") -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self._raise_on = raise_on
        self._fetchval = fetchval

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        if self._raise_on is not None and self._raise_on in sql:
            raise RuntimeError("boom in migration body")
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        # No migrations recorded yet → the loop will attempt to apply them.
        return []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        # Migration 001 probes ``udt_name``; returning "vector" makes it an
        # idempotent no-op (the realistic "already applied" path another pod
        # sees once the lock holder finished).
        return self._fetchval

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(fail=False)


def _provider() -> PostgresProvider:
    # No pool / connection is opened: ``_run_migrations`` operates only on the
    # ``conn`` we pass in, so a bare instance is enough.
    return PostgresProvider(dsn="postgresql://unused/at_no_db")


@pytest.mark.asyncio
async def test_advisory_lock_is_acquired_first_and_released() -> None:
    conn = _FakeConn()
    provider = _provider()

    await provider._run_migrations(conn)  # type: ignore[arg-type]

    sqls = [sql for sql, _ in conn.executed]
    args = [a for _, a in conn.executed]

    # The very first statement must be the advisory lock — before
    # ``schema_migrations`` is created, so nothing races the tracking table.
    assert "pg_advisory_lock(" in sqls[0]
    assert args[0] == (_MIGRATION_LOCK_KEY,)
    lock_idx = next(i for i, s in enumerate(sqls) if "pg_advisory_lock(" in s)
    migrations_idx = next(i for i, s in enumerate(sqls) if "schema_migrations" in s)
    assert lock_idx == 0
    assert lock_idx < migrations_idx

    # A matching unlock must be issued with the same key.
    unlock_calls = [(s, a) for s, a in conn.executed if "pg_advisory_unlock(" in s]
    assert len(unlock_calls) == 1
    assert unlock_calls[0][1] == (_MIGRATION_LOCK_KEY,)

    # And the unlock is the *last* thing we do.
    assert "pg_advisory_unlock(" in conn.executed[-1][0]


@pytest.mark.asyncio
async def test_advisory_lock_released_even_when_migration_raises() -> None:
    # Force migration 001 past its idempotent early-return (udt != "vector"),
    # then blow up on its first ``kb_chunks`` ALTER to exercise the finally.
    conn = _FakeConn(raise_on="ALTER TABLE kb_chunks", fetchval="jsonb")
    provider = _provider()

    with pytest.raises(RuntimeError, match="boom in migration body"):
        await provider._run_migrations(conn)  # type: ignore[arg-type]

    sqls = [sql for sql, _ in conn.executed]
    # Lock acquired first...
    assert "pg_advisory_lock(" in sqls[0]
    # ...and despite the raise, the finally released it.
    unlock_calls = [(s, a) for s, a in conn.executed if "pg_advisory_unlock(" in s]
    assert len(unlock_calls) == 1
    assert unlock_calls[0][1] == (_MIGRATION_LOCK_KEY,)


def test_migration_lock_key_is_a_stable_int64_constant() -> None:
    assert isinstance(_MIGRATION_LOCK_KEY, int)
    # Postgres advisory keys are signed bigint — stay in range.
    assert -(2**63) <= _MIGRATION_LOCK_KEY < 2**63
