"""Alert-router consumer — drains the outbox, routes alerts to sinks (ADR 057 step 2).

This is the **router consumer** ADR 057 names: the producer side
(:mod:`movate.core.alert_emit`) appends an ``alert.raised`` event to the ADR 035
outbox whenever a drift / dead-letter / budget condition fires; this worker reads
those new events and runs the :class:`~movate.core.alerts.AlertRouter` over each,
delivering to the configured notification sinks. Sources and sinks never know
about each other — the outbox is the only thing between them (ADR 057 D1).

It deliberately mirrors :class:`movate.runtime.webhook_worker.WebhookWorker`: a
background async loop that scans the events outbox forward from a cursor, one
tenant at a time. Differences that matter:

* **Filter to ``alert.raised``.** It only reconstructs + routes alert carriers
  (:func:`movate.core.alert_emit.alert_event_from_outbox`); every other lifecycle
  event is skipped (the cursor still advances past it).
* **In-process cursor (no schema change).** Unlike the webhook worker's durable
  per-webhook cursor, this consumer keeps its cursor in memory and starts from
  "now" at construction — it routes alerts raised *after* it started, not the
  historical backlog. A durable cursor (an ``alert_cursors`` row) is a later,
  additive step; an in-memory cursor keeps this PR free of any storage-Protocol
  change while still delivering every alert raised during the worker's lifetime.
* **Opt-in (ADR 057 D7).** When the :class:`AlertRouter` has no routes
  configured it's a no-op — :meth:`AlertRouter.is_active` is ``False`` and the
  worker short-circuits without even reading the outbox, so an unconfigured
  deployment pays nothing and delivers nothing (zero behavior change).
* **Best-effort (ADR 057 D5).** :meth:`AlertRouter.route` never raises on a sink
  failure; on top of that, every per-event + per-tick body here is wrapped so a
  malformed row or a storage hiccup can't wedge the loop or sink the process.

Boundary: runtime-only, wired at the edge. ``core`` owns the router + the alert
data model + the sink Protocol; ``storage`` exposes the outbox via the Protocol;
this module connects them. It imports no concrete sink — the caller hands it a
fully-wired :class:`AlertRouter` (built from ``load_alert_routes`` +
``build_sinks_from_env`` at the CLI edge).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from movate.core.alert_emit import alert_event_from_outbox
from movate.core.alerts import AlertRouter
from movate.core.events import EventKind
from movate.storage.base import StorageProvider

logger = logging.getLogger(__name__)


@dataclass
class AlertWorkerConfig:
    """Knobs for the alert-drain loop. Defaults mirror the webhook worker."""

    poll_interval_seconds: float = 1.0
    """Sleep between drain passes when no new alert events were found."""

    tenant_id: str | None = None
    """Tenant scope for the outbox drain. A single-tenant deployment sets this
    to its tenant id; ``None`` means "this worker has no tenant scope" — see the
    cross-tenant note on :meth:`AlertWorker.run_one_cycle`."""

    event_page_size: int = 200
    """Events read per tick. Caps per-pass work; a backlog catches up over
    multiple ticks (the cursor advances each pass)."""

    sleep_fn: Callable[[float], Awaitable[None]] = field(default=None)  # type: ignore[assignment]
    """Test seam: async sleep override. Defaults to ``asyncio.sleep``."""

    now_fn: Callable[[], datetime] = field(default=None)  # type: ignore[assignment]
    """Test seam: wall-clock override (drives the start cursor + route ``now``).
    Defaults to ``datetime.now(UTC)``."""

    def __post_init__(self) -> None:
        if self.sleep_fn is None:
            self.sleep_fn = asyncio.sleep
        if self.now_fn is None:
            self.now_fn = lambda: datetime.now(UTC)


class AlertWorker:
    """Drains ``alert.raised`` events from the outbox into the alert router.

    Lifecycle mirrors the webhook worker:

    * :meth:`run_one_cycle` — one drain pass; returns the count of alert events
      routed this tick (tests call it directly, no timing flakiness).
    * :meth:`run_forever` — loop until ``stop_event`` is set; cancel-able from a
      CLI signal handler.

    A no-route router makes this a no-op (opt-in, D7). Delivery failures are
    best-effort and never propagate (D5).
    """

    def __init__(
        self,
        *,
        storage: StorageProvider,
        router: AlertRouter,
        config: AlertWorkerConfig | None = None,
    ) -> None:
        self._storage = storage
        self._router = router
        self._config = config or AlertWorkerConfig()
        # In-memory cursor. We start from "now" so the worker routes alerts
        # raised after it started, not the historical backlog (see module
        # docstring). ``None`` once we've begun paging (then it's the last
        # event id we saw).
        self._since: datetime | None = self._config.now_fn()
        self._after_id: str | None = None

    @property
    def is_active(self) -> bool:
        """True iff the underlying router has routes configured. ``False`` ⇒ the
        worker is a pure no-op (opt-in, ADR 057 D7)."""
        return self._router.is_active

    async def run_one_cycle(self) -> int:
        """Process one drain pass; return the count of alerts routed.

        Opt-in short-circuit: an inactive router (no routes) reads nothing and
        routes nothing. A configured ``tenant_id`` scopes the outbox read; with
        no tenant scope there is nothing to drain on the tenant-scoped
        ``list_events`` Protocol (the same single-tenant posture the webhook
        worker ships with — a cross-tenant drain is a later, additive step).
        """
        if not self._router.is_active:
            return 0
        if self._config.tenant_id is None:
            # No tenant scope → nothing to read on the tenant-scoped Protocol.
            # Operators run one alert worker per tenant scope today.
            return 0
        try:
            return await self._drain_tenant(self._config.tenant_id)
        except Exception:
            # Belt-and-suspenders: a storage-layer raise must not sink the loop.
            logger.warning(
                "alert_worker_drain_crashed tenant_id=%s — tick skipped",
                self._config.tenant_id,
                exc_info=True,
            )
            return 0

    async def _drain_tenant(self, tenant_id: str) -> int:
        """Route every ``alert.raised`` event newer than the cursor."""
        page = await self._storage.list_events(
            tenant_id,
            since=self._since if self._after_id is None else None,
            after_id=self._after_id,
            kind=EventKind.ALERT_RAISED.value,
            limit=self._config.event_page_size,
        )
        routed = 0
        for event in page:
            # Advance the cursor over EVERY seen row (delivered or skipped) so a
            # poison/malformed row can't wedge the drain — exactly the webhook
            # worker's cursor discipline.
            self._after_id = event.id
            self._since = None
            alert = alert_event_from_outbox(event)
            if alert is None:
                continue  # already logged inside the reconstructor
            try:
                await self._router.route(alert, now=self._config.now_fn())
            except Exception:
                # The router is best-effort + already guards sink failures
                # (D5); this catch is defensive against an unexpected raise so
                # one bad alert can't stop the rest of the page.
                logger.warning(
                    "alert_worker_route_raised event_id=%s — alert dropped, drain continues",
                    event.id,
                    exc_info=True,
                )
            routed += 1
        return routed

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Loop until ``stop_event`` is set, draining the alert outbox."""
        logger.info(
            "alert_worker_started tenant_id=%s active=%s poll_interval=%.2fs",
            self._config.tenant_id or "<all>",
            self._router.is_active,
            self._config.poll_interval_seconds,
        )
        try:
            while not stop_event.is_set():
                handled = await self.run_one_cycle()
                if handled == 0:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            stop_event.wait(),
                            timeout=self._config.poll_interval_seconds,
                        )
        finally:
            logger.info("alert_worker_stopped")


