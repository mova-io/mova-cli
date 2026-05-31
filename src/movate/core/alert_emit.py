"""Alert *source* edge — emit typed alerts into the ADR 035 outbox (ADR 057 D1, step 2).

This is the step-2 wire ADR 057 names: the conditions mdk already detects
(drift regression, dead-letter, budget burn) emit an
:class:`~movate.core.alerts.AlertEvent` here, and the alert-router consumer
(:class:`movate.runtime.alert_worker.AlertWorker`) drains it and delivers to the
configured sinks. **Sources never import a sink** — they only append an event to
the durable outbox (boundary discipline, ADR 057 D1 / CLAUDE.md rule 6).

How the alert rides the outbox (ADR 035): an :class:`AlertEvent` is serialized
into a lifecycle :class:`~movate.core.events.Event` with
``kind = EventKind.ALERT_RAISED`` (``"alert.raised"``). The full alert payload
lives in ``Event.data`` under :data:`ALERT_DATA_KEY`; the consumer rebuilds the
``AlertEvent`` from it via :func:`alert_event_from_outbox`. Reusing the existing
outbox means no second event stream, no storage-schema change, and the same
at-least-once + tenant-scoping semantics every ADR 035 consumer already relies
on.

**Best-effort + opt-in (ADR 057 D5 / D7).** :func:`emit_alert` is
fire-and-forget: it schedules a background ``record_event`` and returns
immediately, swallowing+logging any storage error. It must **never** raise into
the drift detector / worker / budget checker — alerting can't break the path
that emitted it. With no routes configured the event is still recorded but the
router is a no-op, so nothing is delivered (zero behavior change).

Boundary: this is a thin ``core`` edge helper. It depends only on the
``AlertEvent`` data model + the storage Protocol (``record_event``) — no sink, no
runtime imports. ``core/executor.py`` (budget) imports it directly; the runtime
drift/worker edges import it too. It deliberately mirrors
``movate.runtime.events.emit_event``'s fire-and-forget shape, but lives in
``core`` because the budget source is core-resident.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from movate.core.alerts import AlertEvent, AlertKind, Severity
from movate.core.events import Event, EventKind
from movate.storage.base import StorageProvider

logger = logging.getLogger(__name__)

# Key under ``Event.data`` that carries the serialized ``AlertEvent`` payload.
# Namespaced so it can't collide with any incidental data a future emitter adds
# to an ``alert.raised`` event.
ALERT_DATA_KEY = "alert"


def to_outbox_event(alert: AlertEvent) -> Event:
    """Serialize an :class:`AlertEvent` into an ADR 035 outbox :class:`Event`.

    The carrier kind is :attr:`EventKind.ALERT_RAISED`; the full alert is
    JSON-dumped under :data:`ALERT_DATA_KEY` so the consumer can faithfully
    reconstruct it (severity round-trips via its string label). ``tenant_id`` /
    ``subject`` mirror the alert so the outbox's tenant-scoping + subject index
    apply to alerts exactly as to every other event.
    """
    return Event(
        tenant_id=alert.tenant_id,
        kind=EventKind.ALERT_RAISED.value,
        subject=alert.subject,
        data={ALERT_DATA_KEY: alert.model_dump(mode="json")},
    )


def alert_event_from_outbox(event: Event) -> AlertEvent | None:
    """Reconstruct an :class:`AlertEvent` from an ``alert.raised`` outbox event.

    Returns ``None`` (logged, never raised) when ``event`` isn't an
    ``alert.raised`` carrier or its payload is malformed — a single poison row
    must not wedge the consumer (the worker advances its cursor past it).
    """
    if event.kind != EventKind.ALERT_RAISED.value:
        return None
    payload = event.data.get(ALERT_DATA_KEY)
    if not isinstance(payload, dict):
        logger.warning(
            "alert_outbox_payload_missing event_id=%s — skipped (not a valid alert carrier)",
            event.id,
        )
        return None
    try:
        return AlertEvent.model_validate(payload)
    except Exception:
        logger.warning(
            "alert_outbox_payload_invalid event_id=%s — skipped; consumer advances past it",
            event.id,
            exc_info=True,
        )
        return None


def emit_alert(storage: StorageProvider, alert: AlertEvent) -> None:
    """Fire-and-forget: record one :class:`AlertEvent` onto the ADR 035 outbox.

    Schedules the async ``record_event`` as a background task and returns
    immediately. Exceptions inside the task — and the no-running-loop case — are
    caught + logged (``WARNING``); this function **never** raises into the
    calling source (ADR 057 D5). Mirrors
    :func:`movate.runtime.events.emit_event`.
    """
    try:
        event = to_outbox_event(alert)
    except Exception:
        # Building the event is pure + cheap, but never let even a serialization
        # bug reach the source (D5).
        logger.warning(
            "alert_emit_build_failed kind=%s subject=%s tenant_id=%s — source unaffected",
            alert.kind.value,
            alert.subject,
            alert.tenant_id,
            exc_info=True,
        )
        return

    async def _record() -> None:
        try:
            await storage.record_event(event)
        except Exception:
            logger.warning(
                "alert_emit_record_failed kind=%s subject=%s tenant_id=%s — source unaffected",
                alert.kind.value,
                alert.subject,
                alert.tenant_id,
                exc_info=True,
            )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "alert_emit_skipped_no_loop kind=%s subject=%s tenant_id=%s",
            alert.kind.value,
            alert.subject,
            alert.tenant_id,
        )
        return

    task = loop.create_task(_record())
    task.add_done_callback(_drop_completed_task_exception)


def _drop_completed_task_exception(task: asyncio.Task[None]) -> None:
    """Swallow a completed background task's exception so it never surfaces as a
    "Task exception was never retrieved" warning at GC time."""
    with contextlib.suppress(Exception):
        task.exception()


# ---------------------------------------------------------------------------
# Ergonomic constructors — one clean call per source (ADR 057 D1 sources).
#
# Each builds a well-formed AlertEvent with a stable ``dedup_key`` so D4
# throttle/dedup identifies "the same alert" across repeats. Sources call
# ``emit_alert(storage, drift_alert(...))`` etc. — they never touch a sink.
# ---------------------------------------------------------------------------


def drift_alert(
    *,
    tenant_id: str,
    agent: str,
    summary: str,
    severity: Severity = Severity.CRITICAL,
    data: dict[str, Any] | None = None,
) -> AlertEvent:
    """Build a ``drift_regression`` alert (ADR 016 source / ADR 057 D1).

    ``dedup_key`` is per (agent) so a flapping eval pages once per window. A
    regression is ``CRITICAL`` by default (it can revert a champion); callers
    pass ``severity=`` to downgrade an informational drift.
    """
    return AlertEvent(
        kind=AlertKind.DRIFT_REGRESSION,
        severity=severity,
        tenant_id=tenant_id,
        subject=agent,
        summary=summary,
        data=data or {},
        dedup_key=f"drift:{tenant_id}:{agent}",
    )


def dead_letter_alert(
    *,
    tenant_id: str,
    subject: str,
    summary: str,
    severity: Severity = Severity.WARNING,
    data: dict[str, Any] | None = None,
) -> AlertEvent:
    """Build a ``dead_letter_spike`` alert (``core/job_retry`` source / D1).

    ``subject`` is the target (agent / workflow) whose job exhausted its retry
    budget; ``dedup_key`` is per (tenant, subject) so repeated dead-letters for
    the same target collapse to one page per window.
    """
    return AlertEvent(
        kind=AlertKind.DEAD_LETTER_SPIKE,
        severity=severity,
        tenant_id=tenant_id,
        subject=subject,
        summary=summary,
        data=data or {},
        dedup_key=f"dead_letter:{tenant_id}:{subject}",
    )


def budget_alert(
    *,
    tenant_id: str,
    summary: str,
    severity: Severity = Severity.CRITICAL,
    data: dict[str, Any] | None = None,
) -> AlertEvent:
    """Build a ``budget_threshold`` alert (ADR 036 source / D1).

    The subject is the tenant itself (a budget is tenant-scoped); ``dedup_key``
    is per tenant so a tenant that keeps hitting its cap pages once per window.
    """
    return AlertEvent(
        kind=AlertKind.BUDGET_THRESHOLD,
        severity=severity,
        tenant_id=tenant_id,
        subject=tenant_id,
        summary=summary,
        data=data or {},
        dedup_key=f"budget:{tenant_id}",
    )


__all__ = [
    "ALERT_DATA_KEY",
    "alert_event_from_outbox",
    "budget_alert",
    "dead_letter_alert",
    "drift_alert",
    "emit_alert",
    "to_outbox_event",
]
