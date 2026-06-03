# ADR 072 — Semantic turn-detection: end the turn when the speaker is *done*, not when silence elapses

**Status:** Proposed
**Date:** 2026-06-02
**Deciders:** Engineering + Deva (Movate)
**Builds on / composes with (changes nothing in their wire contracts):**
ADR 048 (the three speech seams + WS transport), ADR 067 (consolidated
`movate.voice` + `AgentTurn`), ADR 070 (speculative agent kickoff — the *other*
endpointing-latency lever), ADR 071 (per-agent voice tuning).
**NOTE:** ADRs 070/071 ship on the in-flight voice stack (#669→#672); this ADR
references them as the landscape it composes with. **Design only — no
implementation until the gate in D6 is met.**

**Defining architectural fact.** Today the turn ends on a **fixed silence
timer** — `DeepgramSTT(endpointing_ms=1500)` plus an `utterance_end_ms` backstop.
That single constant is the dominant fixed cost of a pipeline turn (the bench
measured ~1.66s of recoverable headroom, ADR 070), and it's a blunt instrument:
too short and the agent barges in on a mid-sentence pause ("um, let me
think…"); too long and a finished speaker waits. **Semantic turn-detection
replaces "has it been quiet for N ms?" with "is the speaker actually done?"** —
a decision a small classifier can make from the transcript (and optionally
prosody), behind a new optional seam, leaving the fixed timer as the fallback.

---

## Context

`endpointing_ms` is a no-win tuning knob (ADR 070 D-context + the speculation
bench): every deployment picks one number that is simultaneously too aggressive
for deliberate speakers and too slow for snappy ones. ADR 070 attacks the *cost*
of the wait by speculating *through* it; this ADR attacks the *wait itself* by
ending the turn as soon as the utterance is semantically complete and holding
when it isn't. The two are **complementary, not competing**:

- Better turn-detection → the real final arrives sooner → less wait to recover.
- It also *raises speculation's commit rate* (ADR 070): a speculation fired on a
  semantically-complete interim is far likelier to match the final, so fewer
  cancellations / less wasted agent compute.

The industry pattern is a **small, dedicated turn-detector** (LiveKit's turn
detector, Pipecat's smart-turn, etc.): a lightweight model — text-only or
text+prosody — that classifies "end of turn?" on each stable interim. The
movate-shaped realization is a **new optional Protocol seam** (mirroring
STT/TTS/AgentTurn), default no-op (today's fixed timer), pluggable
implementations.

## Decision

### D1 — A `TurnDetector` Protocol (new optional seam)

Add a tiny Protocol the pipeline consults on each **stable interim** transcript:

```python
class TurnDetector(Protocol):
    name: str
    version: str
    def is_end_of_turn(self, transcript: str, *, language: str | None = None) -> float:
        """Probability in [0,1] that the speaker has finished their turn."""
```

Pure, synchronous, side-effect-free — a classifier over text. The pipeline maps
the score to a decision via a threshold (D3). A new backend is a new class
implementing this Protocol — the same extension story as a speech adapter.

### D2 — Default is the fixed timer (no behavior change)

Absent a `TurnDetector` (the default), the pipeline behaves exactly as today:
Deepgram's `endpointing_ms`/`utterance_end_ms`. The seam is **opt-in**; nothing
about the default path changes. This is the back-compat contract.

### D3 — How the detector shortens / lengthens the wait

When a `TurnDetector` is wired, on each stable interim the pipeline scores it:

- **score ≥ `end_threshold`** (e.g. 0.7) → treat as end-of-turn *now*: commit the
  transcript and run the agent without waiting out the full `endpointing_ms`
  (the latency win — endpoint as soon as the thought is complete).
- **score < `continue_threshold`** (e.g. 0.3) → the speaker is mid-thought
  (a thinking pause): *extend* the silence tolerance so a natural pause doesn't
  cut them off (the accuracy win).
- **in between** → defer to the fixed timer (the detector abstains).

The fixed `endpointing_ms` remains the hard ceiling/floor so a mis-scoring
detector can never hang the turn or fire instantly. Thresholds are config (D5).

### D4 — Two implementations, smallest-first

- **D4a — Text completion heuristic/classifier** (ship first): a fast text-only
  judgment — punctuation/grammar-completion signals, or a small local classifier
  — over the interim transcript. No audio model, no extra latency, no new heavy
  dep. Captures most of the win (most turn-ends are textually obvious).
- **D4b — Model-based detector** (optional extra): a small turn-detection model
  (text, optionally + prosody) behind the same Protocol, as an opt-in extra
  (`movate-voice[turn-detector]`) for deployments that want the last few points
  of accuracy. Added only if D6's data shows D4a leaves meaningful headroom.

### D5 — Per-agent config (ADR 071 surface)

Surface via the `agent.yaml` voice block (ADR 071): an optional
`turn_detection` sub-block — `{enabled, end_threshold, continue_threshold,
backend}` — seeded into the per-turn config like the other voice knobs. Off by
default; explicit request/frame still overrides. No new top-level surface.

### D6 — Gate: measure before defaulting (hard prerequisite)

Like ADR 070, this **ships opt-in and is data-gated**. Before building D4a, and
before any default-on, baseline with `mdk voice bench` + the validation runbook
on real speech and report, vs the fixed-1500ms baseline:

- **false-endpoint rate** (turns cut off mid-thought) — must not regress;
- **p50/p95 time-to-final** improvement;
- the **interaction with ADR 070** — speculation commit-rate with vs without
  turn-detection.

**Critically: this ADR depends on ADR 070's live speculation data first.** If
speculation already recovers most of the endpointing wait at an acceptable
cancel ratio, turn-detection's marginal value shrinks — the data decides whether
D4a is worth building at all, and whether D4b is ever justified.

## Consequences

**Positive**
- Ends turns when the speaker is *done* — lower latency AND fewer mid-thought
  cut-offs, the two things the fixed timer can't optimize together.
- Composes with and *improves* speculation (higher commit rate).
- New optional seam — zero impact on the default path; same extension pattern as
  the existing adapters.

**Negative / risks**
- A new seam + classifier to maintain; a mis-scoring detector could cut off or
  hang turns (mitigated by D3's fixed-timer floor/ceiling).
- D4b adds a model dependency (mitigated: opt-in extra, only if data justifies).
- Yet another latency lever to reason about alongside speculation — D6's data is
  what keeps this from being speculative complexity.

## New surfaces (flagged per CLAUDE.md rule 5)

- `TurnDetector` Protocol (additive, optional) + `run_voice_pipeline(turn_detector=…)`.
- `VoiceConfig.turn_detection` sub-block (ADR 071 surface; additive, default off).
- `movate-voice[turn-detector]` extra (D4b, optional).
- No change to the three speech Protocols, the WS/REST transports, or capability JSON.

## Alternatives considered

- **Just tune `endpointing_ms` per agent** — already possible (ADR 071); it's the
  blunt instrument this ADR exists to replace. Keep it as the fallback, not the
  answer.
- **Rely on speculation alone (ADR 070)** — speculation hides the wait but still
  *pays* it (and cancels on fast talkers). Turn-detection shortens the wait
  itself. The right call is likely *both*, sequenced — which is exactly why D6
  gates this on the speculation data.
- **Provider-native semantic endpointing** — if/when an STT provider ships a
  good semantic end-of-turn signal, wire it as a `TurnDetector` implementation;
  the seam makes that a new impl, not a redesign.

## Boundaries (explicitly NOT in scope)

- No implementation until D6's data is in (design-only ADR).
- Does not change the realtime path (it has no endpointing floor), the Executor,
  or the three speech Protocols.
- Does not replace speculation (ADR 070) — composes with it.

## Cross-references / composition notes

- **ADR 070** — the complementary endpointing-latency lever; D6 gates this ADR on
  ADR 070's live commit-ratio data; turn-detection raises that commit ratio.
- **ADR 071** — per-agent config surface (D5) + the existing `endpointing_ms`
  knob this supersedes as the primary lever.
- **ADR 048/067** — the seam philosophy `TurnDetector` follows.
