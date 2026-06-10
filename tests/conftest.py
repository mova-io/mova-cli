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

Test-authoring conventions
---------------------------
* **Warnings / deprecations: assert via ``caplog``, not ``capsys``.**
  movate emits deprecation and warning messages through the stdlib
  ``logging`` module (e.g. ``movate.core.config``), which pytest captures
  in ``caplog`` — they do NOT go to stdout/stderr. Use
  ``caplog.set_level(logging.WARNING)`` then assert on ``caplog.text``.
  Asserting on ``capsys`` for these is the most common stale-test trap.
* **``--json`` output is stdout-only.** Diagnostic lines (e.g. "memory
  backend: …") must go to stderr so machine-readable stdout stays valid
  JSON. Assert structured output by ``json.loads`` of stdout; route the
  human-readable chatter to ``err_console``.
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
    "bench",
    "audits",
    "agent_bundles",
    "workflow_bundles",
    "workflow_runs",
    "jobs",
    "batches",
    "api_keys",
    "kb_chunks",
    "kb_entities",
    "kb_relations",
    "trigger_deliveries",
    "run_submissions",
    "projects",
    "project_members",
    "project_agents",
    "project_workflows",
    "project_kbs",
    "catalog_entries",
    "catalog_entry_versions",
    "catalog_entry_ratings",
    "catalog_sync_watermark",
    "observability_insights",
    "observability_facts",
    "webhooks",
    "webhook_attempts",
    "webhook_cursors",
)


def _pg_test_url() -> str | None:
    """Postgres DSN for tests, or ``None`` if PG tests are disabled."""
    return os.environ.get("MOVATE_PG_TEST_URL")


@pytest.fixture(autouse=True)
def _isolate_credentials_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Hard safety guard: redirect the credentials store to a per-test tmp
    file so NO test can ever read or clobber the developer's (or CI's) real
    ``~/.movate/credentials``.

    Why this exists: the store's default path was frozen at import time, so a
    test that only monkeypatched ``HOME`` did NOT get isolated — it truncated
    the real credentials file and wrote a stub key (``sk-test-12345``),
    silently destroying a developer's saved OpenAI/Anthropic keys on every
    ``pytest`` run. Setting ``MOVATE_CREDENTIALS_PATH`` here uses the store's
    dynamic env-override branch, so isolation holds regardless of import
    order or per-test HOME patching. A test that needs a specific location
    can still override ``MOVATE_CREDENTIALS_PATH`` itself.
    """
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / "credentials"))


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
