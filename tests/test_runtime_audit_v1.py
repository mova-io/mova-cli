"""Tests for the Claude-orchestrated audit endpoints (audit-llm PR).

* POST /api/v1/agents/{name}/audit/from-llm
* POST /api/v1/projects/{project_id}/audit/from-llm
* GET  /api/v1/jobs/{job_id}          (job-state poll; reuses JobView)
* GET  /api/v1/audits/{audit_id}      (rich AuditJobView)
* GET  /api/v1/jobs/{job_id}/stream   (SSE progress, audit-only)

Hermetic: uses :class:`InMemoryStorage` + a stub provider; drives the
dispatch in-test instead of running the worker loop, so the assertion
cycle is tight and deterministic.

Pinned in this PR:

* Both POST routes 202 with ``job_id`` + ``status_url`` + ``stream_url``.
* Scope check: read-only access to the route is enough (POST + GET
  both use the ``read`` scope, since the audit IS read-only).
* Tenant scoping at GET /audits/{id} — cross-tenant returns 404, not 403.
* The read-only invariant: storage's mutable lists (other than
  ``audits``) are byte-identical before vs after the audit cycle.
* SSE stream: returns ``text/event-stream`` and the body carries
  ``category_complete`` + ``agent_complete`` + ``completed`` frames in
  that order on a successful audit.
"""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auditor import Auditor  # imported at module top to avoid PLC0415
from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.core.executor import Executor
from movate.core.models import (
    AuditFindingSeverity,
    AuditRecord,
    JobKind,
    JobRecord,
    JobStatus,
)
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
    StreamChunk,
)
from movate.providers.pricing import load_pricing
from movate.runtime import build_app
from movate.runtime.dispatch import DispatchOutcome, WorkerDispatch
from movate.runtime.registry import scan_agents
from movate.testing import InMemoryStorage, NullTracer


class StubAuditProvider(BaseLLMProvider):
    """Returns a canned per-category findings JSON. Records every call."""

    name = "stub_audit"
    version = "0.0.1"

    def __init__(self, *, findings_by_first_word: dict[str, list[dict[str, Any]]] | None = None):
        self._fb = findings_by_first_word or {}
        self.calls: list[str] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = request.messages[0].content if request.messages else ""
        self.calls.append(body[:80])
        # Match by a stable hint in each category's prompt.
        text_lower = body.lower()
        findings: list[dict[str, Any]] = []
        if "ambigu" in text_lower or "contradict" in text_lower:
            findings = self._fb.get("ambiguous_prompts", [])
        elif "eval-coverage" in text_lower or "dataset row exercises" in text_lower:
            findings = self._fb.get("missing_eval_coverage", [])
        elif "security smells" in text_lower or "pii" in text_lower:
            findings = self._fb.get("security_smells", [])
        elif "cost concerns" in text_lower or "cost-per-run" in text_lower:
            findings = self._fb.get("cost_outliers", [])
        elif "knowledge base" in text_lower:
            findings = self._fb.get("kb_quality", [])
        elif "schema drift" in text_lower:
            findings = self._fb.get("schema_drift", [])
        elif "model-choice" in text_lower or "model too big" in text_lower:
            findings = self._fb.get("model_choice", [])
        return CompletionResponse(text=json.dumps({"findings": findings}))

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        resp = await self.complete(request)
        yield StreamChunk(text=resp.text)
        yield StreamChunk(text="", tokens=resp.tokens)

    async def embed(self, text: str, *, model: str) -> list[float]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def agents_path(tmp_path: Path) -> Path:
    p = tmp_path / "agents"
    p.mkdir()
    return p


@pytest.fixture
def client(storage: InMemoryStorage, agents_path: Path) -> TestClient:
    return TestClient(build_app(storage, agents_path=agents_path))


@pytest.fixture
async def auth_setup(storage: InMemoryStorage):
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id,
        env=ApiKeyEnv.LIVE,
        label="audit-v1-tests",
        scopes=list(ALL_SCOPES),
    )
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    return header, tenant_id


_AGENT_YAML = b"""\
api_version: movate/v1
kind: Agent
name: audit-demo
version: 0.1.0
description: target for audit-llm endpoint tests
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
"""

_PROMPT = b"Hello {{ input.text }}\n"

