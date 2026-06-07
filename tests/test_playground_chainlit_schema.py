"""The playground's Chainlit SQLite data layer must have a provisioned schema.

Regression for the deployed bug where the agent picker + chat silently broke:
Chainlit's SQLAlchemyDataLayer issues raw SQL against tables it assumes exist,
but nothing created them on the zero-config SQLite path — so every persist
raised ``no such table: threads`` / ``steps`` and clicking an agent did nothing.
``ensure_chainlit_sqlite_schema`` provisions them; these tests pin that it
creates the tables and that they accept the columns Chainlit actually writes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from movate.playground.state import ensure_chainlit_sqlite_schema


def _tables(db: Path) -> set[str]:
    conn = sqlite3.connect(str(db))
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def test_creates_all_chainlit_tables(tmp_path: Path) -> None:
    db = tmp_path / "threads.db"
    ensure_chainlit_sqlite_schema(db)
    assert {"users", "threads", "steps", "elements", "feedbacks"} <= _tables(db)


def test_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "threads.db"
    ensure_chainlit_sqlite_schema(db)
    ensure_chainlit_sqlite_schema(db)  # second run must not raise (IF NOT EXISTS)
    assert "threads" in _tables(db)


def test_steps_table_accepts_chainlit_columns(tmp_path: Path) -> None:
    """The exact column set Chainlit's SQLAlchemyDataLayer inserts into ``steps``
    (captured from the deployed failure) must be writable — a missing column
    would reproduce the bug at runtime even though the table 'exists'."""
    db = tmp_path / "threads.db"
    ensure_chainlit_sqlite_schema(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            'INSERT INTO threads ("id", "name", "createdAt") VALUES (?, ?, ?)',
            ("t-1", "demo", "2026-06-07T00:00:00Z"),
        )
        conn.execute(
            'INSERT INTO steps ("id", "threadId", "createdAt", "start", "end", "output", '
            '"name", "type", "streaming", "isError", "waitForAnswer", "metadata", '
            '"generation") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            ("s-1", "t-1", "t", "t", "t", "hi", "pick", "tool", 0, 0, 0, "{}", None),
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_chainlit_datalayer_round_trip(tmp_path: Path) -> None:
    """End-to-end against the real Chainlit SQLAlchemyDataLayer: after provisioning,
    a thread upsert must actually persist (proves the schema matches this Chainlit
    version's queries, not just that tables exist)."""
    pytest.importorskip("chainlit")
    from chainlit.data.sql_alchemy import SQLAlchemyDataLayer  # noqa: PLC0415

    db = tmp_path / "threads.db"
    ensure_chainlit_sqlite_schema(db)
    dl = SQLAlchemyDataLayer(conninfo=f"sqlite+aiosqlite:///{db}")
    await dl.update_thread("thread-1", name="hello world")
    fetched = await dl.get_thread("thread-1")
    assert fetched is not None
    assert fetched.get("name") == "hello world"