def build_alert_worker(
    *,
    storage: StorageProvider,
    tenant_id: str | None = None,
    config: AlertWorkerConfig | None = None,
) -> AlertWorker:
    """Build an :class:`AlertWorker` from the project's alert config + env sinks.

    Wires the seam at the edge (ADR 057): discovers the route table
    (``load_alert_routes`` — ``alerts.yaml`` / a ``project.yaml`` ``alerts:``
    block), autoloads the configured sinks from env (``build_sinks_from_env`` —
    Slack / Teams / generic webhook / email), and constructs the router over
    them. No routes ⇒ an inactive router ⇒ the worker is a pure no-op (opt-in).

    Imports the concrete-sink + config loaders lazily so the worker module stays
    sink-free at import time (boundary discipline).
    """
    from movate.core.alert_sinks import build_sinks_from_env  # noqa: PLC0415
    from movate.core.alerts import load_alert_routes  # noqa: PLC0415

    table = load_alert_routes()
    registry = build_sinks_from_env()
    router = AlertRouter(table=table, registry=registry)
    cfg = config or AlertWorkerConfig(tenant_id=tenant_id)
    return AlertWorker(storage=storage, router=router, config=cfg)


__all__ = [
    "AlertWorker",
    "AlertWorkerConfig",
    "build_alert_worker",
]
