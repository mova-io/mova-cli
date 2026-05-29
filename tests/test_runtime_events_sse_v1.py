"""HTTP runtime — ``GET /api/v1/events/stream`` SSE event stream (ADR 035 D3).

Coverage:

* **Transport-shape** tests via ``TestClient``:

  * 401 unauthenticated, 403 without ``read`` scope.
  * OpenAPI documents the route with ``text/event-stream``.
  * Per-tenant connection cap rejects an over-cap caller with ``503``.
* **Generator-behavior** tests drive the extracted
  :func:`_events_sse_generator` async generator directly with an
  injected ``is_disconnected`` predicate — TestClient can't gracefully
  shut down an infinite stream + buffers the response, so we'd never
  see the live-push path. Driving the generator hits the same code
  path the endpoint runs, with deterministic teardown:

  * Live push — events recorded AFTER the loop starts surface as
    ``id: <event_id>\\ndata: <EventView JSON>\\n\\n`` frames.
  * ``Last-Event-ID`` resumption — replays the events recorded since
    the cursor.
  * ``since=`` replay — replays matching events before going live.
  * Heartbeat — emits ``:keepalive`` when nothing happens.
  * Disconnect — flipping the predicate to True exits the loop cleanly.

Hermetic: no agents on disk, no worker process; tests seed the outbox by
appending to ``storage.events`` directly (mirrors the D1 contract test).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.core.events import Event, EventKind
from movate.runtime import build_app
from movate.runtime.app import _events_sse_generator
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
def app(storage: InMemoryStorage):
    return build_app(storage)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


async def _mint(
    storage: InMemoryStorage,
    *,
    scopes: list[str],
    tenant_id: str | None = None,
) -> tuple[str, str]:
    """Mint a key with the given scopes; return ``(tenant_id, bearer header)``."""
    tid = tenant_id or uuid4().hex
    minted = mint_api_key(tenant_id=tid, env=ApiKeyEnv.LIVE, label="sse-tests", scopes=scopes)
    await storage.save_api_key(minted.record)
    return tid, f"Bearer {minted.full_key}"


def _seed_event(
    storage: InMemoryStorage,
    *,
    tenant_id: str,
    kind: str = EventKind.RUN_COMPLETED.value,
    subject: str = "faq-agent",
    data: dict | None = None,
    created_at: datetime | None = None,
) -> Event:
    """Append an event to the in-memory outbox. Bypasses ``record_event``
    so ``created_at`` is controllable (mirrors D1 test helper)."""
    e = Event(
        tenant_id=tenant_id,
        kind=kind,
        subject=subject,
        data=data or {},
        created_at=created_at or datetime.now(UTC),
    )
    storage.events.append(e)
    return e


def _parse_sse_frames(body: str) -> list[dict]:
    """Parse the SSE byte stream into ``[{"id": .., "data": ..}, ...]``.

    Each well-formed event frame is ``id: <id>\\ndata: <json>\\n\\n``;
    heartbeats (``:keepalive\\n\\n``) carry no data line and are
    filtered out here — the heartbeat test inspects the raw text."""
    frames: list[dict] = []
    cur: dict[str, str] = {}
    for line in body.split("\n"):
        if line == "":
            if cur:
                frames.append(cur)
            cur = {}
            continue
        if line.startswith(":"):  # SSE comment (heartbeat)
            continue
        if line.startswith("id:"):
            cur["id"] = line[len("id:") :].lstrip(" ")
        elif line.startswith("data:"):
            cur["data"] = line[len("data:") :].lstrip(" ")
    return frames


async def _collect_until(
    gen,
    *,
    stop_pred,
    max_iterations: int = 200,
) -> str:
    """Drain an SSE generator into a single string, stopping when
    ``stop_pred(body)`` returns True (or when ``max_iterations`` worth of
    frames have been collected, as a runaway-test safety net).

    The caller drives the disconnect predicate; this helper just turns
    the async stream into a buffer for assertion.
    """
    body = ""
    n = 0
    async for frame in gen:
        body += frame
        n += 1
        if stop_pred(body):
            break
        if n >= max_iterations:
            break
    # Close the generator explicitly so its ``finally`` runs.
    await gen.aclose()
    return body


class _Disconnect:
    """Mutable disconnect flag — flip ``.disconnected`` to terminate
    the generator on its next iteration. Async-callable so it slots
    into the ``is_disconnected`` predicate seam."""

    def __init__(self) -> None:
        self.disconnected = False
        self.call_count = 0

    async def __call__(self) -> bool:
        self.call_count += 1
        return self.disconnected


# ---------------------------------------------------------------------------
# Transport-shape: auth + OpenAPI + cap
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_401_without_bearer(client: TestClient) -> None:
    resp = client.get("/api/v1/events/stream")
    assert resp.status_code == 401


@pytest.mark.unit
async def test_403_without_read_scope(client: TestClient, storage: InMemoryStorage) -> None:
    _tenant, bearer = await _mint(storage, scopes=["run"])  # no "read"
    resp = client.get("/api/v1/events/stream", headers={"Authorization": bearer})
    assert resp.status_code == 403


@pytest.mark.unit
def test_openapi_advertises_text_event_stream(app) -> None:
    """The generated OpenAPI spec advertises ``text/event-stream`` so
    front-end TypeScript codegen knows it's an SSE endpoint."""
    spec = app.openapi()
    entry = spec["paths"]["/api/v1/events/stream"]["get"]
    content = entry["responses"]["200"]["content"]
    assert "text/event-stream" in content


