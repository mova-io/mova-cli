"""Tests for the storage backend auto-selection in
:func:`movate.storage.build_storage`, plus the ``selected_backend()``
snapshot it exposes for /ready and `mdk doctor target`.

The selection logic is tiny (env var dispatch + a single warning log)
but it's load-bearing: a wrong choice in production silently bricks
every saved API key on the next revision recycle, and the only
externally-observable signal is the WARN log + the durability fields
in /ready. These tests pin both.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from movate.runtime.app import build_app
from movate.storage import SqliteProvider, build_storage, selected_backend
from movate.storage.postgres import PostgresProvider
from movate.testing import InMemoryStorage


@pytest.mark.unit
def test_build_storage_picks_sqlite_when_no_db_url(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Default path — no MOVATE_DB_URL → SqliteProvider + WARN log
    + selected_backend() reports non-durable."""
    monkeypatch.delenv("MOVATE_DB_URL", raising=False)
    monkeypatch.setenv("MOVATE_DB", "/tmp/mdk-test.db")

    with caplog.at_level(logging.WARNING, logger="movate.storage"):
        provider = build_storage()

    assert isinstance(provider, SqliteProvider)
    assert any("NOT durable" in r.message for r in caplog.records), (
        "expected a WARNING describing the durability problem"
    )
    snapshot = selected_backend()
    assert snapshot is not None
    backend, detail, durable = snapshot
    assert backend == "sqlite"
    assert detail == "/tmp/mdk-test.db"
    assert durable is False


@pytest.mark.unit
def test_build_storage_picks_postgres_when_db_url_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """MOVATE_DB_URL=postgresql://... → PostgresProvider + INFO log
    (no WARN about durability) + selected_backend() reports durable."""
    monkeypatch.setenv(
        "MOVATE_DB_URL",
        "postgresql://movate:@db.example.internal:5432/movate?sslmode=require",
    )

    # The real provider hits asyncpg at init() time; we only care that
    # build_storage selects it, not that we actually connect.
    with caplog.at_level(logging.INFO, logger="movate.storage"):
        provider = build_storage()

    assert isinstance(provider, PostgresProvider)
    assert not any("NOT durable" in r.message for r in caplog.records), (
        "must not warn about durability on the Postgres path"
    )
    snapshot = selected_backend()
    assert snapshot is not None
    backend, detail, durable = snapshot
    assert backend == "postgres"
    assert "host=db.example.internal" in detail
    assert "db=movate" in detail
    assert durable is True


@pytest.mark.unit
def test_ready_endpoint_surfaces_backend_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/ready`` exposes ``storage_backend`` + ``storage_durable``
    fields so ``mdk doctor target`` and the Angular admin UI can flag a
    non-durable production deployment without needing pod log access."""
    monkeypatch.delenv("MOVATE_DB_URL", raising=False)
    monkeypatch.setenv("MOVATE_DB", ":memory:")

    # Refresh the module-level snapshot by calling build_storage();
    # downstream /ready handler reads selected_backend() at request
    # time, not at app-construction time.
    _ = build_storage()

    app = build_app(InMemoryStorage())
    client = TestClient(app)

    r = client.get("/ready")
    payload: dict[str, Any] = r.json()
    assert payload["storage_backend"] == "sqlite"
    assert payload["storage_durable"] is False
