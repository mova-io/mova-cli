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
from movate.storage import (
    SqliteProvider,
    _reset_state_for_tests,
    build_storage,
    mark_cli_mode,
    selected_backend,
)
from movate.storage.postgres import PostgresProvider
from movate.testing import InMemoryStorage


@pytest.fixture(autouse=True)
def _reset_storage_globals() -> None:
    """Wipe the once-per-process flags before every test.

    ``build_storage`` emits the durability warning at most ONCE per
    process (added 2026-05-19 to keep ``mdk eval-scorecard``'s 10+
    build_storage calls from spamming stderr). Without this reset
    the first test in this file would consume the warning and any
    later test asserting on it would silently see zero records.
    Also resets the CLI-mode flag so tests don't leak across each
    other.
    """
    _reset_state_for_tests()


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
async def test_postgres_provider_passes_pgpassword_as_kwarg_when_dsn_password_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Azure Container Apps Bicep wires the DSN as
    ``postgresql://user:@host/db`` (empty password slot) and the
    password as a separate ``PGPASSWORD`` env var. asyncpg's documented
    PGPASSWORD fallback only fires when the DSN's password component is
    MISSING, not when it's present-but-empty — so without this shim the
    pod startup dies with ``InvalidPasswordError: password
    authentication failed`` (caught in the wild on dev,
    revision movate-dev-api--0000010, May 2026). Pass PGPASSWORD as an
    explicit kwarg to sidestep it.
    """
    captured: dict[str, Any] = {}

    async def fake_create_pool(dsn: str, **kwargs: Any) -> Any:
        captured["dsn"] = dsn
        captured["kwargs"] = kwargs

        # Return a stub pool whose acquire() context manager returns a
        # stub conn whose execute() coroutine is a no-op — enough to let
        # init() complete past the _SCHEMA execute.
        class _Tx:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *_a: Any) -> None:
                return None

        class _Conn:
            # init() now also runs schema migrations (ADR 009): a no-op
            # fetch (no applied rows) lets migration 001 run as execute
            # no-ops, fetchval(None) means "column not yet vector".
            async def execute(self, *_a: Any, **_kw: Any) -> None:
                return None

            async def fetch(self, *_a: Any, **_kw: Any) -> list[Any]:
                return []

            async def fetchval(self, *_a: Any, **_kw: Any) -> Any:
                return None

            def transaction(self) -> Any:
                return _Tx()

        class _Acq:
            async def __aenter__(self) -> _Conn:
                return _Conn()

            async def __aexit__(self, *_a: Any) -> None:
                return None

        class _Pool:
            def acquire(self) -> _Acq:
                return _Acq()

        return _Pool()

    monkeypatch.setattr("asyncpg.create_pool", fake_create_pool)
    monkeypatch.setenv("PGPASSWORD", "s3cret-from-keyvault")

    provider = PostgresProvider(
        dsn="postgresql://movateadmin:@db.example.internal:5432/movate?sslmode=require",
    )
    await provider.init()

    assert captured["kwargs"].get("password") == "s3cret-from-keyvault", (
        "PGPASSWORD env var must be passed as the password kwarg so "
        "asyncpg doesn't auth with the DSN's empty-string password"
    )


@pytest.mark.unit
async def test_postgres_provider_omits_password_kwarg_when_pgpassword_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When PGPASSWORD is NOT set, don't pass a password kwarg at all —
    that way asyncpg falls back to whatever IS in the DSN (e.g.
    ``postgresql://user:pw@host/db``) without being overridden."""
    captured: dict[str, Any] = {}

    async def fake_create_pool(dsn: str, **kwargs: Any) -> Any:
        captured["kwargs"] = kwargs

        class _Tx:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *_a: Any) -> None:
                return None

        class _Conn:
            # init() now also runs schema migrations (ADR 009): a no-op
            # fetch (no applied rows) lets migration 001 run as execute
            # no-ops, fetchval(None) means "column not yet vector".
            async def execute(self, *_a: Any, **_kw: Any) -> None:
                return None

            async def fetch(self, *_a: Any, **_kw: Any) -> list[Any]:
                return []

            async def fetchval(self, *_a: Any, **_kw: Any) -> Any:
                return None

            def transaction(self) -> Any:
                return _Tx()

        class _Acq:
            async def __aenter__(self) -> _Conn:
                return _Conn()

            async def __aexit__(self, *_a: Any) -> None:
                return None

        class _Pool:
            def acquire(self) -> _Acq:
                return _Acq()

        return _Pool()

    monkeypatch.setattr("asyncpg.create_pool", fake_create_pool)
    monkeypatch.delenv("PGPASSWORD", raising=False)

    provider = PostgresProvider(
        dsn="postgresql://user:dsnpw@host/db",
    )
    await provider.init()

    assert "password" not in captured["kwargs"], (
        "without PGPASSWORD set, leave the password kwarg unspecified "
        "so asyncpg uses whatever's in the DSN"
    )


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


