"""The ``VoiceObserver`` hook — structured router events (ADR 068 D7).

The resilient router emits structured events (a provider was selected, a failover
happened, a breaker opened, a retry was taken) through this thin Protocol. A bare
install gets a no-op (or a stderr observer for debugging); **mdk** wires an
observer that forwards to its metering (ADR 036) and observability-intelligence
(ADR 047) layers — so the *same* router runs measured inside mdk and silent on a
Lyzr deployment, **without this package importing any mdk seam**.

The event names are a small, stable vocabulary:

* ``provider_selected`` — ``provider``, ``kind`` (the chosen provider for a turn).
* ``retry`` — ``provider``, ``failure``, ``attempt`` (a same-provider retry).
* ``failover`` — ``from``, ``to``, ``failure`` (moved to the next provider).
* ``circuit_open`` / ``circuit_close`` — ``provider`` (breaker state changed).
* ``exhausted`` — ``kind``, ``failure`` (every provider failed; the caller's
  error path / ADR 048 text degrade takes over).

The pipeline's speculative-kickoff stage (ADR 070) reuses the same hook:

* ``speculation_started`` — ``chars`` (an agent turn fired on a stable interim).
* ``speculation_committed`` — ``head_start_ms`` (the interim matched the final;
  the agent had already run this long → the latency the speculation saved).
* ``speculation_cancelled`` — (the interim was superseded; the run is discarded).
"""

from __future__ import annotations

import sys
from collections import Counter
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class VoiceObserver(Protocol):
    """Receives one router event with arbitrary structured fields."""

    def on_event(self, event: str, /, **fields: Any) -> None: ...


class NullObserver:
    """Default observer: drop every event (a bare install measures nothing)."""

    def on_event(self, event: str, /, **fields: Any) -> None:
        return None


class StderrObserver:
    """Debug observer: print each event to stderr as ``event key=value …``."""

    def on_event(self, event: str, /, **fields: Any) -> None:
        parts = " ".join(f"{k}={v}" for k, v in fields.items())
        print(f"[voice] {event} {parts}".rstrip(), file=sys.stderr)


class MetricsObserver:
    """Aggregating observer for operability — counts the router's events.

    A drop-in :class:`VoiceObserver` that tallies what the router did so an
    operator can read provider health/usage off :meth:`snapshot` (or scrape it
    into a metrics backend). In-process and lock-free: intended per-worker.
    """

    def __init__(self) -> None:
        self.events: Counter[str] = Counter()
        self.provider_selected: Counter[str] = Counter()
        self.engines: Counter[str] = Counter()  # the REAL serving engine (D7 #1)
        self.failovers: Counter[str] = Counter()
        self.circuit_open: Counter[str] = Counter()
        self.retries: Counter[str] = Counter()
        self.hedge_wins: Counter[str] = Counter()
        self.escalations = 0
        self.cache_hits = 0
        self.silence_frames_dropped = 0
        self.silence_frames_kept = 0
        # Speculative-kickoff outcomes (ADR 070/073): how often a speculation
        # fired, committed (paid off), or was cancelled (wasted compute), plus
        # the total head-start the commits bought. The A/B signal for the
        # ``speculative`` default-flip decision.
        self.speculations_started = 0
        self.speculations_committed = 0
        self.speculations_cancelled = 0
        self.speculation_head_start_ms_total = 0

    def on_event(self, event: str, /, **fields: Any) -> None:
        self.events[event] += 1
        if event == "provider_selected":
            self.provider_selected[str(fields.get("provider", ""))] += 1
        elif event == "stt_engine":
            # The actual engine that served (reported by ConfidenceGatedSTT etc.),
            # so ops can answer "what transcribed this?" even through wrappers.
            self.engines[str(fields.get("provider", ""))] += 1
            if fields.get("escalated"):
                self.escalations += 1
        elif event == "failover":
            self.failovers[str(fields.get("from", ""))] += 1
        elif event == "circuit_open":
            self.circuit_open[str(fields.get("provider", ""))] += 1
        elif event == "retry":
            self.retries[str(fields.get("provider", ""))] += 1
        elif event == "hedge_won":
            self.hedge_wins[str(fields.get("provider", ""))] += 1
        elif event == "cache_hit":
            self.cache_hits += 1
        elif event == "audio_gated":
            self.silence_frames_dropped += int(fields.get("dropped", 0))
            self.silence_frames_kept += int(fields.get("kept", 0))
        elif event == "speculation_started":
            self.speculations_started += 1
        elif event == "speculation_committed":
            self.speculations_committed += 1
            self.speculation_head_start_ms_total += int(fields.get("head_start_ms", 0))
        elif event == "speculation_cancelled":
            self.speculations_cancelled += 1

    def reset(self) -> None:
        """Zero every counter (useful between demo scenarios)."""
        self.events.clear()
        self.provider_selected.clear()
        self.engines.clear()
        self.failovers.clear()
        self.circuit_open.clear()
        self.retries.clear()
        self.hedge_wins.clear()
        self.escalations = 0
        self.cache_hits = 0
        self.silence_frames_dropped = 0
        self.silence_frames_kept = 0
        self.speculations_started = 0
        self.speculations_committed = 0
        self.speculations_cancelled = 0
        self.speculation_head_start_ms_total = 0

    def speculation_snapshot(self) -> dict[str, Any]:
        """Just the speculative-kickoff outcomes + the derived A/B metrics.

        ``commit_ratio`` — committed ÷ started (how often a speculation paid
        off; the lever's hit rate). ``avg_head_start_ms`` — mean latency saved
        per committed turn (how much it bought when it did). Both are ``0.0``
        when nothing speculated, so a caller can render them unconditionally.
        """
        started = self.speculations_started
        committed = self.speculations_committed
        return {
            "started": started,
            "committed": committed,
            "cancelled": self.speculations_cancelled,
            "commit_ratio": (committed / started) if started else 0.0,
            "avg_head_start_ms": (
                (self.speculation_head_start_ms_total / committed) if committed else 0.0
            ),
        }

    def snapshot(self) -> dict[str, Any]:
        """A plain-dict view of the counters (safe to serialize/log)."""
        return {
            "events": dict(self.events),
            "provider_selected": dict(self.provider_selected),
            "engines": dict(self.engines),
            "escalations": self.escalations,
            "failovers": dict(self.failovers),
            "circuit_open": dict(self.circuit_open),
            "retries": dict(self.retries),
            "hedge_wins": dict(self.hedge_wins),
            "cache_hits": self.cache_hits,
            "silence_frames_dropped": self.silence_frames_dropped,
            "silence_frames_kept": self.silence_frames_kept,
            "speculation": self.speculation_snapshot(),
        }
