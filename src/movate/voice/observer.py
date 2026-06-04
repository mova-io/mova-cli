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
from dataclasses import dataclass
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
        # Mid-stream failover outcomes (batch 2, #211): how often a provider
        # failed mid-stream and the composite transparently switched.
        self.midstream_failovers: Counter[str] = Counter()

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
        elif event in ("failover", "midstream_failover"):
            provider = str(fields.get("from") or fields.get("provider", ""))
            self.failovers[provider] += 1
            # Track mid-stream failovers separately for operability (#211).
            self.midstream_failovers[provider] += event == "midstream_failover"
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
        elif event in ("speculation_started", "speculation_committed", "speculation_cancelled"):
            self._record_speculation(event, fields)

    def _record_speculation(self, event: str, fields: dict[str, Any]) -> None:
        if event == "speculation_started":
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
        self.midstream_failovers.clear()

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
            "midstream_failovers": dict(self.midstream_failovers),
        }


# ----------------------------------------------------------------------
# Speculation A/B verdict (ADR 070/073 Phase 1) — turn the live telemetry
# into a flip/no-flip recommendation for the ``speculative`` default.
# ----------------------------------------------------------------------

# Defaults for the verdict thresholds. Speculation is worth defaulting ON when it
# commits often enough that wasted (cancelled) compute is acceptable AND each
# commit buys back meaningful latency — measured, not assumed (ADR 073 D1).
_AB_MIN_SAMPLES = 20  # too few speculations → "insufficient-data", don't decide
_AB_MIN_COMMIT_RATIO = 0.5  # below this, cancelled runs dominate → not worth it
_AB_MIN_HEAD_START_MS = 200.0  # below this, the latency saved is in the noise


@dataclass(frozen=True)
class SpeculationABVerdict:
    """The flip/no-flip recommendation for the ``speculative`` default.

    ``recommendation`` is one of:

    * ``"enable"`` — commit-ratio + head-start both clear the bar over enough
      samples → default speculation ON pays off.
    * ``"hold"`` — enough data, but the ratio or the head-start is too low →
      keep it opt-in (the cancelled-run cost isn't repaid).
    * ``"insufficient-data"`` — fewer than ``min_samples`` speculations fired →
      gather more before deciding.
    """

    started: int
    committed: int
    cancelled: int
    commit_ratio: float
    avg_head_start_ms: float
    recommendation: str
    rationale: str


def speculation_ab_report(
    snapshot: dict[str, Any],
    *,
    min_samples: int = _AB_MIN_SAMPLES,
    min_commit_ratio: float = _AB_MIN_COMMIT_RATIO,
    min_head_start_ms: float = _AB_MIN_HEAD_START_MS,
) -> SpeculationABVerdict:
    """Turn a speculation snapshot into a default-flip verdict (ADR 073 Phase 1).

    ``snapshot`` is either a :meth:`MetricsObserver.speculation_snapshot` dict or
    a full :meth:`MetricsObserver.snapshot` (the ``speculation`` block is read if
    present). The decision is deliberately conservative: it only says
    ``"enable"`` when there is *enough* data AND both the commit-ratio and the
    head-start clear their bars — the cancelled-run compute (ADR 070's risk) must
    be repaid by real latency saved.
    """
    spec = snapshot.get("speculation", snapshot)
    started = int(spec.get("started", 0))
    committed = int(spec.get("committed", 0))
    cancelled = int(spec.get("cancelled", 0))
    commit_ratio = float(spec.get("commit_ratio", (committed / started) if started else 0.0))
    avg_head_start_ms = float(spec.get("avg_head_start_ms", 0.0))

    if started < min_samples:
        rec = "insufficient-data"
        rationale = (
            f"only {started} speculation(s) observed (need ≥{min_samples}); "
            "gather more turns before deciding."
        )
    elif commit_ratio >= min_commit_ratio and avg_head_start_ms >= min_head_start_ms:
        rec = "enable"
        rationale = (
            f"{commit_ratio:.0%} commit-ratio (≥{min_commit_ratio:.0%}) and "
            f"~{round(avg_head_start_ms)}ms saved/turn (≥{round(min_head_start_ms)}ms) — "
            "the latency saved repays the cancelled-run cost; default it ON."
        )
    else:
        reasons = []
        if commit_ratio < min_commit_ratio:
            reasons.append(
                f"commit-ratio {commit_ratio:.0%} < {min_commit_ratio:.0%} "
                "(too many cancelled runs)"
            )
        if avg_head_start_ms < min_head_start_ms:
            reasons.append(
                f"head-start ~{round(avg_head_start_ms)}ms < {round(min_head_start_ms)}ms "
                "(latency saved is marginal)"
            )
        rec = "hold"
        rationale = "keep opt-in: " + "; ".join(reasons) + "."

    return SpeculationABVerdict(
        started=started,
        committed=committed,
        cancelled=cancelled,
        commit_ratio=commit_ratio,
        avg_head_start_ms=avg_head_start_ms,
        recommendation=rec,
        rationale=rationale,
    )


# Cost-guard defaults: a session must run at least this many speculations before
# the guard will trip, and it trips when the running commit-ratio is below this.
# Looser than the A/B *enable* bar — the guard exists to stop pathological waste
# mid-session, not to make the default-flip call.
_GUARD_MIN_SAMPLES = 8
_GUARD_MIN_COMMIT_RATIO = 0.35


class SpeculationGuard:
    """Session-scoped cost-guard for speculative kickoff (ADR 070 risk / ADR 073).

    Speculation costs a wasted agent run every time it cancels. On a speech
    profile where it rarely commits, that cost isn't repaid — so this guard
    watches the *running* commit-ratio across a session and, once it has seen
    enough speculations, **trips off** (sticky) when the ratio is too low. The
    caller gates each turn on :meth:`should_speculate` and feeds each turn's
    speculation snapshot to :meth:`record`.

    It is a no-op until it trips: a session that never speculates (the default)
    or that commits well never disables anything. Tripping is one-way within a
    session — once a profile shows speculation loses, stop paying for it.
    """

    def __init__(
        self,
        *,
        min_samples: int = _GUARD_MIN_SAMPLES,
        min_commit_ratio: float = _GUARD_MIN_COMMIT_RATIO,
    ) -> None:
        self._min_samples = max(1, min_samples)
        self._min_commit_ratio = min_commit_ratio
        self._started = 0
        self._committed = 0
        self._tripped = False

    @property
    def tripped(self) -> bool:
        return self._tripped

    @property
    def commit_ratio(self) -> float:
        return (self._committed / self._started) if self._started else 0.0

    def should_speculate(self) -> bool:
        """Whether the next turn should still speculate (False once tripped)."""
        return not self._tripped

    def record(self, snapshot: dict[str, Any]) -> bool:
        """Absorb one turn's speculation snapshot; return True if it tripped now.

        ``snapshot`` is a per-turn :meth:`MetricsObserver.speculation_snapshot`
        (or full ``snapshot``). Accumulates into the running totals and, once
        ``min_samples`` speculations have been seen, trips if the running ratio
        is below ``min_commit_ratio``. Returns True **only on the transition**
        so the caller can emit a one-time "speculation disabled" signal.
        """
        spec = snapshot.get("speculation", snapshot)
        self._started += int(spec.get("started", 0))
        self._committed += int(spec.get("committed", 0))
        if (
            not self._tripped
            and self._started >= self._min_samples
            and self.commit_ratio < self._min_commit_ratio
        ):
            self._tripped = True
            return True
        return False
