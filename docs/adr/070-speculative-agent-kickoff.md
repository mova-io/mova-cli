# ADR 070 — Speculative agent kickoff: beat the endpointing latency floor in pipeline mode

**Status:** Accepted (2026-06-02) — implemented opt-in (`speculative=False` default) behind the quiet-gap gate, per the D7 verdict. Default-on pending live A/B against badge telemetry.
**Date:** 2026-06-02
**Deciders:** Engineering + Deva (Movate)
**Context window:** the single largest fixed cost in a pipeline-mode voice turn
is the **endpointing wait** — we hold ~1500 ms of trailing silence before we
even know the caller finished, then start the agent. Deva wants lower perceived
latency without giving up the pipeline's provider-portability + cost control
(i.e. without forcing everyone onto a speech-to-speech realtime model). This ADR
proposes **speculatively starting the agent on a stable interim transcript** and
cancelling if the caller keeps talking — trading some wasted agent compute for
up to ~1 s off time-to-first-audio.
**Builds on / composes with (changes nothing in their wire contracts):**
ADR 048 (the three speech seams + the WS transport — unchanged),
ADR 067 (the `AgentTurn` seam — this ADR *extends its contract* with an explicit
cancel-safety clause; see D3),
ADR 068 (the resilient router — speculation sits above it, unchanged),
ADR 049 (`mdk voice bench` — the measurement harness this ADR gates itself on).

**Defining architectural fact.** Deepgram already streams *stable interim*
transcripts (`is_final=True` mid-utterance) well before the endpointed
`speech_final`. The agent stage is an injected `AgentTurn` (ADR 067). So we can
start `agent.run(stable_interim)` early, in parallel with the still-open STT
socket, and **cancel it** if the caller resumes — the seam already exists; what's
missing is a cancel-safe contract and a speculation policy. This is a pipeline
change *above* the providers, not a provider or transport change.

---

## Context

A pipeline-mode turn is `audio → STT → agent → TTS → audio`. The latency
breakdown the demo's badge already measures (`stt_final_ms`,
`agent_first_token_ms`, `tts_first_audio_ms`) shows the dominant term is the gap
between the caller actually finishing and `stt_final_ms` — because we wait
`endpointing_ms` (default 1500 ms, ADR/`deepgram.py`) of silence to be *sure*
the turn ended. Lowering `endpointing_ms` trades that latency for false
turn-ends (the agent barges in on a mid-sentence pause — the exact bug ADR-era
work fixed by *raising* it). So the silence wait is a real floor in pipeline
mode.

The only thing that currently beats it is **realtime / speech-to-speech** (ADR
048 D2b, already shipped) — but that gives up provider portability, the failover
router (ADR 068), per-stage cost control, and text-side observability. Deva
wants the latency win *without* that trade for the mainline pipeline path.

**Speculative execution** is the standard answer: Deepgram emits stable interims
(`is_final=True`) mid-utterance. We can start the agent on the latest stable
interim *before* `speech_final`, run it concurrently with the still-listening
STT socket, and:

* if the caller stays silent and the interim becomes the endpointed final →
  we've already got tokens (and maybe audio) in flight — a big head start;
* if the caller resumes (the interim was not the end of the turn) → **cancel**
  the speculative run, discard its output, and restart on the new transcript.

The cost is wasted agent invocations on cancelled speculations. That is the
explicit trade this ADR asks Deva to accept (bounded by D4).

## Decision

### D1 — Speculation trigger: stable interim + a quiet-gap heuristic

Start a speculative `agent.run` when STT emits a **stable interim**
(`is_final=True`, not yet `speech_final`) AND a short quiet gap (configurable,
default ~300 ms) has elapsed since the last interim — i.e. the caller has
*probably* finished but the endpointing timer hasn't fired yet. The quiet-gap
guard keeps us from speculating on every mid-sentence comma. This is the only
new heuristic; everything else reuses existing signals.

### D2 — Run speculation concurrently with the open STT socket; commit or cancel

The pipeline keeps consuming STT events while the speculative agent runs:

