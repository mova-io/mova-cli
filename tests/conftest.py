"""Shared test infrastructure — parametrized storage backend fixture.

The ``storage`` fixture yields a freshly-initialized
:class:`StorageProvider` against one of three backends:

* ``memory`` — :class:`InMemoryStorage` (always available)
* ``sqlite`` — :class:`SqliteProvider` against a tmp_path file
  (always available)
* ``postgres`` — :class:`PostgresProvider` against the URL in
  ``MOVATE_PG_TEST_URL`` if set, else the test is skipped

Tests opt in via ``def test_x(storage): ...`` and get the test
parametrized over all three backends automatically.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from movate.storage.base import StorageProvider
from movate.storage.sqlite import SqliteProvider
from movate.testing import InMemoryStorage

# Tables to truncate between Postgres tests so each one starts from a
# clean DB without re-creating the schema (cheap; ~1ms per truncate).
_PG_TABLES = (
    "runs",
    "failures",
    "evals",
    "bench_records",
    "workflow_runs",
    "jobs",
    "api_keys",
)


def _pg_test_url() -> str | None:
    """Postgres DSN for tests, or ``None`` if PG tests are disabled."""
    return os.environ.get("MOVATE_PG_TEST_URL")


@pytest.fixture(
    params=[
        "memory",
        "sqlite",
        pytest.param("postgres", marks=pytest.mark.postgres),
    ]
)
async def storage(request, tmp_path: Path) -> AsyncIterator[StorageProvider]:
    """Parametrized storage backend.

    Postgres skips automatically when ``MOVATE_PG_TEST_URL`` isn't set,
    so devs without a local PG don't see noisy failures and CI can
    add a service-container job to exercise that branch.
    """
    if request.param == "memory":
        storage: StorageProvider = InMemoryStorage()
    elif request.param == "sqlite":
        storage = SqliteProvider(db_path=tmp_path / "test.db")
    else:  # postgres
        url = _pg_test_url()
        if url is None:
            pytest.skip("MOVATE_PG_TEST_URL not set; skipping postgres backend")
        # Lazy import keeps asyncpg optional for sqlite-only test runs.
        from movate.storage.postgres import PostgresProvider  # noqa: PLC0415

        storage = PostgresProvider(dsn=url)

    await storage.init()

    # Postgres-specific: truncate tables so each test starts hermetic.
    # The schema persists across tests (idempotent CREATE TABLE),
    # but data does not.
    if request.param == "postgres":
        await _truncate_pg_tables(storage)

    try:
        yield storage
    finally:
        if request.param == "postgres":
            await _truncate_pg_tables(storage)
        await storage.close()


async def _truncate_pg_tables(storage: StorageProvider) -> None:
    """Wipe every table movate writes to. Used as both setup and teardown
    so a previous test leaving rows behind doesn't bleed into the next."""
    # Reach into the pool — we know it's PostgresProvider in this branch.
    pool = storage._db  # type: ignore[attr-defined]
    tables = ", ".join(_PG_TABLES)
    await pool.execute(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE")
