"""Runtime integration tests for the eval generator endpoints.

Hermetic: builds the FastAPI app in-process from an InMemoryStorage
double + a tmp ``agents_path``, monkeypatches the generator pipeline
so no real LLM is called, and exercises the full review-then-commit
lifecycle through the HTTP surface.

Coverage:

* Happy path: POST generate → 202 + job_id, GET /jobs/{id} reports
  ``running`` → ``completed``, response carries the generated cases
  in the new ``EvalGenerateJobView`` shape.
* SSE: GET /jobs/{id}/stream emits the documented event taxonomy
  (``category_complete`` / ``completed``).
* Commit: POST /jobs/{id}/commit appends the accepted cases to the
  agent's ``evals/dataset.jsonl`` on disk + supports selective
  acceptance via ``case_ids``.
* Tenant scoping: cross-tenant job_id returns 404 (no leak).
* Budget abort: budget_usd=0 fails the job cleanly with
  ``code: budget_exceeded`` on the persisted error.
* Auth/scope: missing scope → 403; missing token → 401.
* Edge validation: bad ``count`` / unknown category → 422.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core import eval_generator as evg
from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.core.eval_generator import GeneratedEvalCase, GenerationResult
from movate.runtime import app as runtime_app
from movate.runtime import build_app
from movate.testing import InMemoryStorage

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
async def auth_setup(storage: InMemoryStorage) -> tuple[dict[str, str], str]:
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id,
        env=ApiKeyEnv.LIVE,
        label="evals-generate-v1-tests",
        scopes=list(ALL_SCOPES),
    )
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    return header, tenant_id


# Agent bundle suitable for ``mdk eval generate`` — single string-in,
# string-out so the generator's schema validation has something to
# exercise without making the test fixture heavy.
_AGENT_YAML = b"""\
api_version: movate/v1
kind: Agent
name: eval-gen-demo
version: 0.1.0
description: target for eval-generate tests
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
"""

_PROMPT = b'Respond with valid JSON: {"label": "<your answer>"}\n\nInput: {{ input.text }}\n'

_INPUT_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
).encode()

_OUTPUT_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {"label": {"type": "string"}},
        "required": ["label"],
    }
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


# ---------------------------------------------------------------------------
# Patches — the pipeline is unit-tested in test_eval_generator_core.py;
# here we stub it so the route exercises the lifecycle, not the LLM.
# ---------------------------------------------------------------------------


def _patch_generate(monkeypatch: pytest.MonkeyPatch, *, fail: bool = False) -> None:
    """Replace the pipeline with a deterministic generator.

    The runtime endpoint imports ``generate_eval_cases`` from
    ``movate.core.eval_generator`` inside the background task — we patch
    the module attribute so the task gets our stub.
    """

    async def _stub(
        *,
        bundle: Any,
        description: str,
        provider_impl: Any,
        model: str,
        count: int = 20,
        categories: list[str] | None = None,
        include_judge: bool = False,
        budget_usd: float | None = None,
        on_event: Any = None,
    ) -> GenerationResult:
        if fail:
            raise evg.GenerationFailedError("stub failure")
        cats = categories or ["happy", "edge", "adversarial"]
        cases: list[GeneratedEvalCase] = []
        for i, cat in enumerate(cats):
            case = GeneratedEvalCase(
                id=f"c{i + 1}",
                category=cat,
                input={"text": f"sample-{cat}"},
                expected={"label": "ok"} if cat != "adversarial" else None,
                rationale=f"{cat} case",
            )
            cases.append(case)
            if on_event:
                on_event("category_complete", {"category": cat, "cases_so_far": len(cases)})
        if on_event:
            on_event("completed", {"case_count": len(cases), "cost_usd": 0.012})
        judge_yaml = None
        if include_judge:
            judge_yaml = "version: 1\ndimensions:\n  - name: accuracy\n    weight: 1.0\n"
        return GenerationResult(cases=cases, judge_yaml=judge_yaml, tokens_used=200, cost_usd=0.012)

    monkeypatch.setattr(evg, "generate_eval_cases", _stub)


def _patch_generate_budget_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the pipeline with one that raises ``BudgetExceededError``."""

    async def _stub(**_kwargs: Any) -> GenerationResult:
        raise evg.BudgetExceededError(spent=0.10, ceiling=0.0, after_category="happy")

    monkeypatch.setattr(evg, "generate_eval_cases", _stub)


