"""Alert routing seam — telemetry signals → notification sinks (ADR 057).

This module is the **wire** ADR 057 describes: sources emit typed
:class:`AlertEvent`s; an :class:`AlertRouter` matches each event against a
configured, ordered **route table** and delivers it best-effort to one or more
**sinks**. Sources and sinks never know about each other (no NxM wiring).

It is **additive and opt-in** (ADR 057, CLAUDE.md rules 4/5): with no routes
configured the router is empty and *nothing fires* — exactly today's behavior.

What lives here (ADR 057 D1-D5):

* **D1** — :class:`AlertEvent` + :class:`AlertKind` (StrEnum, grows additively)
  + :class:`Severity`. A small typed event every alert source emits.
* **D2** — :class:`AlertRouter` over a :class:`RouteTable` of :class:`Route`s.
  A ``match`` is an AND of ``{kind?, min_severity?, tenant?, subject_glob?}``;
  first-match or all-match (configurable). No routes ⇒ no delivery.
* **D3** — the :class:`AlertSink` Protocol (the alert-delivery contract). The
  concrete HTTP sinks (Slack / Teams / generic webhook) live in
  :mod:`movate.core.alert_sinks` as thin adapters; they ride the existing BYOK
  env seam (ADR 018) for credentials and add **no new shipped dependency**.
* **D4** — per ``(route, dedup_key)`` :class:`Throttle` window. A flapping
  signal pages once per window; suppressed duplicates are counted and the count
  is surfaced on the next delivery (``+37 since …``).
* **D5** — delivery is **best-effort**: a sink that raises or times out is
  logged and dropped, **never** propagated back to the caller (alerting must
  never break execution). An optional in-memory :class:`DeliveryLog` records
  sent / suppressed / failed for audit.

Boundary discipline (CLAUDE.md rule 6): this module is pure routing + data + a
Protocol. It does **not** import a concrete sink, the storage layer, or the
runtime. Sinks are adapters resolved by name from a registry the caller wires
at the edge. Step 2 (wiring drift / dead-letter / budget sources to emit
``AlertEvent``s) is a separate PR — nothing emits here yet.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from enum import IntEnum, StrEnum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

# Default throttle window (ADR 057 D4). Conservative on purpose — one delivery
# per (route, dedup_key) per 15 minutes prevents a flapping signal from paging
# someone hundreds of times. Operators tune it per-route or globally in config.
DEFAULT_THROTTLE_WINDOW = timedelta(minutes=15)


# ---------------------------------------------------------------------------
# D1 — typed alert event + enums
# ---------------------------------------------------------------------------


class Severity(IntEnum):
    """Alert severity, ordered so ``min_severity`` gating is a simple compare.

    ``IntEnum`` (not ``StrEnum``) precisely because routes gate on
    ``min_severity`` — ``event.severity >= route.min_severity`` is the whole
    point (ADR 057 D4). The string form (``"warning"``) round-trips through
    :meth:`from_str` / :attr:`label` for config + payloads.
    """

    INFO = 10
    WARNING = 20
    CRITICAL = 30

    @property
    def label(self) -> str:
        """Lower-case name used in config + payloads (``"warning"``)."""
        return self.name.lower()

    @classmethod
    def from_str(cls, value: str | Severity) -> Severity:
        """Parse a config/payload string (case-insensitive) into a member."""
        if isinstance(value, Severity):
            return value
        try:
            return cls[str(value).strip().upper()]
        except KeyError as exc:
            valid = ", ".join(s.label for s in cls)
            raise ValueError(f"unknown severity {value!r}; expected one of: {valid}") from exc


class AlertKind(StrEnum):
    """Canonical alert kinds (ADR 057 D1). Grows **additively** — a new source
    adds a new value, never a migration (kinds are matched as plain strings).

    The step-1 set mirrors the sources ADR 057 names; step 2 wires them to
    actually emit. ``job_failure_rate`` is included for the dead-letter /
    retry surface.
    """

    DRIFT_REGRESSION = "drift_regression"
    """A scheduled eval regressed vs. its baseline (ADR 016)."""

    DEAD_LETTER_SPIKE = "dead_letter_spike"
    """Dead-letter accumulation crossed a threshold (``core/job_retry``)."""

    BUDGET_THRESHOLD = "budget_threshold"
    """A per-tenant budget crossed a burn threshold (ADR 036)."""

    SLO_BREACH = "slo_breach"
    """An app-evaluated golden-signal SLO was breached (tracker #27 / D6)."""

    JOB_FAILURE_RATE = "job_failure_rate"
    """Job failure rate over a window crossed a threshold."""


class AlertEvent(BaseModel):
    """A typed, source-agnostic alert (ADR 057 D1).

    Every alert source emits one of these; the router resolves it to sinks. The
    source sets :attr:`severity` and a stable :attr:`dedup_key` so throttle /
    dedup (D4) can identify "the same alert" across repeats.
    """

    model_config = ConfigDict(extra="forbid")

    kind: AlertKind
    """What condition fired (see :class:`AlertKind`)."""

    severity: Severity
    """How bad — gates routes via ``min_severity`` (D4)."""

    tenant_id: str
    """The tenant the alert is about. Routes may match on ``tenant``."""

    subject: str
    """The agent / job / tenant the alert concerns. Routes may glob on it."""

    summary: str
    """One-line, human-readable: what happened + how bad."""

    data: dict[str, Any] = Field(default_factory=dict)
    """Structured context — scores, thresholds, ids, trace_id. Keep small."""

    dedup_key: str
    """Stable key for throttle/dedup (D4). Same logical alert ⇒ same key."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    """Opaque per-emission id (audit / delivery-log correlation)."""

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    """UTC emission time."""

    @field_validator("severity", mode="before")
    @classmethod
    def _coerce_severity(cls, v: Any) -> Any:
        if isinstance(v, str):
            return Severity.from_str(v)
        return v


# ---------------------------------------------------------------------------
# D2 — route table
# ---------------------------------------------------------------------------


class RouteMatch(BaseModel):
    """The predicate of a route — an **AND** of the present criteria (D2).

    An empty match (``{}``) is the catch-all (matches every event). Any absent
    field is "don't care".
    """

    model_config = ConfigDict(extra="forbid")

    kind: AlertKind | None = None
    min_severity: Severity | None = None
    tenant: str | None = None
    subject_glob: str | None = None
    """``fnmatch``-style glob against :attr:`AlertEvent.subject` (case-sensitive
    via :func:`fnmatch.fnmatchcase`, so ``acme-*`` matches ``acme-billing``)."""

    @field_validator("min_severity", mode="before")
    @classmethod
    def _coerce_min_severity(cls, v: Any) -> Any:
        if v is None or isinstance(v, Severity):
            return v
        if isinstance(v, str):
            return Severity.from_str(v)
        return v

    def matches(self, event: AlertEvent) -> bool:
        """True iff every *present* criterion matches the event (AND)."""
        if self.kind is not None and event.kind != self.kind:
            return False
        if self.min_severity is not None and event.severity < self.min_severity:
            return False
        if self.tenant is not None and event.tenant_id != self.tenant:
            return False
        return self.subject_glob is None or fnmatchcase(event.subject, self.subject_glob)


class Route(BaseModel):
    """One row of the route table (D2): a match → a sink (by name)."""

    model_config = ConfigDict(extra="forbid")

    match: RouteMatch = Field(default_factory=RouteMatch)
    sink: str
    """Name of a sink in the registry the caller wired (e.g. ``ops-slack``)."""

    throttle_window_seconds: int | None = None
    """Per-route override of the throttle window (D4). Absent ⇒ table default."""

    @field_validator("sink")
    @classmethod
    def _sink_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("route.sink must be a non-empty sink name")
        return v.strip()


class RouteTable(BaseModel):
    """The ordered route table loaded from config (D2).

    ``first_match`` (default) selects the **first** matching route's sink;
    ``first_match: false`` selects **every** matching route's sink (fan-out).
    """

    model_config = ConfigDict(extra="forbid")

    routes: list[Route] = Field(default_factory=list)
    first_match: bool = True
    throttle_window_seconds: int = int(DEFAULT_THROTTLE_WINDOW.total_seconds())

    def resolve(self, event: AlertEvent) -> list[Route]:
        """Return the route(s) whose match selects this event, honoring
        :attr:`first_match`. Empty list ⇒ no delivery (opt-in)."""
        selected: list[Route] = []
        for route in self.routes:
            if route.match.matches(event):
                selected.append(route)
                if self.first_match:
                    break
        return selected


# ---------------------------------------------------------------------------
# D3 — sink contract (concrete sinks live in alert_sinks.py)
# ---------------------------------------------------------------------------


@runtime_checkable
class AlertSink(Protocol):
    """The alert-delivery contract (ADR 057 D3).

    This is the alerting-side companion to ``NotificationDispatcher`` in
    ``core/notify.py``: concrete sinks (Slack / Teams / webhook) are thin
    adapters behind this Protocol, just as SMTP / console sit behind the
    dispatcher. Implementations should surface failure by returning ``False``
    or raising; the router (D5) is what guarantees a failure never reaches the
    source.
    """

    name: str
    """Short id for ops logging + route references (``ops-slack``)."""

    async def deliver(self, event: AlertEvent, *, suppressed_count: int = 0) -> bool:
        """Deliver one event. ``suppressed_count`` is how many duplicates were
        throttled since the last delivery (D4) — sinks surface it in the
        payload. Return ``True`` on success."""
        ...


class SinkRegistry:
    """Name → :class:`AlertSink` lookup the caller wires at the edge.

    The router resolves a route's ``sink`` name through this registry, so the
    routing logic never imports a concrete sink (boundary discipline).
    """

    def __init__(self, sinks: Iterable[AlertSink] | None = None) -> None:
        self._sinks: dict[str, AlertSink] = {}
        for sink in sinks or ():
            self.register(sink)

    def register(self, sink: AlertSink) -> None:
        self._sinks[sink.name] = sink

    def get(self, name: str) -> AlertSink | None:
        return self._sinks.get(name)

    def names(self) -> list[str]:
        return list(self._sinks)


# ---------------------------------------------------------------------------
# D4 — throttle + dedup
# ---------------------------------------------------------------------------


class Throttle:
    """Per ``(route, dedup_key)`` window de-duplicator (ADR 057 D4).

    The first event for a key in a window is *admitted*; further events for the
    same key inside the window are *suppressed* and counted. When the window
    rolls over (or no prior delivery exists), the next admitted event carries
    the accumulated suppressed count so operators see ``+37 since 12:04``.

    In-memory and deterministic (uses the ``now`` passed in) so it's trivial to
    test. A flapping signal pages once per window, period.
    """

    def __init__(self, *, default_window: timedelta = DEFAULT_THROTTLE_WINDOW) -> None:
        self._default_window = default_window
        # key -> (last_admitted_at, suppressed_since_last_admit)
        self._state: dict[str, tuple[datetime, int]] = {}

    @staticmethod
    def _key(route: Route, event: AlertEvent) -> str:
        return f"{route.sink}\x00{event.dedup_key}"

    def _window_for(self, route: Route) -> timedelta:
        if route.throttle_window_seconds is not None:
            return timedelta(seconds=route.throttle_window_seconds)
        return self._default_window

    def admit(
        self, route: Route, event: AlertEvent, *, now: datetime | None = None
    ) -> tuple[bool, int]:
        """Decide whether to deliver ``event`` on ``route``.

        Returns ``(admit, suppressed_count)``:

        * ``(True, n)`` — deliver now; ``n`` duplicates were suppressed since
          the previous delivery (0 on the first ever / post-window delivery).
        * ``(False, n)`` — suppressed (inside the active window); do not deliver.
        """
        now = now or datetime.now(UTC)
        key = self._key(route, event)
        window = self._window_for(route)
        prior = self._state.get(key)
        if prior is None or (now - prior[0]) >= window:
            # First ever, or the window has elapsed → admit and report the
            # count accumulated while suppressed (then reset the counter).
            suppressed = 0 if prior is None else prior[1]
            self._state[key] = (now, 0)
            return True, suppressed
        # Inside the active window → suppress, bump the counter.
        self._state[key] = (prior[0], prior[1] + 1)
        return False, prior[1] + 1


# ---------------------------------------------------------------------------
# D5 — delivery log (optional, in-memory)
# ---------------------------------------------------------------------------


class DeliveryStatus(StrEnum):
    SENT = "sent"
    SUPPRESSED = "suppressed"
    FAILED = "failed"
    NO_SINK = "no_sink"


class DeliveryRecord(BaseModel):
    """One audit row for the optional delivery log (ADR 057 D5)."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    kind: AlertKind
    severity: Severity
    tenant_id: str
    subject: str
    sink: str
    status: DeliveryStatus
    suppressed_count: int = 0
    error: str | None = None
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DeliveryLog:
    """In-memory ring of recent :class:`DeliveryRecord`s (opt-in audit, D5).

    Best-effort + bounded so it never grows unboundedly in a long-lived worker.
    A durable ``alert_deliveries`` table is a later, additive step (ADR 057).
    """

    def __init__(self, *, capacity: int = 1000) -> None:
        self._capacity = capacity
        self._records: list[DeliveryRecord] = []

    def record(self, record: DeliveryRecord) -> None:
        self._records.append(record)
        if len(self._records) > self._capacity:
            del self._records[: len(self._records) - self._capacity]

    def records(self) -> list[DeliveryRecord]:
        return list(self._records)


# ---------------------------------------------------------------------------
# D2/D5 — the router
# ---------------------------------------------------------------------------


class AlertRouter:
    """Resolves each :class:`AlertEvent` to sink(s) and delivers best-effort.

    Wiring (at the edge): the caller builds a :class:`SinkRegistry`, a
    :class:`RouteTable` (from config), and passes them here. The router never
    imports a concrete sink — it resolves names through the registry.

    Delivery is **best-effort** (ADR 057 D5): a sink that raises / times out /
    returns ``False`` is logged and dropped; :meth:`route` never raises on a
    sink failure. With an empty table it's a no-op (opt-in).
    """

    def __init__(
        self,
        *,
        table: RouteTable | None = None,
        registry: SinkRegistry | None = None,
        throttle: Throttle | None = None,
        delivery_log: DeliveryLog | None = None,
    ) -> None:
        self._table = table or RouteTable()
        self._registry = registry or SinkRegistry()
        self._throttle = throttle or Throttle(
            default_window=timedelta(seconds=self._table.throttle_window_seconds)
        )
        self._log = delivery_log

    @property
    def is_active(self) -> bool:
        """True iff at least one route is configured. ``False`` ⇒ pure no-op."""
        return bool(self._table.routes)

    async def route(self, event: AlertEvent, *, now: datetime | None = None) -> None:
        """Route + deliver one event, best-effort. Never raises on delivery
        failure (D5). No matching route ⇒ silently dropped (opt-in)."""
        try:
            await self._route_inner(event, now=now)
        except Exception:
            # The whole point of D5: alerting must never break the caller.
            logger.warning(
                "alert_router_unexpected_error event_id=%s kind=%s — dropped; "
                "the emitting source is unaffected",
                event.id,
                event.kind.value,
                exc_info=True,
            )

    async def _route_inner(self, event: AlertEvent, *, now: datetime | None) -> None:
        routes = self._table.resolve(event)
        if not routes:
            return
        for route in routes:
            admit, suppressed = self._throttle.admit(route, event, now=now)
            if not admit:
                logger.debug(
                    "alert_suppressed sink=%s dedup_key=%s suppressed=%d",
                    route.sink,
                    event.dedup_key,
                    suppressed,
                )
                self._record(event, route.sink, DeliveryStatus.SUPPRESSED, suppressed)
                continue
            sink = self._registry.get(route.sink)
            if sink is None:
                logger.warning(
                    "alert_sink_unknown sink=%s event_id=%s — route references a "
                    "sink that isn't registered; dropped",
                    route.sink,
                    event.id,
                )
                self._record(event, route.sink, DeliveryStatus.NO_SINK, suppressed)
                continue
            await self._deliver(sink, route, event, suppressed)

    async def _deliver(
        self, sink: AlertSink, route: Route, event: AlertEvent, suppressed: int
    ) -> None:
        try:
            ok = await sink.deliver(event, suppressed_count=suppressed)
        except Exception as exc:  # best-effort (D5)
            logger.warning(
                "alert_delivery_failed sink=%s event_id=%s kind=%s — logged + "
                "dropped; the emitting source is unaffected",
                route.sink,
                event.id,
                event.kind.value,
                exc_info=True,
            )
            self._record(event, route.sink, DeliveryStatus.FAILED, suppressed, error=str(exc))
            return
        if ok:
            self._record(event, route.sink, DeliveryStatus.SENT, suppressed)
        else:
            logger.warning(
                "alert_delivery_unsuccessful sink=%s event_id=%s — sink returned "
                "false; logged + dropped",
                route.sink,
                event.id,
            )
            self._record(event, route.sink, DeliveryStatus.FAILED, suppressed)

    def _record(
        self,
        event: AlertEvent,
        sink: str,
        status: DeliveryStatus,
        suppressed: int,
        *,
        error: str | None = None,
    ) -> None:
        if self._log is None:
            return
        self._log.record(
            DeliveryRecord(
                event_id=event.id,
                kind=event.kind,
                severity=event.severity,
                tenant_id=event.tenant_id,
                subject=event.subject,
                sink=sink,
                status=status,
                suppressed_count=suppressed,
                error=error,
            )
        )


# ---------------------------------------------------------------------------
# Config loading (D2) — alerts.yaml / movate.yaml `alerts:` block
# ---------------------------------------------------------------------------


def load_route_table(data: Mapping[str, Any] | None) -> RouteTable:
    """Build a :class:`RouteTable` from a parsed ``alerts:`` mapping.

    ``data`` is the ``alerts:`` block (from ``alerts.yaml`` or a
    ``movate.yaml`` ``alerts:`` key). ``None`` / empty ⇒ an **empty** table
    (opt-in: absent config = no behavior change). Validation errors propagate
    (a malformed route table is an operator config error, surfaced loudly — not
    silently ignored).
    """
    if not data:
        return RouteTable()
    return RouteTable.model_validate(dict(data))


# Standalone alert-config filename, sibling to ``project.yaml`` /
# ``policy.yaml`` (the canonical-split pattern in ``core/config.py``). Opt-in:
# absent ⇒ an empty router (no behavior change).
ALERTS_FILE_NAME = "alerts.yaml"

# Project base files that may carry an inline ``alerts:`` block, in the same
# precedence order ``load_project_config`` uses. A dedicated ``alerts.yaml``
# wins over an inline block (dedicated-file-wins, matching the rest of config).
_PROJECT_BASE_FILES: tuple[str, ...] = ("project.yaml", "policy.yaml", "movate.yaml")


def load_alert_routes(root: Path | str | None = None) -> RouteTable:
    """Discover + load the alert route table from the project root (D2).

    Resolution (opt-in; any absent layer is a silent no-op):

    1. A dedicated ``alerts.yaml`` at ``root`` (whole file is the ``alerts:``
       body — i.e. a top-level ``routes:`` / ``first_match:`` /
       ``throttle_window_seconds:``). **Wins** if present.
    2. Otherwise, an ``alerts:`` block inside the project base file
       (``project.yaml`` → ``policy.yaml`` → ``movate.yaml``, first found).

    Neither present ⇒ an **empty** :class:`RouteTable` (router is a no-op;
    zero behavior change — ADR 057 opt-in). A malformed table raises (operator
    config error, surfaced loudly — never silently ignored), matching
    ``load_project_config``'s "never silently degrade on a typo" contract.
    """
    base = Path(root) if root is not None else Path.cwd()

    dedicated = base / ALERTS_FILE_NAME
    if dedicated.is_file():
        data = yaml.safe_load(dedicated.read_text()) or {}
        return load_route_table(data)

    for name in _PROJECT_BASE_FILES:
        candidate = base / name
        if candidate.is_file():
            doc = yaml.safe_load(candidate.read_text()) or {}
            block = doc.get("alerts") if isinstance(doc, Mapping) else None
            return load_route_table(block)

    return RouteTable()


__all__ = [
    "ALERTS_FILE_NAME",
    "DEFAULT_THROTTLE_WINDOW",
    "AlertEvent",
    "AlertKind",
    "AlertRouter",
    "AlertSink",
    "DeliveryLog",
    "DeliveryRecord",
    "DeliveryStatus",
    "Route",
    "RouteMatch",
    "RouteTable",
    "Severity",
    "SinkRegistry",
    "Throttle",
    "load_alert_routes",
    "load_route_table",
]
