"""Tests for char-budget truncation of injected thread history (PR-U).

PR-R injects up to 20 prior turns into the input dict. PR-U adds
a char budget so a thread with verbose turns doesn't blow past
the model's context window. Drops OLDEST turns first to preserve
the most recent (highest-signal) context.

Coverage:
* Pure helper ``_apply_history_char_budget``:
  - Empty input → unchanged
  - Total fits under budget → unchanged
  - Total exceeds budget → drops oldest turns first
  - Single huge turn → kept (better to overflow than empty)
  - Newest-N-fit boundary behavior
* End-to-end via POST /api/v1/threads/{id}/messages:
  - Many small prior runs → all kept
  - Few large prior runs → oldest dropped
  - Caller-supplied conversation_history bypasses the budget
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.core.models import JobStatus, Metrics, RunRecord
from movate.runtime import build_app
from movate.runtime.app import _apply_history_char_budget
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_budget_empty_input_returns_empty() -> None:
    assert _apply_history_char_budget([]) == []


@pytest.mark.unit
def test_budget_under_total_returns_unchanged() -> None:
    """Total bytes fit under budget → no truncation, list is a copy
    (we don't mutate the caller's list)."""
    turns = [{"input": {"q": "x"}, "output": {"a": "y"}} for _ in range(5)]
    out = _apply_history_char_budget(turns, budget=100_000)
    assert out == turns
    # Defensive copy: mutating the result doesn't touch the input.
    out.clear()
    assert len(turns) == 5


@pytest.mark.unit
def test_budget_drops_oldest_turns_first() -> None:
    """When total exceeds budget, oldest turns drop first so the
    most recent context survives."""
    # Each turn is ~100 chars after json.dumps. 5 turns = ~500 chars.
    # Budget of 250 chars should keep only the last 2 (most recent).
    turns = [{"input": {"q": f"question {i}"}, "output": {"a": f"answer {i}"}} for i in range(5)]
    out = _apply_history_char_budget(turns, budget=110)
    # Most recent turn (i=4) survives.
    assert any(t["input"]["q"] == "question 4" for t in out)
    # Oldest turn (i=0) dropped.
    assert all(t["input"]["q"] != "question 0" for t in out)


@pytest.mark.unit
def test_budget_keeps_single_overflow_turn() -> None:
    """A single turn that alone exceeds the budget is kept anyway —
    better to overflow than send empty history. Operators with this
    pathological case should pre-summarize via caller-supplied path."""
    huge_turn = {"input": {"q": "x" * 50_000}, "output": {"a": "y"}}
    out = _apply_history_char_budget([huge_turn], budget=1000)
    assert out == [huge_turn]


@pytest.mark.unit
def test_budget_preserves_chronological_order_of_survivors() -> None:
    """The kept turns stay in their original (ASC) order — most-recent
    last in the list, matching list_runs_for_thread."""
    turns = [{"input": {"q": f"q{i}"}, "output": {"a": f"a{i}"}} for i in range(5)]
    out = _apply_history_char_budget(turns, budget=70)
    # Whatever survives must be a contiguous TAIL of the input.
    questions = [t["input"]["q"] for t in out]
    assert questions == sorted(questions, key=lambda q: int(q[1:]))
    # And the LAST one is always present (most recent).
    assert questions[-1] == "q4"


# ---------------------------------------------------------------------------
# End-to-end via POST messages
# ---------------------------------------------------------------------------


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def client(storage: InMemoryStorage) -> TestClient:
    return TestClient(build_app(storage))


@pytest.fixture
async def auth_header(storage: InMemoryStorage) -> dict[str, str]:
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="history-budget-tests")
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


def _seed_huge_run(
    storage: InMemoryStorage,
    *,
    thread_id: str,
    tenant_id: str,
    run_id: str,
    char_size: int,
    created_at: datetime,
) -> None:
    """Helper: seed a RunRecord with a payload of approximately
    ``char_size`` bytes — for budget-overflow scenarios."""
    big_str = "x" * char_size
    run = RunRecord(
        run_id=run_id,
        job_id=f"job_{run_id}",
        tenant_id=tenant_id,
        agent="rag-qa",
        agent_version="0.1.0",
        prompt_hash="h",
        provider="openai/gpt-4o-mini-2024-07-18",
        provider_version="1.0",
        pricing_version="2024-01-01",
        status=JobStatus.SUCCESS,
        input={"q": big_str},
        output={"a": "ok"},
        metrics=Metrics(latency_ms=50, cost_usd=0.001, tokens_in=5, tokens_out=5),
        created_at=created_at,
        thread_id=thread_id,
    )
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(storage.save_run(run))
    finally:
        loop.close()


@pytest.mark.integration
def test_messages_endpoint_drops_oldest_when_over_budget(
    client: TestClient,
    auth_header: dict[str, str],
    storage: InMemoryStorage,
) -> None:
    """Three huge prior turns (each ~30k chars) → budget=40k means
    only the most recent survives."""
    r = client.post("/api/v1/threads", json={"agent": "rag-qa"}, headers=auth_header)
    thread_id = r.json()["thread_id"]
    tenant_id = storage.conversation_threads[0].tenant_id
    now = datetime.now(UTC)
    for i, ts in enumerate([now, now + timedelta(seconds=10), now + timedelta(seconds=20)]):
        _seed_huge_run(
            storage,
            thread_id=thread_id,
            tenant_id=tenant_id,
            run_id=f"r{i}",
            char_size=30_000,
            created_at=ts,
        )
    client.post(
        f"/api/v1/threads/{thread_id}/messages",
        json={"input": {"q": "new"}},
        headers=auth_header,
    )
    # Inspect what made it into the queued job.
    job_input = storage.jobs[-1].input
    history = job_input["conversation_history"]
    # 3 * 30k = 90k chars. Budget = 40k. Only the most recent
    # (r2, ~30k chars) should fit.
    assert len(history) == 1
    # And it's the MOST RECENT turn (r2's content).
    assert history[0]["input"]["q"].startswith("x")
    # Sanity: the kept turn matches the LAST one we seeded.
    assert len(history[0]["input"]["q"]) == 30_000


@pytest.mark.integration
def test_messages_endpoint_keeps_all_when_under_budget(
    client: TestClient,
    auth_header: dict[str, str],
    storage: InMemoryStorage,
) -> None:
    """Three tiny prior turns under the budget → all kept."""
    r = client.post("/api/v1/threads", json={"agent": "rag-qa"}, headers=auth_header)
    thread_id = r.json()["thread_id"]
    tenant_id = storage.conversation_threads[0].tenant_id
    now = datetime.now(UTC)
    for i, ts in enumerate([now, now + timedelta(seconds=1), now + timedelta(seconds=2)]):
        _seed_huge_run(
            storage,
            thread_id=thread_id,
            tenant_id=tenant_id,
            run_id=f"r{i}",
            char_size=100,  # tiny
            created_at=ts,
        )
    client.post(
        f"/api/v1/threads/{thread_id}/messages",
        json={"input": {"q": "new"}},
        headers=auth_header,
    )
    history = storage.jobs[-1].input["conversation_history"]
    assert len(history) == 3


@pytest.mark.integration
def test_messages_endpoint_caller_supplied_history_bypasses_budget(
    client: TestClient,
    auth_header: dict[str, str],
    storage: InMemoryStorage,
) -> None:
    """When the caller supplies ``conversation_history``, the
    endpoint preserves it verbatim — no budget enforcement on
    operator-supplied values (they own the policy)."""
    r = client.post("/api/v1/threads", json={"agent": "rag-qa"}, headers=auth_header)
    thread_id = r.json()["thread_id"]
    # Seed a real prior run that would normally be injected.
    _seed_huge_run(
        storage,
        thread_id=thread_id,
        tenant_id=storage.conversation_threads[0].tenant_id,
        run_id="r1",
        char_size=10,
        created_at=datetime.now(UTC),
    )
    # Operator supplies a custom (potentially huge) history block —
    # the budget MUST NOT touch it.
    custom = [{"input": {"q": "x" * 60_000}, "output": {"a": "y"}}]
    client.post(
        f"/api/v1/threads/{thread_id}/messages",
        json={
            "input": {"q": "new", "conversation_history": custom},
        },
        headers=auth_header,
    )
    history = storage.jobs[-1].input["conversation_history"]
    assert history == custom