# ---------------------------------------------------------------------------
# Durability warning suppression (2026-05-19)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_durability_warning_emits_only_once_per_process(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``build_storage`` is called repeatedly per ``mdk eval-scorecard``
    sweep (preflight + N agents). Without dedup the durability
    warning lands 10+ times on every sweep. Pin that the warning
    fires once + every subsequent ``build_storage`` call is silent."""
    monkeypatch.delenv("MOVATE_DB_URL", raising=False)
    monkeypatch.setenv("MOVATE_DB", "/tmp/mdk-test.db")

    with caplog.at_level(logging.WARNING, logger="movate.storage"):
        build_storage()
        build_storage()
        build_storage()

    warnings = [r for r in caplog.records if "NOT durable" in r.message]
    assert len(warnings) == 1, (
        f"expected the durability warning to fire ONCE per process, "
        f"got {len(warnings)}: {[r.message for r in warnings]}"
    )


@pytest.mark.unit
def test_mark_cli_mode_drops_warning_to_debug(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """After ``mark_cli_mode()``, the durability warning is logged at
    DEBUG level instead of WARNING. CLI invocations always use SQLite
    locally — the warning targets production containers, so dropping
    it from CLI output stops cluttering ``mdk ...`` runs.

    The message itself is preserved (operators with --verbose / DEBUG
    logging still see it); only the LEVEL changes."""
    monkeypatch.delenv("MOVATE_DB_URL", raising=False)
    monkeypatch.setenv("MOVATE_DB", "/tmp/mdk-test.db")

    mark_cli_mode()
    # Capture at DEBUG so we can confirm the message still emits,
    # just at a lower level.
    with caplog.at_level(logging.DEBUG, logger="movate.storage"):
        build_storage()

    # No record at WARNING level.
    warning_records = [
        r for r in caplog.records if "NOT durable" in r.message and r.levelno >= logging.WARNING
    ]
    assert not warning_records, (
        f"CLI mode must downgrade the warning below WARNING, got: {warning_records}"
    )
    # But the message DID emit at DEBUG so --verbose / debug logging
    # still surfaces it.
    debug_records = [
        r for r in caplog.records if "NOT durable" in r.message and r.levelno == logging.DEBUG
    ]
    assert debug_records, "CLI mode should still emit the message at DEBUG level"


@pytest.mark.unit
def test_warning_still_fires_at_warning_level_without_cli_mode(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Pin the regression guard: without ``mark_cli_mode()``, the
    durability warning STILL fires at WARNING level. Production
    server / container deployments don't call mark_cli_mode, so
    their misconfiguration signal is preserved."""
    monkeypatch.delenv("MOVATE_DB_URL", raising=False)
    monkeypatch.setenv("MOVATE_DB", "/tmp/mdk-test.db")
    # Do NOT call mark_cli_mode — simulate the server / container path.

    with caplog.at_level(logging.WARNING, logger="movate.storage"):
        build_storage()

    warnings = [
        r for r in caplog.records if "NOT durable" in r.message and r.levelno >= logging.WARNING
    ]
    assert warnings, (
        "without CLI mode, the durability warning must still fire at "
        "WARNING level (server / container deployments rely on it)"
    )
