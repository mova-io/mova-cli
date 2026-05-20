"""Tests for PR-R — auto-inject prior turns into threaded message input.

POST /api/v1/threads/{id}/messages now fetches prior runs and adds
them to the job's input under ``conversation_history`` so agent
prompts can render them via ``{{ input.conversation_history }}``.

Coverage:
* First message in a new thread → empty history list
* Second message after one prior turn → history has 1 entry with
  prior input + output
* Multi-turn thread → history in chronological order
* Caller-supplied ``conversation_history`` is preserved (operator
  override path)
* Other tenants' threads' runs never leak into the history
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
from movate.testing import InMemoryStorage


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
    minted = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="history-tests"
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


def _create_thread(client: TestClient, auth_header: dict[str, str]) -> str:
    r = client.post(
        "/api/v1/threads", json={"agent": "rag-qa"}, headers=auth_header
    )
    return r.json()["thread_id"]


def _seed_prior_run(
    storage: InMemoryStorage,
    *,
    thread_id: str,
    tenant_id: str,
    run_id: str,
    input_data: dict,
    output_data: dict,
    created_at: datetime,
) -> None:
    """Helper: write a RunRecord linked to thread_id directly to
    storage (simulating the worker having already processed a turn)."""
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
        input=input_data,
        output=output_data,
        metrics=Metrics(latency_ms=50, cost_usd=0.001, tokens_in=5, tokens_out=5),
        created_at=created_at,
        thread_id=thread_id,
    )
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(storage.save_run(run))
    finally:
        loop.close()


def _last_job_input(storage: InMemoryStorage) -> dict:
    """Look up the most-recently-saved job's input dict."""
    assert storage.jobs, "no jobs saved"
    return storage.jobs[-1].input


# ---------------------------------------------------------------------------
# First message — empty history
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_first_message_gets_empty_history(
    client: TestClient,
    auth_header: dict[str, str],
    storage: InMemoryStorage,
) -> None:
    """A brand-new thread's first message → conversation_history is
    an empty list. Operator-visible: agent prompts can iterate over
    the field safely from turn 1."""
    thread_id = _create_thread(client, auth_header)
    r = client.post(
        f"/api/v1/threads/{thread_id}/messages",
        json={"input": {"q": "first question"}},
        headers=auth_header,
    )
    assert r.status_code == 202

    job_input = _last_job_input(storage)
    assert "conversation_history" in job_input
    assert job_input["conversation_history"] == []
    # Operator-supplied keys are preserved.
    assert job_input["q"] == "first question"


# ---------------------------------------------------------------------------
# Second message — history has one entry
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_second_message_carries_prior_turn(
    client: TestClient,
    auth_header: dict[str, str],
    storage: InMemoryStorage,
) -> None:
    """After one prior run, the next message's history contains that
    run's input + output."""
    thread_id = _create_thread(client, auth_header)
    tenant_id = storage.conversation_threads[0].tenant_id
    _seed_prior_run(
        storage,
        thread_id=thread_id,
        tenant_id=tenant_id,
        run_id="r1",
        input_data={"q": "what's the refund policy?"},
        output_data={"a": "14 days for annual plans."},
        created_at=datetime.now(UTC),
    )
    r = client.post(
        f"/api/v1/threads/{thread_id}/messages",
        json={"input": {"q": "and what about monthly plans?"}},
        headers=auth_header,
    )
    assert r.status_code == 202

    history = _last_job_input(storage)["conversation_history"]
    assert len(history) == 1
    assert history[0]["input"] == {"q": "what's the refund policy?"}
    assert history[0]["output"] == {"a": "14 days for annual plans."}


