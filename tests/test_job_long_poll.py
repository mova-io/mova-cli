"""Universal ``?wait=`` long-poll on the async-job GET endpoints.

A client that submitted an async job can pass ``?wait=30s`` to
``GET /jobs/{id}`` (or its ``/api/v1`` alias) to block until the job is
terminal instead of poll-looping. These tests pin the contract:

* An already-terminal job returns IMMEDIATELY regardless of ``wait``.
* A still-running job + ``wait`` against a job that flips terminal mid-wait
  returns the terminal state (driven by a background task advancing the row).
* A timeout returns HTTP 200 + the current (running) state + the
  ``X-MDK-Poll-Timeout`` header.
* ``wait`` over the server max is clamped + reports ``X-MDK-Wait-Clamped``.
* Client disconnect mid-wait exits cleanly (no full-deadline pin).
* ``wait`` parsing rejects garbage with 400.
* Both job GET routes document the ``wait`` param in OpenAPI.
* Omitting ``wait`` behaves EXACTLY as before (back-compat).

Hermetic: :class:`InMemoryStorage`, no real socket (``httpx.ASGITransport``),
no real time waited for terminal/flip cases (the poll interval is shrunk and
the flip is driven in the same event loop).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest

from movate.core.auth import ALL_SCOPES, mint_api_key
from movate.core.models import ApiKeyEnv, JobKind, JobRecord, JobStatus
from movate.runtime import build_app
from movate.runtime import long_poll as lp
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
async def minted(storage: InMemoryStorage):
    """A persisted all-scopes API key + its bearer header."""
    tenant_id = uuid4().hex
    key = mint_api_key(
        tenant_id=tenant_id,
        env=ApiKeyEnv.LIVE,
        label="long-poll-tests",
        scopes=list(ALL_SCOPES),
    )
    await storage.save_api_key(key.record)
    return key


@pytest.fixture
def app(storage: InMemoryStorage):
    return build_app(storage)


def _auth(key) -> dict[str, str]:
    return {"Authorization": f"Bearer {key.full_key}"}


async def _seed_job(
    storage: InMemoryStorage,
    *,
    tenant_id: str,
    status: JobStatus,
    job_id: str | None = None,
) -> str:
    job_id = job_id or uuid4().hex
    await storage.save_job(
        JobRecord(
            job_id=job_id,
            tenant_id=tenant_id,
            kind=JobKind.AGENT,
            target="demo",
            status=status,
            input={"x": 1},
        )
    )
    return job_id


def _async_client(app, key) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="http://test",
        transport=httpx.ASGITransport(app=app),
        headers=_auth(key),
        # Generous client read timeout so the SERVER's wait deadline (not the
        # httpx client) is always what ends a long-poll.
        timeout=30.0,
    )


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the poll interval so the running/flip/timeout cases finish in
    milliseconds, not the production 0.5s cadence."""
    monkeypatch.setattr(lp, "POLL_INTERVAL_SECONDS", 0.01)


# ---------------------------------------------------------------------------
# parse_duration / resolve_wait — pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("30s", 30.0),
        ("2m", 120.0),
        ("1h", 3600.0),
        ("90", 90.0),  # bare int → seconds
        ("0s", 0.0),
        (" 15s ", 15.0),  # surrounding whitespace tolerated
        ("45S", 45.0),  # case-insensitive unit
    ],
)
def test_parse_duration_valid(raw: str, expected: float) -> None:
    assert lp.parse_duration(raw) == expected


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["", "abc", "30sx", "-5s", "1.5s", "s", "30 s s"])
def test_parse_duration_rejects_garbage(raw: str) -> None:
    with pytest.raises(lp.DurationParseError):
        lp.parse_duration(raw)


@pytest.mark.unit
def test_resolve_wait_none_is_zero_no_clamp() -> None:
    # Omitting wait → (0, False): return-immediately, no header.
    assert lp.resolve_wait(None) == (0.0, False)


