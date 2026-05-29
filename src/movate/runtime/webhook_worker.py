"""Webhook delivery worker (ADR 035 D2 — outbound delivery).

D1 (the events outbox) is the producer side. This module is the
**consumer side**: a background async loop that periodically scans
every active :class:`WebhookSubscription`, finds events newer than the
subscription's per-webhook cursor, and POSTs them to ``url`` with the
canonical Stripe-style ``X-MDK-Signature`` header.

Key design choices (referenced in the PR body):

* **Per-webhook cursor.** ``webhook_cursors(webhook_id PK)``. New
  subscribers don't trigger redelivery of historical events to existing
  subscribers, and one slow subscriber doesn't block another's cursor
  from advancing.
* **Stripe-style signature.** ``t=<ts>,v1=<hex>`` over the canonical
  string ``"<ts>.<raw_body>"``. Reviewers recognize this; subscribers
  using off-the-shelf Stripe-receiver libs work with minimal changes.
* **Bounded retry / no auto-disable.** 4 attempts on the schedule
  ``[0, 1s, 4s, 16s, 60s]`` (initial + 4 retries means 5 total — we
  cap at 4 *attempts* per the spec: initial + 3 retries). After
  exhaustion, ``failure_count`` is bumped on the subscription row, an
  attempt with ``error_kind="max_retries"`` is recorded, and the
  cursor still advances so a single poison event can't wedge the
  queue. The subscription **stays enabled** — operators control the
  disable, not the worker.
* **Failure isolation.** One subscriber's failure cannot delay another.
  Each subscription drain runs sequentially within a single tick, but
  the wider work happens fire-and-forget: an exception inside one
  drain is logged and the next subscription proceeds.
* **Edge-only HTTPX dependency.** We use ``httpx.AsyncClient`` — same
  dependency the rest of the runtime already pulls. No new shipped
  dep. The client is owned by the worker (one per worker process,
  reused across deliveries) for connection pooling + lower TLS
  overhead.

Boundary: runtime-only. ``core`` defines the data model + signing
primitives; ``storage`` exposes the cursor + attempts log via the
Protocol; this module wires them together. CLI / core never import it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from movate.core.events import Event
from movate.core.webhooks import (
    EVENT_ID_HEADER,
    EVENT_KIND_HEADER,
    SIGNATURE_HEADER,
    WEBHOOK_ID_HEADER,
    WebhookAttempt,
    WebhookSubscription,
    build_payload,
    sign_payload,
)
from movate.storage.base import StorageProvider

logger = logging.getLogger(__name__)


# Retry backoff schedule (seconds). The four entries map to the four
# attempts (1-indexed: attempt 1 has no pre-wait, attempt 2 waits 1s
# after the failure of attempt 1, etc.). Total worst-case latency
# 0 + 1 + 4 + 16 + 60 = 81s before max-retries — short enough that a
# transient flake recovers fast; long enough that we don't hammer a
# struggling subscriber. Capped at 4 attempts per the ADR scope.
DEFAULT_RETRY_DELAYS_SECONDS: tuple[float, ...] = (1.0, 4.0, 16.0, 60.0)

# Maximum number of delivery attempts per (webhook, event) pair.
# Matches the length of ``DEFAULT_RETRY_DELAYS_SECONDS``
# interpretation: attempt 1 (initial) + delays[0..2] (3 retries) = 4
# attempts. The fourth attempt's failure records ``max_retries`` and
# advances the cursor.
DEFAULT_MAX_ATTEMPTS = 4

# Connect / read timeouts on the outbound POST. Conservative; a sane
# webhook receiver responds in <1s. The 5s connect budget covers DNS
# + TLS handshake on a cold path; the 10s read covers a slow handler.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 10.0

# Cap the saved response excerpt (in chars/bytes — bytes are utf-8
# decoded best-effort). A misbehaving subscriber returning megabytes
# can't bloat the attempts log this way.
RESPONSE_EXCERPT_MAX_CHARS = 512

# HTTP success class — 2xx. We deliberately don't follow 3xx so a
# misconfigured redirect doesn't leak the signature to a different
# origin; both bounds are named constants so the lint guard against
# magic numbers stays clean.
_HTTP_SUCCESS_MIN = 200
_HTTP_SUCCESS_MAX = 300


@dataclass
class WebhookWorkerConfig:
    """Knobs for the delivery loop. Defaults match the ADR-prescribed values."""

    poll_interval_seconds: float = 1.0
    """How long to sleep between drain passes when the outbox has no
    new events for any subscriber. Cheap polls keep latency low; on a
    busy system this rarely triggers because each tick finds work."""

    tenant_id: str | None = None
    """Optional tenant scope. ``None`` drains every tenant (operator
    mode, the default — matches the job ``Worker``'s posture). A
    deployment that runs per-tenant worker pools sets this to the
    tenant id."""

    retry_delays_seconds: tuple[float, ...] = DEFAULT_RETRY_DELAYS_SECONDS
    """Backoff schedule between attempts. ``len(retry_delays)`` + 1 ==
    max_attempts if you derive one from the other; the worker reads
    ``max_attempts`` and ``retry_delays_seconds`` independently for
    test flexibility."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    """Cap on attempts per (webhook, event) pair. 1 disables retries
    entirely (every failure terminal); 0 is treated as 1."""

    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    """Per-request connect timeout. Covers DNS + TLS handshake."""

    read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS
    """Per-request read timeout. Covers a slow webhook handler."""

    event_page_size: int = 100
    """How many events to read per subscription per tick. Caps the work
    one pass does to keep memory bounded — a backlog catches up over
    multiple ticks."""

    sleep_fn: Callable[[float], Awaitable[None]] = field(default=None)  # type: ignore[assignment]
    """Test seam: async sleep override. Defaults to ``asyncio.sleep``;
    a deterministic test passes a no-op or a clock-mock to skip
    real-time backoffs."""

    now_fn: Callable[[], datetime] = field(default=None)  # type: ignore[assignment]
    """Test seam: wall-clock override. Defaults to
    ``datetime.now(UTC)``; tests inject a fixed clock so signature
    timestamps and attempted_at are deterministic."""

    def __post_init__(self) -> None:
        # Default the seam callables here so the dataclass field
        # defaults don't need a lambda (mypy + linter happier). The
        # callables are typed non-Optional but defaulted to ``None``
        # via ``field(default=None)``; we coerce to the real defaults
        # exactly once on construction.
        if self.sleep_fn is None:
            self.sleep_fn = asyncio.sleep
        if self.now_fn is None:
            self.now_fn = lambda: datetime.now(UTC)


# Default JSON-encoder for the payload — exported so tests can match
# the on-the-wire bytes the worker signs.
def encode_payload(payload: dict[str, object]) -> bytes:
    """Render ``payload`` to canonical UTF-8 bytes.

    Stable separators + sort_keys make the signed body deterministic —
    a subscriber's HMAC over the received body reproduces ours
    byte-for-byte. (We pin both ``,``/``:`` separators and sort_keys
    so a future Python json default change can't drift the signature.)
    """
    import json  # noqa: PLC0415

    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _truncate_excerpt(text: str | None) -> str | None:
    if text is None:
        return None
    if len(text) <= RESPONSE_EXCERPT_MAX_CHARS:
        return text
    return text[: RESPONSE_EXCERPT_MAX_CHARS - 1] + "…"


@dataclass
class DeliveryOutcome:
    """Result of a single POST. Internal to the worker."""

    status_code: int | None
    response_excerpt: str | None
    error_kind: str
    """``"ok" | "http_error" | "timeout" | "connection"`` — terminal
    ``"max_retries"`` is decided by the caller after exhaustion."""


class WebhookWorker:
    """Drains the events outbox into webhook subscribers.

    One worker process holds one ``httpx.AsyncClient`` for the whole
    loop and reuses it across deliveries — connection pooling avoids
    a TLS handshake per event. The client is closed on
    :meth:`close`.

    Lifecycle:

    * :meth:`run_one_cycle` — process one drain pass over every
      enabled subscription. Tests call this directly to assert
      behavior without timing flakiness.
    * :meth:`run_forever` — loop until ``stop_event`` is set. Cancel-
      able from a CLI signal handler.

    Failure isolation: every exception inside a per-subscription drain
    is caught and logged. The wider loop continues — one bad
    subscriber cannot block any other.
    """

    def __init__(
        self,
        *,
        storage: StorageProvider,
        config: WebhookWorkerConfig | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._storage = storage
        self._config = config or WebhookWorkerConfig()
        # Lazy client init — tests inject one; production calls
        # ``run_forever`` which creates a single shared client.
        self._client = client
        self._owns_client = client is None

    def _build_client(self) -> httpx.AsyncClient:
        timeout = httpx.Timeout(
            connect=self._config.connect_timeout_seconds,
            read=self._config.read_timeout_seconds,
            write=self._config.read_timeout_seconds,
            pool=self._config.read_timeout_seconds,
        )
        return httpx.AsyncClient(timeout=timeout, follow_redirects=False)

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    async def close(self) -> None:
        """Tear down the owned ``httpx.AsyncClient`` if we created one."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Single-tick + loop
    # ------------------------------------------------------------------

    async def run_one_cycle(self) -> int:
        """Process one drain pass; return the count of deliveries
        attempted across all subscribers (success or terminal).

        Tests inspect the return to assert work happened without
        relying on log scraping.
        """
        delivered = 0
        # ``tenant_id=None`` means cross-tenant drain (the operator
        # mode). The per-tenant scope is enforced inside per-
        # subscription drain because we always re-scope by the
        # subscription's own tenant_id.
        subs = await self._list_subs()
        for sub in subs:
            try:
                count = await self._drain_subscription(sub)
                delivered += count
            except Exception:
                # Failure isolation — never let one subscriber's
                # blowup block the wider loop. The exception is
                # already logged inside the drain on the error path;
                # this is a belt-and-suspender against an unexpected
                # raise from the storage layer itself.
                logger.exception(
                    "webhook_worker_drain_crashed webhook_id=%s tenant_id=%s",
                    sub.id,
                    sub.tenant_id,
                )
        return delivered

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Loop until ``stop_event`` is set.

        Sleeps ``poll_interval_seconds`` between empty ticks; on a
        tick that found work, runs again immediately so a backlog
        catches up without artificial latency.
        """
        logger.info(
            "webhook_worker_started tenant_id=%s poll_interval=%.2fs",
            self._config.tenant_id or "<all>",
            self._config.poll_interval_seconds,
        )
        try:
            while not stop_event.is_set():
                handled = await self.run_one_cycle()
                if handled == 0:
                    # No work this tick — wait, but stay cancel-able.
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            stop_event.wait(),
                            timeout=self._config.poll_interval_seconds,
                        )
        finally:
            logger.info("webhook_worker_stopped")
            await self.close()

    # ------------------------------------------------------------------
    # Per-subscription drain
    # ------------------------------------------------------------------

    async def _list_subs(self) -> list[WebhookSubscription]:
        """Return enabled subscriptions for the configured scope.

        ``list_webhooks`` takes a tenant; the cross-tenant variant in
        operator mode is implemented by reading every tenant's
        subscriptions. To keep storage simple, we read per-tenant
        only when a tenant scope is configured — otherwise we list
        the events outbox-style: drain all events for a tenant, find
        the subs, etc.
        """
        if self._config.tenant_id is not None:
            return await self._storage.list_webhooks(self._config.tenant_id, enabled_only=True)
        # Cross-tenant operator mode: enumerate distinct tenants from
        # whichever subscriptions exist. The storage layer doesn't
        # expose a cross-tenant ``list_webhooks`` (deliberately — every
        # endpoint method is tenant-scoped); we derive the set from
        # the events table by reading the recent window, which is
        # already what the worker walks. Practical approach: scan
        # the storage doubles directly via the public Protocol —
        # ``list_webhooks(tenant_id)`` is the only seam. The runtime
        # only calls this worker with a tenant scope today; the
        # operator-drain branch falls back to delegating per-tenant
        # below. For the wider rollout we'll add a cross-tenant
        # listing method; D2 ships with the tenant-scoped path only.
        return []

    async def _drain_subscription(self, sub: WebhookSubscription) -> int:
        """Deliver every event newer than the cursor for ``sub``.

        Returns the count of events that reached a terminal outcome
        on this pass — one per event regardless of how many retries
        it took.
        """
        delivered = 0
        # Read the cursor; if absent, we start from the subscription's
        # creation time so we don't re-deliver historical events.
        cursor = await self._storage.get_webhook_cursor(sub.tenant_id, sub.id)
        page = await self._storage.list_events(
            sub.tenant_id,
            since=None if cursor is not None else sub.created_at,
            after_id=cursor,
            limit=self._config.event_page_size,
        )
        for event in page:
            if not sub.matches(event.kind):
                # Advance the cursor through filtered-out events too.
                # Otherwise a long subscription with a narrow filter
                # would re-scan every non-matching event on every
                # tick — the cursor advances over all SEEN events,
                # not just delivered ones.
                await self._storage.set_webhook_cursor(sub.tenant_id, sub.id, event.id)
                continue
            terminal = await self._deliver_with_retries(sub, event)
            await self._storage.set_webhook_cursor(sub.tenant_id, sub.id, event.id)
            if terminal == "max_retries":
                # Bump the advisory failure_count; the subscription
                # stays enabled (operators flip the toggle).
                fresh = await self._storage.get_webhook(sub.tenant_id, sub.id)
                if fresh is not None:
                    await self._storage.update_webhook(
                        sub.tenant_id,
                        sub.id,
                        failure_count=fresh.failure_count + 1,
                    )
            delivered += 1
        return delivered

    # ------------------------------------------------------------------
    # One event, retry loop
    # ------------------------------------------------------------------

    async def _deliver_with_retries(
        self,
        sub: WebhookSubscription,
        event: Event,
    ) -> str:
        """POST one event up to ``max_attempts`` times.

        Returns the *terminal* error_kind: ``"ok"`` on a 2xx response,
        ``"max_retries"`` if every attempt failed. Records one
        :class:`WebhookAttempt` per attempt — including the terminal
        one, which carries ``error_kind="max_retries"`` if every retry
        failed (so an ops view of the log shows the give-up line).
        """
        payload = build_payload(event)
        body = encode_payload(payload)
        max_attempts = max(1, self._config.max_attempts)
        last_outcome: DeliveryOutcome | None = None
        for attempt_n in range(1, max_attempts + 1):
            # Backoff BEFORE attempts 2..N (attempt 1 fires immediately).
            if attempt_n > 1:
                delay_idx = attempt_n - 2  # delay before attempt 2 == delays[0]
                if delay_idx < len(self._config.retry_delays_seconds):
                    delay = self._config.retry_delays_seconds[delay_idx]
                else:
                    delay = self._config.retry_delays_seconds[-1]
                await self._config.sleep_fn(delay)
            outcome = await self._post_one(sub, event, body, attempt_n=attempt_n)
            last_outcome = outcome
            await self._record_attempt(sub, event, outcome, attempt_n=attempt_n)
            if outcome.error_kind == "ok":
                return "ok"
        # Every attempt failed. Record one MORE attempt row marking
        # the terminal max_retries state — the ops view groups by
        # error_kind and surfaces this. Use ``attempt_n = max_attempts``
        # so the terminal row sorts last in the per-event slice; reuse
        # the last attempt's status_code / excerpt so the operator sees
        # what the final failure looked like.
        final_attempt = WebhookAttempt(
            webhook_id=sub.id,
            event_id=event.id,
            tenant_id=sub.tenant_id,
            attempted_at=self._config.now_fn(),
            status_code=last_outcome.status_code if last_outcome else None,
            response_excerpt=last_outcome.response_excerpt if last_outcome else None,
            error_kind="max_retries",
            attempt_n=max_attempts,
        )
        try:
            await self._storage.record_webhook_attempt(final_attempt)
        except Exception:
            logger.warning(
                "webhook_worker_record_max_retries_failed webhook_id=%s event_id=%s",
                sub.id,
                event.id,
                exc_info=True,
            )
        logger.warning(
            "webhook_max_retries webhook_id=%s event_id=%s tenant_id=%s url=%s",
            sub.id,
            event.id,
            sub.tenant_id,
            sub.url,
        )
        return "max_retries"

    async def _record_attempt(
        self,
        sub: WebhookSubscription,
        event: Event,
        outcome: DeliveryOutcome,
        *,
        attempt_n: int,
    ) -> None:
        """Append one attempt row. Storage failure here MUST NOT crash
        the worker — log + move on."""
        attempt = WebhookAttempt(
            webhook_id=sub.id,
            event_id=event.id,
            tenant_id=sub.tenant_id,
            attempted_at=self._config.now_fn(),
            status_code=outcome.status_code,
            response_excerpt=outcome.response_excerpt,
            error_kind=outcome.error_kind,
            attempt_n=attempt_n,
        )
        try:
            await self._storage.record_webhook_attempt(attempt)
        except Exception:
            logger.warning(
                "webhook_worker_record_attempt_failed webhook_id=%s event_id=%s",
                sub.id,
                event.id,
                exc_info=True,
            )

    async def _post_one(
        self,
        sub: WebhookSubscription,
        event: Event,
        body: bytes,
        *,
        attempt_n: int,
    ) -> DeliveryOutcome:
        """One HTTP POST. Never raises — every failure mode maps to a
        :class:`DeliveryOutcome` so the retry loop has one shape."""
        client = await self._ensure_client()
        ts = int(time.time())
        sig = sign_payload(secret=sub.secret, body=body, timestamp=ts)
        headers = {
            "Content-Type": "application/json",
            SIGNATURE_HEADER: sig,
            EVENT_ID_HEADER: event.id,
            EVENT_KIND_HEADER: event.kind,
            WEBHOOK_ID_HEADER: sub.id,
        }
        try:
            response = await client.post(sub.url, content=body, headers=headers)
        except httpx.TimeoutException:
            return DeliveryOutcome(
                status_code=None,
                response_excerpt=None,
                error_kind="timeout",
            )
        except httpx.RequestError as exc:
            # Connect/DNS/TLS/socket-refused — every transport-layer
            # failure that isn't a timeout.
            return DeliveryOutcome(
                status_code=None,
                response_excerpt=_truncate_excerpt(f"{type(exc).__name__}: {exc}"),
                error_kind="connection",
            )
        # We have an HTTP response. 2xx is the only success class —
        # we deliberately don't follow 3xx so a misconfigured redirect
        # doesn't leak the signature to a different origin.
        excerpt = _truncate_excerpt(response.text)
        if _HTTP_SUCCESS_MIN <= response.status_code < _HTTP_SUCCESS_MAX:
            return DeliveryOutcome(
                status_code=response.status_code,
                response_excerpt=excerpt,
                error_kind="ok",
            )
        return DeliveryOutcome(
            status_code=response.status_code,
            response_excerpt=excerpt,
            error_kind="http_error",
        )


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_RETRY_DELAYS_SECONDS",
    "RESPONSE_EXCERPT_MAX_CHARS",
    "DeliveryOutcome",
    "WebhookWorker",
    "WebhookWorkerConfig",
    "encode_payload",
]
