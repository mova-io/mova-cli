"""Universal ``?wait=`` long-poll for the async-job GET endpoints.

A client that submitted an async job (``POST /run`` → ``job_id``) can poll
``GET /api/v1/jobs/{job_id}`` until the job reaches a terminal state. Tight
client poll loops are wasteful (one round-trip per check, plus auth + a DB
hit each time). ``?wait=30s`` lets the *server* hold the request open and
return as soon as the job finishes — at most one round-trip per terminal
transition.

Design (cross-process correct, non-busy):

* The worker that advances a job is frequently a DIFFERENT process than the
  API replica serving this GET (separate ACA replicas). An in-process
  ``asyncio.Event`` the worker signals would therefore never fire for the
  waiting GET. So we use a **bounded poll** instead: re-read the job from
  storage every :data:`POLL_INTERVAL_SECONDS`, wrapped in an overall ``wait``
  deadline. ``asyncio.sleep`` yields the event loop between checks, so this
  does NOT busy-wait and does NOT block other requests on the same worker.
* Each check **acquires and releases** the storage connection independently
  (``StorageProvider.get_job`` is request-scoped) — we never hold a DB
  connection open across the whole ``wait`` window.
* The loop exits **immediately** on a terminal state or if the client
  disconnects mid-wait (``request.is_disconnected()`` between checks), so an
  abandoned long-poll doesn't pin a worker for the full deadline.

A future enhancement could replace the poll with Postgres ``LISTEN/NOTIFY``
push (as the SSE ADR noted) to drop the worst-case latency to ~0; that is a
separate change and is intentionally NOT built here.

The ``wait`` value is clamped to :data:`MAX_WAIT_SECONDS` so a client can't
ask an API replica to hold a connection open arbitrarily long.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request, Response

    from movate.core.models import JobRecord
    from movate.storage.base import StorageProvider

# Server-enforced ceiling on ``wait`` (seconds). A requested wait larger than
# this is clamped down (and the clamp is surfaced via a header), so no single
# long-poll can hold an API connection open beyond this. Kept comfortably under
# typical proxy/load-balancer idle timeouts (e.g. Azure Front Door ~4 min).
MAX_WAIT_SECONDS: float = 60.0

# How long to sleep between storage re-checks while waiting. Small enough that
# terminal transitions surface quickly, large enough that the poll is cheap.
POLL_INTERVAL_SECONDS: float = 0.5

# Response headers (contract — additive, only ever present on ``?wait=`` calls).
HEADER_POLL_TIMEOUT = "X-MDK-Poll-Timeout"
HEADER_WAIT_CLAMPED = "X-MDK-Wait-Clamped"

# ``<int><unit>`` where unit is s(econds), m(inutes), or h(ours). Bare integers
# are treated as seconds for convenience.
_DURATION_RE = re.compile(r"^\s*(\d+)\s*(s|m|h)?\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}


class DurationParseError(ValueError):
    """``wait`` was not a recognised duration string."""


def parse_duration(value: str) -> float:
    """Parse a duration string (``"30s"``, ``"2m"``, ``"90"``) → seconds.

    Accepts a non-negative integer with an optional unit suffix
    (``s`` / ``m`` / ``h``); a bare integer is seconds. Raises
    :class:`DurationParseError` on anything else (negative, float,
    unknown unit, empty). The result is NOT clamped here — clamping is
    the handler's job (so it can report the clamp) via :func:`resolve_wait`.
    """
    match = _DURATION_RE.match(value)
    if match is None:
        raise DurationParseError(f"invalid duration {value!r} — expected e.g. '30s', '2m', '90'")
    magnitude = int(match.group(1))
    unit = (match.group(2) or "s").lower()
    return float(magnitude) * _UNIT_SECONDS[unit]


def resolve_wait(value: str | None) -> tuple[float, bool]:
    """Resolve the raw ``wait`` query param to ``(seconds, clamped)``.

    ``None`` (param omitted) → ``(0.0, False)`` — i.e. no waiting, the
    handler returns the current state immediately, exactly as before this
    feature existed. A value above :data:`MAX_WAIT_SECONDS` is clamped and
    ``clamped`` is ``True`` so the caller can stamp the clamp header.
    """
    if value is None:
        return 0.0, False
    seconds = parse_duration(value)
    if seconds > MAX_WAIT_SECONDS:
        return MAX_WAIT_SECONDS, True
    return seconds, False


async def long_poll_job(
    *,
    job_id: str,
    tenant_id: str,
    store: StorageProvider,
    request: Request,
    response: Response,
    wait_raw: str | None,
) -> JobRecord | None:
    """Fetch a job, optionally blocking until it is terminal or ``wait`` elapses.

    The single helper behind every async-job GET so they share one
    long-poll implementation. Behaviour:

    * Always does an initial ``get_job``. Returns ``None`` (so the caller
      can 404) if the job doesn't exist for this tenant.
    * ``wait`` omitted → returns the first read immediately (legacy behaviour).
    * Job already terminal → returns immediately regardless of ``wait``.
    * Otherwise polls every :data:`POLL_INTERVAL_SECONDS` until the job is
      terminal, the deadline passes, or the client disconnects — whichever is
      first. On deadline timeout it stamps :data:`HEADER_POLL_TIMEOUT` and
      returns the current (still-running) record (the caller serializes it
      with HTTP 200, so the client knows to poll again).

    Sets :data:`HEADER_WAIT_CLAMPED` to the enforced max when the requested
    ``wait`` exceeded :data:`MAX_WAIT_SECONDS`. Raises
    :class:`DurationParseError` for an unparseable ``wait`` (the caller maps
    it to 400).

    The deadline is measured with the event loop's monotonic clock, so it is
    immune to wall-clock jumps.
    """
    wait_seconds, clamped = resolve_wait(wait_raw)
    if clamped:
        response.headers[HEADER_WAIT_CLAMPED] = _format_seconds(MAX_WAIT_SECONDS)

    record = await store.get_job(job_id, tenant_id=tenant_id)
    if record is None:
        return None
    if wait_seconds <= 0 or record.status.is_terminal:
        return record

    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_seconds
    while True:
        # Bail the moment the client gives up — never pin a worker for the
        # full deadline on an abandoned connection.
        if await request.is_disconnected():
            return record

        remaining = deadline - loop.time()
        if remaining <= 0:
            # Timed out still non-terminal: hand back the latest state and
            # tell the client to come back. HTTP stays 200 (the caller's
            # response_model is unchanged) — the header is the signal.
            response.headers[HEADER_POLL_TIMEOUT] = "true"
            return record

        # Sleep without overshooting the deadline. ``asyncio.sleep`` yields
        # the loop, so other requests on this worker keep flowing.
        await asyncio.sleep(min(POLL_INTERVAL_SECONDS, remaining))

        # Re-read with a fresh, request-scoped connection (acquired + released
        # by ``get_job``); we hold no DB connection across the sleep above.
        latest = await store.get_job(job_id, tenant_id=tenant_id)
        if latest is None:
            # Vanished mid-wait (e.g. retention sweep) — treat as 404.
            return None
        record = latest
        if record.status.is_terminal:
            return record


def _format_seconds(seconds: float) -> str:
    """Render a whole-second duration as ``"<n>s"`` for header values."""
    return f"{int(seconds)}s"
