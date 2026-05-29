"""Runtime helper for emitting lifecycle events (ADR 035 D1 — events outbox).

The runtime calls :func:`emit_event` at terminal-state transitions in the
executor / dispatch / deploy paths to record a domain event
(``run.completed``, ``agent.published``, ``eval.failed``,
``drift.detected``, ``canary.promoted/demoted``, ...) onto the durable
outbox via :meth:`StorageProvider.record_event`.

Failure isolation is the **load-bearing** discipline here: ADR 035's D1
requires that event recording NEVER blocks or breaks the primary path.
Storage flakiness, a DB temporarily down, a schema-not-yet-migrated
replica — none of those may take down a run / publish / promote.

We achieve this with a fire-and-forget pattern (matches the rest of the
runtime's edge-effect style, e.g. ``_safe_touch`` for api_keys / runs):

* Caller builds the :class:`Event` synchronously (cheap; just dataclass
  construction).
* This helper schedules an :func:`asyncio.create_task` that awaits
  ``storage.record_event(event)`` and swallows + logs any exception.
* Caller returns immediately — the emit happens concurrently with the
  HTTP response / worker progress, and any storage error becomes a
  ``WARNING`` log line, not a request failure.

Boundary: this is a thin runtime-edge helper. It depends only on the
core ``Event`` dataclass + the storage Protocol — no execution-plane
imports. ``cli`` / ``core`` never import it; runtime + dispatch +
worker + the publish/canary edges do.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from movate.core.events import Event, EventKind
from movate.storage.base import StorageProvider

logger = logging.getLogger(__name__)


def emit_event(
    storage: StorageProvider,
    *,
    tenant_id: str,
    kind: EventKind | str,
    subject: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Fire-and-forget: record one lifecycle event to the outbox.

    Synchronous wrapper that schedules the async ``record_event`` call
    as a background task and returns immediately. Exceptions inside the
    task are caught + logged (``WARNING``) so a storage hiccup never
    flips the caller's terminal status.

    ``kind`` accepts both :class:`EventKind` and a raw string — the
    storage column is free-form text, so callers emitting a not-yet-
    canonical kind can pass a string directly without first extending
    the enum.

    ``data`` is the small JSON-serializable payload; ``None`` becomes
    ``{}``.

    Must be called from inside a running event loop (the caller is
    already in an async handler / dispatch coroutine — that's the only
    place lifecycle events emit from). When no loop is running (a
    misconfigured sync caller), we fall back to logging a warning so
    the contract — "never break the primary path" — still holds.
    """
    kind_str = kind.value if isinstance(kind, EventKind) else str(kind)
    event = Event(
        tenant_id=tenant_id,
        kind=kind_str,
        subject=subject,
        data=data or {},
    )

    async def _record() -> None:
        try:
            await storage.record_event(event)
        except Exception:
            # Swallow + log: the primary path has already committed its
            # work. A storage flake here is observability noise, not a
            # correctness failure.
            logger.warning(
                "event_record_failed kind=%s subject=%s tenant_id=%s — primary path unaffected",
                event.kind,
                event.subject,
                event.tenant_id,
                exc_info=True,
            )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — caller is misconfigured (or this is being
        # invoked from a sync context). Log and bail; never raise.
        logger.warning(
            "event_emit_skipped_no_loop kind=%s subject=%s tenant_id=%s",
            event.kind,
            event.subject,
            event.tenant_id,
        )
        return

    task = loop.create_task(_record())
    # Drop the strong reference to keep the task GC-safe; the loop owns
    # the lifetime now. ``add_done_callback`` keeps a weak link for the
    # logger to pick up an exception if the task somehow re-raised
    # outside our try/except (defensive — _record() above already
    # catches everything).
    task.add_done_callback(_drop_completed_task_exception)


def _drop_completed_task_exception(task: asyncio.Task[None]) -> None:
    """Final swallow: ensure a completed background task's exception
    (if any escaped the inner try/except) doesn't surface as a
    "Task exception was never retrieved" warning at GC time."""
    with contextlib.suppress(Exception):
        task.exception()


__all__ = ["emit_event"]
