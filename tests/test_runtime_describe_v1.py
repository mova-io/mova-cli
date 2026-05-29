"""ADR 032 D1 — describe / preview agent endpoint.

``POST /api/v1/agents/preview`` — read-only LLM-scaffold preview (scope
``admin``). The same generate-then-validate pipeline ``mdk init --llm`` runs
(:func:`movate.core.scaffold_preview.preview_agent_from_description`) wired
behind the runtime API so the Mova iO front end can describe → preview →
commit (commits via the existing ``POST /agents`` / ``POST
/agents/from-wizard``).

Hermetic: TestClient + an ``InMemoryStorage`` + the deterministic
``MockProvider`` (no real LLM calls). The endpoint's ``mock=true`` switch
selects the ``MockProvider`` inside the handler so the test goes through the
production code path — no monkey-patching of the provider seam.

Coverage:

* Happy path with ``mock=true`` returns the candidate shape (agent_yaml,
  prompt_md, schemas, sample_evals, tokens, cost_usd, preview=true).
* The endpoint NEVER writes to disk or to storage (read-only contract).
* Tenant scoping — a non-admin key is forbidden; an admin key works.
* Auth — 401 unauthed; ``admin`` scope gates the route (matches the wizard
  create endpoint's scope).
* Body shape — 422 for an empty description or a missing required field
  (FastAPI handles via Pydantic).
* Provider-timeout failure mode → 504 (mapped from asyncio.TimeoutError).
* Provider-error failure mode → 502 (mapped from a ScaffoldPreviewError
  with mode=GENERATION).
* Validation failure mode → 422 (mapped from a ScaffoldPreviewError with
  mode=VALIDATION).
* Cost estimate present (zero for the mock provider; ``None`` is tolerated
  when the model isn't in the pricing table).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core import scaffold_preview as preview_mod
from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.core.models import TokenUsage
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
async def admin_auth(storage: InMemoryStorage):
    """An ``admin``-scoped key + its tenant_id. Preview endpoint gates on ``admin``."""
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="describe-v1-tests", scopes=["admin"]
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}, tenant_id


@pytest.fixture
async def read_only_auth(storage: InMemoryStorage):
    """A ``read``-only key — must be FORBIDDEN by the preview endpoint."""
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="describe-v1-read-tests", scopes=["read"]
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}, tenant_id


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def _ok_body(**overrides: Any) -> dict[str, Any]:
    """A minimal valid POST body for the happy-path tests; overridable per case."""
    body = {
        "description": "A friendly FAQ agent that answers product pricing questions.",
        "name": "faq-bot",
        "mock": True,
        "dry_run": True,
    }
    body.update(overrides)
    return body


async def test_describe_happy_path_returns_candidate(client: TestClient, admin_auth) -> None:
    """A successful preview returns the candidate shape — agent_yaml + prompt
    + schemas + sample evals + cost forecast, with ``preview=true``."""
    auth_header, _tenant = admin_auth
    r = client.post("/api/v1/agents/preview", json=_ok_body(), headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()

    # Wire-shape sanity: the front end branches on these top-level fields.
    assert body["preview"] is True
    assert body["name"] == "faq-bot"
    # The candidate's agent_yaml is a dict carrying the canonical fields.
    agent_yaml = body["agent_yaml"]
    assert isinstance(agent_yaml, dict)
    assert agent_yaml["name"] == "faq-bot"
    assert agent_yaml.get("api_version") == "movate/v1"
    assert agent_yaml.get("kind") == "Agent"
    # The defensive coercion fires — model.provider matches target_model so
    # the committed agent would run with the operator's key.
    model_block = agent_yaml.get("model", {})
    assert isinstance(model_block, dict)
    assert model_block.get("provider") == body["target_model"]
    # Canonical-defaults gap-fill is applied (matches a hand-init'd agent).
    assert "timeouts" in agent_yaml
    assert "budget" in agent_yaml
    assert "tags" in agent_yaml

    # The prompt + schemas are non-empty strings / dicts the front end renders.
    assert isinstance(body["prompt_md"], str) and body["prompt_md"]
    assert isinstance(body["input_schema"], dict)
    assert isinstance(body["output_schema"], dict)
    assert isinstance(body["sample_evals"], list)

    # Token / cost forecast: MockProvider stamps token usage, the cost may be
    # zero or None depending on the pricing table; the field must be present.
    tokens = body["tokens"]
    assert {"input", "output", "cached_input"} <= tokens.keys()
    assert "cost_usd" in body
    assert "retried" in body
    # ``retried`` is a boolean — true value depends on whether the MockProvider's
    # first attempt validated. The MockProvider may produce a candidate that needs
    # one defensive-feedback retry to validate; both shapes are healthy. We just
    # pin the FIELD's presence + type so the front end's TypeScript shape is stable.
    assert isinstance(body["retried"], bool)


async def test_describe_does_not_persist(
    client: TestClient, storage: InMemoryStorage, admin_auth
) -> None:
    """ADR 032 D1 read-only contract: the endpoint NEVER writes the scaffold
    to disk or to the runtime's storage. Sanity-check by snapshotting the
    in-memory agent registry before+after a preview — counts must match."""
    auth_header, tenant_id = admin_auth
    before = await storage.list_agents(tenant_id=tenant_id, limit=100)
    r = client.post("/api/v1/agents/preview", json=_ok_body(), headers=auth_header)
    assert r.status_code == 200, r.text
    after = await storage.list_agents(tenant_id=tenant_id, limit=100)
    assert len(before) == len(after) == 0  # neither persisted nor pre-existing


async def test_describe_target_model_pinned(client: TestClient, admin_auth) -> None:
    """Callers can pin the model declared in the GENERATED agent.yaml via
    ``target_model``. The defensive coercion forces the candidate's
    ``model.provider`` to match — front-end's contract bug-magnet."""
    auth_header, _tenant = admin_auth
    body = _ok_body(target_model="anthropic/claude-haiku-4-5-20251001")
    r = client.post("/api/v1/agents/preview", json=body, headers=auth_header)
    assert r.status_code == 200, r.text
    response_body = r.json()
    assert response_body["target_model"] == "anthropic/claude-haiku-4-5-20251001"
    assert response_body["agent_yaml"]["model"]["provider"] == "anthropic/claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# Auth + scope gating
