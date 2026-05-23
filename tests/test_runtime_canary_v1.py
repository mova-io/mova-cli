"""Canary rollout runtime endpoints + routing + dispatch (ADR 016 D3).

Covers:

* set / status / delete (admin/read scope gating, tenant isolation)
* the NO-CONFIG regression guard — the run/enqueue path is unchanged
  (JobRecord.target_version is None, identical to pre-canary) when no canary
  is set; and stamps the chosen version when one is.
* dispatch honors JobRecord.target_version (resolves THAT version).
* compare aggregation (runs + feedback sliced by agent_version → deltas).
* promote (admin required; assisted default; auto-promote eval-gate
  blocked/allowed; promotion recorded; rollback reverts).

Requires the runtime extras (fastapi) — skipped where only core is installed.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, SCOPE_READ, ApiKeyEnv, mint_api_key
from movate.core.executor import Executor
from movate.core.models import (
    AgentBundleRecord,
    FeedbackRecord,
    JobKind,
    JobRecord,
    JobStatus,
    Metrics,
    RunRecord,
)
from movate.providers.mock import MockProvider
from movate.providers.pricing import load_pricing
from movate.runtime import build_app
from movate.runtime.agent_resolver import content_hash
from movate.runtime.dispatch import WorkerDispatch
from movate.testing import InMemoryStorage, NullTracer


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
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="canary-tests", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}, tenant_id


def _bundle_record(*, name: str, tenant_id: str, version: str) -> AgentBundleRecord:
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


async def _publish_two_versions(storage: InMemoryStorage, *, tenant_id: str, name: str = "bot"):
    """Publish v1 (champion-latest will be v2) then v2 into the registry."""
    await storage.save_agent_bundle(_bundle_record(name=name, tenant_id=tenant_id, version="1.0.0"))
    await storage.save_agent_bundle(_bundle_record(name=name, tenant_id=tenant_id, version="2.0.0"))


# ---------------------------------------------------------------------------
# set / status / delete + scope gating
# ---------------------------------------------------------------------------


async def test_set_canary_then_status(client: TestClient, auth_setup, storage) -> None:
    header, tenant_id = auth_setup
    await _publish_two_versions(storage, tenant_id=tenant_id)
    r = client.post(
        "/api/v1/agents/bot/canary",
        json={"challenger_version": "2.0.0", "weight": 25},
        headers=header,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["challenger_version"] == "2.0.0"
    assert body["weight"] == 25
    assert body["sticky"] is True

    s = client.get("/api/v1/agents/bot/canary", headers=header)
    assert s.status_code == 200
    assert s.json()["weight"] == 25


async def test_set_unknown_challenger_version_404(client: TestClient, auth_setup, storage) -> None:
    header, tenant_id = auth_setup
    await _publish_two_versions(storage, tenant_id=tenant_id)
    r = client.post(
        "/api/v1/agents/bot/canary",
        json={"challenger_version": "9.9.9", "weight": 10},
        headers=header,
    )
    assert r.status_code == 404


async def test_set_auto_promote_without_gate_422(client: TestClient, auth_setup, storage) -> None:
    header, tenant_id = auth_setup
    await _publish_two_versions(storage, tenant_id=tenant_id)
    r = client.post(
        "/api/v1/agents/bot/canary",
        json={"challenger_version": "2.0.0", "weight": 10, "auto_promote": True},
        headers=header,
    )
    assert r.status_code == 422


async def test_status_404_when_no_canary(client: TestClient, auth_setup) -> None:
    header, _ = auth_setup
    assert client.get("/api/v1/agents/bot/canary", headers=header).status_code == 404


async def test_set_requires_admin_scope(client: TestClient, storage) -> None:
    minted = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="ro", scopes=[SCOPE_READ]
    )
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    r = client.post(
        "/api/v1/agents/bot/canary",
        json={"challenger_version": "2.0.0", "weight": 10},
        headers=header,
    )
    assert r.status_code == 403


async def test_delete_canary_is_idempotent(client: TestClient, auth_setup, storage) -> None:
    header, tenant_id = auth_setup
    await _publish_two_versions(storage, tenant_id=tenant_id)
    client.post(
        "/api/v1/agents/bot/canary",
        json={"challenger_version": "2.0.0", "weight": 10},
        headers=header,
    )
    assert client.delete("/api/v1/agents/bot/canary", headers=header).status_code == 204
    assert client.get("/api/v1/agents/bot/canary", headers=header).status_code == 404
    # Idempotent.
    assert client.delete("/api/v1/agents/bot/canary", headers=header).status_code == 204


async def test_canary_is_tenant_scoped(client: TestClient, auth_setup, storage) -> None:
    header, tenant_id = auth_setup
    await _publish_two_versions(storage, tenant_id=tenant_id)
    client.post(
        "/api/v1/agents/bot/canary",
        json={"challenger_version": "2.0.0", "weight": 10},
        headers=header,
    )
    other = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="other", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(other.record)
    other_header = {"Authorization": f"Bearer {other.full_key}"}
    assert client.get("/api/v1/agents/bot/canary", headers=other_header).status_code == 404


# ---------------------------------------------------------------------------
# The #1 invariant: NO config → the run path is byte-for-byte unchanged
# ---------------------------------------------------------------------------


async def test_no_canary_async_run_has_null_target_version(
    client: TestClient, auth_setup, storage
) -> None:
    """The critical regression guard: with NO canary, the enqueued JobRecord
    is identical to a pre-canary one — target_version is None (→ worker
    resolves latest)."""
    header, tenant_id = auth_setup
    await _publish_two_versions(storage, tenant_id=tenant_id)
    r = client.post("/api/v1/agents/bot/runs", json={"input": {"text": "hi"}}, headers=header)
    assert r.status_code == 202, r.text
    job = storage.jobs[0]
    assert job.target_version is None  # unchanged from pre-canary
    assert job.thread_id is None
    assert job.target == "bot"
    assert job.kind == JobKind.AGENT


async def test_canary_weight_100_stamps_challenger_on_job(
    client: TestClient, auth_setup, storage
) -> None:
    header, tenant_id = auth_setup
    await _publish_two_versions(storage, tenant_id=tenant_id)
    client.post(
        "/api/v1/agents/bot/canary",
        json={"challenger_version": "2.0.0", "weight": 100},
        headers=header,
    )
    r = client.post("/api/v1/agents/bot/runs", json={"input": {"text": "hi"}}, headers=header)
    assert r.status_code == 202
    job = storage.jobs[0]
    assert job.target_version == "2.0.0"


async def test_canary_kill_switch_stamps_no_version(
    client: TestClient, auth_setup, storage
) -> None:
    header, tenant_id = auth_setup
    await _publish_two_versions(storage, tenant_id=tenant_id)
    # weight 0 = kill switch → champion (no pin) → None on the job.
    client.post(
        "/api/v1/agents/bot/canary",
        json={"challenger_version": "2.0.0", "weight": 0},
        headers=header,
    )
    r = client.post("/api/v1/agents/bot/runs", json={"input": {"text": "hi"}}, headers=header)
    assert r.status_code == 202
    assert storage.jobs[0].target_version is None


async def test_sticky_canary_keeps_thread_on_one_side(
    client: TestClient, auth_setup, storage
) -> None:
    header, tenant_id = auth_setup
    await _publish_two_versions(storage, tenant_id=tenant_id)
    client.post(
        "/api/v1/agents/bot/canary",
        json={"challenger_version": "2.0.0", "weight": 50, "sticky": True},
        headers=header,
    )
    # Same thread across several submissions → identical target_version.
    versions = set()
    for _ in range(5):
        client.post(
            "/api/v1/agents/bot/runs",
            json={"input": {"text": "hi"}, "thread_id": "thread-stable"},
            headers=header,
        )
    versions = {j.target_version for j in storage.jobs}
    assert len(versions) == 1


# ---------------------------------------------------------------------------
# Dispatch honors JobRecord.target_version
# ---------------------------------------------------------------------------


async def test_dispatch_honors_target_version(storage, monkeypatch) -> None:
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "ok"}')
    tenant_id = "t-disp"
    await _publish_two_versions(storage, tenant_id=tenant_id)
    executor = Executor(
        provider=MockProvider(),
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
        tenant_id="worker-default",
    )
    dispatch = WorkerDispatch(storage=storage, executor=executor, agents=[])
    # Pin the OLDER version (1.0.0) even though latest is 2.0.0.
    job = JobRecord(
        job_id=str(uuid4()),
        tenant_id=tenant_id,
        kind=JobKind.AGENT,
        target="bot",
        status=JobStatus.QUEUED,
        input={"text": "hi"},
        target_version="1.0.0",
    )
    await storage.save_job(job)
    outcome = await dispatch.execute_job(job)
    assert outcome.status == JobStatus.SUCCESS, outcome.error
    run = await storage.get_run(outcome.result_run_id, tenant_id=tenant_id)
    assert run is not None
    assert run.agent_version == "1.0.0"  # ran the pinned version, not latest


async def test_dispatch_none_target_version_resolves_latest(storage, monkeypatch) -> None:
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "ok"}')
    tenant_id = "t-disp2"
    await _publish_two_versions(storage, tenant_id=tenant_id)
    executor = Executor(
        provider=MockProvider(),
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
        tenant_id="worker-default",
    )
    dispatch = WorkerDispatch(storage=storage, executor=executor, agents=[])
    job = JobRecord(
        job_id=str(uuid4()),
        tenant_id=tenant_id,
        kind=JobKind.AGENT,
        target="bot",
        status=JobStatus.QUEUED,
        input={"text": "hi"},
        target_version=None,
    )
    await storage.save_job(job)
    outcome = await dispatch.execute_job(job)
    assert outcome.status == JobStatus.SUCCESS, outcome.error
    run = await storage.get_run(outcome.result_run_id, tenant_id=tenant_id)
    assert run is not None
    assert run.agent_version == "2.0.0"  # latest


# ---------------------------------------------------------------------------
# Compare aggregation
# ---------------------------------------------------------------------------


def _seed_run(
    storage: InMemoryStorage,
    *,
    tenant_id: str,
    version: str,
    status: JobStatus = JobStatus.SUCCESS,
    run_id: str | None = None,
) -> str:
    rid = run_id or uuid4().hex
    storage.runs.append(
        RunRecord(
            run_id=rid,
            job_id=uuid4().hex,
            tenant_id=tenant_id,
            agent="bot",
            agent_version=version,
            prompt_hash="ph",
            provider="mock",
            provider_version="1",
            pricing_version="1",
            status=status,
            input={"text": "x"},
            metrics=Metrics(),
        )
    )
    return rid


def _seed_feedback(storage: InMemoryStorage, *, tenant_id: str, run_id: str, score: int) -> None:
    storage.feedback.append(
        FeedbackRecord(
            run_id=run_id,
            tenant_id=tenant_id,
            agent="bot",
            user_id="u",
            score=score,
        )
    )


async def test_compare_aggregates_by_version(client: TestClient, auth_setup, storage) -> None:
    header, tenant_id = auth_setup
    await _publish_two_versions(storage, tenant_id=tenant_id)
    client.post(
        "/api/v1/agents/bot/canary",
        json={"challenger_version": "2.0.0", "champion_version": "1.0.0", "weight": 50},
        headers=header,
    )
    # Champion v1: 2 success, 1 error; one 👍.
    r1 = _seed_run(storage, tenant_id=tenant_id, version="1.0.0", status=JobStatus.SUCCESS)
    _seed_run(storage, tenant_id=tenant_id, version="1.0.0", status=JobStatus.SUCCESS)
    _seed_run(storage, tenant_id=tenant_id, version="1.0.0", status=JobStatus.ERROR)
    _seed_feedback(storage, tenant_id=tenant_id, run_id=r1, score=1)
    # Challenger v2: 2 success; both 👍 → higher thumbs-up rate.
    c1 = _seed_run(storage, tenant_id=tenant_id, version="2.0.0", status=JobStatus.SUCCESS)
    c2 = _seed_run(storage, tenant_id=tenant_id, version="2.0.0", status=JobStatus.SUCCESS)
    _seed_feedback(storage, tenant_id=tenant_id, run_id=c1, score=1)
    _seed_feedback(storage, tenant_id=tenant_id, run_id=c2, score=1)

    r = client.get("/api/v1/agents/bot/canary/compare", headers=header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["champion"]["version"] == "1.0.0"
    assert body["champion"]["run_count"] == 3
    assert body["champion"]["error_count"] == 1
    assert body["champion"]["thumbs_up"] == 1
    assert body["challenger"]["version"] == "2.0.0"
    assert body["challenger"]["run_count"] == 2
    assert body["challenger"]["error_count"] == 0
    assert body["challenger"]["thumbs_up"] == 2
    # Challenger has the better success rate (1.0 vs 0.667) → positive delta.
    assert body["success_rate_delta"] > 0
    assert body["thumbs_up_rate_delta"] == pytest.approx(0.0)  # both 1.0


async def test_compare_422_without_challenger(client: TestClient, auth_setup, storage) -> None:
    header, tenant_id = auth_setup
    await _publish_two_versions(storage, tenant_id=tenant_id)
    # No canary, no ?challenger= → 422.
    r = client.get("/api/v1/agents/bot/canary/compare", headers=header)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Promote / rollback
# ---------------------------------------------------------------------------


async def test_promote_requires_admin(client: TestClient, auth_setup, storage) -> None:
    admin_header, tenant_id = auth_setup
    await _publish_two_versions(storage, tenant_id=tenant_id)
    client.post(
        "/api/v1/agents/bot/canary",
        json={"challenger_version": "2.0.0", "weight": 50},
        headers=admin_header,
    )
    ro = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="ro", scopes=[SCOPE_READ])
    await storage.save_api_key(ro.record)
    ro_header = {"Authorization": f"Bearer {ro.full_key}"}
    r = client.post("/api/v1/agents/bot/canary/promote", json={}, headers=ro_header)
    assert r.status_code == 403


async def test_assisted_promote_default(client: TestClient, auth_setup, storage) -> None:
    header, tenant_id = auth_setup
    await _publish_two_versions(storage, tenant_id=tenant_id)
    client.post(
        "/api/v1/agents/bot/canary",
        json={"challenger_version": "2.0.0", "champion_version": "1.0.0", "weight": 50},
        headers=header,
    )
    r = client.post("/api/v1/agents/bot/canary/promote", json={}, headers=header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "assisted"
    assert body["promoted_version"] == "2.0.0"
    assert body["previous_champion"] == "1.0.0"
    # Config updated: challenger → champion, weight → 0.
    assert body["canary"]["champion_version"] == "2.0.0"
    assert body["canary"]["weight"] == 0
    # Persisted.
    cfg = await storage.get_canary_config("bot", tenant_id=tenant_id)
    assert cfg.champion_version == "2.0.0"
    assert cfg.weight == 0


async def test_auto_promote_blocked_when_gate_unmet(
    client: TestClient, auth_setup, storage
) -> None:
    header, tenant_id = auth_setup
    await _publish_two_versions(storage, tenant_id=tenant_id)
    client.post(
        "/api/v1/agents/bot/canary",
        json={
            "challenger_version": "2.0.0",
            "weight": 50,
            "auto_promote": True,
            "eval_gate": 0.9,
        },
        headers=header,
    )
    # Challenger has a poor thumbs-up rate (1 down, 1 up → 0.5 < 0.9).
    c1 = _seed_run(storage, tenant_id=tenant_id, version="2.0.0")
    c2 = _seed_run(storage, tenant_id=tenant_id, version="2.0.0")
    _seed_feedback(storage, tenant_id=tenant_id, run_id=c1, score=1)
    _seed_feedback(storage, tenant_id=tenant_id, run_id=c2, score=-1)
    r = client.post(
        "/api/v1/agents/bot/canary/promote", json={"auto_promote": True}, headers=header
    )
    assert r.status_code == 409, r.text
    # Not promoted.
    cfg = await storage.get_canary_config("bot", tenant_id=tenant_id)
    assert cfg.champion_version is None
    assert cfg.weight == 50


async def test_auto_promote_allowed_when_gate_met(client: TestClient, auth_setup, storage) -> None:
    header, tenant_id = auth_setup
    await _publish_two_versions(storage, tenant_id=tenant_id)
    client.post(
        "/api/v1/agents/bot/canary",
        json={
            "challenger_version": "2.0.0",
            "weight": 50,
            "auto_promote": True,
            "eval_gate": 0.9,
        },
        headers=header,
    )
    # Challenger thumbs-up rate 1.0 ≥ 0.9.
    c1 = _seed_run(storage, tenant_id=tenant_id, version="2.0.0")
    c2 = _seed_run(storage, tenant_id=tenant_id, version="2.0.0")
    _seed_feedback(storage, tenant_id=tenant_id, run_id=c1, score=1)
    _seed_feedback(storage, tenant_id=tenant_id, run_id=c2, score=1)
    r = client.post(
        "/api/v1/agents/bot/canary/promote", json={"auto_promote": True}, headers=header
    )
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "auto"
    cfg = await storage.get_canary_config("bot", tenant_id=tenant_id)
    assert cfg.champion_version == "2.0.0"
    assert cfg.weight == 0


async def test_promote_unknown_target_404(client: TestClient, auth_setup, storage) -> None:
    header, tenant_id = auth_setup
    await _publish_two_versions(storage, tenant_id=tenant_id)
    client.post(
        "/api/v1/agents/bot/canary",
        json={"challenger_version": "2.0.0", "weight": 50},
        headers=header,
    )
    r = client.post(
        "/api/v1/agents/bot/canary/promote", json={"to_version": "9.9.9"}, headers=header
    )
    assert r.status_code == 404


async def test_promote_404_when_no_canary(client: TestClient, auth_setup) -> None:
    header, _ = auth_setup
    r = client.post("/api/v1/agents/bot/canary/promote", json={}, headers=header)
    assert r.status_code == 404


async def test_rollback_reverts_to_zero_weight(client: TestClient, auth_setup, storage) -> None:
    header, tenant_id = auth_setup
    await _publish_two_versions(storage, tenant_id=tenant_id)
    client.post(
        "/api/v1/agents/bot/canary",
        json={"challenger_version": "2.0.0", "champion_version": "1.0.0", "weight": 80},
        headers=header,
    )
    r = client.post("/api/v1/agents/bot/canary/rollback", headers=header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "rollback"
    assert body["canary"]["weight"] == 0
    # Champion stays the recorded champion (1.0.0).
    cfg = await storage.get_canary_config("bot", tenant_id=tenant_id)
    assert cfg.weight == 0
    assert cfg.champion_version == "1.0.0"


async def test_rollback_requires_admin(client: TestClient, auth_setup, storage) -> None:
    header, tenant_id = auth_setup
    await _publish_two_versions(storage, tenant_id=tenant_id)
    client.post(
        "/api/v1/agents/bot/canary",
        json={"challenger_version": "2.0.0", "weight": 50},
        headers=header,
    )
    ro = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="ro", scopes=[SCOPE_READ])
    await storage.save_api_key(ro.record)
    ro_header = {"Authorization": f"Bearer {ro.full_key}"}
    assert client.post("/api/v1/agents/bot/canary/rollback", headers=ro_header).status_code == 403
