"""Submission idempotency (item 37) — the OPTIONAL ``Idempotency-Key`` header.

A client that retries an async submit (network blip / timeout) double-enqueues
today. With an ``Idempotency-Key`` header, a retry returns the SAME job instead
of creating a second one, bounding the at-least-once submit story (mirrors the
item-23 trigger-delivery dedup).

Covers the two primary async (queued, 202) submit endpoints:

* ``POST /api/v1/agents/{name}/runs`` (default async path)
* ``POST /run`` (legacy generic job submit)

Asserts: same key → same job, only ONE job; different keys → two jobs; NO header
→ two jobs (back-compat); oversized / empty key → treated as absent; the dedup
is per-tenant. Requires the runtime extras (fastapi) — skipped where only core
is installed.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.core.models import AgentBundleRecord
from movate.runtime import build_app
from movate.runtime.agent_resolver import content_hash
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
async def auth_setup(storage: InMemoryStorage):
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id,
        env=ApiKeyEnv.LIVE,
        label="idempotency-tests",
        scopes=list(ALL_SCOPES),
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}, tenant_id


def _bundle_record(*, name: str, tenant_id: str, version: str = "1.0.0") -> AgentBundleRecord:
    files = {
        "agent.yaml": (
            "api_version: movate/v1\n"
            "kind: Agent\n"
            f"name: {name}\n"
            f"version: {version}\n"
            "model:\n  provider: openai/gpt-4o-mini-2024-07-18\n"
            "prompt: ./prompt.md\n"
            "schema:\n  input:\n    text: string\n  output:\n    message: string\n"
        ),
        "prompt.md": "Reply to {{ input.text }}\n",
    }
    return AgentBundleRecord(
        name=name,
        tenant_id=tenant_id,
        version=version,
        created_by="tester",
        content_hash=content_hash(files),
        files=files,
    )


async def _publish(storage: InMemoryStorage, *, tenant_id: str, name: str = "bot") -> None:
    await storage.save_agent_bundle(_bundle_record(name=name, tenant_id=tenant_id))


# ---------------------------------------------------------------------------
# POST /api/v1/agents/{name}/runs (default async path)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_same_key_returns_same_job_and_enqueues_once(
    storage: InMemoryStorage, client: TestClient, auth_setup
) -> None:
    headers, tenant_id = auth_setup
    await _publish(storage, tenant_id=tenant_id)

    first = client.post(
        "/api/v1/agents/bot/runs",
        json={"input": {"text": "hi"}},
        headers={**headers, "Idempotency-Key": "abc-123"},
    )
    second = client.post(
        "/api/v1/agents/bot/runs",
        json={"input": {"text": "hi"}},
        headers={**headers, "Idempotency-Key": "abc-123"},
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["deduplicated"] is False
    assert second.json()["deduplicated"] is True
    # Same job returned, and only ONE job was enqueued.
    assert second.json()["job_id"] == first.json()["job_id"]
    jobs = await storage.list_jobs(tenant_id=tenant_id, limit=100)
    assert len(jobs) == 1


@pytest.mark.unit
async def test_different_keys_enqueue_two_jobs(
    storage: InMemoryStorage, client: TestClient, auth_setup
) -> None:
    headers, tenant_id = auth_setup
    await _publish(storage, tenant_id=tenant_id)

    r1 = client.post(
        "/api/v1/agents/bot/runs",
        json={"input": {"text": "hi"}},
        headers={**headers, "Idempotency-Key": "key-1"},
    )
    r2 = client.post(
        "/api/v1/agents/bot/runs",
        json={"input": {"text": "hi"}},
        headers={**headers, "Idempotency-Key": "key-2"},
    )

    assert r1.json()["job_id"] != r2.json()["job_id"]
    assert r1.json()["deduplicated"] is False
    assert r2.json()["deduplicated"] is False
    jobs = await storage.list_jobs(tenant_id=tenant_id, limit=100)
    assert len(jobs) == 2


@pytest.mark.unit
async def test_no_header_enqueues_each_call(
    storage: InMemoryStorage, client: TestClient, auth_setup
) -> None:
    """Back-compat: with no header the path is byte-for-byte today's."""
    headers, tenant_id = auth_setup
    await _publish(storage, tenant_id=tenant_id)

    r1 = client.post("/api/v1/agents/bot/runs", json={"input": {"text": "hi"}}, headers=headers)
    r2 = client.post("/api/v1/agents/bot/runs", json={"input": {"text": "hi"}}, headers=headers)

    assert r1.json()["job_id"] != r2.json()["job_id"]
    assert r1.json()["deduplicated"] is False
    assert r2.json()["deduplicated"] is False
    jobs = await storage.list_jobs(tenant_id=tenant_id, limit=100)
    assert len(jobs) == 2


