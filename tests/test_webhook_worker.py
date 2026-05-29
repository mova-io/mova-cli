"""Webhook delivery worker — cursor advance, kind filter, retries, isolation
(ADR 035 D2).

Hermetic: every HTTP request goes through an ``httpx.MockTransport`` so
no real network is touched. The async ``sleep`` seam is overridden to
a no-op so backoff schedule logic is asserted deterministically (no
real-time delays).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from movate.core.events import Event, EventKind
from movate.core.webhooks import (
    SIGNATURE_HEADER,
    WebhookSubscription,
    verify_signature,
)
from movate.runtime.webhook_worker import (
    WebhookWorker,
    WebhookWorkerConfig,
    encode_payload,
)
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _no_sleep(_: float) -> None:
    """Skip the real-time wait so backoff schedule tests are instant."""
    return None


def _mock_transport(
    handler,
) -> httpx.MockTransport:
    """Build an httpx MockTransport from a callable that takes an
    ``httpx.Request`` and returns an ``httpx.Response``."""
    return httpx.MockTransport(handler)


async def _seed_event(
    storage: InMemoryStorage,
    *,
    tenant_id: str = "tenant-a",
    kind: str = EventKind.RUN_COMPLETED.value,
    subject: str = "faq-agent",
    data: dict | None = None,
    created_at: datetime | None = None,
) -> Event:
    e = Event(
        tenant_id=tenant_id,
        kind=kind,
        subject=subject,
        data=data or {},
        created_at=created_at or datetime.now(UTC),
    )
    storage.events.append(e)
    return e


async def _build_storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


# ---------------------------------------------------------------------------
# Happy path — one delivery
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_delivers_matching_event() -> None:
    storage = await _build_storage()
    sub = WebhookSubscription(
        tenant_id="tenant-a",
        url="https://hook.example/recv",
        kind_filter=["*"],
    )
    await storage.create_webhook(sub)
    event = await _seed_event(storage, tenant_id="tenant-a", subject="run-1")

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text="ok")

    client = httpx.AsyncClient(transport=_mock_transport(handler))
    worker = WebhookWorker(
        storage=storage,
        config=WebhookWorkerConfig(
            tenant_id="tenant-a",
            sleep_fn=_no_sleep,
        ),
        client=client,
    )
    delivered = await worker.run_one_cycle()
    await client.aclose()

    assert delivered == 1
    assert len(captured) == 1
    request = captured[0]
    assert str(request.url) == "https://hook.example/recv"
    body = await request.aread()
    # The HMAC over the body verifies under the subscription's secret.
    sig = request.headers[SIGNATURE_HEADER]
    assert verify_signature(secret=sub.secret, body=body, header_value=sig)
    # The body matches the encoded payload (mirror the event view).
    decoded = json.loads(body)
    assert decoded["id"] == event.id
    assert decoded["kind"] == event.kind
    assert decoded["subject"] == "run-1"
    # Cursor advanced.
    cursor = await storage.get_webhook_cursor("tenant-a", sub.id)
    assert cursor == event.id
    # Attempts log shows one ok row.
    rows = await storage.list_webhook_attempts("tenant-a", webhook_id=sub.id)
    assert len(rows) == 1
    assert rows[0].error_kind == "ok"
    assert rows[0].status_code == 200


# ---------------------------------------------------------------------------
# Kind filter
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_kind_filter_skips_non_matching_event() -> None:
    storage = await _build_storage()
    sub = WebhookSubscription(
        tenant_id="tenant-a",
        url="https://hook.example/recv",
        kind_filter=["eval.failed"],
    )
    await storage.create_webhook(sub)
    # Seed a run.completed; the subscriber only wants eval.failed.
    event = await _seed_event(storage, tenant_id="tenant-a", kind=EventKind.RUN_COMPLETED.value)

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=_mock_transport(handler))
    worker = WebhookWorker(
        storage=storage,
        config=WebhookWorkerConfig(tenant_id="tenant-a", sleep_fn=_no_sleep),
        client=client,
    )
    delivered = await worker.run_one_cycle()
    await client.aclose()
    assert delivered == 0
    assert captured == []
    # The cursor still advanced — we don't want to re-scan filtered-
    # out events on every pass.
    cursor = await storage.get_webhook_cursor("tenant-a", sub.id)
    assert cursor == event.id


# ---------------------------------------------------------------------------
# Retries — backoff schedule + max retries
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_retries_then_max_retries() -> None:
    """4 failed attempts → one terminal ``max_retries`` row + cursor advance."""
    storage = await _build_storage()
    sub = WebhookSubscription(
        tenant_id="tenant-a",
        url="https://hook.example/recv",
        kind_filter=["*"],
    )
    await storage.create_webhook(sub)
    event = await _seed_event(storage, tenant_id="tenant-a")

    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = httpx.AsyncClient(transport=_mock_transport(handler))
    worker = WebhookWorker(
        storage=storage,
        config=WebhookWorkerConfig(
            tenant_id="tenant-a",
            sleep_fn=record_sleep,
            retry_delays_seconds=(1.0, 4.0, 16.0),
            max_attempts=4,
        ),
        client=client,
    )
    delivered = await worker.run_one_cycle()
    await client.aclose()

    assert delivered == 1
    # Backoff: before attempt 2 → 1s; before attempt 3 → 4s; before
    # attempt 4 → 16s. Three pre-attempt waits.
    assert delays == [1.0, 4.0, 16.0]

    # Cursor advanced past the failing event — a poison event can't
    # wedge the queue.
    cursor = await storage.get_webhook_cursor("tenant-a", sub.id)
    assert cursor == event.id

    # Attempts log: 4 attempt rows (one per try) + 1 terminal max_retries.
    rows = await storage.list_webhook_attempts("tenant-a", webhook_id=sub.id, limit=20)
    kinds = [r.error_kind for r in rows]
    assert kinds.count("http_error") == 4
    assert kinds.count("max_retries") == 1

    # failure_count bumped on the subscription (advisory; not disabled).
    fresh = await storage.get_webhook("tenant-a", sub.id)
    assert fresh is not None
    assert fresh.failure_count == 1
    assert fresh.enabled is True  # NEVER auto-disable


@pytest.mark.unit
async def test_succeeds_on_retry_after_transient_failure() -> None:
    """A transient 500 followed by 200 records both attempts and an ``ok`` terminal."""
    storage = await _build_storage()
    sub = WebhookSubscription(
        tenant_id="tenant-a",
        url="https://hook.example/recv",
        kind_filter=["*"],
    )
    await storage.create_webhook(sub)
    await _seed_event(storage, tenant_id="tenant-a")

    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, text="ok")

    client = httpx.AsyncClient(transport=_mock_transport(handler))
    worker = WebhookWorker(
        storage=storage,
        config=WebhookWorkerConfig(
            tenant_id="tenant-a",
            sleep_fn=_no_sleep,
        ),
        client=client,
    )
    await worker.run_one_cycle()
    await client.aclose()

    rows = await storage.list_webhook_attempts("tenant-a", webhook_id=sub.id, limit=10)
    kinds = sorted([r.error_kind for r in rows])
    assert kinds == ["http_error", "ok"]
    # failure_count NOT bumped — the eventual success means no terminal
    # max_retries was recorded.
    fresh = await storage.get_webhook("tenant-a", sub.id)
    assert fresh is not None
    assert fresh.failure_count == 0


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_one_failing_subscriber_does_not_block_others() -> None:
    """A subscriber that 500s on every attempt doesn't stop another from receiving."""
    storage = await _build_storage()
    bad = WebhookSubscription(
        tenant_id="tenant-a",
        url="https://bad.example/recv",
        kind_filter=["*"],
    )
    good = WebhookSubscription(
        tenant_id="tenant-a",
        url="https://good.example/recv",
        kind_filter=["*"],
    )
    await storage.create_webhook(bad)
    await storage.create_webhook(good)
    await _seed_event(storage, tenant_id="tenant-a")

    good_hits: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "bad.example" in str(request.url):
            return httpx.Response(500, text="boom")
        good_hits.append(request)
        return httpx.Response(200, text="ok")

    client = httpx.AsyncClient(transport=_mock_transport(handler))
    worker = WebhookWorker(
        storage=storage,
        config=WebhookWorkerConfig(
            tenant_id="tenant-a",
            sleep_fn=_no_sleep,
            max_attempts=2,
            retry_delays_seconds=(0.0,),
        ),
        client=client,
    )
    await worker.run_one_cycle()
    await client.aclose()

    assert len(good_hits) == 1  # good subscriber received the event
    # Both subscribers advanced their cursor — failure on one doesn't
    # leave the other stuck.
    assert await storage.get_webhook_cursor("tenant-a", bad.id) is not None
    assert await storage.get_webhook_cursor("tenant-a", good.id) is not None
    # bad got a max_retries row; good got an ok row.
    bad_rows = await storage.list_webhook_attempts("tenant-a", webhook_id=bad.id, limit=10)
    good_rows = await storage.list_webhook_attempts("tenant-a", webhook_id=good.id, limit=10)
    assert any(r.error_kind == "max_retries" for r in bad_rows)
    assert any(r.error_kind == "ok" for r in good_rows)


