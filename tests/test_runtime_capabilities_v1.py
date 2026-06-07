"""Tests for capability discovery — GET /api/v1/capabilities.

The endpoint's whole value is that it reflects REALITY: a client (e.g. Mova
iO fanning out to many heterogeneous customer runtimes) learns exactly what
THIS build supports. So the tests pin not just the shape but the *honesty*:

* The full view (read scope) carries models / features / scopes / limits /
  extras, all derived from the deployed runtime.
* An unauthenticated request gets the MINIMAL subset (version + api_version),
  never a 401 — it's a probe-friendly endpoint.
* A valid bearer WITHOUT ``read`` also degrades to minimal (not a 403).
* Feature flags are NOT a static dict: register / deregister a route on a bare
  app and assert the flag flips. This is the load-bearing "not hardcoded"
  proof.
* BYOK reports provider NAMES, never the key values.
* This tenant's effective limits surface from live config.
* Contract: the route + the meta tag are pinned.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.core.provider_keys import mint_tenant_provider_key
from movate.providers.model_catalog import model_catalog
from movate.runtime import build_app
from movate.runtime.capabilities import detect_extras, detect_features
from movate.testing import InMemoryStorage


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
    # An explicit rate-limit so the `limits` block has a concrete value to
    # assert against (not the NoOp/None path).
    return TestClient(build_app(storage, agents_path=agents_path, rate_limit_per_minute=600))


@pytest.fixture
async def read_auth(storage: InMemoryStorage):
    """A key with the legacy default scopes (which include ``read``)."""
    tenant_id = uuid4().hex
    minted = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="caps-tests")
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}, tenant_id


# ---------------------------------------------------------------------------
# Full (read-scoped) view
# ---------------------------------------------------------------------------


def test_full_view_shape(client: TestClient, read_auth) -> None:
    header, _ = read_auth
    r = client.get("/api/v1/capabilities", headers=header)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["minimal"] is False
    assert body["api_version"] == "v1"
    assert isinstance(body["mdk_version"], str) and body["mdk_version"]
    assert body["served_at"]  # ISO timestamp present

    # Every richer block is populated in the full view.
    assert body["models"] is not None
    assert body["features"] is not None
    assert body["scopes_supported"] is not None
    assert body["limits"] is not None
    assert body["extras_installed"] is not None


def test_full_view_resources_reflect_route_table(client: TestClient, read_auth) -> None:
    """The ``resources`` block enumerates the manageable surface, derived from
    the live route table — agents/projects/kb are fully managed, skills is
    create-only (``managed: false``), and contexts is absent until its API
    ships (ADR 060)."""
    header, _ = read_auth
    body = client.get("/api/v1/capabilities", headers=header).json()

    resources = body["resources"]
    assert resources is not None
    by_name = {r["name"]: r for r in resources}

    # Agents / projects / KB are fully managed here.
    for name in ("agents", "projects", "kb"):
        assert name in by_name, f"{name} missing from resources"
        assert by_name[name]["managed"] is True
        assert by_name[name]["path"].startswith("/api/v1/")
        assert by_name[name]["operations"]  # non-empty

    # Skills exists but is create-only on this build → managed=false, and the
    # honest operation list reflects exactly that.
    assert by_name["skills"]["operations"] == ["create"]
    assert by_name["skills"]["managed"] is False

    # Contexts has no API on this build → omitted entirely (not promised).
    assert "contexts" not in by_name


def test_minimal_view_omits_resources(client: TestClient) -> None:
    """The unauthenticated minimal probe carries no ``resources`` block."""
    body = client.get("/api/v1/capabilities").json()
    assert body["minimal"] is True
    assert body["resources"] is None


def test_full_view_models_match_catalog(client: TestClient, read_auth) -> None:
    header, _ = read_auth
    body = client.get("/api/v1/capabilities", headers=header).json()
    available = body["models"]["available"]
    # Same model set the shared catalog (and GET /api/v1/models) uses.
    assert available == [info.model_id for info in model_catalog()]


def test_full_view_scopes_match_vocabulary(client: TestClient, read_auth) -> None:
    header, _ = read_auth
    body = client.get("/api/v1/capabilities", headers=header).json()
    assert body["scopes_supported"] == sorted(ALL_SCOPES)
    # The runtime's actual scope vocabulary (ADR 013) — read/admin/fleet-admin
    # are the load-bearing ones a client branches on.
    for scope in ("read", "admin", "fleet-admin"):
        assert scope in body["scopes_supported"]


def test_full_view_limits_from_live_config(client: TestClient, read_auth) -> None:
    header, _ = read_auth
    body = client.get("/api/v1/capabilities", headers=header).json()
    limits = body["limits"]
    # The fixture built the app with rate_limit_per_minute=600.
    assert limits["rate_limit_per_min"] == 600
    # Per-tenant limiter is OFF by default → None.
    assert limits["tenant_rate_limit_per_min"] is None
    # Batch cap is the server-enforced default (10_000) absent an override.
    assert limits["max_batch_size"] == 10_000


def test_tenant_limit_surfaced_when_configured(
    storage: InMemoryStorage, agents_path: Path, read_auth
) -> None:
    header, _ = read_auth
    app = build_app(
        storage,
        agents_path=agents_path,
        rate_limit_per_minute=120,
        tenant_rate_limit_per_minute=5000,
    )
    body = TestClient(app).get("/api/v1/capabilities", headers=header).json()
    assert body["limits"]["rate_limit_per_min"] == 120
    assert body["limits"]["tenant_rate_limit_per_min"] == 5000


def test_rate_limit_none_when_disabled(
    storage: InMemoryStorage, agents_path: Path, read_auth
) -> None:
    header, _ = read_auth
    app = build_app(storage, agents_path=agents_path, rate_limit_per_minute=None)
    body = TestClient(app).get("/api/v1/capabilities", headers=header).json()
    assert body["limits"]["rate_limit_per_min"] is None


# ---------------------------------------------------------------------------
# Unauthenticated / minimal subset
# ---------------------------------------------------------------------------


def test_unauthenticated_returns_minimal(client: TestClient) -> None:
    r = client.get("/api/v1/capabilities")
    assert r.status_code == 200, r.text  # never 401 — probe-friendly
    body = r.json()
    assert body["minimal"] is True
    assert body["api_version"] == "v1"
    assert body["mdk_version"]
    # The richer fields are withheld in the minimal subset.
    assert body["models"] is None
    assert body["features"] is None
    assert body["scopes_supported"] is None
    assert body["limits"] is None
    assert body["extras_installed"] is None


def test_bad_bearer_returns_minimal(client: TestClient) -> None:
    r = client.get("/api/v1/capabilities", headers={"Authorization": "Bearer not-a-real-key"})
    assert r.status_code == 200, r.text
    assert r.json()["minimal"] is True


def test_valid_bearer_without_read_returns_minimal(
    storage: InMemoryStorage, client: TestClient
) -> None:
    # A key with ONLY the run scope — authenticated, but no `read`.
    import asyncio  # noqa: PLC0415

    tenant_id = uuid4().hex
    minted = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="run-only", scopes=["run"])
    asyncio.get_event_loop().run_until_complete(storage.save_api_key(minted.record))
    r = client.get(
        "/api/v1/capabilities",
        headers={"Authorization": f"Bearer {minted.full_key}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["minimal"] is True


# ---------------------------------------------------------------------------
# BYOK — provider NAMES only, never values
# ---------------------------------------------------------------------------


def test_byok_reports_names_not_values(storage: InMemoryStorage, agents_path: Path) -> None:
    import asyncio  # noqa: PLC0415

    tenant_id = uuid4().hex
    minted = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="byok-test")
    fernet = Fernet(Fernet.generate_key())
    secret_value = "sk-super-secret-do-not-leak-1234567890"
    key = mint_tenant_provider_key(
        tenant_id=tenant_id,
        provider="openai",
        plaintext=secret_value,
        fernet=fernet,
    )

    async def _setup() -> None:
        await storage.save_api_key(minted.record)
        await storage.save_tenant_provider_key(key)

    asyncio.get_event_loop().run_until_complete(_setup())

    app = build_app(storage, agents_path=agents_path)
    r = TestClient(app).get(
        "/api/v1/capabilities",
        headers={"Authorization": f"Bearer {minted.full_key}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["models"]["byok_configured"] == ["openai"]
    # The secret value (and its ciphertext) appear NOWHERE in the response.
    text = r.text
    assert secret_value not in text
    assert key.ciphertext not in text
    assert key.fingerprint not in text


def test_byok_empty_for_tenant_without_keys(client: TestClient, read_auth) -> None:
    header, _ = read_auth
    body = client.get("/api/v1/capabilities", headers=header).json()
    assert body["models"]["byok_configured"] == []


# ---------------------------------------------------------------------------
# Feature flags reflect REALITY (the not-hardcoded proof)
# ---------------------------------------------------------------------------


def test_features_true_when_route_registered(client: TestClient, read_auth) -> None:
    header, _ = read_auth
    body = client.get("/api/v1/capabilities", headers=header).json()
    feats = body["features"]
    # The real runtime registers all of these surfaces.
    assert feats["sse_events"] is True
    assert feats["webhooks"] is True
    assert feats["workflows_api"] is True
    assert feats["provider_keys"] is True
    assert feats["batch_runs"] is True
    # ADR 050 D2 — the one-shot REST voice surface is advertised distinctly from
    # the WS ``voice`` transport (both are wired on the real runtime).
    assert feats["voice"] is True
    assert feats["voice_rest"] is True
    # ADR 045 D13 — run replay / time-travel is wired on the real runtime.
    assert feats["run_replay"] is True


def test_voice_rest_flag_flips_with_route_table() -> None:
    """``voice_rest`` is route-detected: absent on a bare app, True once the
    one-shot POST /agents/{name}/voice APIRoute is registered (ADR 050 D2)."""
    bare = FastAPI()
    assert detect_features(bare)["voice_rest"] is False

    router = APIRouter()

    @router.post("/agents/{name}/voice")
    async def _voice() -> dict[str, str]:  # pragma: no cover - never called
        return {}

    bare.include_router(router)
    assert detect_features(bare)["voice_rest"] is True


def test_run_replay_flag_flips_with_route_table() -> None:
    """``run_replay`` is route-detected: absent on a bare app, True once the
    POST /runs/{run_id}/replay APIRoute is registered (ADR 045 D13)."""
    bare = FastAPI()
    assert detect_features(bare)["run_replay"] is False

    router = APIRouter()

    @router.post("/runs/{run_id}/replay")
    async def _replay() -> dict[str, str]:  # pragma: no cover - never called
        return {}

    bare.include_router(router)
    assert detect_features(bare)["run_replay"] is True


def test_feature_flag_flips_with_route_table() -> None:
    """The load-bearing proof: flags are computed from the live route table.

    A bare app without the SSE-stream route reports ``sse_events: False``;
    registering that exact path flips it to ``True``. If the flag were a
    static dict this assertion pair could not both hold.
    """
    bare = FastAPI()
    assert detect_features(bare)["sse_events"] is False
    assert detect_features(bare)["workflows_api"] is False

    router = APIRouter()

    @router.get("/agents/{name}/runs/stream")
    async def _stream() -> dict[str, str]:  # pragma: no cover - never called
        return {}

    @router.get("/workflow-runs")
    async def _wf() -> dict[str, str]:  # pragma: no cover - never called
        return {}

    bare.include_router(router)
    after = detect_features(bare)
    assert after["sse_events"] is True
    assert after["workflows_api"] is True


def test_features_sorted_and_bool_valued(client: TestClient, read_auth) -> None:
    header, _ = read_auth
    feats = client.get("/api/v1/capabilities", headers=header).json()["features"]
    assert list(feats.keys()) == sorted(feats.keys())
    assert all(isinstance(v, bool) for v in feats.values())


# ---------------------------------------------------------------------------
# Extras detection
# ---------------------------------------------------------------------------


def test_extras_are_importable_only(client: TestClient, read_auth) -> None:
    header, _ = read_auth
    body = client.get("/api/v1/capabilities", headers=header).json()
    extras = body["extras_installed"]
    assert isinstance(extras, list)
    assert extras == sorted(extras)
    # Whatever the endpoint reports must agree with the live import probe.
    assert extras == detect_extras()
    # The test suite needs fastapi (the `runtime` extra) to even build the app.
    assert "runtime" in extras


# ---------------------------------------------------------------------------
# Contract — pin the route + tag
# ---------------------------------------------------------------------------


def test_contract_route_registered(storage: InMemoryStorage, agents_path: Path) -> None:
    app = build_app(storage, agents_path=agents_path)
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/v1/capabilities" in paths
    # Find the route and assert its method + tag.
    route = next(r for r in app.routes if getattr(r, "path", None) == "/api/v1/capabilities")
    assert "GET" in route.methods
    assert "meta-v1" in route.tags


# ---------------------------------------------------------------------------
# MovateClient round-trip (the CLI / Mova iO wire path)
# ---------------------------------------------------------------------------


async def test_client_capabilities_round_trip(storage: InMemoryStorage) -> None:
    """``MovateClient.capabilities()`` parses the full view over the wire."""
    import httpx  # noqa: PLC0415

    from movate.core.client import MovateClient  # noqa: PLC0415

    tenant_id = uuid4().hex
    minted = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="client-caps")
    await storage.save_api_key(minted.record)
    app = build_app(storage, rate_limit_per_minute=300)

    async with MovateClient(
        base_url="http://test",
        api_key=minted.full_key,
        transport=httpx.ASGITransport(app=app),
    ) as client:
        view = await client.capabilities()

    assert view.minimal is False
    assert view.api_version == "v1"
    assert view.features is not None and view.features["batch_runs"] is True
    assert view.limits is not None and view.limits.rate_limit_per_min == 300


async def test_client_capabilities_minimal_without_key(storage: InMemoryStorage) -> None:
    """A keyless client still gets a parseable minimal view (no 401)."""
    import httpx  # noqa: PLC0415

    from movate.core.client import MovateClient  # noqa: PLC0415

    app = build_app(storage)
    # Empty api_key → no Authorization header value the runtime accepts → the
    # endpoint degrades to the minimal subset rather than erroring.
    async with MovateClient(
        base_url="http://test",
        api_key="",
        transport=httpx.ASGITransport(app=app),
    ) as client:
        view = await client.capabilities()

    assert view.minimal is True
    assert view.mdk_version
    assert view.features is None
