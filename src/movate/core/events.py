"""Lifecycle event model (ADR 035 D1 — events outbox).

A small, typed, tenant-scoped record of a platform lifecycle event
("a run finished", "an eval failed", "a canary was promoted"). The
runtime emits one at every terminal-state transition at the *edges*
(executor / dispatch / deploy paths), persists it via
:meth:`StorageProvider.record_event`, and the read API
(``GET /api/v1/events``) returns it tenant-scoped.

D1 only records + exposes. D2 (webhook delivery) and D3 (SSE stream)
consume the same outbox in later PRs; the model here is the shape they
will read.

Conventions:

* ``id`` is a uuid4 hex, mirroring the rest of the runtime's
  control-plane ids (``ApiKeyRecord.key_id`` style, ``Trigger.trigger_id``
  style). Stable, opaque, unguessable; doubles as the cursor for
  pagination (see :attr:`StorageProvider.list_events.after_id`).
* ``tenant_id`` is **NOT NULL** at the storage layer — a hard invariant
  matching the rest of the schema (ADR 013/014).
* ``kind`` is intentionally a free-form string with a small canonical
  enum (:class:`EventKind`) for D1; new kinds may be added as new
  domain events ship. Storing the string (not the enum) keeps the
  schema forward-compatible.
* ``subject`` is the human-meaningful **thing the event is about** —
  the agent name, run id, eval id, canary target, etc. Short, low
  cardinality enough to index ``(tenant_id, kind, subject)`` cheaply.
* ``data`` is a small JSON-serializable payload (version, score,
  reason, etc.). Keep it small — this isn't a log replacement.
* ``created_at`` is UTC.

Boundary discipline: this module is pure data + an enum. It does NOT
import the storage layer or runtime — the storage Protocol depends on
it (one-way), and the runtime emits via a small helper that lives next
to its other edge wiring (``movate.runtime.events``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class EventKind(StrEnum):
    """Canonical lifecycle event kinds for ADR 035 D1.

    Short on purpose — only the small set the D1 outbox emits today.
    Adding a kind is additive: emit a new ``EventKind`` value at a new
    edge, no schema/migration change needed (the storage column is a
    free-form string).
    """

    RUN_COMPLETED = "run.completed"
    """A run reached a SUCCESS terminal state."""

    RUN_FAILED = "run.failed"
    """A run reached a non-SUCCESS terminal state — ERROR /
    SAFETY_BLOCKED / DEAD_LETTER / CANCELLED."""

    AGENT_PUBLISHED = "agent.published"
    """A new (name, version) was written to the durable agent
    registry (ADR 014). ``subject`` = agent name; ``data.version`` =
    the published version."""

    AGENT_REVERTED = "agent.reverted"
    """An agent was reverted to a prior version (re-published forward
    as a new immutable row, ADR 014 D3). ``subject`` = agent name;
    ``data`` carries the new + target versions."""

    EVAL_FAILED = "eval.failed"
    """An eval landed below its gate. ``subject`` = agent name;
    ``data`` carries the eval id + score + gate."""

    DRIFT_DETECTED = "drift.detected"
    """A scheduled eval regressed vs. its baseline (ADR 016 D2).
    ``subject`` = agent name; ``data`` carries the drift deltas."""

    CANARY_PROMOTED = "canary.promoted"
    """A canary's challenger was promoted to champion (ADR 016 D3).
    ``subject`` = agent name; ``data`` carries the promoted version
    + mode (assisted/auto)."""

    CANARY_DEMOTED = "canary.demoted"
    """A canary's challenger was demoted — manual rollback OR
    automated rollback on drift regression (ADR 016 D5). ``subject``
    = agent name; ``data.reason`` records the cause."""

    GRAPH_NODE_ADDED = "graph.node.added"
    """A knowledge-graph node was upserted during KB ingest (ADR 046 D6
    growth stream — a typed projection of this ADR 035 outbox). ``subject``
    = agent name; ``data`` carries a one-node graphology fragment plus the
    optional ``project_id`` so the SSE growth stream can replay/live-tail
    it project-scoped. A re-ingest that touches an existing node emits the
    same kind (the viewer merges it idempotently)."""

    GRAPH_EDGE_ADDED = "graph.edge.added"
    """A knowledge-graph edge (relation) was upserted during KB ingest
    (ADR 046 D6). ``subject`` = agent name; ``data`` carries a one-edge
    graphology fragment plus the optional ``project_id``. Same outbox,
    same scoping, same at-least-once + dedupe-on-id semantics as every
    other ADR 035 event."""

    ALERT_RAISED = "alert.raised"
    """An alert source raised a typed alert (ADR 057 D1 step 2). The
    carrier for an :class:`~movate.core.alerts.AlertEvent` on the ADR 035
    outbox: the drift / dead-letter / budget edges emit one of these
    (fire-and-forget), and the alert-router consumer
    (:class:`movate.runtime.alert_worker.AlertWorker`) drains them and
    routes to the configured notification sinks. ``subject`` = the
    alert's subject (agent / job / tenant); ``data`` is the serialized
    ``AlertEvent`` (kind/severity/summary/dedup_key/…). Sources never
    import a sink — they only append this event (boundary discipline,
    ADR 057 D1)."""


class Event(BaseModel):
    """One persisted lifecycle event.

    Stored in the ``events`` outbox table; returned to clients via the
    :class:`EventView` API projection. Tenant-scoped throughout.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    """uuid4 hex (32 lower-hex chars). Opaque, unguessable, stable —
    doubles as the cursor in the list API's ``after_id`` pagination."""

    tenant_id: str
    """The tenant the event belongs to. **NOT NULL** in storage. Every
    list/query is tenant-scoped in the WHERE clause."""

    kind: str
    """Event kind — typically one of :class:`EventKind` (e.g.
    ``"run.completed"``). Stored as free-form text so new kinds emitted
    by newer code don't require a schema migration."""

    subject: str
    """The thing the event is about — agent name / run id / eval id /
    canary target. Short, low-cardinality enough to index against."""

    data: dict[str, Any] = Field(default_factory=dict)
    """Small JSON-serializable payload (version, score, reason, ...).
    Intentionally not a log line — keep payloads small."""

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    """UTC timestamp the event was recorded. Drives the
    ``since`` / ``until`` filters + the (tenant_id, created_at) index."""


class EventView(BaseModel):
    """Wire shape returned by ``GET /api/v1/events``.

    Mirrors :class:`Event` 1:1 today; the separate type matches the
    repo convention (``schemas.py`` decouples wire from persisted
    shape) and gives a stable surface to evolve independently.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str
    kind: str
    subject: str
    data: dict[str, Any]
    created_at: datetime

    @classmethod
    def from_record(cls, record: Event) -> EventView:
        return cls(
            id=record.id,
            tenant_id=record.tenant_id,
            kind=record.kind,
            subject=record.subject,
            data=record.data,
            created_at=record.created_at,
        )


class EventListView(BaseModel):
    """Cursor-paginated event list response (``GET /api/v1/events``).

    ``next_after_id`` is populated only when results were truncated at
    ``limit`` — the caller passes it back as ``?after_id=`` on the next
    request to continue. ``None`` means the page is the tail of the
    matching set.
    """

    model_config = ConfigDict(extra="forbid")

    events: list[EventView]
    count: int
    next_after_id: str | None = None


__all__ = [
    "Event",
    "EventKind",
    "EventListView",
    "EventView",
]