# ---------------------------------------------------------------------------


def test_describe_requires_auth(client: TestClient) -> None:
    """Unauthenticated → 401."""
    assert client.post("/api/v1/agents/preview", json=_ok_body()).status_code == 401


async def test_describe_requires_admin_scope(client: TestClient, read_only_auth) -> None:
    """A ``read``-only key is FORBIDDEN — the preview endpoint spends LLM
    tokens, so it gates on ``admin`` (matches the wizard create endpoint)."""
    auth_header, _tenant = read_only_auth
    r = client.post("/api/v1/agents/preview", json=_ok_body(), headers=auth_header)
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# 422 — body shape failure modes
# ---------------------------------------------------------------------------


async def test_describe_rejects_empty_description(client: TestClient, admin_auth) -> None:
    """An empty description is rejected at the API boundary (min_length=1)."""
    auth_header, _tenant = admin_auth
    body = _ok_body(description="")
    r = client.post("/api/v1/agents/preview", json=body, headers=auth_header)
    assert r.status_code == 422, r.text


async def test_describe_rejects_oversized_description(client: TestClient, admin_auth) -> None:
    """A 4001-char description is rejected at the API boundary (max_length cap)."""
    auth_header, _tenant = admin_auth
    body = _ok_body(description="x" * 4001)
    r = client.post("/api/v1/agents/preview", json=body, headers=auth_header)
    assert r.status_code == 422, r.text


async def test_describe_rejects_missing_name(client: TestClient, admin_auth) -> None:
    """The slug the candidate will declare is required."""
    auth_header, _tenant = admin_auth
    body = {"description": "a useful agent", "mock": True}
    r = client.post("/api/v1/agents/preview", json=body, headers=auth_header)
    assert r.status_code == 422, r.text


async def test_describe_rejects_unknown_field(client: TestClient, admin_auth) -> None:
    """``extra='forbid'`` — an unknown field surfaces as 422 so a typo in the
    front end's TypeScript codegen surfaces immediately."""
    auth_header, _tenant = admin_auth
    body = _ok_body(unrecognized_field="surprise")
    r = client.post("/api/v1/agents/preview", json=body, headers=auth_header)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# 502 / 504 — provider failure modes (mock the shared pipeline)
