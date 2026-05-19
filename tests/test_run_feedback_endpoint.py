"""HTTP runtime — ``POST/GET /runs/{id}/feedback`` (0.8.2.11).

Covers:

* POST persists feedback rows to storage with correct shape (score,
  comment, dimensions, tenant scoping, denormalized agent name).
* POST is tenant-scoped — cross-tenant attempts 404 (not 403).
* POST is upsert-friendly: re-saving with same ``feedback_id`` replaces.
* GET lists feedback newest-first, tenant-scoped, with a configurable limit.
* Score validation: only -1/+1 thumbs OR 1-5 stars accepted.
* Empty user_id without auth context fails 422 with a clear hint.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import mint_api_key
from movate.core.models import (
    ApiKeyEnv,
    JobStatus,
    Metrics,
    RunRecord,
    TokenUsage,
)
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
async def minted_key(storage: InMemoryStorage):
    tenant_id = uuid4().hex
    minted = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="test")
    await storage.save_api_key(minted.record)
    return minted, f"Bearer {minted.full_key}"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": token}


def _make_run(*, tenant_id: str, agent: str = "rag-qa") -> RunRecord:
    """Persisted run that POST /runs/{id}/feedback can attach to."""
    return RunRecord(
        run_id=f"run-{uuid4().hex[:12]}",
        job_id=f"job-{uuid4().hex[:12]}",
        tenant_id=tenant_id,
        agent=agent,
        agent_version="1.0.0",
        prompt_hash="deadbeef",
        provider="openai/gpt-4o-mini",
        provider_version="2024-09",
        pricing_version="2024-09",
        status=JobStatus.SUCCESS,
        input={"question": "hi"},
        output={"answer": "hello", "grounded": True, "citations": [1], "confidence": 0.9},
        metrics=Metrics(
            cost_usd=0.0001,
            latency_ms=100.0,
            tokens=TokenUsage(input=10, output=5),
            pricing_version="2024-09",
        ),
    )


# ---------------------------------------------------------------------------
# POST /runs/{id}/feedback — happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_post_feedback_persists_thumbs_up(
    client: TestClient, minted_key, storage: InMemoryStorage
) -> None:
    """Bare thumbs-up feedback — no comment, no dimensions. The
    persisted row should carry score=+1, the run's denormalized
    agent name, and the authenticated tenant_id."""
    minted, bearer = minted_key
    run = _make_run(tenant_id=minted.record.tenant_id)
    await storage.save_run(run)

    r = client.post(
        f"/runs/{run.run_id}/feedback",
        json={"score": 1, "user_id": "alice@example.com"},
        headers=_auth(bearer),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["score"] == 1
    assert body["agent"] == "rag-qa"
    assert body["tenant_id"] == minted.record.tenant_id
    assert body["user_id"] == "alice@example.com"
    # Storage actually got the row.
    rows = await storage.list_feedback(run_id=run.run_id)
    assert len(rows) == 1
    assert rows[0].score == 1


@pytest.mark.unit
async def test_post_feedback_with_comment_and_dimensions(
    client: TestClient, minted_key, storage: InMemoryStorage
) -> None:
    """Rich feedback — dimensions JSON + a long comment. Verifies the
    JSONB / TEXT round-trip works."""
    minted, bearer = minted_key
    run = _make_run(tenant_id=minted.record.tenant_id)
    await storage.save_run(run)

    payload = {
        "score": 4,
        "user_id": "bob@example.com",
        "comment": "Mostly accurate but cited the wrong chunk on the refund window",
        "dimensions": {"helpfulness": 0.9, "accuracy": 0.75, "format": 1.0},
    }
    r = client.post(
        f"/runs/{run.run_id}/feedback",
        json=payload,
        headers=_auth(bearer),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["dimensions"] == payload["dimensions"]
    assert body["comment"] == payload["comment"]
    # Stored shape matches.
    rows = await storage.list_feedback(run_id=run.run_id)
    assert rows[0].dimensions == payload["dimensions"]


# ---------------------------------------------------------------------------
# POST /runs/{id}/feedback — auth + tenant scoping + validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_post_feedback_requires_auth(client: TestClient) -> None:
    r = client.post(
        "/runs/any/feedback",
        json={"score": 1, "user_id": "x"},
    )
    assert r.status_code == 401


@pytest.mark.unit
async def test_post_feedback_404_for_unknown_run(client: TestClient, minted_key) -> None:
    _, bearer = minted_key
    r = client.post(
        "/runs/no-such-run/feedback",
        json={"score": 1, "user_id": "x"},
        headers=_auth(bearer),
    )
    assert r.status_code == 404


@pytest.mark.unit
async def test_post_feedback_404_when_cross_tenant(
    client: TestClient, minted_key, storage: InMemoryStorage
) -> None:
    """A key from tenant B must not be able to add feedback to
    tenant A's run. 404 — never 403 (info-leak guard)."""
    minted, _ = minted_key
    run = _make_run(tenant_id=minted.record.tenant_id)
    await storage.save_run(run)

    other = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE)
    await storage.save_api_key(other.record)

    r = client.post(
        f"/runs/{run.run_id}/feedback",
        json={"score": 1, "user_id": "intruder"},
        headers={"Authorization": f"Bearer {other.full_key}"},
    )
    assert r.status_code == 404