@pytest.mark.unit
async def test_per_tenant_cap_rejects_over_cap_with_503(
    app, client: TestClient, storage: InMemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the per-tenant SSE connection counter is at the cap, a new
    connection is rejected with 503. The advisory cap protects the pool."""
    tenant_id, bearer = await _mint(storage, scopes=["read"])
    monkeypatch.setenv("MDK_EVENTS_SSE_MAX_PER_TENANT", "1")
    counts: dict[str, int] = app.state.events_sse_connections
    counts[tenant_id] = 1  # simulate one active subscriber
    try:
        resp = client.get("/api/v1/events/stream", headers={"Authorization": bearer})
        assert resp.status_code == 503
    finally:
        counts[tenant_id] = 0


# ---------------------------------------------------------------------------
# Generator behavior — driven directly with an injected disconnect predicate
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_live_push_emits_recorded_events(storage: InMemoryStorage) -> None:
    """An event recorded AFTER the live loop starts surfaces as a
    well-formed SSE frame."""
    tenant_id = uuid4().hex
    dc = _Disconnect()
    # Background coroutine: wait a beat, then push an event into the
    # outbox so the polling loop picks it up.
    seeded_id: dict[str, str] = {}

    async def _seed_after_start() -> None:
        await asyncio.sleep(0.02)
        e = _seed_event(storage, tenant_id=tenant_id, subject="run-live")
        seeded_id["id"] = e.id

    seeder = asyncio.create_task(_seed_after_start())
    try:
        gen = _events_sse_generator(
            store=storage,
            target_tenant=tenant_id,
            kind=None,
            subject=None,
            since=None,
            last_event_id=None,
            poll_interval_s=0.005,
            heartbeat_interval_s=10.0,  # high → no heartbeats during this test
            is_disconnected=dc,
        )
        body = await _collect_until(
            gen,
            stop_pred=lambda b: seeded_id.get("id") is not None and seeded_id["id"] in b,
        )
    finally:
        await seeder

    frames = _parse_sse_frames(body)
    ids = [f.get("id") for f in frames]
    assert seeded_id["id"] in ids
    payload = json.loads(next(f["data"] for f in frames if f.get("id") == seeded_id["id"]))
    assert payload["id"] == seeded_id["id"]
    assert payload["subject"] == "run-live"
    assert payload["kind"] == EventKind.RUN_COMPLETED.value


@pytest.mark.unit
async def test_no_backfill_by_default(storage: InMemoryStorage) -> None:
    """Without ``since`` / ``Last-Event-ID``, events recorded BEFORE the
    loop starts are not replayed — the stream is "from now on"."""
    tenant_id = uuid4().hex
    old = _seed_event(
        storage,
        tenant_id=tenant_id,
        subject="old",
        created_at=datetime.now(UTC) - timedelta(seconds=30),
    )
    # Tiny pause so the generator's `live_since = now()` anchor is
    # AFTER the seeded "old" event's created_at.
    await asyncio.sleep(0.01)
    dc = _Disconnect()
    new_id: dict[str, str] = {}

    async def _seed_new() -> None:
        await asyncio.sleep(0.02)
        e = _seed_event(storage, tenant_id=tenant_id, subject="new")
        new_id["id"] = e.id

    seeder = asyncio.create_task(_seed_new())
    try:
        gen = _events_sse_generator(
            store=storage,
            target_tenant=tenant_id,
            kind=None,
            subject=None,
            since=None,
            last_event_id=None,
            poll_interval_s=0.005,
            heartbeat_interval_s=10.0,
            is_disconnected=dc,
        )
        body = await _collect_until(
            gen, stop_pred=lambda b: new_id.get("id") is not None and new_id["id"] in b
        )
    finally:
        await seeder

    ids = {f.get("id") for f in _parse_sse_frames(body)}
    assert new_id["id"] in ids
    assert old.id not in ids


@pytest.mark.unit
async def test_last_event_id_replays_missed_events(storage: InMemoryStorage) -> None:
    """Pre-seed a cursor + two events after it. A generator started
    with ``last_event_id=<cursor>`` replays the two missed events before
    going live — SSE-standard resumption."""
    tenant_id = uuid4().hex
    t0 = datetime.now(UTC) - timedelta(seconds=10)
    cursor = _seed_event(storage, tenant_id=tenant_id, subject="seen", created_at=t0)
    missed_1 = _seed_event(
        storage,
        tenant_id=tenant_id,
        subject="missed-1",
        created_at=t0 + timedelta(seconds=1),
    )
    missed_2 = _seed_event(
        storage,
        tenant_id=tenant_id,
        subject="missed-2",
        created_at=t0 + timedelta(seconds=2),
    )
    dc = _Disconnect()
    gen = _events_sse_generator(
        store=storage,
        target_tenant=tenant_id,
        kind=None,
        subject=None,
        since=None,
        last_event_id=cursor.id,
        poll_interval_s=0.005,
        heartbeat_interval_s=10.0,
        is_disconnected=dc,
    )
    body = await _collect_until(
        gen,
        stop_pred=lambda b: missed_1.id in b and missed_2.id in b,
    )
    ids = [f.get("id") for f in _parse_sse_frames(body)]
    assert missed_1.id in ids
    assert missed_2.id in ids
    # The already-delivered cursor is NOT replayed (avoids duplicate
    # delivery to a resuming client).
    assert cursor.id not in ids
    # Replay is oldest-first.
    assert ids.index(missed_1.id) < ids.index(missed_2.id)


@pytest.mark.unit
async def test_since_replays_matching_events(storage: InMemoryStorage) -> None:
    """``since=<ts>`` triggers a one-shot replay of matching events
    (at-or-after the timestamp) before the live loop starts."""
    tenant_id = uuid4().hex
    t0 = datetime.now(UTC) - timedelta(seconds=10)
    old = _seed_event(
        storage,
        tenant_id=tenant_id,
        subject="old",
        created_at=t0 - timedelta(seconds=5),
    )
    target = _seed_event(
        storage,
        tenant_id=tenant_id,
        subject="target",
        created_at=t0 + timedelta(seconds=1),
    )
    dc = _Disconnect()
    gen = _events_sse_generator(
        store=storage,
        target_tenant=tenant_id,
        kind=None,
        subject=None,
        since=t0,
        last_event_id=None,
        poll_interval_s=0.005,
        heartbeat_interval_s=10.0,
        is_disconnected=dc,
    )
    body = await _collect_until(gen, stop_pred=lambda b: target.id in b)
    ids = {f.get("id") for f in _parse_sse_frames(body)}
    assert target.id in ids
    assert old.id not in ids


@pytest.mark.unit
async def test_heartbeat_emitted_when_idle(storage: InMemoryStorage) -> None:
    """With nothing happening, the generator emits ``:keepalive`` on the
    heartbeat tick (driven here by a 20ms test-only interval)."""
    tenant_id = uuid4().hex
    dc = _Disconnect()
    gen = _events_sse_generator(
        store=storage,
        target_tenant=tenant_id,
        kind=None,
        subject=None,
        since=None,
        last_event_id=None,
        poll_interval_s=0.005,
        heartbeat_interval_s=0.02,
        is_disconnected=dc,
    )
    body = await _collect_until(gen, stop_pred=lambda b: ":keepalive" in b)
    assert ":keepalive" in body


@pytest.mark.unit
async def test_disconnect_exits_loop_cleanly(storage: InMemoryStorage) -> None:
    """Flipping the disconnect predicate to True between iterations
    exits the generator without raising. Mirrors a client TCP drop."""
    tenant_id = uuid4().hex
    dc = _Disconnect()
    gen = _events_sse_generator(
        store=storage,
        target_tenant=tenant_id,
        kind=None,
        subject=None,
        since=None,
        last_event_id=None,
        poll_interval_s=0.005,
        heartbeat_interval_s=10.0,
        is_disconnected=dc,
    )

    # Schedule the disconnect flip shortly after the loop starts.
    async def _flip() -> None:
        await asyncio.sleep(0.02)
        dc.disconnected = True

    flipper = asyncio.create_task(_flip())
    try:
        # Drain until the generator naturally exits (returns).
        body = ""
        n = 0
        async for frame in gen:
            body += frame
            n += 1
            if n > 500:  # safety net
                break
    finally:
        await flipper
        await gen.aclose()

    # The predicate was consulted at least once; the loop exited
    # without a CancelledError reaching the caller.
    assert dc.call_count >= 1


@pytest.mark.unit
async def test_filter_by_kind_and_subject(storage: InMemoryStorage) -> None:
    """``kind`` + ``subject`` filters apply to the live loop — only
    matching events surface as frames."""
    tenant_id = uuid4().hex
    matched_id: dict[str, str] = {}
    unmatched_ids: dict[str, list[str]] = {"ids": []}

    async def _seed() -> None:
        await asyncio.sleep(0.02)
        wrong_kind = _seed_event(
            storage,
            tenant_id=tenant_id,
            kind="run.failed",
            subject="faq-agent",
        )
        wrong_subject = _seed_event(
            storage,
            tenant_id=tenant_id,
            kind="run.completed",
            subject="other",
        )
        matched = _seed_event(
            storage,
            tenant_id=tenant_id,
            kind="run.completed",
            subject="faq-agent",
        )
        matched_id["id"] = matched.id
        unmatched_ids["ids"] = [wrong_kind.id, wrong_subject.id]

    seeder = asyncio.create_task(_seed())
    dc = _Disconnect()
    try:
        gen = _events_sse_generator(
            store=storage,
            target_tenant=tenant_id,
            kind="run.completed",
            subject="faq-agent",
            since=None,
            last_event_id=None,
            poll_interval_s=0.005,
            heartbeat_interval_s=10.0,
            is_disconnected=dc,
        )
        body = await _collect_until(
            gen,
            stop_pred=lambda b: matched_id.get("id") is not None and matched_id["id"] in b,
        )
    finally:
        await seeder

    ids = {f.get("id") for f in _parse_sse_frames(body)}
    assert matched_id["id"] in ids
    for uid in unmatched_ids["ids"]:
        assert uid not in ids


@pytest.mark.unit
async def test_tenant_scoping_isolates_outbox(storage: InMemoryStorage) -> None:
    """The generator scopes by ``target_tenant`` — events recorded
    against a different tenant never surface, even if the storage
    holds them. Mirrors the GET /api/v1/events contract."""
    tenant_a = uuid4().hex
    tenant_b = uuid4().hex
    own_id: dict[str, str] = {}

    async def _seed() -> None:
        await asyncio.sleep(0.02)
        other = _seed_event(storage, tenant_id=tenant_b, subject="other")
        own = _seed_event(storage, tenant_id=tenant_a, subject="own")
        own_id["id"] = own.id
        own_id["other_id"] = other.id

    seeder = asyncio.create_task(_seed())
    dc = _Disconnect()
    try:
        gen = _events_sse_generator(
            store=storage,
            target_tenant=tenant_a,
            kind=None,
            subject=None,
            since=None,
            last_event_id=None,
            poll_interval_s=0.005,
            heartbeat_interval_s=10.0,
            is_disconnected=dc,
        )
        body = await _collect_until(
            gen,
            stop_pred=lambda b: own_id.get("id") is not None and own_id["id"] in b,
        )
    finally:
        await seeder

    ids = {f.get("id") for f in _parse_sse_frames(body)}
    assert own_id["id"] in ids
    assert own_id["other_id"] not in ids
