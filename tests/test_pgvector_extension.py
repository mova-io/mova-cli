"""Task 1 (ADR 009): the pgvector extension is created on PostgresProvider.init().

Postgres-gated — skips unless ``MOVATE_PG_TEST_URL`` is set (CI's postgres job
runs against the ``pgvector/pgvector:pg16`` image, where the extension is
available to ``CREATE EXTENSION``).
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.postgres


def _pg_url() -> str | None:
    return os.environ.get("MOVATE_PG_TEST_URL")


@pytest.mark.asyncio
async def test_init_creates_pgvector_extension() -> None:
    url = _pg_url()
    if url is None:
        pytest.skip("MOVATE_PG_TEST_URL not set; skipping postgres backend")

    from movate.storage.postgres import PostgresProvider  # noqa: PLC0415

    storage = PostgresProvider(dsn=url)
    await storage.init()
    try:
        present = await storage._db.fetchval("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        assert present == 1, "init() should CREATE EXTENSION vector"
    finally:
        await storage.close()