# ---------------------------------------------------------------------------
# Cursor — new subscriber does not re-deliver historical events
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_new_subscriber_does_not_get_historical_events() -> None:
    """A subscription created AFTER an event was recorded skips it."""
    storage = await _build_storage()
    # Seed an event FIRST (before the subscription is created).
    await _seed_event(storage, tenant_id="tenant-a", subject="run-old")
    # Now create the subscription.
    sub = WebhookSubscription(
        tenant_id="tenant-a",
        url="https://hook.example/recv",
        kind_filter=["*"],
    )
    await storage.create_webhook(sub)

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=_mock_transport(handler))
    worker = WebhookWorker(
        storage=storage,
        config=WebhookWorkerConfig(tenant_id="tenant-a", sleep_fn=_no_sleep),
        client=client,
    )
    delivered = await worker.run_one_cycle()
    await client.aclose()
    assert delivered == 0
    assert captured == []


# ---------------------------------------------------------------------------
# Headers + payload shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_headers_carry_event_metadata() -> None:
    storage = await _build_storage()
    sub = WebhookSubscription(
        tenant_id="tenant-a",
        url="https://hook.example/recv",
        kind_filter=["*"],
    )
    await storage.create_webhook(sub)
    event = await _seed_event(
        storage,
        tenant_id="tenant-a",
        kind=EventKind.AGENT_PUBLISHED.value,
        subject="faq-agent",
    )

    captured_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        for k, v in request.headers.items():
            captured_headers[k.lower()] = v
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=_mock_transport(handler))
    worker = WebhookWorker(
        storage=storage,
        config=WebhookWorkerConfig(tenant_id="tenant-a", sleep_fn=_no_sleep),
        client=client,
    )
    await worker.run_one_cycle()
    await client.aclose()

    assert captured_headers["x-mdk-event-id"] == event.id
    assert captured_headers["x-mdk-event-kind"] == "agent.published"
    assert captured_headers["x-mdk-webhook-id"] == sub.id
    assert captured_headers["content-type"] == "application/json"
    assert "t=" in captured_headers["x-mdk-signature"]
    assert "v1=" in captured_headers["x-mdk-signature"]


@pytest.mark.unit
def test_encode_payload_is_deterministic() -> None:
    """``encode_payload`` is a stable transformation used in the signature."""
    body1 = encode_payload({"b": 2, "a": 1})
    body2 = encode_payload({"a": 1, "b": 2})
    # Keys sorted, separators pinned → identical bytes.
    assert body1 == body2