# ---------------------------------------------------------------------------


async def test_describe_provider_generation_failure_502(
    client: TestClient, admin_auth, monkeypatch
) -> None:
    """A persistent provider wire error (ScaffoldPreviewError(GENERATION))
    surfaces as 502 — the LLM provider is the failure, not the caller's
    payload, so we don't 4xx it."""

    async def _always_fail(**kwargs: Any):
        raise preview_mod.ScaffoldPreviewError(
            mode=preview_mod.PreviewFailureMode.GENERATION,
            message="provider call failed: connection reset",
            tokens=TokenUsage(),
            partial_agent=None,
            retried=True,
        )

    monkeypatch.setattr(
        "movate.runtime.app.preview_agent_from_description",
        _always_fail,
        raising=False,
    )
    # The handler does a lazy import — patch the module-level binding it'll
    # see. Use the import path the handler actually uses.
    monkeypatch.setattr(preview_mod, "preview_agent_from_description", _always_fail)

    auth_header, _tenant = admin_auth
    r = client.post("/api/v1/agents/preview", json=_ok_body(), headers=auth_header)
    assert r.status_code == 502, r.text
    body = r.json()
    # FastAPI wraps the ``HTTPException.detail`` body under ``detail``; we then
    # land on the runtime's standard ``{"error": {"code", "message", ...}}``
    # envelope (ADR 033). Surface the message so a regression in the mapping
    # is obvious.
    assert "LLM scaffold generation failed" in body["detail"]["error"]["message"]


async def test_describe_provider_timeout_504(client: TestClient, admin_auth, monkeypatch) -> None:
    """A provider timeout (asyncio.TimeoutError) surfaces as 504."""

    async def _timeout(**kwargs: Any):
        raise TimeoutError("preview ceiling exceeded")

    monkeypatch.setattr(preview_mod, "preview_agent_from_description", _timeout)

    auth_header, _tenant = admin_auth
    r = client.post("/api/v1/agents/preview", json=_ok_body(), headers=auth_header)
    assert r.status_code == 504, r.text
    body = r.json()
    assert "timed out" in body["detail"]["error"]["message"].lower()


async def test_describe_validation_failure_422(client: TestClient, admin_auth, monkeypatch) -> None:
    """A candidate that fails load-validation (ScaffoldPreviewError(VALIDATION))
    surfaces as 422 — the caller's description is the proximate cause."""

    async def _bad_candidate(**kwargs: Any):
        raise preview_mod.ScaffoldPreviewError(
            mode=preview_mod.PreviewFailureMode.VALIDATION,
            message="output_schema missing required 'type' key",
            tokens=TokenUsage(input=42, output=11),
            partial_agent=None,
            retried=True,
        )

    monkeypatch.setattr(preview_mod, "preview_agent_from_description", _bad_candidate)

    auth_header, _tenant = admin_auth
    r = client.post("/api/v1/agents/preview", json=_ok_body(), headers=auth_header)
    assert r.status_code == 422, r.text
    body = r.json()
    assert "failed validation" in body["detail"]["error"]["message"]


# ---------------------------------------------------------------------------
# Cost forecast
# ---------------------------------------------------------------------------


async def test_describe_emits_cost_estimate(client: TestClient, admin_auth) -> None:
    """Every successful preview carries a token-usage block and a cost field
    (the front end shows LLM spend at preview time). ``cost_usd`` may be
    ``None`` when the model isn't in the pricing table — both shapes are
    acceptable, but the field is always present."""
    auth_header, _tenant = admin_auth
    r = client.post("/api/v1/agents/preview", json=_ok_body(), headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()

    tokens = body["tokens"]
    # MockProvider always stamps token counts (deterministic).
    assert isinstance(tokens["input"], int)
    assert isinstance(tokens["output"], int)
    assert isinstance(tokens["cached_input"], int)

    cost = body["cost_usd"]
    # Either a finite float (when the pricing table has the model) OR None —
    # both are valid; the front end renders None as "N/A".
    assert cost is None or (isinstance(cost, (int, float)) and cost >= 0.0)