@pytest.mark.unit
def test_resolve_wait_clamps_over_max() -> None:
    seconds, clamped = lp.resolve_wait("999s")
    assert seconds == lp.MAX_WAIT_SECONDS
    assert clamped is True


@pytest.mark.unit
def test_resolve_wait_under_max_not_clamped() -> None:
    assert lp.resolve_wait("10s") == (10.0, False)


# ---------------------------------------------------------------------------
# Already-terminal → returns immediately regardless of wait
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("path", ["/jobs/{}", "/api/v1/jobs/{}"])
async def test_terminal_job_returns_immediately(
    app, minted, storage: InMemoryStorage, path: str
) -> None:
    job_id = await _seed_job(storage, tenant_id=minted.record.tenant_id, status=JobStatus.SUCCESS)
    async with _async_client(app, minted) as client:
        # wait=10s but the job is already done — must NOT block.
        resp = await asyncio.wait_for(
            client.get(path.format(job_id), params={"wait": "10s"}),
            timeout=2.0,
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    # Terminal on the first read → no timeout header.
    assert lp.HEADER_POLL_TIMEOUT not in resp.headers


# ---------------------------------------------------------------------------
# Running job that flips terminal mid-wait → returns the terminal state
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("path", ["/jobs/{}", "/api/v1/jobs/{}"])
async def test_flips_terminal_mid_wait(app, minted, storage: InMemoryStorage, path: str) -> None:
    tenant_id = minted.record.tenant_id
    job_id = await _seed_job(storage, tenant_id=tenant_id, status=JobStatus.RUNNING)

    async def _flip_after_delay() -> None:
        # Let the GET enter its poll loop, then advance the row to terminal —
        # simulating a (possibly out-of-process) worker finishing the job.
        await asyncio.sleep(0.05)
        await storage.update_job(job_id, tenant_id=tenant_id, status=JobStatus.SUCCESS)

    async with _async_client(app, minted) as client:
        flip = asyncio.create_task(_flip_after_delay())
        resp = await asyncio.wait_for(
            client.get(path.format(job_id), params={"wait": "5s"}),
            timeout=3.0,
        )
        await flip

    assert resp.status_code == 200
    # Saw the terminal transition, not the initial running state.
    assert resp.json()["status"] == "success"
    assert lp.HEADER_POLL_TIMEOUT not in resp.headers


# ---------------------------------------------------------------------------
# Timeout → 200 + running state + X-MDK-Poll-Timeout
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_timeout_returns_running_with_header(app, minted, storage: InMemoryStorage) -> None:
    # A job that never flips; a tiny wait so the deadline fires fast.
    job_id = await _seed_job(storage, tenant_id=minted.record.tenant_id, status=JobStatus.RUNNING)
    async with _async_client(app, minted) as client:
        resp = await asyncio.wait_for(
            client.get(f"/jobs/{job_id}", params={"wait": "1s"}),
            timeout=5.0,
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"
    assert resp.headers.get(lp.HEADER_POLL_TIMEOUT) == "true"


# ---------------------------------------------------------------------------
# wait over the server max → clamped + X-MDK-Wait-Clamped header
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_wait_over_max_is_clamped_with_header(app, minted, storage: InMemoryStorage) -> None:
    # Terminal job so the request returns at once — we only assert the clamp
    # header, which is stamped before any waiting happens.
    job_id = await _seed_job(storage, tenant_id=minted.record.tenant_id, status=JobStatus.SUCCESS)
    async with _async_client(app, minted) as client:
        resp = await client.get(f"/jobs/{job_id}", params={"wait": "999s"})
    assert resp.status_code == 200
    assert resp.headers.get(lp.HEADER_WAIT_CLAMPED) == f"{int(lp.MAX_WAIT_SECONDS)}s"


@pytest.mark.unit
async def test_wait_under_max_has_no_clamp_header(app, minted, storage: InMemoryStorage) -> None:
    job_id = await _seed_job(storage, tenant_id=minted.record.tenant_id, status=JobStatus.SUCCESS)
    async with _async_client(app, minted) as client:
        resp = await client.get(f"/jobs/{job_id}", params={"wait": "5s"})
    assert resp.status_code == 200
    assert lp.HEADER_WAIT_CLAMPED not in resp.headers


# ---------------------------------------------------------------------------
# Invalid wait → 400
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_invalid_wait_is_400(app, minted, storage: InMemoryStorage) -> None:
    job_id = await _seed_job(storage, tenant_id=minted.record.tenant_id, status=JobStatus.RUNNING)
    async with _async_client(app, minted) as client:
        resp = await client.get(f"/jobs/{job_id}", params={"wait": "soon"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"]["code"] == "bad_request"


# ---------------------------------------------------------------------------
# Client disconnect mid-wait → exits cleanly (no full-deadline pin)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_client_disconnect_exits_cleanly(storage: InMemoryStorage) -> None:
    """Drive ``long_poll_job`` directly with a request whose
    ``is_disconnected()`` returns True so we exercise the disconnect branch
    deterministically (a real ASGI disconnect is awkward to force)."""
    tenant_id = uuid4().hex
    job_id = await _seed_job(storage, tenant_id=tenant_id, status=JobStatus.RUNNING)

    request = MagicMock()
    # First poll iteration observes the disconnect and bails.
    request.is_disconnected = AsyncMock(return_value=True)
    response = MagicMock()
    response.headers = {}

    # A long deadline: if disconnect were ignored, this would hang well past
    # the test's wait_for guard.
    record = await asyncio.wait_for(
        lp.long_poll_job(
            job_id=job_id,
            tenant_id=tenant_id,
            store=storage,
            request=request,
            response=response,
            wait_raw="30s",
        ),
        timeout=2.0,
    )
    assert record is not None
    assert record.status is JobStatus.RUNNING
    # Bailed on disconnect, not on the deadline → no timeout header stamped.
    assert lp.HEADER_POLL_TIMEOUT not in response.headers
    request.is_disconnected.assert_awaited()


# ---------------------------------------------------------------------------
# Back-compat — omitting wait behaves exactly as before
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("path", ["/jobs/{}", "/api/v1/jobs/{}"])
async def test_no_wait_returns_running_immediately(
    app, minted, storage: InMemoryStorage, path: str
) -> None:
    job_id = await _seed_job(storage, tenant_id=minted.record.tenant_id, status=JobStatus.RUNNING)
    async with _async_client(app, minted) as client:
        resp = await asyncio.wait_for(
            client.get(path.format(job_id)),  # no wait param
            timeout=2.0,
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"
    # No long-poll happened → neither header present.
    assert lp.HEADER_POLL_TIMEOUT not in resp.headers
    assert lp.HEADER_WAIT_CLAMPED not in resp.headers


@pytest.mark.unit
async def test_no_wait_unknown_job_still_404(app, minted) -> None:
    async with _async_client(app, minted) as client:
        resp = await client.get("/jobs/no-such-id")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Contract — the wait param is documented in OpenAPI for both job routes
# ---------------------------------------------------------------------------


def _wait_param(openapi: dict[str, Any], route: str) -> dict[str, Any] | None:
    params = openapi["paths"][route]["get"].get("parameters", [])
    return next((p for p in params if p["name"] == "wait" and p["in"] == "query"), None)


@pytest.mark.unit
@pytest.mark.parametrize("route", ["/jobs/{job_id}", "/api/v1/jobs/{job_id}"])
def test_openapi_documents_wait_param(app, route: str) -> None:
    schema = app.openapi()
    param = _wait_param(schema, route)
    assert param is not None, f"wait param missing from OpenAPI for GET {route}"
    # Optional (no `required: true`) → omitting it stays back-compatible.
    assert param.get("required", False) is False
    assert param.get("description"), "wait param should carry a description"