@pytest.mark.unit
async def test_empty_key_treated_as_absent(
    storage: InMemoryStorage, client: TestClient, auth_setup
) -> None:
    headers, tenant_id = auth_setup
    await _publish(storage, tenant_id=tenant_id)

    r1 = client.post(
        "/api/v1/agents/bot/runs",
        json={"input": {"text": "hi"}},
        headers={**headers, "Idempotency-Key": "   "},
    )
    r2 = client.post(
        "/api/v1/agents/bot/runs",
        json={"input": {"text": "hi"}},
        headers={**headers, "Idempotency-Key": "   "},
    )

    assert r1.json()["job_id"] != r2.json()["job_id"]
    assert r2.json()["deduplicated"] is False
    jobs = await storage.list_jobs(tenant_id=tenant_id, limit=100)
    assert len(jobs) == 2


@pytest.mark.unit
async def test_overlong_key_treated_as_absent(
    storage: InMemoryStorage, client: TestClient, auth_setup
) -> None:
    headers, tenant_id = auth_setup
    await _publish(storage, tenant_id=tenant_id)

    oversized = "x" * 201  # > IDEMPOTENCY_KEY_MAX_LEN (200)
    r1 = client.post(
        "/api/v1/agents/bot/runs",
        json={"input": {"text": "hi"}},
        headers={**headers, "Idempotency-Key": oversized},
    )
    r2 = client.post(
        "/api/v1/agents/bot/runs",
        json={"input": {"text": "hi"}},
        headers={**headers, "Idempotency-Key": oversized},
    )

    assert r1.json()["job_id"] != r2.json()["job_id"]
    assert r2.json()["deduplicated"] is False
    jobs = await storage.list_jobs(tenant_id=tenant_id, limit=100)
    assert len(jobs) == 2


@pytest.mark.unit
async def test_wait_true_inline_path_is_not_deduped(
    storage: InMemoryStorage, client: TestClient, auth_setup
) -> None:
    """The inline ?wait=true path is out of scope — it returns a RunView (200),
    never touches the dedup store, so a repeat does NOT dedup."""
    headers, tenant_id = auth_setup
    await _publish(storage, tenant_id=tenant_id)

    r1 = client.post(
        "/api/v1/agents/bot/runs?wait=true",
        json={"input": {"text": "hi"}, "mock": True},
        headers={**headers, "Idempotency-Key": "abc-123"},
    )
    assert r1.status_code == 200
    # No run_submission row was recorded by the inline path.
    assert await storage.get_run_submission(tenant_id, "abc-123") is None


# ---------------------------------------------------------------------------
# POST /run (legacy generic job submit)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_legacy_run_same_key_returns_same_job(
    storage: InMemoryStorage, client: TestClient, auth_setup
) -> None:
    headers, tenant_id = auth_setup
    body = {"kind": "agent", "target": "bot", "input": {"text": "hi"}}

    first = client.post("/run", json=body, headers={**headers, "Idempotency-Key": "run-1"})
    second = client.post("/run", json=body, headers={**headers, "Idempotency-Key": "run-1"})

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["job_id"] == first.json()["job_id"]
    assert second.json()["deduplicated"] is True
    jobs = await storage.list_jobs(tenant_id=tenant_id, limit=100)
    assert len(jobs) == 1


@pytest.mark.unit
async def test_legacy_run_no_header_enqueues_each_call(
    storage: InMemoryStorage, client: TestClient, auth_setup
) -> None:
    headers, tenant_id = auth_setup
    body = {"kind": "agent", "target": "bot", "input": {"text": "hi"}}

    r1 = client.post("/run", json=body, headers=headers)
    r2 = client.post("/run", json=body, headers=headers)

    assert r1.json()["job_id"] != r2.json()["job_id"]
    jobs = await storage.list_jobs(tenant_id=tenant_id, limit=100)
    assert len(jobs) == 2