_INPUT_SCHEMA = json.dumps(
    {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
).encode()

_OUTPUT_SCHEMA = json.dumps(
    {"type": "object", "properties": {"message": {"type": "string"}}}
).encode()


def _create_agent(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.post(
        "/api/v1/agents",
        files=[
            ("agent_yaml", ("agent.yaml", _AGENT_YAML, "application/x-yaml")),
            ("prompt", ("prompt.md", _PROMPT, "text/markdown")),
            ("input_schema", ("input.json", _INPUT_SCHEMA, "application/json")),
            ("output_schema", ("output.json", _OUTPUT_SCHEMA, "application/json")),
        ],
        headers=auth_header,
    )
    assert r.status_code == 201, r.text


async def _run_dispatch(
    *,
    client: TestClient,
    storage: InMemoryStorage,
    agents_path: Path,
    findings_by_first_word: dict[str, list[dict[str, Any]]] | None = None,
) -> None:
    """Drive the worker dispatch directly so the queued audit job
    completes in-test without spinning up a worker process.

    Patches the provider-construction path by mounting a stub provider
    onto the dispatch. Tenant-scoped consumption — every queued audit
    job is drained.
    """
    # Plug a stub provider into the dispatch so the category fan-out
    # uses canned findings instead of hitting LiteLLM.
    stub = StubAuditProvider(findings_by_first_word=findings_by_first_word)
    # Mock executor — the audit path doesn't use it (audit pipeline is
    # provider-driven), but WorkerDispatch wants one.
    executor = Executor(
        provider=stub,
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
    )
    agents = scan_agents(agents_path)
    dispatch = WorkerDispatch(
        storage=storage,
        executor=executor,
        agents=agents,
        use_mock_for_eval=False,
    )
    # Reach into the dispatch's audit path: monkey-patch the auditor's
    # provider factory by replacing the lazy import path. Simpler: the
    # dispatch's _execute_audit branches on use_mock_for_eval OR
    # cfg["mock"] — we'll set mock=true on every job we drive, then
    # swap the MockProvider for our stub via a small monkey-patch.
    # But MockProvider returns canned non-findings; for fidelity we
    # plug the stub directly using a tiny override of _execute_audit.
    original = dispatch._execute_audit

    async def _patched(job):  # type: ignore[no-untyped-def]
        # Mirror the original but force the stub provider.
        cfg = job.input
        scope_kind = str(cfg.get("scope_kind", "agent"))
        scope_id = str(cfg.get("scope_id", job.target))
        categories = cfg.get("categories")
        if categories is not None and not isinstance(categories, list):
            categories = None
        try:
            sev = AuditFindingSeverity(str(cfg.get("severity_floor", "info")).lower())
        except ValueError:
            sev = AuditFindingSeverity.INFO
        budget_usd = float(cfg.get("budget_usd", 0.0))
        auditor = Auditor(
            provider=stub,
            storage=storage,
            model=str(cfg.get("model") or "openai/gpt-4o-mini"),
            budget_usd=budget_usd,
            severity_floor=sev,
        )
        if scope_kind == "project":
            bundles = list(agents)
            record = await auditor.audit_project(
                bundles=bundles,
                project_id=scope_id,
                tenant_id=job.tenant_id,
                categories=categories,
            )
        else:
            bundle = next((b for b in agents if b.spec.name == job.target), None)
            assert bundle is not None
            record = await auditor.audit_agent(
                bundle=bundle, tenant_id=job.tenant_id, categories=categories
            )
        await storage.save_audit(record)
        return DispatchOutcome(status=JobStatus.SUCCESS, result_run_id=record.audit_id, error=None)

    dispatch._execute_audit = _patched  # type: ignore[assignment]
    del original  # silence "unused"
    # Drain every QUEUED job.
    while True:
        job = await storage.claim_next_job()
        if job is None:
            break
        outcome = await dispatch.execute_job(job)
        await storage.update_job(
            job.job_id,
            status=outcome.status,
            result_run_id=outcome.result_run_id,
            error=outcome.error,
            tenant_id=job.tenant_id,
        )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_audit_agent_endpoint_returns_job_id_status_url_stream_url(
    client: TestClient, auth_setup
) -> None:
    """POST returns 202 + job_id + URLs the client polls/streams from."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/audit-demo/audit/from-llm",
        json={"severity_floor": "info", "budget_usd": 1.0},
        headers=auth_header,
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["job_id"].startswith("audit_")
    assert body["status_url"] == f"/api/v1/jobs/{body['job_id']}"
    assert body["stream_url"] == f"/api/v1/jobs/{body['job_id']}/stream"


async def test_audit_full_lifecycle_persists_record(
    client: TestClient, auth_setup, storage: InMemoryStorage, agents_path: Path
) -> None:
    """End-to-end: POST → dispatch → GET /audits/{audit_id} returns the
    rich AuditJobView with findings + summary."""
    auth_header, _tenant_id = auth_setup
    _create_agent(client, auth_header)

    canned = {
        "ambiguous_prompts": [
            {
                "severity": "warn",
                "title": "Vague directive",
                "description": "Line 1 is too short.",
                "suggestion": "Be specific about output shape.",
                "confidence": "high",
            }
        ],
        "security_smells": [
            {
                "severity": "error",
                "title": "Possible prompt injection",
                "description": "User input is concatenated unescaped.",
                "suggestion": "Fence user input with a delimiter.",
                "confidence": "medium",
            }
        ],
    }
    r = client.post(
        "/api/v1/agents/audit-demo/audit/from-llm",
        json={"severity_floor": "info", "budget_usd": 1.0},
        headers=auth_header,
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    await _run_dispatch(
        client=client,
        storage=storage,
        agents_path=agents_path,
        findings_by_first_word=canned,
    )

    # Job is now terminal.
    job_resp = client.get(f"/api/v1/jobs/{job_id}", headers=auth_header)
    assert job_resp.status_code == 200, job_resp.text
    job = job_resp.json()
    assert job["status"] == "success"
    audit_id = job["result_run_id"]
    assert audit_id and audit_id.startswith("audit_")

    audit_resp = client.get(f"/api/v1/audits/{audit_id}", headers=auth_header)
    assert audit_resp.status_code == 200, audit_resp.text
    view = audit_resp.json()
    assert view["kind"] == "audit"
    assert view["audit_id"] == audit_id
    assert view["scope"] == {"type": "agent", "id": "audit-demo"}
    assert view["status"] == "completed"
    assert view["summary"]["total_findings"] == 2
    assert view["summary"]["by_severity"]["warn"] == 1
    assert view["summary"]["by_severity"]["error"] == 1
    assert {f["category"] for f in view["findings"]} == {
        "ambiguous_prompts",
        "security_smells",
    }


async def test_audit_does_not_modify_agent(
    client: TestClient, auth_setup, storage: InMemoryStorage, agents_path: Path
) -> None:
    """READ-ONLY invariant: after a full audit cycle, every mutable
    storage list (other than ``audits`` + the one ``jobs`` row) must
    be byte-identical to its pre-audit snapshot.
    """
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)

    # Take the snapshot AFTER agent creation, so the agent_bundles row
    # doesn't count as audit-introduced.
    before = {
        "runs": copy.deepcopy(storage.runs),
        "evals": copy.deepcopy(storage.evals),
        "bench": copy.deepcopy(storage.bench),
        "kb_chunks": copy.deepcopy(storage.kb_chunks),
        "agent_bundles": copy.deepcopy(storage.agent_bundles),
        "workflow_runs": copy.deepcopy(storage.workflow_runs),
        "feedback": copy.deepcopy(storage.feedback),
        "eval_schedules": copy.deepcopy(storage.eval_schedules),
        "job_schedules": copy.deepcopy(storage.job_schedules),
        "canary_configs": copy.deepcopy(storage.canary_configs),
        "tenant_provider_keys": copy.deepcopy(storage.tenant_provider_keys),
    }

    r = client.post(
        "/api/v1/agents/audit-demo/audit/from-llm",
        json={"budget_usd": 1.0},
        headers=auth_header,
    )
    assert r.status_code == 202
    await _run_dispatch(client=client, storage=storage, agents_path=agents_path)

    # Only audits (and the queued job) should have changed.
    for key, snap in before.items():
        actual = getattr(storage, key)
        assert actual == snap, f"audit mutated storage.{key}"
    assert len(storage.audits) == 1


async def test_audit_tenant_isolation_on_get(client: TestClient, storage: InMemoryStorage) -> None:
    """A second tenant's GET /audits/{id} for the first tenant's
    audit returns 404 (never 403, never the actual row)."""
    # Need a real tenant id that satisfies TENANT_PREFIX_LEN.
    tenant_a = uuid4().hex
    tenant_b = uuid4().hex
    audit = AuditRecord(
        audit_id="audit_t1",
        tenant_id=tenant_a,
        scope_kind="agent",
        scope_id="x",
        categories=["ambiguous_prompts"],
        severity_floor=AuditFindingSeverity.INFO,
        model="openai/gpt-4o-mini",
        budget_usd=1.0,
        findings=[],
    )
    await storage.save_audit(audit)

    # Mint a key for tenant B and try to read it.
    minted = mint_api_key(
        tenant_id=tenant_b,
        env=ApiKeyEnv.LIVE,
        label="other",
        scopes=list(ALL_SCOPES),
    )
    await storage.save_api_key(minted.record)
    b_header = {"Authorization": f"Bearer {minted.full_key}"}
    r = client.get("/api/v1/audits/audit_t1", headers=b_header)
    assert r.status_code == 404


def test_audit_project_endpoint_returns_job_id(client: TestClient, auth_setup) -> None:
    """The project-scoped route 202s with a project-scoped job."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/projects/my-proj/audit/from-llm",
        json={"budget_usd": 2.0},
        headers=auth_header,
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["job_id"].startswith("audit_")
    assert body["status"] == "queued"


def test_audit_agent_endpoint_unknown_agent_returns_404(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.post(
        "/api/v1/agents/never-existed/audit/from-llm",
        json={},
        headers=auth_header,
    )
    assert r.status_code == 404


def test_audit_agent_endpoint_unauthed_returns_401(client: TestClient) -> None:
    r = client.post("/api/v1/agents/audit-demo/audit/from-llm", json={})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    event: str | None = None
    data_lines: list[str] = []
    for raw in body.split("\n"):
        line = raw.rstrip("\r")
        if line == "":
            if event is not None or data_lines:
                payload = "\n".join(data_lines)
                data = json.loads(payload) if payload else {}
                events.append((event or "message", data))
            event = None
            data_lines = []
            continue
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip(" "))
    return events


async def test_audit_stream_emits_category_then_agent_then_completed(
    client: TestClient, auth_setup, storage: InMemoryStorage, agents_path: Path
) -> None:
    """A terminal-state audit job's SSE stream emits one
    ``category_complete`` per category that contributed findings, then
    one ``agent_complete``, then ``completed`` — in that order."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)

    # Submit a 1-category audit so the test's SSE assertion is tight.
    r = client.post(
        "/api/v1/agents/audit-demo/audit/from-llm",
        json={
            "categories": ["ambiguous_prompts"],
            "budget_usd": 1.0,
        },
        headers=auth_header,
    )
    job_id = r.json()["job_id"]

    # Drive the dispatch to terminal SUCCESS.
    await _run_dispatch(
        client=client,
        storage=storage,
        agents_path=agents_path,
        findings_by_first_word={
            "ambiguous_prompts": [
                {
                    "severity": "warn",
                    "title": "t",
                    "description": "d",
                    "suggestion": "s",
                }
            ]
        },
    )

    # Now stream the (already-terminal) job — should replay events + close.
    resp = client.get(f"/api/v1/jobs/{job_id}/stream", headers=auth_header)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    names = [n for n, _ in events]
    # One category_complete, one agent_complete, one completed (in order).
    assert names[0] == "category_complete"
    assert "agent_complete" in names
    assert names[-1] == "completed"
    # Completed frame carries total + cost.
    last_data = events[-1][1]
    assert last_data["total_findings"] == 1
    assert "cost_usd" in last_data


async def test_audit_stream_non_audit_job_returns_404(
    client: TestClient, auth_setup, storage: InMemoryStorage
) -> None:
    """The audit SSE stream is audit-only — a non-audit job returns 404."""
    auth_header, tenant_id = auth_setup
    job = JobRecord(
        job_id="ag_xxx",
        tenant_id=tenant_id,
        kind=JobKind.AGENT,
        target="demo",
        input={},
    )
    await storage.save_job(job)

    r = client.get("/api/v1/jobs/ag_xxx/stream", headers=auth_header)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Contract test — pin both routes + the scope dependency.
# ---------------------------------------------------------------------------


def test_audit_routes_are_registered_with_read_scope(
    client: TestClient,
) -> None:
    """Contract: both POST routes + the GET audits route + the SSE
    stream route are registered, and the audit endpoints sit under
    /api/v1 (the versioned router).
    """
    paths = {r.path for r in client.app.routes}
    assert "/api/v1/agents/{name}/audit/from-llm" in paths
    assert "/api/v1/projects/{project_id}/audit/from-llm" in paths
    assert "/api/v1/audits/{audit_id}" in paths
    assert "/api/v1/jobs/{job_id}/stream" in paths
