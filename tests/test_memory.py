"""Sprint T — `mdk memory` tests.

Three layers:

1. **Store unit** — InMemoryStore round-trips through JSON, persists
   across instances, handles eviction.
2. **build_memory_store** — env var dispatch + MOVATE_MEMORY_FILE.
3. **CLI** — list / get / set / delete / evict / summarise / query
   end-to-end with a temp memory file.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.memory import (
    InMemoryStore,
    MemoryEntry,
    PostgresStore,
    SqliteStore,
    build_memory_store,
)

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# InMemoryStore unit
# ---------------------------------------------------------------------------


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "memory.json"


@pytest.mark.unit
class TestInMemoryStore:
    def test_set_then_get(self, store_path: Path) -> None:
        store = InMemoryStore(_path=store_path)
        asyncio.run(store.set("triage", "k1", {"foo": "bar"}))
        entry = asyncio.run(store.get("triage", "k1"))
        assert entry is not None
        assert entry.value == {"foo": "bar"}

    def test_persists_across_instances(self, store_path: Path) -> None:
        """Second instance reads what first instance wrote."""
        first = InMemoryStore(_path=store_path)
        asyncio.run(first.set("triage", "k1", {"x": 1}))
        # Brand-new instance pointing at the same file
        second = InMemoryStore(_path=store_path)
        entry = asyncio.run(second.get("triage", "k1"))
        assert entry is not None
        assert entry.value == {"x": 1}

    def test_list_sorted_by_created_at(self, store_path: Path) -> None:
        store = InMemoryStore(_path=store_path)
        asyncio.run(store.set("a", "k1", {"i": 1}))
        asyncio.run(store.set("a", "k2", {"i": 2}))
        entries = asyncio.run(store.list("a"))
        assert len(entries) == 2
        # Sorted ascending by created_at
        assert entries[0].created_at <= entries[1].created_at

    def test_delete_returns_true_on_hit_false_on_miss(self, store_path: Path) -> None:
        store = InMemoryStore(_path=store_path)
        asyncio.run(store.set("a", "k1", {"x": 1}))
        assert asyncio.run(store.delete("a", "k1")) is True
        assert asyncio.run(store.delete("a", "k1")) is False

    def test_evict_older_than(self, store_path: Path) -> None:
        store = InMemoryStore(_path=store_path)
        asyncio.run(store.set("a", "k1", {"x": 1}))
        # Cutoff in the future → evict everything
        cutoff = "9999-01-01T00:00:00.000Z"
        n = asyncio.run(store.evict_older_than("a", cutoff))
        assert n == 1
        # Now nothing left
        entries = asyncio.run(store.list("a"))
        assert entries == []

    def test_evict_returns_zero_when_nothing_stale(self, store_path: Path) -> None:
        store = InMemoryStore(_path=store_path)
        asyncio.run(store.set("a", "k1", {"x": 1}))
        n = asyncio.run(store.evict_older_than("a", "1900-01-01T00:00:00.000Z"))
        assert n == 0

    def test_missing_file_yields_empty_store(self, tmp_path: Path) -> None:
        store = InMemoryStore(_path=tmp_path / "does-not-exist.json")
        entries = asyncio.run(store.list("any"))
        assert entries == []

    def test_corrupted_file_yields_empty_store(self, store_path: Path) -> None:
        store_path.write_text("not valid json{")
        store = InMemoryStore(_path=store_path)
        entries = asyncio.run(store.list("any"))
        assert entries == []

    def test_ttl_expired_entry_invisible_to_get(self, store_path: Path) -> None:
        """get() returns None for an entry whose TTL has elapsed."""
        store = InMemoryStore(_path=store_path)
        # Inject an entry with a 1-second TTL whose created_at is far in the past.
        entry = MemoryEntry(
            agent="a",
            key="k",
            value={"x": 1},
            created_at="2020-01-01T00:00:00.000Z",
            ttl_seconds=1,
        )
        with store._lock:
            data = store._load()
            data.setdefault("a", {})["k"] = entry
            store._save(data)
        result = asyncio.run(store.get("a", "k"))
        assert result is None

    def test_ttl_expired_entry_invisible_to_list(self, store_path: Path) -> None:
        """list() silently drops entries whose TTL has elapsed."""
        store = InMemoryStore(_path=store_path)
        entry = MemoryEntry(
            agent="a",
            key="k",
            value={"x": 1},
            created_at="2020-01-01T00:00:00.000Z",
            ttl_seconds=1,
        )
        with store._lock:
            data = store._load()
            data.setdefault("a", {})["k"] = entry
            store._save(data)
        entries = asyncio.run(store.list("a"))
        assert entries == []

    def test_ttl_zero_means_no_expiry(self, store_path: Path) -> None:
        """ttl_seconds=0 means immortal — entry survives any amount of time."""
        store = InMemoryStore(_path=store_path)
        # Entry with ttl_seconds=0 and ancient created_at — should still be alive.
        entry = MemoryEntry(
            agent="a",
            key="k",
            value={"x": 1},
            created_at="2020-01-01T00:00:00.000Z",
            ttl_seconds=0,
        )
        with store._lock:
            data = store._load()
            data.setdefault("a", {})["k"] = entry
            store._save(data)
        result = asyncio.run(store.get("a", "k"))
        assert result is not None
        assert result.value == {"x": 1}

    def test_ttl_live_entry_still_visible(self, store_path: Path) -> None:
        """An entry with a future expiry is still returned."""
        store = InMemoryStore(_path=store_path)
        asyncio.run(store.set("a", "k", {"x": 1}, ttl_seconds=3600))
        result = asyncio.run(store.get("a", "k"))
        assert result is not None


# ---------------------------------------------------------------------------
# SqliteStore — full implementation tests
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(db_path=tmp_path / "memory.db")


@pytest.mark.unit
class TestSqliteStore:
    def test_set_then_get(self, sqlite_store: SqliteStore) -> None:
        asyncio.run(sqlite_store.set("triage", "k1", {"x": 42}))
        entry = asyncio.run(sqlite_store.get("triage", "k1"))
        assert entry is not None
        assert entry.value == {"x": 42}
        assert entry.agent == "triage"
        assert entry.key == "k1"

    def test_get_missing_returns_none(self, sqlite_store: SqliteStore) -> None:
        result = asyncio.run(sqlite_store.get("nobody", "missing"))
        assert result is None

    def test_list_empty_agent(self, sqlite_store: SqliteStore) -> None:
        assert asyncio.run(sqlite_store.list("nobody")) == []

    def test_list_returns_entries_sorted_by_created_at(
        self, sqlite_store: SqliteStore
    ) -> None:
        asyncio.run(sqlite_store.set("a", "k1", {"n": 1}))
        asyncio.run(sqlite_store.set("a", "k2", {"n": 2}))
        asyncio.run(sqlite_store.set("a", "k3", {"n": 3}))
        entries = asyncio.run(sqlite_store.list("a"))
        assert [e.key for e in entries] == ["k1", "k2", "k3"]

    def test_set_upserts_existing_key(self, sqlite_store: SqliteStore) -> None:
        asyncio.run(sqlite_store.set("a", "k", {"v": 1}))
        asyncio.run(sqlite_store.set("a", "k", {"v": 2}))
        entry = asyncio.run(sqlite_store.get("a", "k"))
        assert entry is not None
        assert entry.value == {"v": 2}
        # Only one entry — not two.
        all_entries = asyncio.run(sqlite_store.list("a"))
        assert len(all_entries) == 1

    def test_delete_existing_returns_true(self, sqlite_store: SqliteStore) -> None:
        asyncio.run(sqlite_store.set("a", "k", {"v": 1}))
        assert asyncio.run(sqlite_store.delete("a", "k")) is True
        assert asyncio.run(sqlite_store.get("a", "k")) is None

    def test_delete_missing_returns_false(self, sqlite_store: SqliteStore) -> None:
        assert asyncio.run(sqlite_store.delete("nobody", "missing")) is False

    def test_evict_older_than_removes_stale(self, sqlite_store: SqliteStore) -> None:
        asyncio.run(sqlite_store.set("a", "old", {"v": "old"}))
        # Timestamp guaranteed later than the entry above.
        import time  # noqa: PLC0415

        time.sleep(0.01)
        from movate.memory.store import _now_iso  # noqa: PLC0415

        cutoff = _now_iso()
        asyncio.run(sqlite_store.set("a", "new", {"v": "new"}))
        removed = asyncio.run(sqlite_store.evict_older_than("a", cutoff))
        assert removed == 1
        assert asyncio.run(sqlite_store.get("a", "old")) is None
        assert asyncio.run(sqlite_store.get("a", "new")) is not None

    def test_evict_nothing_to_remove(self, sqlite_store: SqliteStore) -> None:
        asyncio.run(sqlite_store.set("a", "k", {"v": 1}))
        # Cutoff in the past — nothing should be removed.
        removed = asyncio.run(sqlite_store.evict_older_than("a", "2020-01-01T00:00:00.000Z"))
        assert removed == 0

    def test_persists_across_instances(self, tmp_path: Path) -> None:
        db = tmp_path / "shared.db"
        first = SqliteStore(db_path=db)
        asyncio.run(first.set("agent", "k", {"hello": "world"}))
        second = SqliteStore(db_path=db)
        entry = asyncio.run(second.get("agent", "k"))
        assert entry is not None
        assert entry.value == {"hello": "world"}

    def test_ttl_seconds_stored_and_retrieved(self, sqlite_store: SqliteStore) -> None:
        asyncio.run(sqlite_store.set("a", "k", {"v": 1}, ttl_seconds=3600))
        entry = asyncio.run(sqlite_store.get("a", "k"))
        assert entry is not None
        assert entry.ttl_seconds == 3600

    def test_ttl_expired_entry_invisible_to_get(self, sqlite_store: SqliteStore) -> None:
        """get() returns None for an entry whose TTL has elapsed."""

        async def _insert_expired() -> None:
            conn = await sqlite_store._conn()
            try:
                await conn.execute(
                    "INSERT OR REPLACE INTO memory_entries "
                    "(agent, key, value_json, created_at, ttl_seconds) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("a", "k", '{"x": 1}', "2020-01-01T00:00:00.000Z", 1),
                )
                await conn.commit()
            finally:
                await conn.close()

        asyncio.run(_insert_expired())
        result = asyncio.run(sqlite_store.get("a", "k"))
        assert result is None

    def test_ttl_expired_entry_invisible_to_list(self, sqlite_store: SqliteStore) -> None:
        """list() silently drops entries whose TTL has elapsed."""

        async def _insert_expired() -> None:
            conn = await sqlite_store._conn()
            try:
                await conn.execute(
                    "INSERT OR REPLACE INTO memory_entries "
                    "(agent, key, value_json, created_at, ttl_seconds) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("a", "k", '{"x": 1}', "2020-01-01T00:00:00.000Z", 1),
                )
                await conn.commit()
            finally:
                await conn.close()

        asyncio.run(_insert_expired())
        entries = asyncio.run(sqlite_store.list("a"))
        assert entries == []

    def test_ttl_zero_means_no_expiry(self, sqlite_store: SqliteStore) -> None:
        """ttl_seconds=0 means immortal regardless of created_at."""

        async def _insert_immortal() -> None:
            conn = await sqlite_store._conn()
            try:
                await conn.execute(
                    "INSERT OR REPLACE INTO memory_entries "
                    "(agent, key, value_json, created_at, ttl_seconds) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("a", "k", '{"x": 1}', "2020-01-01T00:00:00.000Z", 0),
                )
                await conn.commit()
            finally:
                await conn.close()

        asyncio.run(_insert_immortal())
        result = asyncio.run(sqlite_store.get("a", "k"))
        assert result is not None
        assert result.value == {"x": 1}

    def test_ttl_live_entry_still_visible(self, sqlite_store: SqliteStore) -> None:
        """An entry with a future expiry is still returned."""
        asyncio.run(sqlite_store.set("a", "k", {"x": 1}, ttl_seconds=3600))
        result = asyncio.run(sqlite_store.get("a", "k"))
        assert result is not None

    def test_agent_isolation(self, sqlite_store: SqliteStore) -> None:
        asyncio.run(sqlite_store.set("agent-a", "k", {"who": "a"}))
        asyncio.run(sqlite_store.set("agent-b", "k", {"who": "b"}))
        a_entry = asyncio.run(sqlite_store.get("agent-a", "k"))
        b_entry = asyncio.run(sqlite_store.get("agent-b", "k"))
        assert a_entry is not None and a_entry.value["who"] == "a"
        assert b_entry is not None and b_entry.value["who"] == "b"
        assert asyncio.run(sqlite_store.list("agent-a")) == [a_entry]


# ---------------------------------------------------------------------------
# PostgresStore — mocked asyncpg, no real DB connection required
# ---------------------------------------------------------------------------


def _make_pg_conn(rows: list[dict] | None = None, fetchrow_result: dict | None = None,
                  execute_tag: str = "DELETE 0") -> MagicMock:
    """Return a mock asyncpg connection with async fetch/fetchrow/execute/close."""
    conn = MagicMock()
    # _ensure_schema calls conn.execute (CREATE TABLE / INDEX)
    conn.execute = AsyncMock(return_value=execute_tag)
    conn.fetch = AsyncMock(return_value=[_make_record(r) for r in (rows or [])])
    conn.fetchrow = AsyncMock(return_value=_make_record(fetchrow_result) if fetchrow_result else None)
    conn.fetchval = AsyncMock(return_value=None)
    conn.close = AsyncMock()
    return conn


def _make_record(data: dict | None) -> MagicMock:
    """Minimal asyncpg.Record stand-in that supports dict-style access."""
    if data is None:
        return None  # type: ignore[return-value]
    rec = MagicMock()
    rec.__getitem__ = lambda self, key: data[key]
    return rec


@pytest.mark.unit
class TestPostgresStore:
    """Unit tests for PostgresStore — asyncpg is fully mocked; no DB needed."""

    def test_postgres_set_and_get(self) -> None:
        """set() stores a value; get() retrieves it with correct fields."""
        store = PostgresStore(dsn="postgresql://test/fake")

        set_conn = _make_pg_conn(execute_tag="INSERT 0 1")
        get_conn = _make_pg_conn(
            fetchrow_result={
                "agent": "triage",
                "key": "k1",
                "value_json": '{"x": 42}',
                "created_at": "2024-01-01T00:00:00.000Z",
                "ttl_seconds": 0,
            }
        )

        with patch("asyncpg.connect", side_effect=[set_conn, get_conn]):
            asyncio.run(store.set("triage", "k1", {"x": 42}))
            entry = asyncio.run(store.get("triage", "k1"))

        assert entry is not None
        assert entry.agent == "triage"
        assert entry.key == "k1"
        assert entry.value == {"x": 42}
        assert entry.ttl_seconds == 0

    def test_postgres_delete(self) -> None:
        """delete() returns True after removing a row."""
        store = PostgresStore(dsn="postgresql://test/fake")

        set_conn = _make_pg_conn(execute_tag="INSERT 0 1")
        del_conn = _make_pg_conn(execute_tag="DELETE 1")
        get_conn = _make_pg_conn(fetchrow_result=None)

        with patch("asyncpg.connect", side_effect=[set_conn, del_conn, get_conn]):
            asyncio.run(store.set("a", "k1", {"v": 1}))
            deleted = asyncio.run(store.delete("a", "k1"))
            result = asyncio.run(store.get("a", "k1"))

        assert deleted is True
        assert result is None

    def test_postgres_delete_missing_returns_false(self) -> None:
        """delete() returns False when the key does not exist."""
        store = PostgresStore(dsn="postgresql://test/fake")
        conn = _make_pg_conn(execute_tag="DELETE 0")

        with patch("asyncpg.connect", return_value=conn):
            result = asyncio.run(store.delete("nobody", "missing"))

        assert result is False

    def test_postgres_list_empty(self) -> None:
        """list() returns [] for an agent with no entries."""
        store = PostgresStore(dsn="postgresql://test/fake")
        conn = _make_pg_conn(rows=[])

        with patch("asyncpg.connect", return_value=conn):
            entries = asyncio.run(store.list("unknown-agent"))

        assert entries == []

    def test_postgres_list_returns_entries(self) -> None:
        """list() deserialises rows into MemoryEntry objects."""
        store = PostgresStore(dsn="postgresql://test/fake")
        conn = _make_pg_conn(rows=[
            {
                "agent": "a",
                "key": "k1",
                "value_json": '{"n": 1}',
                "created_at": "2024-01-01T00:00:00.000Z",
                "ttl_seconds": 0,
            },
            {
                "agent": "a",
                "key": "k2",
                "value_json": '{"n": 2}',
                "created_at": "2024-01-02T00:00:00.000Z",
                "ttl_seconds": 0,
            },
        ])

        with patch("asyncpg.connect", return_value=conn):
            entries = asyncio.run(store.list("a"))

        assert len(entries) == 2
        assert entries[0].key == "k1"
        assert entries[1].key == "k2"

    def test_postgres_evict_older_than(self) -> None:
        """evict_older_than() returns the count of deleted rows."""
        store = PostgresStore(dsn="postgresql://test/fake")
        conn = _make_pg_conn(execute_tag="DELETE 3")

        with patch("asyncpg.connect", return_value=conn):
            count = asyncio.run(store.evict_older_than("a", "9999-01-01T00:00:00.000Z"))

        assert count == 3

    def test_postgres_evict_returns_zero_on_no_match(self) -> None:
        """evict_older_than() returns 0 when nothing is deleted."""
        store = PostgresStore(dsn="postgresql://test/fake")
        conn = _make_pg_conn(execute_tag="DELETE 0")

        with patch("asyncpg.connect", return_value=conn):
            count = asyncio.run(store.evict_older_than("a", "1900-01-01T00:00:00.000Z"))

        assert count == 0

    def test_postgres_dsn_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Constructor reads MOVATE_PG_URL when dsn arg is omitted."""
        monkeypatch.setenv("MOVATE_PG_URL", "postgresql://env-host/db")
        store = PostgresStore()
        assert store._dsn == "postgresql://env-host/db"

    def test_postgres_dsn_arg_takes_precedence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit dsn= arg overrides MOVATE_PG_URL env var."""
        monkeypatch.setenv("MOVATE_PG_URL", "postgresql://env-host/db")
        store = PostgresStore(dsn="postgresql://explicit/db")
        assert store._dsn == "postgresql://explicit/db"


# ---------------------------------------------------------------------------
# build_memory_store — postgres case
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildMemoryStorePostgres:
    def test_postgres_selected_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MOVATE_MEMORY_BACKEND", "postgres")
        monkeypatch.setenv("MOVATE_PG_URL", "postgresql://localhost/test")
        store = build_memory_store()
        assert isinstance(store, PostgresStore)

    def test_postgres_missing_url_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MOVATE_MEMORY_BACKEND", "postgres")
        monkeypatch.delenv("MOVATE_PG_URL", raising=False)
        with pytest.raises(RuntimeError, match="MOVATE_PG_URL"):
            build_memory_store()


# ---------------------------------------------------------------------------
# build_memory_store
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildMemoryStore:
    def test_default_is_in_memory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MOVATE_MEMORY_BACKEND", raising=False)
        store = build_memory_store()
        assert isinstance(store, InMemoryStore)

    def test_sqlite_selected_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MOVATE_MEMORY_BACKEND", "sqlite")
        store = build_memory_store()
        assert isinstance(store, SqliteStore)

    def test_memory_file_env_var_honored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        custom = tmp_path / "custom.json"
        monkeypatch.setenv("MOVATE_MEMORY_FILE", str(custom))
        monkeypatch.delenv("MOVATE_MEMORY_BACKEND", raising=False)
        store = build_memory_store()
        assert isinstance(store, InMemoryStore)
        asyncio.run(store.set("a", "k", {"x": 1}))
        assert custom.is_file()


# ---------------------------------------------------------------------------
# MemoryEntry
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_memory_entry_is_frozen() -> None:
    e = MemoryEntry(agent="a", key="k", value={"x": 1})
    with pytest.raises(Exception):
        e.key = "different"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CLI: end-to-end with isolated MOVATE_MEMORY_FILE
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate memory state to a per-test JSON file."""
    target = tmp_path / "memory.json"
    monkeypatch.setenv("MOVATE_MEMORY_FILE", str(target))
    monkeypatch.delenv("MOVATE_MEMORY_BACKEND", raising=False)
    return target


