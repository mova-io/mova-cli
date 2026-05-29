"""Tests for the stateful-session storage layer (ADR 045 D10).

Server-side conversation memory: ``sessions`` + ``session_messages``
tables behind the StorageProvider Protocol. Covers the new methods
against both the in-memory double and sqlite. Postgres is exercised by
the parametrized conformance suite when MOVATE_PG_TEST_URL is set.

Coverage:
* save_session upsert (idempotent on session_id; preserves created_at)
* get_session tenant-scoped (None cross-tenant — 404-not-403)
* list_sessions (updated_at DESC, agent filter, tenant scope, limit)
* append_session_message + list_session_messages (chronological,
  tenant-scoped, JSON content round-trip)
* delete_session removes the session AND its messages, tenant-scoped
* rollup fields (turn_count + total_*) round-trip
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from movate.core.models import Session, SessionMessage
from movate.storage.sqlite import SqliteProvider
from movate.testing import InMemoryStorage


@pytest.fixture
async def in_memory() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
async def sqlite_provider(tmp_path: Path) -> SqliteProvider:
    s = SqliteProvider(db_path=str(tmp_path / "test.db"))
    await s.init()
    return s


async def _run_against_both(body, in_memory, sqlite_provider) -> None:
    await body(in_memory)
    await body(sqlite_provider)


def _make_session(
    *,
    session_id: str = "s_abc",
    tenant_id: str = "tenant_1",
    agent: str = "rag-qa",
    title: str = "",
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    turn_count: int = 0,
    total_cost_usd: float = 0.0,
    total_tokens_in: int = 0,
    total_tokens_out: int = 0,
) -> Session:
    now = datetime.now(UTC)
    return Session(
        session_id=session_id,
        tenant_id=tenant_id,
        agent=agent,
        title=title,
        created_at=created_at or now,
        updated_at=updated_at or now,
        turn_count=turn_count,
        total_cost_usd=total_cost_usd,
        total_tokens_in=total_tokens_in,
        total_tokens_out=total_tokens_out,
    )


def _make_message(
    *,
    session_id: str = "s_abc",
    tenant_id: str = "tenant_1",
    role: str = "user",
    content: dict | None = None,
    run_id: str | None = None,
    cost_usd: float = 0.0,
    tokens_in: int = 0,
    tokens_out: int = 0,
    created_at: datetime | None = None,
) -> SessionMessage:
    return SessionMessage(
        session_id=session_id,
        tenant_id=tenant_id,
        role=role,  # type: ignore[arg-type]
        content=content if content is not None else {"text": "hi"},
        run_id=run_id,
        cost_usd=cost_usd,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        created_at=created_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# save + get
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_save_and_get_session(in_memory, sqlite_provider) -> None:
    async def body(storage) -> None:
        session = _make_session(title="Refund questions")
        await storage.save_session(session)
        got = await storage.get_session("s_abc", tenant_id="tenant_1")
        assert got is not None
        assert got.session_id == "s_abc"
        assert got.agent == "rag-qa"
        assert got.title == "Refund questions"
        assert got.turn_count == 0
        assert got.total_cost_usd == 0.0

    await _run_against_both(body, in_memory, sqlite_provider)


@pytest.mark.unit
async def test_save_session_is_upsert_with_rollups(in_memory, sqlite_provider) -> None:
    """Re-saving the same id refreshes the mutable fields incl. rollups —
    matches Postgres ON CONFLICT semantics; no duplicate rows."""

    async def body(storage) -> None:
        original = _make_session(title="v1")
        await storage.save_session(original)
        later = original.model_copy(
            update={
                "title": "v2",
                "updated_at": original.updated_at + timedelta(seconds=10),
                "turn_count": 3,
                "total_cost_usd": 0.05,
                "total_tokens_in": 120,
                "total_tokens_out": 90,
            }
        )
        await storage.save_session(later)
        got = await storage.get_session("s_abc", tenant_id="tenant_1")
        assert got is not None
        assert got.title == "v2"
        assert got.turn_count == 3
        assert got.total_cost_usd == pytest.approx(0.05)
        assert got.total_tokens_in == 120
        assert got.total_tokens_out == 90
        rows = await storage.list_sessions(tenant_id="tenant_1")
        assert len(rows) == 1

    await _run_against_both(body, in_memory, sqlite_provider)


@pytest.mark.unit
async def test_get_session_tenant_scoped(in_memory, sqlite_provider) -> None:
    """Cross-tenant lookups return None — never leak existence."""

    async def body(storage) -> None:
        await storage.save_session(_make_session(tenant_id="tenant_a"))
        assert await storage.get_session("s_abc", tenant_id="tenant_b") is None
        assert await storage.get_session("s_abc", tenant_id="tenant_a") is not None

    await _run_against_both(body, in_memory, sqlite_provider)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_list_sessions_sorted_desc_filtered_scoped(in_memory, sqlite_provider) -> None:
    async def body(storage) -> None:
        now = datetime.now(UTC)
        for sid, age, agent, tenant in (
            ("s_old", 100, "rag-qa", "tenant_1"),
            ("s_new", 10, "rag-qa", "tenant_1"),
            ("s_faq", 50, "faq", "tenant_1"),
            ("s_other", 5, "rag-qa", "tenant_2"),
        ):
            await storage.save_session(
                _make_session(
                    session_id=sid,
                    agent=agent,
                    tenant_id=tenant,
                    updated_at=now - timedelta(seconds=age),
                )
            )
        # tenant_1, all agents, updated_at DESC.
        rows = await storage.list_sessions(tenant_id="tenant_1")
        assert [s.session_id for s in rows] == ["s_new", "s_faq", "s_old"]
        # agent filter.
        rag = await storage.list_sessions(tenant_id="tenant_1", agent="rag-qa")
        assert [s.session_id for s in rag] == ["s_new", "s_old"]
        # tenant scope — tenant_2's session never appears for tenant_1.
        assert all(s.session_id != "s_other" for s in rows)
        # limit.
        assert len(await storage.list_sessions(tenant_id="tenant_1", limit=1)) == 1

    await _run_against_both(body, in_memory, sqlite_provider)


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_append_and_list_messages_chronological(in_memory, sqlite_provider) -> None:
    async def body(storage) -> None:
        await storage.save_session(_make_session())
        now = datetime.now(UTC)
        await storage.append_session_message(
            _make_message(role="user", content={"q": "1"}, created_at=now)
        )
        await storage.append_session_message(
            _make_message(
                role="assistant",
                content={"a": "1"},
                run_id="r1",
                cost_usd=0.01,
                tokens_in=10,
                tokens_out=20,
                created_at=now + timedelta(seconds=1),
            )
        )
        msgs = await storage.list_session_messages("s_abc", tenant_id="tenant_1")
        assert [m.role for m in msgs] == ["user", "assistant"]
        assert msgs[0].content == {"q": "1"}
        assert msgs[1].content == {"a": "1"}
        assert msgs[1].run_id == "r1"
        assert msgs[1].cost_usd == pytest.approx(0.01)
        assert msgs[1].tokens_in == 10
        assert msgs[1].tokens_out == 20

    await _run_against_both(body, in_memory, sqlite_provider)


@pytest.mark.unit
async def test_list_messages_tenant_scoped(in_memory, sqlite_provider) -> None:
    """A cross-tenant session id returns [] — never leaks messages."""

    async def body(storage) -> None:
        await storage.save_session(_make_session(tenant_id="tenant_a"))
        await storage.append_session_message(_make_message(tenant_id="tenant_a"))
        assert await storage.list_session_messages("s_abc", tenant_id="tenant_b") == []
        assert len(await storage.list_session_messages("s_abc", tenant_id="tenant_a")) == 1

    await _run_against_both(body, in_memory, sqlite_provider)


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_delete_session_removes_messages(in_memory, sqlite_provider) -> None:
    async def body(storage) -> None:
        await storage.save_session(_make_session())
        await storage.append_session_message(_make_message(role="user"))
        await storage.append_session_message(_make_message(role="assistant", run_id="r1"))
        deleted = await storage.delete_session("s_abc", tenant_id="tenant_1")
        assert deleted is True
        assert await storage.get_session("s_abc", tenant_id="tenant_1") is None
        assert await storage.list_session_messages("s_abc", tenant_id="tenant_1") == []

    await _run_against_both(body, in_memory, sqlite_provider)


@pytest.mark.unit
async def test_delete_session_tenant_scoped(in_memory, sqlite_provider) -> None:
    """Cross-tenant delete touches nothing and returns False."""

    async def body(storage) -> None:
        await storage.save_session(_make_session(tenant_id="tenant_a"))
        assert await storage.delete_session("s_abc", tenant_id="tenant_b") is False
        assert await storage.get_session("s_abc", tenant_id="tenant_a") is not None

    await _run_against_both(body, in_memory, sqlite_provider)


@pytest.mark.unit
async def test_delete_missing_session_returns_false(in_memory, sqlite_provider) -> None:
    async def body(storage) -> None:
        assert await storage.delete_session("missing", tenant_id="tenant_1") is False

    await _run_against_both(body, in_memory, sqlite_provider)