* **Commit** — STT yields `speech_final` and the endpointed transcript **equals**
  (normalized) the interim we speculated on → adopt the in-flight run's tokens /
  audio as the real turn. Net win = everything produced before `speech_final`.
* **Cancel** — STT yields more speech (the interim grew) or a *different* final →
  cancel the speculative run, drop its output (it must not reach TTS / the wire),
  and start a fresh run on the corrected transcript. Net cost = the cancelled
  run's tokens.

Only **one** speculation is in flight at a time (a new stable interim cancels
and replaces the prior speculation), bounding fan-out.

### D3 — `AgentTurn` cancel-safety contract (the one seam change)

Speculation requires that a cancelled `agent.run` is **safe to discard**: no
side effects the caller would regret (no committed memory write, no irreversible
tool call) before the first token, and prompt cancellation via
`asyncio.CancelError`. This ADR adds a clause to the ADR 067 `AgentTurn`
contract:

> An `AgentTurn` MAY be cancelled (its awaitable cancelled) before completion
> when run speculatively. Implementations SHOULD make pre-first-token work
> side-effect-free and MUST treat cancellation as "this turn did not happen."
> An implementation that cannot meet this (irreversible early side effects)
> sets `speculatable = False` and the pipeline never speculates on it.

`ExecutorAgentTurn` (mdk) and `LyzrAgentTurn` both already run a stateless turn
before the first token, so both can opt in; an adapter with eager side effects
opts out via the flag. **This is additive** — a default `speculatable` of
`False` preserves today's behavior for any adapter that doesn't declare it.

### D4 — Cost guard: speculation is opt-in and bounded

Speculation is **off by default** (`speculative=False` on `run_voice_pipeline`),
because it changes the cost profile. When on:

* a per-session **speculation budget** caps wasted runs (default: cancel ratio
  ceiling, e.g. stop speculating after N cancellations in a session — the caller
  is a fast talker and speculation is losing);
* it composes with ADR 068 D4's cost-bounded routing — under budget pressure,
  speculation is the first thing dropped.

This makes the cost bounded and observable rather than open-ended.

### D5 — Observability: surface speculation outcomes

Emit speculation events through the existing `VoiceObserver` (ADR 068 D7):
`speculation_started`, `speculation_committed` (with ms saved),
`speculation_cancelled` (with tokens wasted). The demo's event stream + latency
badge can then show "saved 740 ms (speculative)" and the cancel ratio, so the
win and its cost are both visible — and so D7 below can be measured.

### D6 — Scope: pipeline mode only

Speculation applies to the STT→agent→TTS pipeline. Realtime / speech-to-speech
(ADR 048 D2b) already has no endpointing floor and is out of scope. Barge-in
(the existing `cancel` event) is orthogonal and unchanged — it cancels the
*spoken answer*; speculation cancels an *unconfirmed agent run*.

### D7 — Measure before committing to the build (gate)

Before merging the implementation, baseline with `mdk voice bench` (ADR 049 D5)
on a fixed utterance set and report: p50/p95 time-to-first-audio with and
without speculation, the **cancel ratio**, and the **wasted-token cost per
committed turn**. The feature ships only if it shows a material TTFA win at an
acceptable cancel ratio (proposed bar: ≥300 ms p50 improvement at ≤25% cancel
ratio on the bench set). This keeps us honest — speculation can *lose* for fast
back-and-forth speakers, and we want data, not vibes.

#### Measured baseline (2026-06-02)

First pass via [`scripts/voice_bench.py`](../../scripts/voice_bench.py) — n=6
enterprise IT-support utterances, OpenAI-TTS-synthesized and **real-time-paced**
into Deepgram nova-3, keyterms on/off. Caveat up front: the corpus is *synthetic*
speech, so these are a **ceiling** on stability and a **floor** on the keyterm
win; the real distribution must come from production telemetry (the demo's
latency badge already records `stt_final` per turn).

- **Endpointing headroom ≈ 1662 ms mean** (range 1221–2152). This is the dead
  time between the transcript reaching its final text and the endpointed
  `speech_final` — i.e. the latency speculation recovers. It tracks
  `endpointing_ms` (1500) plus VAD lag, as predicted. **This is the win's
  ceiling and it is large** — comfortably above the ≥300 ms bar.
