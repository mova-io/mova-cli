"""Economics + rate-limit response-header ergonomics (additive).

Every response carries what the caller needs to budget + back off:

* **Economics** — ``X-MDK-Cost-USD`` / ``X-MDK-Tokens-In`` / ``X-MDK-Tokens-Out``
  on a cost-incurring response (inline ``?wait=true`` agent run), ABSENT on a
  pure read (best-effort R2 rule: omit unknown, never emit a misleading zero).
* **Cache** — ``X-MDK-Cache`` reflects the response-cache disposition
  (``none`` with the default NoOp cache).
* **Correlation** — ``X-MDK-Request-Id`` echoes the id on EVERY response (an
  additive alias of the untouched ``X-Request-Id``).
* **Rate-limit** — ``X-RateLimit-*`` on every authed response; ``Retry-After``
  on a 429 (focused regression — the limiter logic lives in test_rate_limit).

Hermetic: a fresh app over ``InMemoryStorage`` driven via ``TestClient``.
A scaffolded agent + ``mock=True`` runs the deterministic MockProvider so the
cost-incurring path needs no API key and stays sub-second.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.runtime import build_app
from movate.runtime.hardening import (
    ECONOMICS_HEADER_CACHE,
    ECONOMICS_HEADER_COST,
    ECONOMICS_HEADER_REQUEST_ID,
    ECONOMICS_HEADER_TOKENS_IN,
    ECONOMICS_HEADER_TOKENS_OUT,
    ResponseEconomics,
    set_response_economics,
)
from movate.runtime.request_context import REQUEST_ID_HEADER
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Fixtures + helpers
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
async def auth_header(storage: InMemoryStorage) -> dict[str, str]:
    minted = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="hdr-tests", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


_AGENT_YAML = b"""\
api_version: movate/v1
kind: Agent
name: hdr-demo
version: 0.1.0
description: demo for response-header tests
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
"""
_PROMPT = b"Hi {{ input.text }}\n"
_INPUT_SCHEMA = json.dumps(
    {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
).encode()
# Matches the MockProvider's default ``{"message": "mock response"}`` so the
# inline run SUCCEEDS (and thus carries real token usage + cost) rather than
# failing output-schema validation (which would zero the metrics).
_OUTPUT_SCHEMA = json.dumps(
    {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}
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
# Economics headers — present on a cost-incurring response.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_economics_headers_present_on_cost_incurring_run(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """An inline ``?wait=true`` run executes the MockProvider → real token
    counts → economics headers attach. Token headers are deterministic
    non-zero; the cost header is present as a parseable decimal string."""
    _create_agent(client, auth_header)
    r = client.post(
        "/api/v1/agents/hdr-demo/runs?wait=true",
        json={"input": {"text": "hello world"}, "mock": True},
        headers=auth_header,
    )
    assert r.status_code == 200, r.text
    # Tokens are deterministic + non-zero for the MockProvider.
    assert int(r.headers[ECONOMICS_HEADER_TOKENS_IN]) >= 1
    assert int(r.headers[ECONOMICS_HEADER_TOKENS_OUT]) >= 1
    # Cost header present + a parseable float (>= 0; MockProvider may be $0
    # if pricing for the model is absent — the point is the header is emitted
    # because this response path incurred LLM work).
    assert ECONOMICS_HEADER_COST in r.headers
    assert float(r.headers[ECONOMICS_HEADER_COST]) >= 0.0
    # Cache status: default backend is NoOp → this response does NOT involve
    # the response cache → "none".
    assert r.headers[ECONOMICS_HEADER_CACHE] == "none"


# ---------------------------------------------------------------------------
# Economics headers — ABSENT on a pure read (best-effort: omit unknown).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_economics_headers_absent_on_pure_read(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """A pure read (``GET /agents``) involves no LLM spend → the cost + token
    headers are OMITTED (never a misleading zero). The correlation echo still
    rides along."""
    r = client.get("/agents", headers=auth_header)
    assert r.status_code == 200
    assert ECONOMICS_HEADER_COST not in r.headers
    assert ECONOMICS_HEADER_TOKENS_IN not in r.headers
    assert ECONOMICS_HEADER_TOKENS_OUT not in r.headers
    # No economics object set → cache header omitted too (unknown, not "none").
    assert ECONOMICS_HEADER_CACHE not in r.headers
    # But the namespaced correlation echo is on EVERY response.
    assert r.headers[ECONOMICS_HEADER_REQUEST_ID]


# ---------------------------------------------------------------------------
# Correlation echo on every response (success + error + unauthed).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_request_id_echo_on_success_and_matches_canonical(client: TestClient) -> None:
    """``X-MDK-Request-Id`` is an additive ALIAS of ``X-Request-Id`` — same
    value, the original header untouched."""
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.headers[ECONOMICS_HEADER_REQUEST_ID] == r.headers[REQUEST_ID_HEADER]


@pytest.mark.unit
def test_request_id_echo_on_unauthed_error(client: TestClient) -> None:
    """Even a 401 (no economics, no auth) carries the namespaced correlation
    echo, matching the canonical header + envelope id."""
    r = client.post("/run", json={"kind": "agent", "target": "x", "input": {}})
    assert r.status_code == 401
    rid = r.headers[ECONOMICS_HEADER_REQUEST_ID]
    assert rid == r.headers[REQUEST_ID_HEADER]
    assert rid == r.json()["detail"]["error"]["request_id"]


# ---------------------------------------------------------------------------
# Rate-limit headers — present on authed responses; Retry-After on 429.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ratelimit_headers_present_on_authed_response(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """Every authed response carries the ``X-RateLimit-*`` budget snapshot so a
    client can pace proactively (even with the limiter at its NoOp default)."""
    r = client.get("/agents", headers=auth_header)
    assert r.status_code == 200
    assert "X-RateLimit-Limit" in r.headers
    assert "X-RateLimit-Remaining" in r.headers
    assert "X-RateLimit-Reset" in r.headers


@pytest.mark.unit
async def test_retry_after_on_429() -> None:
    """Driving the limiter to reject yields a 429 WITH ``Retry-After`` +
    ``X-RateLimit-*`` so a client can recover programmatically."""
    s = InMemoryStorage()
    await s.init()
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="rl")
    await s.save_api_key(minted.record)
    client = TestClient(build_app(s, rate_limit_per_minute=1))
    token = {"Authorization": f"Bearer {minted.full_key}"}
    assert client.get("/agents", headers=token).status_code == 200
    r = client.get("/agents", headers=token)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1
    assert r.headers["X-RateLimit-Limit"] == "1"
    assert r.headers["X-RateLimit-Remaining"] == "0"
    assert int(r.headers["X-RateLimit-Reset"]) > 0
    # Correlation echo present even on the 429.
    assert r.headers[ECONOMICS_HEADER_REQUEST_ID] == r.headers[REQUEST_ID_HEADER]


# ---------------------------------------------------------------------------
# Best-effort omission — unit-level: only known fields emit.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_partial_economics_only_emits_known_headers(
    storage: InMemoryStorage, agents_path: Path
) -> None:
    """A handler that knows only the cache disposition (no cost/tokens) emits
    ONLY ``X-MDK-Cache`` — the cost/token headers stay omitted. Proven by
    mounting a tiny probe route that sets a partial ``ResponseEconomics``."""
    app = build_app(storage, agents_path=agents_path)

    @app.get("/_probe_partial")
    async def _probe_partial(request: Request) -> dict[str, bool]:
        set_response_economics(request, ResponseEconomics(cache="miss"))
        return {"ok": True}

    client = TestClient(app)
    r = client.get("/_probe_partial")
    assert r.status_code == 200
    assert r.headers[ECONOMICS_HEADER_CACHE] == "miss"
    assert ECONOMICS_HEADER_COST not in r.headers
    assert ECONOMICS_HEADER_TOKENS_IN not in r.headers
    assert ECONOMICS_HEADER_TOKENS_OUT not in r.headers


@pytest.mark.unit
def test_invalid_cache_value_is_omitted(storage: InMemoryStorage, agents_path: Path) -> None:
    """A cache value outside {hit,miss,none} is treated as unknown → omitted
    (defensive: the middleware never emits a junk cache disposition)."""
    app = build_app(storage, agents_path=agents_path)

    @app.get("/_probe_badcache")
    async def _probe_badcache(request: Request) -> dict[str, bool]:
        set_response_economics(request, ResponseEconomics(cache="bogus"))
        return {"ok": True}

    client = TestClient(app)
    r = client.get("/_probe_badcache")
    assert r.status_code == 200
    assert ECONOMICS_HEADER_CACHE not in r.headers