@pytest.mark.unit
def test_cli_memory_set_then_list(memory_env: Path) -> None:
    set_result = runner.invoke(app, ["memory", "set", "triage", "k1", '{"foo": "bar"}'])
    assert set_result.exit_code == 0, set_result.stdout + set_result.stderr
    list_result = runner.invoke(app, ["memory", "list", "triage"])
    assert list_result.exit_code == 0
    assert "k1" in list_result.stdout
    assert "foo" in list_result.stdout


@pytest.mark.unit
def test_cli_memory_get_prints_value(memory_env: Path) -> None:
    runner.invoke(app, ["memory", "set", "a", "k1", '{"x": 1}'])
    result = runner.invoke(app, ["memory", "get", "a", "k1"])
    assert result.exit_code == 0
    # Plain stdout JSON
    data = json.loads(result.stdout)
    assert data == {"x": 1}


@pytest.mark.unit
def test_cli_memory_get_missing_exits_1(memory_env: Path) -> None:
    result = runner.invoke(app, ["memory", "get", "a", "ghost"])
    assert result.exit_code == 1


@pytest.mark.unit
def test_cli_memory_set_bad_json_exits_2(memory_env: Path) -> None:
    result = runner.invoke(app, ["memory", "set", "a", "k1", "not-json"])
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_memory_set_non_object_value_exits_2(memory_env: Path) -> None:
    result = runner.invoke(app, ["memory", "set", "a", "k1", '"just a string"'])
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_memory_delete_dry_run_exits_1(memory_env: Path) -> None:
    runner.invoke(app, ["memory", "set", "a", "k1", '{"x": 1}'])
    result = runner.invoke(app, ["memory", "delete", "a", "k1"])
    # Dry-run → exit 1, file still has the entry
    assert result.exit_code == 1
    list_result = runner.invoke(app, ["memory", "list", "a", "--json"])
    assert "k1" in list_result.stdout