# ---------------------------------------------------------------------------
# Payload-conflict guard (item 37): a key reused for a DIFFERENT payload must
# 409 — never silently return the wrong run. A same-payload retry still dedups.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_same_key_different_payload_conflicts(
    storage: InMemoryStorage, client: TestClient, auth_setup
) -> None:
    """Reusing a key with a different agent input → 409, no second job."""
    headers, tenant_id = auth_setup
    await _publish(storage, tenant_id=tenant_id)

    first = client.post(
        "/api/v1/agents/bot/runs",
        json={"input": {"text": "hi"}},
        headers={**headers, "Idempotency-Key": "dup-key"},
    )
    conflicting = client.post(
        "/api/v1/agents/bot/runs",
        json={"input": {"text": "DIFFERENT"}},
        headers={**headers, "Idempotency-Key": "dup-key"},
    )

    assert first.status_code == 202
    assert conflicting.status_code == 409
    # Only the first submission's job exists — the conflicting one never enqueued.
    jobs = await storage.list_jobs(tenant_id=tenant_id, limit=100)
    assert len(jobs) == 1
    assert jobs[0].job_id == first.json()["job_id"]


@pytest.mark.unit
async def test_same_key_same_payload_still_dedups_not_conflicts(
    storage: InMemoryStorage, client: TestClient, auth_setup
) -> None:
    """An IDENTICAL retry must dedup (202), not 409 — the fingerprint matches."""
    headers, tenant_id = auth_setup
    await _publish(storage, tenant_id=tenant_id)

    first = client.post(
        "/api/v1/agents/bot/runs",
        json={"input": {"text": "hi"}},
        headers={**headers, "Idempotency-Key": "same-key"},
    )
    retry = client.post(
        "/api/v1/agents/bot/runs",
        json={"input": {"text": "hi"}},
        headers={**headers, "Idempotency-Key": "same-key"},
    )

    assert first.status_code == 202
    assert retry.status_code == 202
    assert retry.json()["deduplicated"] is True
    assert retry.json()["job_id"] == first.json()["job_id"]


@pytest.mark.unit
async def test_legacy_run_same_key_different_payload_conflicts(
    storage: InMemoryStorage, client: TestClient, auth_setup
) -> None:
    """The /run path 409s on a key reused for a different payload too."""
    headers, tenant_id = auth_setup

    first = client.post(
        "/run",
        json={"kind": "agent", "target": "bot", "input": {"text": "hi"}},
        headers={**headers, "Idempotency-Key": "run-dup"},
    )
    conflicting = client.post(
        "/run",
        json={"kind": "agent", "target": "bot", "input": {"text": "DIFFERENT"}},
        headers={**headers, "Idempotency-Key": "run-dup"},
    )

    assert first.status_code == 202
    assert conflicting.status_code == 409
    jobs = await storage.list_jobs(tenant_id=tenant_id, limit=100)
    assert len(jobs) == 1


# ---------------------------------------------------------------------------
# Cross-tenant isolation (two tenants reusing the same key must not collide)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_same_key_across_tenants_is_independent(storage: InMemoryStorage) -> None:
    app = build_app(storage)
    client = TestClient(app)

    tenant_headers = []
    for label in ("t1", "t2"):
        tenant_id = uuid4().hex
        minted = mint_api_key(
            tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label=label, scopes=list(ALL_SCOPES)
        )
        await storage.save_api_key(minted.record)
        await _publish(storage, tenant_id=tenant_id)
        tenant_headers.append({"Authorization": f"Bearer {minted.full_key}"})

    job_ids = []
    for headers in tenant_headers:
        r = client.post(
            "/api/v1/agents/bot/runs",
            json={"input": {"text": "hi"}},
            headers={**headers, "Idempotency-Key": "shared-key"},
        )
        assert r.json()["deduplicated"] is False
        job_ids.append(r.json()["job_id"])

    # Same key string, different tenants → two DISTINCT jobs (no collision).
    assert job_ids[0] != job_ids[1]