# Disable the preview-eval smoke step in the runtime — it spins up an
# Executor + InMemoryStorage which we don't care to exercise here. The
# stubbed pipeline already returns a known result.
@pytest.fixture(autouse=True)
def _disable_preview_eval(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(bundle: Any, cases: Any) -> None:
        return None

    monkeypatch.setattr(runtime_app, "_smoke_preview_score", _noop)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _wait_for_completion(
    client: TestClient, auth_header: dict[str, str], job_id: str
) -> dict[str, Any]:
    """Poll GET /jobs/{id} until the job reports a terminal status."""
    for _ in range(50):
        r = client.get(f"/api/v1/jobs/{job_id}", headers=auth_header)
        assert r.status_code == 200, r.text
        body = r.json()
        if body.get("status") in ("completed", "failed"):
            return body
        await asyncio.sleep(0.02)
    raise AssertionError(f"job {job_id} did not terminate")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_happy_path_returns_cases(
    client: TestClient,
    auth_setup: tuple[dict[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST → 202 + job_id; GET resolves to a completed job carrying
    the generated cases in the new view shape."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    _patch_generate(monkeypatch)

    r = client.post(
        "/api/v1/agents/eval-gen-demo/evals/generate",
        json={"description": "triages support tickets", "count": 3},
        headers=auth_header,
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "running"
    assert body["job_id"].startswith("evgen_")
    assert body["status_url"].endswith(body["job_id"])
    assert body["stream_url"].endswith("/stream")

    completed = await _wait_for_completion(client, auth_header, body["job_id"])
    assert completed["kind"] == "evals_generate"
    assert completed["status"] == "completed"
    assert completed["agent_name"] == "eval-gen-demo"
    assert len(completed["result"]["cases"]) == 3
    cats = {c["category"] for c in completed["result"]["cases"]}
    assert cats == {"happy", "edge", "adversarial"}


@pytest.mark.asyncio
async def test_generate_with_judge(
    client: TestClient,
    auth_setup: tuple[dict[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``include_judge=true`` populates ``judge_yaml`` on the result."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    _patch_generate(monkeypatch)

    r = client.post(
        "/api/v1/agents/eval-gen-demo/evals/generate",
        json={"description": "x", "count": 3, "include_judge": True},
        headers=auth_header,
    )
    assert r.status_code == 202
    completed = await _wait_for_completion(client, auth_header, r.json()["job_id"])
    assert completed["result"]["judge_yaml"] is not None
    assert "dimensions" in completed["result"]["judge_yaml"]


# ---------------------------------------------------------------------------
# SSE — event taxonomy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_replays_terminal_event_when_job_finished(
    client: TestClient,
    auth_setup: tuple[dict[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stream endpoint should always send a terminal frame — even
    when the client connects AFTER the job already finished."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    _patch_generate(monkeypatch)

    r = client.post(
        "/api/v1/agents/eval-gen-demo/evals/generate",
        json={"description": "x", "count": 2},
        headers=auth_header,
    )
    job_id = r.json()["job_id"]
    await _wait_for_completion(client, auth_header, job_id)

    # Connect AFTER completion; the runtime should replay a terminal
    # ``completed`` frame off the persisted record.
    with client.stream("GET", f"/api/v1/jobs/{job_id}/stream", headers=auth_header) as resp:
        assert resp.status_code == 200
        body = "".join(chunk for chunk in resp.iter_text())
    # Pin the documented event-name vocabulary (route docstring +
    # contract test depend on these).
    assert "event: completed" in body
    assert '"case_count"' in body


# ---------------------------------------------------------------------------
# Commit — selective acceptance + disk mutation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_appends_selected_cases(
    client: TestClient,
    auth_setup: tuple[dict[str, str], str],
    agents_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``case_ids=[...]`` filters; the dataset file gains exactly those
    cases, every line marked ``generated: true``."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    _patch_generate(monkeypatch)

    r = client.post(
        "/api/v1/agents/eval-gen-demo/evals/generate",
        json={"description": "x", "count": 3},
        headers=auth_header,
    )
    job_id = r.json()["job_id"]
    await _wait_for_completion(client, auth_header, job_id)

    # Commit just c1 + c3.
    r = client.post(
        f"/api/v1/jobs/{job_id}/commit",
        json={"case_ids": ["c1", "c3"]},
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cases_added"] == 2
    assert body["agent_name"] == "eval-gen-demo"
    assert body["judge_yaml_updated"] is False

    dataset = agents_path / "eval-gen-demo" / "evals" / "dataset.jsonl"
    assert dataset.is_file()
    lines = dataset.read_text().strip().splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    for entry in parsed:
        assert entry["generated"] is True
        assert "input" in entry


@pytest.mark.asyncio
async def test_commit_with_judge_writes_judge_yaml(
    client: TestClient,
    auth_setup: tuple[dict[str, str], str],
    agents_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the job drafted a judge AND the caller asks to commit it,
    ``evals/judge.yaml`` lands on disk."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    _patch_generate(monkeypatch)

    r = client.post(
        "/api/v1/agents/eval-gen-demo/evals/generate",
        json={"description": "x", "count": 2, "include_judge": True},
        headers=auth_header,
    )
    job_id = r.json()["job_id"]
    await _wait_for_completion(client, auth_header, job_id)

    r = client.post(
        f"/api/v1/jobs/{job_id}/commit",
        json={"commit_judge": True},
        headers=auth_header,
    )
    assert r.status_code == 200
    assert r.json()["judge_yaml_updated"] is True
    judge = agents_path / "eval-gen-demo" / "evals" / "judge.yaml"
    assert judge.is_file()
    assert "dimensions" in judge.read_text()


# ---------------------------------------------------------------------------
# Tenant scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_job_id_returns_404(
    client: TestClient,
    storage: InMemoryStorage,
    auth_setup: tuple[dict[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller authed as tenant B can't GET tenant A's job — always
    404 (no 403 leak that the id exists)."""
    auth_header_a, _ = auth_setup
    _create_agent(client, auth_header_a)
    _patch_generate(monkeypatch)

    r = client.post(
        "/api/v1/agents/eval-gen-demo/evals/generate",
        json={"description": "x", "count": 1, "categories": ["happy"]},
        headers=auth_header_a,
    )
    job_id = r.json()["job_id"]
    await _wait_for_completion(client, auth_header_a, job_id)

    # Mint a SECOND tenant + try to read tenant A's job.
    minted_b = mint_api_key(
        tenant_id=uuid4().hex,
        env=ApiKeyEnv.LIVE,
        label="b",
        scopes=list(ALL_SCOPES),
    )
    await storage.save_api_key(minted_b.record)
    header_b = {"Authorization": f"Bearer {minted_b.full_key}"}
    r = client.get(f"/api/v1/jobs/{job_id}", headers=header_b)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Budget abort
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_abort_fails_job_with_typed_error(
    client: TestClient,
    auth_setup: tuple[dict[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A budget overrun lands as ``status=failed`` + a typed
    ``budget_exceeded`` error code on the persisted job."""
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    _patch_generate_budget_abort(monkeypatch)

    r = client.post(
        "/api/v1/agents/eval-gen-demo/evals/generate",
        json={"description": "x", "count": 1, "budget_usd": 0.0},
        headers=auth_header,
    )
    assert r.status_code == 202
    body = await _wait_for_completion(client, auth_header, r.json()["job_id"])
    assert body["status"] == "failed"
    assert body["error"]["code"] == "budget_exceeded"


# ---------------------------------------------------------------------------
# Auth + edge validation
# ---------------------------------------------------------------------------


def test_unauthenticated_request_is_401(client: TestClient) -> None:
    r = client.post(
        "/api/v1/agents/eval-gen-demo/evals/generate",
        json={"description": "x", "count": 1},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_commit_requires_admin_scope(
    client: TestClient,
    storage: InMemoryStorage,
    auth_setup: tuple[dict[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token with ``eval`` but not ``admin`` can kick off + read but
    not commit — pinned because the commit step mutates the agent
    bundle on disk."""
    auth_header, tenant_id = auth_setup
    _create_agent(client, auth_header)
    _patch_generate(monkeypatch)

    r = client.post(
        "/api/v1/agents/eval-gen-demo/evals/generate",
        json={"description": "x", "count": 1, "categories": ["happy"]},
        headers=auth_header,
    )
    job_id = r.json()["job_id"]
    await _wait_for_completion(client, auth_header, job_id)

    # Mint a SECOND key for the SAME tenant carrying only read+eval —
    # commit must 403.
    minted_eval = mint_api_key(
        tenant_id=tenant_id,
        env=ApiKeyEnv.LIVE,
        label="eval-only",
        scopes=["read", "eval"],
    )
    await storage.save_api_key(minted_eval.record)
    eval_header = {"Authorization": f"Bearer {minted_eval.full_key}"}
    r = client.post(f"/api/v1/jobs/{job_id}/commit", json={}, headers=eval_header)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_count_out_of_range_returns_422(
    client: TestClient, auth_setup: tuple[dict[str, str], str]
) -> None:
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/eval-gen-demo/evals/generate",
        json={"description": "x", "count": 999},
        headers=auth_header,
    )
    # Pydantic's edge-check fires at the request shape (Field(le=100)).
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_unknown_category_returns_422(
    client: TestClient,
    auth_setup: tuple[dict[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_header, _ = auth_setup
    _create_agent(client, auth_header)
    # validate_categories raises ValueError, the route maps to 422.
    r = client.post(
        "/api/v1/agents/eval-gen-demo/evals/generate",
        json={"description": "x", "count": 1, "categories": ["happy", "bogus"]},
        headers=auth_header,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_commit_on_non_existent_job_is_404(
    client: TestClient, auth_setup: tuple[dict[str, str], str]
) -> None:
    auth_header, _ = auth_setup
    r = client.post(
        "/api/v1/jobs/evgen_does_not_exist/commit",
        json={},
        headers=auth_header,
    )
    assert r.status_code == 404