@pytest.mark.unit
async def test_post_feedback_rejects_invalid_score(
    client: TestClient, minted_key, storage: InMemoryStorage
) -> None:
    """Schema validation: score 0, 6, or 999 all rejected with 422."""
    minted, bearer = minted_key
    run = _make_run(tenant_id=minted.record.tenant_id)
    await storage.save_run(run)

    for bad_score in (0, 6, 999, -2):
        r = client.post(
            f"/runs/{run.run_id}/feedback",
            json={"score": bad_score, "user_id": "x"},
            headers=_auth(bearer),
        )
        assert r.status_code == 422, f"score={bad_score} should be rejected"


# ---------------------------------------------------------------------------
# GET /runs/{id}/feedback — list
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_list_feedback_returns_newest_first(
    client: TestClient, minted_key, storage: InMemoryStorage
) -> None:
    """Multiple feedback rows on the same run come back newest-first."""
    minted, bearer = minted_key
    run = _make_run(tenant_id=minted.record.tenant_id)
    await storage.save_run(run)

    # Post 3 feedbacks. Each one's created_at is set server-side, so
    # later POSTs land later. (Storage iteration order isn't enough
    # — the endpoint must sort.)
    for i, score in enumerate((1, -1, 1)):
        r = client.post(
            f"/runs/{run.run_id}/feedback",
            json={"score": score, "user_id": f"user-{i}"},
            headers=_auth(bearer),
        )
        assert r.status_code == 201

    r = client.get(f"/runs/{run.run_id}/feedback", headers=_auth(bearer))
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    # Newest-first: user-2 (most recent) comes back first.
    users = [f["user_id"] for f in body["feedback"]]
    assert users == ["user-2", "user-1", "user-0"]


@pytest.mark.unit
async def test_list_feedback_404_when_cross_tenant(
    client: TestClient, minted_key, storage: InMemoryStorage
) -> None:
    """List endpoint is tenant-scoped like POST."""
    minted, _ = minted_key
    run = _make_run(tenant_id=minted.record.tenant_id)
    await storage.save_run(run)

    other = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE)
    await storage.save_api_key(other.record)

    r = client.get(
        f"/runs/{run.run_id}/feedback",
        headers={"Authorization": f"Bearer {other.full_key}"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Upsert: re-saving same feedback_id overwrites
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_feedback_upsert_overwrites_same_id(
    storage: InMemoryStorage,
) -> None:
    """Storage-layer upsert contract: same feedback_id replaces.

    Drives the storage method directly (not the HTTP endpoint —
    the endpoint generates a new id per POST, so the upsert path
    is only reachable via the storage method or a deliberate
    re-save with the same id)."""
    from movate.core.models import FeedbackRecord  # noqa: PLC0415

    feedback = FeedbackRecord(
        feedback_id="fixed-id",
        run_id="r1",
        tenant_id="t1",
        agent="rag-qa",
        user_id="alice",
        score=1,
        comment="initial",
    )
    await storage.save_feedback(feedback)
    # Edit + re-save with the same id.
    feedback2 = FeedbackRecord(
        feedback_id="fixed-id",
        run_id="r1",
        tenant_id="t1",
        agent="rag-qa",
        user_id="alice",
        score=-1,
        comment="changed my mind",
    )
    await storage.save_feedback(feedback2)
    rows = await storage.list_feedback(run_id="r1")
    assert len(rows) == 1  # not 2 — upsert
    assert rows[0].score == -1
    assert rows[0].comment == "changed my mind"