- **Interim==final 83%** (≈17% would have cancelled) on clean audio — *clears*
  the ≤25% cancel bar here, but human speech (disfluencies, mid-sentence pauses)
  will push the cancel rate up. So the quiet-gap gate (D1) is the load-bearing
  knob, not an afterthought.
- **Keyterm WER 4.4% → 4.4%** (no change). On studio-clean TTS the base model
  already gets the words; keyterms only corrected *casing*, which WER ignores.
  **Conclusion: keyterms are insurance for noisy/accented human audio, not a
  measured win on clean audio** — keep them (zero downside, default-on in the
  demo) but measure the real win on a human corpus before claiming it.

**Verdict:** the headroom justifies building speculation; the cancel-rate risk
justifies shipping it **opt-in and behind the quiet-gap gate**, then A/B-ing it
live against the badge telemetry before defaulting it on. Proceeding to
implement on exactly those terms (opt-in, `speculative=False` default).

## Consequences

**Positive**
- Up to ~1 s off perceived latency in pipeline mode without giving up provider
  portability, failover, or cost control (the realtime trade).
- Reuses the existing seam, STT signals, observer, and bench harness — small
  surface, mostly policy.
- Bounded and opt-in: the cost is capped and measured, never silent.

**Negative / risks**
- Wasted agent compute on cancelled speculations — real $ for fast talkers
  (mitigated by D4's budget + D7's gate).
- Adds concurrency to the hottest path; a cancelled run that leaks output to TTS
  would be a correctness bug (D2 makes discard mandatory; needs careful tests).
- The `AgentTurn` cancel-safety clause (D3) is a contract the ecosystem must
  honor; non-conforming adapters must opt out, or they risk double side effects.

## New surfaces (flagged per CLAUDE.md rule 5)

- `run_voice_pipeline(..., speculative: bool = False)` — additive, default off.
- `AgentTurn.speculatable: bool = False` — additive contract field (D3).
- New `VoiceObserver` event kinds (D5) — additive, no-op for default observers.
- No change to: the three speech Protocols, the WS `/voice` wire protocol, the
  `agent.yaml` voice block, `MDK_VOICE_*` env, or capability JSON.

## Alternatives considered

- **Lower `endpointing_ms`** — trades latency for false turn-ends (the bug we
  fixed by raising it). Rejected as the primary lever.
- **Semantic / model-based turn detection** (a small turn-end classifier instead
  of fixed silence) — complementary and promising, but a new model dependency
  and its own ADR; can stack with speculation later.
- **Force realtime everywhere** — gives up portability/failover/cost control
  (ADR 068) and text observability. Rejected as the mainline.
- **Prompt-prefix warmup only** (start the agent's prompt assembly early but not
  generation) — smaller, safer win; a reasonable phase-0 if D7 shows full
  speculation too costly.

## Boundaries (explicitly NOT in scope)

- Not a realtime / speech-to-speech change (that path has no endpointing floor).
- Not a change to barge-in semantics (the `cancel` event is orthogonal).
- Not a provider or transport change — speculation lives in the pipeline driver,
  above the seams (CLAUDE.md rule 6).
- Does not make the Executor speculation-aware beyond the cancel-safety the
  `AgentTurn` contract already implies.

## Cross-references / composition notes

- **ADR 067** — extends the `AgentTurn` contract (D3); the seam is unchanged in
  shape, only annotated with cancel-safety + the `speculatable` opt-out.
- **ADR 068** — speculation composes with the failover router and cost-bounded
  routing (D4); it sits above the composites, which are unchanged.
- **ADR 049** — `mdk voice bench` is the gate (D7); shadow/canary rollout flags
  (ADR 049 D12) are the natural way to roll speculation out per tenant.
- **ADR 048 D2b** — realtime remains the zero-endpointing path for latency-
  critical callers who accept its trade-offs; speculation is the pipeline-mode
  alternative.
