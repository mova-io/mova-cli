"""Connection-status state machine for the playground (pure logic).

Tracks whether the runtime is reachable and surfaces one of three states:

* **CONNECTED** — the last health check completed within the fast threshold.
* **SLOW** — the last health check succeeded but took longer than
  ``SLOW_THRESHOLD_S`` (e.g. a cold-start or network hiccup).
* **DISCONNECTED** — the last health check timed out or raised an error.

The state machine is **pure logic** (no Chainlit, no httpx at import) so it
is unit-testable in isolation. The Chainlit app (:mod:`movate.playground.app`)
instantiates a :class:`ConnectionMonitor` per chat session and calls
:meth:`~ConnectionMonitor.check` after each ``on_message`` /
``on_audio_chunk`` call, then compares the returned :class:`ConnectionState`
to the previous one to decide whether to emit a status banner.

Checking after every turn rather than on a timer keeps the playground
dependency-light (no background tasks / asyncio loops beyond Chainlit's own)
while still surfacing drops to the operator within one turn.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Thresholds (tunable at construction time for tests)
# ---------------------------------------------------------------------------

#: Responses faster than this are CONNECTED.
DEFAULT_FAST_THRESHOLD_S: float = 2.0
#: Timeout used for the lightweight HEAD/GET health probe.
DEFAULT_PROBE_TIMEOUT_S: float = 5.0


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------


class ConnectionState(StrEnum):
    """Three-level runtime reachability indicator.

    String subclass so the value can be used in format strings without
    ``.value`` boilerplate (e.g. ``f"state: {state}"``).
    """

    CONNECTED = "CONNECTED"
    SLOW = "SLOW"
    DISCONNECTED = "DISCONNECTED"

    @property
    def emoji(self) -> str:
        """Traffic-light emoji for UI display."""
        return {"CONNECTED": "🟢", "SLOW": "🟡", "DISCONNECTED": "🔴"}[self.value]

    @property
    def label(self) -> str:
        """Short human-readable label."""
        return {"CONNECTED": "Connected", "SLOW": "Slow", "DISCONNECTED": "Disconnected"}[
            self.value
        ]

    def banner(self) -> str:
        """One-line status banner shown in the chat."""
        return f"{self.emoji} Runtime: **{self.label}**"


# ---------------------------------------------------------------------------
# Protocol — the slim interface ConnectionMonitor needs from httpx
# ---------------------------------------------------------------------------


class _HttpClient(Protocol):
    """Structural type matching the subset of ``httpx.AsyncClient`` we use."""

    async def get(self, url: str, **kwargs: Any) -> Any: ...


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


@dataclass
class ConnectionMonitor:
    """Session-scoped runtime reachability checker.

    Performs a lightweight ``GET /api/v1/capabilities`` (already the
    playground's warmup probe) to measure round-trip time and classify the
    connection. Results are cached for ``cache_ttl_s`` seconds so rapid-fire
    messages don't each probe the runtime.

    Attributes
    ----------
    client:
        The ``httpx.AsyncClient`` from the session's :class:`PlaygroundClient`.
        Injected so tests can pass a fake.
    fast_threshold_s:
        Round-trip latency below which the state is CONNECTED.
    probe_timeout_s:
        Per-probe HTTP timeout; failure → DISCONNECTED.
    cache_ttl_s:
        Minimum seconds between actual probes (result is cached in-between).
    """

    client: _HttpClient
    fast_threshold_s: float = DEFAULT_FAST_THRESHOLD_S
    probe_timeout_s: float = DEFAULT_PROBE_TIMEOUT_S
    cache_ttl_s: float = 10.0

    _last_state: ConnectionState = field(default=ConnectionState.CONNECTED, init=False)
    # ``-inf`` rather than 0.0 so the first check always probes, regardless of
    # what value ``time.monotonic()`` happens to return on the host (on Linux
    # it's time-since-boot, which on a fresh CI runner can be smaller than the
    # default ``cache_ttl_s=10s`` — making 0.0 look like a still-fresh entry
    # and short-circuiting the first probe).
    _last_check_at: float = field(default=float("-inf"), init=False)
    _last_duration_s: float | None = field(default=None, init=False)

    @property
    def state(self) -> ConnectionState:
        """The most recently measured connection state (cached)."""
        return self._last_state

    @property
    def last_duration_s(self) -> float | None:
        """Round-trip time of the last actual probe (``None`` before first probe)."""
        return self._last_duration_s

    async def check(self) -> ConnectionState:
        """Probe the runtime and return the current :class:`ConnectionState`.

        Returns the cached state when the last probe is still within
        ``cache_ttl_s`` — so rapid ``on_message`` calls don't hammer the API.

        On success: latency < ``fast_threshold_s`` → CONNECTED, else SLOW.
        On any error (timeout, connection refused, HTTP error): DISCONNECTED.
        """
        now = time.monotonic()
        if now - self._last_check_at < self.cache_ttl_s:
            return self._last_state

        try:
            import httpx  # noqa: PLC0415 — lazy; not all callers need it

            t0 = time.monotonic()
            await self.client.get(
                "/api/v1/capabilities",
                timeout=httpx.Timeout(self.probe_timeout_s),
            )
            duration = time.monotonic() - t0
            self._last_duration_s = duration
            self._last_state = (
                ConnectionState.CONNECTED
                if duration < self.fast_threshold_s
                else ConnectionState.SLOW
            )
        except Exception:
            self._last_duration_s = None
            self._last_state = ConnectionState.DISCONNECTED

        self._last_check_at = time.monotonic()
        return self._last_state

    def status_changed(self, previous: ConnectionState) -> bool:
        """True when the current state differs from ``previous``."""
        return self._last_state != previous


# ---------------------------------------------------------------------------
# UI message builders (called from app.py — pure strings, no Chainlit import)
# ---------------------------------------------------------------------------


def unreachable_banner() -> str:
    """Warning shown when the runtime goes down mid-session."""
    return "⚠️ Runtime unreachable — retrying..."


def reconnected_banner() -> str:
    """Confirmation shown when the runtime recovers."""
    return "✓ Reconnected"


def slow_banner(duration_s: float) -> str:
    """Notice shown when responses take longer than the fast threshold."""
    return f"🟡 Runtime responding slowly ({duration_s:.1f}s)"


__all__ = [
    "DEFAULT_FAST_THRESHOLD_S",
    "DEFAULT_PROBE_TIMEOUT_S",
    "ConnectionMonitor",
    "ConnectionState",
    "reconnected_banner",
    "slow_banner",
    "unreachable_banner",
]