@pytest.mark.unit
def test_cli_memory_delete_force_removes(memory_env: Path) -> None:
    runner.invoke(app, ["memory", "set", "a", "k1", '{"x": 1}'])
    result = runner.invoke(app, ["memory", "delete", "a", "k1", "--force"])
    assert result.exit_code == 0
    list_result = runner.invoke(app, ["memory", "list", "a", "--json"])
    data = json.loads(list_result.stdout)
    assert data == []


@pytest.mark.unit
def test_cli_memory_evict_dry_run_counts(memory_env: Path) -> None:
    runner.invoke(app, ["memory", "set", "a", "k1", '{"x": 1}'])
    result = runner.invoke(app, ["memory", "evict", "a", "--before-days", "0"])
    assert result.exit_code == 2  # 0 days = "specify a real threshold"


@pytest.mark.unit
def test_cli_memory_evict_force_removes_old(memory_env: Path) -> None:
    runner.invoke(app, ["memory", "set", "a", "k1", '{"x": 1}'])
    # Use a future cutoff via --before so the entry is evicted.
    result = runner.invoke(
        app,
        [
            "memory",
            "evict",
            "a",
            "--before",
            "9999-01-01T00:00:00.000Z",
            "--force",
        ],
    )
    assert result.exit_code == 0
    list_result = runner.invoke(app, ["memory", "list", "a", "--json"])
    data = json.loads(list_result.stdout)
    assert data == []


@pytest.mark.unit
def test_cli_memory_summarise_renders_counts(memory_env: Path) -> None:
    runner.invoke(app, ["memory", "set", "a", "k1", '{"x": 1}'])
    runner.invoke(app, ["memory", "set", "a", "k2", '{"y": 2}'])
    result = runner.invoke(app, ["memory", "summarise", "a"])
    assert result.exit_code == 0
    assert "Entries" in result.stdout
    assert "2" in result.stdout


@pytest.mark.unit
def test_cli_memory_query_matches_substring(memory_env: Path) -> None:
    runner.invoke(app, ["memory", "set", "a", "k1", '{"reason": "refund request"}'])
    runner.invoke(app, ["memory", "set", "a", "k2", '{"reason": "new feature"}'])
    result = runner.invoke(app, ["memory", "query", "a", "refund"])
    assert result.exit_code == 0
    assert "k1" in result.stdout
    # Non-matching key shouldn't appear
    assert "k2" not in result.stdout


@pytest.mark.unit
def test_cli_memory_list_empty_prints_hint(memory_env: Path) -> None:
    result = runner.invoke(app, ["memory", "list", "nobody"])
    assert result.exit_code == 0
    assert "no memory entries" in result.stdout.lower()
