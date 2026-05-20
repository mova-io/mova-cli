"""Tests for ConversationThread storage layer (PR-N).

Foundation for multi-turn agents (Tier 10.5). Covers the four new
StorageProvider methods + RunRecord.thread_id field, against both
in-memory and sqlite backends. Postgres backend exercised by the
existing parametrized conformance suite (storage protocol tests)
when MOVATE_PG_TEST_URL is set.

Coverage:
* save_conversation_thread upsert (idempotent on thread_id)
* get_conversation_thread tenant-scoped (returns None cross-tenant)
* list_conversation_threads (sort by updated_at DESC, agent filter)
* list_runs_for_thread (chronological order, tenant-scoped)
* RunRecord.thread_id flows through save_run + load round-trips
* Default thread_id=None on RunRecord (back-compat for existing rows)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from movate.core.models import (
    ConversationThread,
    JobStatus,
    Metrics,
    RunRecord,
)
from movate.storage.sqlite import SqliteProvider
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Fixtures (parametrize over both backends so the contract is tested twice)
# ---------------------------------------------------------------------------


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


# Per-backend test runners: each calls a `body(storage)` async fn so the
# same assertions run against both backends without duplicating bodies.


async def _run_against_both(body, in_memory, sqlite_provider) -> None:
    await body(in_memory)
    await body(sqlite_provider)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_thread(
    *,
    thread_id: str = "t_abc",
    tenant_id: str = "tenant_1",
    agent: str = "rag-qa",
    title: str = "",
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> ConversationThread:
    now = datetime.now(UTC)
    return ConversationThread(
        thread_id=thread_id,
        tenant_id=tenant_id,
        agent=agent,
        title=title,
        created_at=created_at or now,
        updated_at=updated_at or now,
    )


def _make_run(
    *,
    run_id: str,
    thread_id: str | None = None,
    tenant_id: str = "tenant_1",
    agent: str = "rag-qa",
    created_at: datetime | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        job_id=f"job_{run_id}",
        tenant_id=tenant_id,
        agent=agent,
        agent_version="0.1.0",
        prompt_hash="abc",
        provider="openai/gpt-4o-mini-2024-07-18",
        provider_version="1.0",
        pricing_version="2024-01-01",
        status=JobStatus.SUCCESS,
        input={"question": f"{run_id} input"},
        output={"answer": f"{run_id} output"},
        metrics=Metrics(latency_ms=100, cost_usd=0.001, tokens_in=10, tokens_out=10),
        created_at=created_at or datetime.now(UTC),
        thread_id=thread_id,
    )


# ---------------------------------------------------------------------------
# Save + get
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_save_and_get_thread(
    in_memory: InMemoryStorage, sqlite_provider: SqliteProvider
) -> None:
    """Round-trip: save → get back the same fields."""

    async def body(storage) -> None:
        thread = _make_thread(title="Refund policy questions")
        await storage.save_conversation_thread(thread)
        got = await storage.get_conversation_thread(thread.thread_id, tenant_id="tenant_1")
        assert got is not None
        assert got.thread_id == "t_abc"
        assert got.agent == "rag-qa"
        assert got.title == "Refund policy questions"

    await _run_against_both(body, in_memory, sqlite_provider)


@pytest.mark.unit
async def test_save_thread_is_upsert(
    in_memory: InMemoryStorage, sqlite_provider: SqliteProvider
) -> None:
    """Re-saving the same thread_id refreshes title + updated_at —
    matches the Postgres ON CONFLICT semantics."""

    async def body(storage) -> None:
        original = _make_thread(title="v1 title")
        await storage.save_conversation_thread(original)
        # Re-save with updated title + updated_at.
        later = original.model_copy(
            update={
                "title": "v2 title",
                "updated_at": original.updated_at + timedelta(seconds=10),
            }
        )
        await storage.save_conversation_thread(later)
        got = await storage.get_conversation_thread(original.thread_id, tenant_id="tenant_1")
        assert got is not None
        assert got.title == "v2 title"
        # No duplicate rows.
        threads = await storage.list_conversation_threads(tenant_id="tenant_1")
        assert len(threads) == 1

    await _run_against_both(body, in_memory, sqlite_provider)


@pytest.mark.unit
async def test_get_thread_tenant_scoped(
    in_memory: InMemoryStorage, sqlite_provider: SqliteProvider
) -> None:
    """get_conversation_thread filters by tenant_id at the SQL layer.
    Cross-tenant lookups return None — never leak existence."""

    async def body(storage) -> None:
        thread = _make_thread(tenant_id="tenant_a")
        await storage.save_conversation_thread(thread)
        # Wrong tenant → None.
        got = await storage.get_conversation_thread(thread.thread_id, tenant_id="tenant_b")
        assert got is None
        # Right tenant → finds it.
        got = await storage.get_conversation_thread(thread.thread_id, tenant_id="tenant_a")
        assert got is not None

    await _run_against_both(body, in_memory, sqlite_provider)


@pytest.mark.unit
async def test_get_missing_thread_returns_none(
    in_memory: InMemoryStorage, sqlite_provider: SqliteProvider
) -> None:
    async def body(storage) -> None:
        got = await storage.get_conversation_thread("missing", tenant_id="tenant_1")
        assert got is None

    await _run_against_both(body, in_memory, sqlite_provider)


# ---------------------------------------------------------------------------
# List threads
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_list_threads_sorted_by_updated_at_desc(
    in_memory: InMemoryStorage, sqlite_provider: SqliteProvider
) -> None:
    """Most-recently-updated thread floats to position 0 — clients
    rely on this for the typical 'recent conversations' list."""

    async def body(storage) -> None:
        now = datetime.now(UTC)
        for tid, age in (("t_old", 100), ("t_mid", 50), ("t_new", 10)):
            t = _make_thread(
                thread_id=tid,
                updated_at=now - timedelta(seconds=age),
            )
            await storage.save_conversation_thread(t)
        rows = await storage.list_conversation_threads(tenant_id="tenant_1")
        assert [t.thread_id for t in rows] == ["t_new", "t_mid", "t_old"]

    await _run_against_both(body, in_memory, sqlite_provider)


@pytest.mark.unit
async def test_list_threads_filters_by_agent(
    in_memory: InMemoryStorage, sqlite_provider: SqliteProvider
) -> None:
    """Optional agent= filter scopes the list to one agent — matches
    the typical Chainlit picker which is per-agent."""

    async def body(storage) -> None:
        await storage.save_conversation_thread(_make_thread(thread_id="t_rag", agent="rag-qa"))
        await storage.save_conversation_thread(_make_thread(thread_id="t_faq", agent="faq"))
        rag_threads = await storage.list_conversation_threads(tenant_id="tenant_1", agent="rag-qa")
        assert [t.thread_id for t in rag_threads] == ["t_rag"]
        faq_threads = await storage.list_conversation_threads(tenant_id="tenant_1", agent="faq")
        assert [t.thread_id for t in faq_threads] == ["t_faq"]

    await _run_against_both(body, in_memory, sqlite_provider)


@pytest.mark.unit
async def test_list_threads_tenant_scoped(
    in_memory: InMemoryStorage, sqlite_provider: SqliteProvider
) -> None:
    """Cross-tenant rows excluded from list results."""

    async def body(storage) -> None:
        await storage.save_conversation_thread(_make_thread(thread_id="t_a", tenant_id="tenant_a"))
        await storage.save_conversation_thread(_make_thread(thread_id="t_b", tenant_id="tenant_b"))
        rows = await storage.list_conversation_threads(tenant_id="tenant_a")
        assert [t.thread_id for t in rows] == ["t_a"]

    await _run_against_both(body, in_memory, sqlite_provider)


@pytest.mark.unit
async def test_list_threads_respects_limit(
    in_memory: InMemoryStorage, sqlite_provider: SqliteProvider
) -> None:
    async def body(storage) -> None:
        now = datetime.now(UTC)
        for i in range(10):
            await storage.save_conversation_thread(
                _make_thread(thread_id=f"t_{i}", updated_at=now - timedelta(seconds=i))
            )
        rows = await storage.list_conversation_threads(tenant_id="tenant_1", limit=3)
        assert len(rows) == 3
        # Newest first (t_0 has smallest age delta).
        assert rows[0].thread_id == "t_0"

    await _run_against_both(body, in_memory, sqlite_provider)


# ---------------------------------------------------------------------------
# RunRecord.thread_id round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_run_thread_id_round_trips(
    in_memory: InMemoryStorage, sqlite_provider: SqliteProvider
) -> None:
    """save_run with thread_id set → load_run returns the same id."""

    async def body(storage) -> None:
        run = _make_run(run_id="r1", thread_id="t_abc")
        await storage.save_run(run)
        got = await storage.get_run("r1", tenant_id="tenant_1")
        assert got is not None
        assert got.thread_id == "t_abc"

    await _run_against_both(body, in_memory, sqlite_provider)


@pytest.mark.unit
async def test_run_without_thread_id_defaults_to_none(
    in_memory: InMemoryStorage, sqlite_provider: SqliteProvider
) -> None:
    """Pre-PR-N runs (or new standalone runs) leave thread_id None.
    Back-compat for the millions of rows already in production."""

    async def body(storage) -> None:
        run = _make_run(run_id="r1")  # thread_id defaults to None
        await storage.save_run(run)
        got = await storage.get_run("r1", tenant_id="tenant_1")
        assert got is not None
        assert got.thread_id is None

    await _run_against_both(body, in_memory, sqlite_provider)


# ---------------------------------------------------------------------------
# list_runs_for_thread
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_list_runs_for_thread_chronological(
    in_memory: InMemoryStorage, sqlite_provider: SqliteProvider
) -> None:
    """Runs returned in ASC created_at order — earliest turn first —
    so the runtime renders conversation history straight from the list."""

    async def body(storage) -> None:
        now = datetime.now(UTC)
        for i in range(3):
            run = _make_run(
                run_id=f"r{i}",
                thread_id="t_abc",
                created_at=now + timedelta(seconds=i),
            )
            await storage.save_run(run)
        # A run in a DIFFERENT thread shouldn't appear.
        await storage.save_run(_make_run(run_id="r_other", thread_id="t_xyz"))
        runs = await storage.list_runs_for_thread("t_abc", tenant_id="tenant_1")
        assert [r.run_id for r in runs] == ["r0", "r1", "r2"]

    await _run_against_both(body, in_memory, sqlite_provider)


@pytest.mark.unit
async def test_list_runs_for_thread_tenant_scoped(
    in_memory: InMemoryStorage, sqlite_provider: SqliteProvider
) -> None:
    """Cross-tenant access returns [] — never leaks runs across tenants
    even when a caller guesses the thread_id."""

    async def body(storage) -> None:
        await storage.save_run(_make_run(run_id="r1", thread_id="t_abc", tenant_id="tenant_a"))
        rows = await storage.list_runs_for_thread("t_abc", tenant_id="tenant_b")
        assert rows == []

    await _run_against_both(body, in_memory, sqlite_provider)


@pytest.mark.unit
async def test_list_runs_for_thread_respects_limit(
    in_memory: InMemoryStorage, sqlite_provider: SqliteProvider
) -> None:
    async def body(storage) -> None:
        now = datetime.now(UTC)
        for i in range(10):
            await storage.save_run(
                _make_run(
                    run_id=f"r{i}",
                    thread_id="t_abc",
                    created_at=now + timedelta(seconds=i),
                )
            )
        rows = await storage.list_runs_for_thread("t_abc", tenant_id="tenant_1", limit=5)
        assert len(rows) == 5
        # First 5 by ASC created_at.
        assert [r.run_id for r in rows] == ["r0", "r1", "r2", "r3", "r4"]

    await _run_against_both(body, in_memory, sqlite_provider)
