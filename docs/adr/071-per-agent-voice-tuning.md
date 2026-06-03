# ADR 071 — Per-agent voice tuning in the `agent.yaml` voice block

**Status:** Accepted (2026-06-02) for D1–D3 (implemented, additive). D4 (keyterms at the STT seam) is **Proposed** — needs sign-off before the Protocol change.
**Date:** 2026-06-02
**Deciders:** Engineering + Deva (Movate)
**Builds on / composes with (changes nothing in their wire contracts):**
ADR 048 (the three speech seams + WS transport), ADR 050 (`agent.yaml` voice
block, ADR 050 D4), ADR 067 (consolidated `movate.voice` + `AgentTurn`), ADR 070
(speculative kickoff). Carries the perf knobs from #669 (nova-3 + keyterms,
`tts_streaming`, `speculative`) from the **demo** to **production** mdk agents.

**Defining architectural fact.** The `VoiceConfig` block on `AgentSpec`
(`enabled/mode/stt/tts/voice_id/language`) already exists (ADR 050 D4) — but the
runtime WS handler **never reads it**: `_run_voice_pipeline_ws` builds a fresh
`_VoiceTurnConfig()` from defaults and only applies client `config` frames. So
per-agent voice settings are silently ignored today. This ADR (a) **fixes that
gap** by seeding the per-turn config from the agent's voice block, and (b)
**extends the block** with the latency/cost knobs the demo proved out.

---

## Context

#669 shipped three perf wins but wired two of them (`tts_streaming` default-on,
`speculative` opt-in) only into the runtime's *client-frame* config and the
**demo**; `keyterms` is demo-only. Production mdk voice agents — the actual
customer deliverable — get none of them per-agent, and can't even set
`voice_id`/`language` per agent because the block is ignored. An enterprise
deployment wants each agent to carry its own voice profile (its domain vocab,
its latency/cost posture) in `agent.yaml`, not rely on a UI toggle.

The blocker is uneven: `voice_id`, `language`, `tts_streaming`, `speculative`
all map to existing `_VoiceTurnConfig` fields / `run_voice_pipeline` params — pure
plumbing, no seam change. `keyterms` is different: it's a **constructor-time**
param on `DeepgramSTT`, but the runtime builds STT from a tenant-level
`voice_stt_factory()` and the pipeline only ever calls
`stt.transcribe(audio, language=, api_key=)`. Reaching per-agent keyterms means
either rebuilding/wrapping the STT per connection or adding `keyterms` to the
`SpeechToTextProvider.transcribe` Protocol — the latter touches every adapter +
the failover composite + wrappers. That asymmetry drives the split below.

## Decision

### D1 — Seed the per-turn config from the agent's voice block (fixes the gap)

When a voice WS connection resolves an agent whose `spec.voice` is present, seed
`_VoiceTurnConfig` from it **before** the first turn: `voice_id`, `language`
(and `mode` continues to select pipeline vs realtime). A client `config` frame
can still override per-turn (unchanged precedence: agent block = the default,
client frame = the override). Agents with no voice block are byte-for-byte
unchanged (defaults).

### D2 — Extend `VoiceConfig` with `tts_streaming` (additive)

Add `tts_streaming: bool | None = None` to the block. `None` (default / absent
block) preserves the runtime default (currently on); `true`/`false` pins it for
this agent. Threads straight into `run_voice_pipeline(tts_streaming=...)` — no
seam change.

### D3 — Extend `VoiceConfig` with `speculative` (additive, ADR 070)

Add `speculative: bool = False` to the block. Off by default (matches ADR 070's
opt-in posture + cost profile). Threads into
`run_voice_pipeline(speculative=...)`; still only fires when the agent stage is
cancel-safe (`AgentTurn.speculatable` — `ExecutorAgentTurn` is). No seam change.

### D4 — `keyterms` per agent (Proposed — needs sign-off; NOT in this PR)

Add `keyterms: list[str]` to the block AND an additive
`keyterms: Sequence[str] | None = None` kwarg to
`SpeechToTextProvider.transcribe(...)`, defaulting to `None` so every existing
adapter/caller is unaffected. `DeepgramSTT.transcribe` would merge it with its
constructor list; other adapters ignore it. The pipeline passes
`stt.transcribe(..., keyterms=...)`. This is the only change that touches the
**ADR 048 D3 seam** (and the failover composite + `SilenceGatedSTT` wrapper +
test doubles), so per CLAUDE.md rules 1/7 it is held as **Proposed** for explicit
agreement before implementation. Until then, per-agent keyterms is documented as
unsupported in production (demo-only); the curated `DEEPGRAM_KEYTERMS` env on the
runtime STT factory is the interim lever.

### D5 — Validation + surfacing

`mdk validate` type-checks the new fields (Pydantic `extra="forbid"` already
rejects typos). `mdk show` renders them. No new CLI verbs.

## Consequences

**Positive**
- Production agents get the demo's latency/cost knobs per agent, declaratively.
- Fixes a real latent gap (the voice block was ignored by the runtime).
- D1–D3 are pure plumbing behind existing params — no seam change, low risk.

**Negative / risks**
- D4 widens the STT Protocol; deferred precisely because of that blast radius.
- More per-agent surface to document and keep in sync with the demo defaults.

## New surfaces (flagged per CLAUDE.md rule 5)

- `VoiceConfig.tts_streaming` (D2), `VoiceConfig.speculative` (D3) — additive,
  default-None/False → existing `agent.yaml` serializes identically.
- (Proposed, D4) `VoiceConfig.keyterms` + `SpeechToTextProvider.transcribe(keyterms=)`.
- No change to the WS wire protocol, the `done`/event shapes, `MDK_VOICE_*`, or
  capability JSON. `mode`/`stt`/`tts`/`voice_id`/`language` semantics unchanged.

## Alternatives considered

- **Keep it demo-only** — rejected; production agents are the deliverable.
- **Per-agent keyterms via STT-factory wrapping** instead of a Protocol kwarg —
  rejected as the primary path (the factory returns a provider-agnostic STT;
  wrapping to inject a Deepgram-only constructor arg is leakier than an additive,
  universally-ignorable `transcribe` kwarg). Revisit under D4's sign-off.
- **Tenant-level only** (no per-agent) — rejected; enterprise agents have
  distinct vocab/latency postures.

## Boundaries (explicitly NOT in scope)

- Does not change the realtime path, the Executor, or make any provider
  voice-aware beyond the existing seams.
- Does not implement D4 (keyterms at the seam) — Proposed, pending agreement.
- Does not change client-frame override precedence.

## Cross-references / composition notes

- **ADR 050 D4** — this is the first runtime *consumption* of the voice block it
  defined; D1 closes the parse-but-ignore gap.
- **ADR 070** — D3 is the production exposure of speculative kickoff; the
  `speculatable` cancel-safety contract is unchanged.
- **#669** — carries nova-3/keyterms/cache/streaming + ADR 070; this ADR extends
  that work from demo to production `agent.yaml`.