# ---------------------------------------------------------------------------
# Multi-turn — chronological order
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_multi_turn_history_in_chronological_order(
    client: TestClient,
    auth_header: dict[str, str],
    storage: InMemoryStorage,
) -> None:
    """Three prior turns → history is [t1, t2, t3] earliest first.
    Matches list_runs_for_thread's ASC ordering."""
    thread_id = _create_thread(client, auth_header)
    tenant_id = storage.conversation_threads[0].tenant_id
    now = datetime.now(UTC)
    for i, ts in enumerate([now, now + timedelta(seconds=10), now + timedelta(seconds=20)]):
        _seed_prior_run(
            storage,
            thread_id=thread_id,
            tenant_id=tenant_id,
            run_id=f"r{i}",
            input_data={"q": f"turn {i} in"},
            output_data={"a": f"turn {i} out"},
            created_at=ts,
        )
    client.post(
        f"/api/v1/threads/{thread_id}/messages",
        json={"input": {"q": "turn 3"}},
        headers=auth_header,
    )
    history = _last_job_input(storage)["conversation_history"]
    assert len(history) == 3
    assert [h["input"]["q"] for h in history] == ["turn 0 in", "turn 1 in", "turn 2 in"]


# ---------------------------------------------------------------------------
# Caller-supplied history takes precedence
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_caller_supplied_history_is_preserved(
    client: TestClient,
    auth_header: dict[str, str],
    storage: InMemoryStorage,
) -> None:
    """Advanced operators can pre-format the history (e.g. with a
    summarized older-turn block) and submit it under
    ``conversation_history``. The endpoint MUST NOT overwrite it."""
    thread_id = _create_thread(client, auth_header)
    tenant_id = storage.conversation_threads[0].tenant_id
    _seed_prior_run(
        storage,
        thread_id=thread_id,
        tenant_id=tenant_id,
        run_id="r1",
        input_data={"q": "would be auto-injected"},
        output_data={"a": "..."},
        created_at=datetime.now(UTC),
    )
    custom_history = [{"input": {"q": "summarized"}, "output": {"a": "redacted"}}]
    client.post(
        f"/api/v1/threads/{thread_id}/messages",
        json={
            "input": {
                "q": "new question",
                "conversation_history": custom_history,
            }
        },
        headers=auth_header,
    )
    history = _last_job_input(storage)["conversation_history"]
    # Operator value preserved — auto-injected value did NOT overwrite.
    assert history == custom_history


# ---------------------------------------------------------------------------
# Tenant isolation — no cross-tenant leakage
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_other_tenants_runs_dont_leak_into_history(
    client: TestClient,
    auth_header: dict[str, str],
    storage: InMemoryStorage,
) -> None:
    """A run in some other tenant's thread MUST NOT appear in the
    injected history — tenant isolation enforced at the storage
    list_runs_for_thread layer."""
    thread_id = _create_thread(client, auth_header)
    tenant_id = storage.conversation_threads[0].tenant_id
    # Inject a run with the same thread_id but a DIFFERENT tenant — the
    # storage list filter should drop it.
    other_run = RunRecord(
        run_id="r_other_tenant",
        job_id="j_x",
        tenant_id="other-tenant-bogus",
        agent="rag-qa",
        agent_version="0.1.0",
        prompt_hash="h",
        provider="openai/gpt-4o-mini-2024-07-18",
        provider_version="1.0",
        pricing_version="2024-01-01",
        status=JobStatus.SUCCESS,
        input={"q": "should NOT leak"},
        output={"a": "should NOT leak"},
        metrics=Metrics(latency_ms=50, cost_usd=0.001, tokens_in=5, tokens_out=5),
        created_at=datetime.now(UTC),
        thread_id=thread_id,
    )
    await storage.save_run(other_run)
    # Also seed a same-tenant run that SHOULD appear.
    _seed_prior_run(
        storage,
        thread_id=thread_id,
        tenant_id=tenant_id,
        run_id="r_mine",
        input_data={"q": "should appear"},
        output_data={"a": "yes"},
        created_at=datetime.now(UTC),
    )
    client.post(
        f"/api/v1/threads/{thread_id}/messages",
        json={"input": {"q": "next"}},
        headers=auth_header,
    )
    history = _last_job_input(storage)["conversation_history"]
    assert len(history) == 1
    assert history[0]["input"]["q"] == "should appear"
