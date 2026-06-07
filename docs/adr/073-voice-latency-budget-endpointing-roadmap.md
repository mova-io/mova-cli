# ADR 073 — Voice latency budget + endpointing-optimization roadmap

**Status:** Proposed
**Date:** 2026-06-03
**Deciders:** Engineering + Deva (Movate)
**Builds on / composes with (changes nothing in their wire contracts):**
ADR 048 (the three speech seams + WS transport), ADR 049 (`mdk voice bench` —
the measurement harness this ADR makes mandatory), ADR 067 (consolidated
`movate.voice` + the `AgentTurn` seam), ADR 070 (speculative agent kickoff),
ADR 071 (per-agent voice tuning), ADR 072 (semantic turn-detection — Proposed).
**NOTE:** ADRs 070/071 ship on the in-flight voice stack (#669→#672); 072 is
design-only. This ADR is the **umbrella** that sequences them into one
measure-first programme — it adds no new runtime seam of its own; it ranks and
schedules the levers the other ADRs already define.

**Defining architectural fact.** In a pipeline-mode turn
(`audio → STT → agent → TTS → audio`) the dominant *fixed* cost is not the model
— it is the **endpointing silence wait**: we hold ~1500 ms of trailing silence
(`DeepgramSTT(endpointing_ms=1500)` + an `utterance_end_ms` backstop) before we
even know the caller stopped. The bench measured **~1.66 s of recoverable
headroom** in that single constant — larger than agent first-token *and* TTS
first-audio combined. Every latency lever worth pulling is therefore a way to
either *recover* that wait (speculate through it) or *shorten* it (end the turn
when the speaker is actually done). This ADR makes that the explicit operating
budget and orders the work against it.

---

## Context

Deva's ask is standing: **lower perceived latency** without giving up the
pipeline's provider-portability and cost control (i.e. without forcing everyone
onto a single-vendor speech-to-speech realtime model). Two questions came up
that this ADR answers directly:

1. **"Are Lyzr agents inherently limited vs native (mdk) agents?"** Yes, *for
   latency* — but the limit is the *turn contract*, not the platform. The
   pipeline's latency floor is dominated by endpointing (STT), which is identical
   regardless of who runs the agent. Where the agent stage *does* matter is
   **streaming**: an `AgentTurn` that streams text deltas lets TTS start on the
   first sentence (and is *speculatable*, ADR 070), while a non-streaming turn
   forces buffered TTS — adding the whole agent think-time to time-to-first-audio.
   - `ExecutorAgentTurn` (mdk) — streams; `speculatable = True`.
   - `OpenAIChatAgent` (demo, used for the OpenAI tier **and** the Lyzr **v4
     streaming** tier) — streams; `speculatable = True`.
   - `LyzrAgentTurn` (Lyzr **SDK** via `agent.run()`, ADR 069) — synchronous,
     non-streaming; `speculatable = False`.
   - `LangGraphAgentTurn` — non-streaming today; `speculatable = False`.

   So "Lyzr is slower" is really "the **Lyzr SDK turn** is non-streaming." The
   **Lyzr v4 streaming HTTP path already streams** and is speculatable — it is
   not inherently limited. The binding guidance (D4) follows from this, not from
   a platform verdict.

2. **"What reduces latency / improves endpointing?"** Everything ranks against
   the budget below. The mistake to avoid is tuning blind: `endpointing_ms` is a
   no-win single constant (ADR 070 D-context, ADR 072) — too short barges in on
   a mid-sentence pause, too long makes a finished speaker wait. We do not pick a
   new number by feel; we **measure, then recover/shorten the wait** with levers
   that don't reintroduce the barge-in bug.

### The latency budget (measure-first, from the bench + demo badge)

The demo badge already instruments the three legs (`stt_final_ms`,
`agent_first_token_ms`, `tts_first_audio_ms`). A representative pipeline turn:

| Leg | Typical | Notes |
|---|---|---|
| **STT endpointing wait** | **~1500–1660 ms** | `endpointing_ms` silence hold + `utterance_end_ms` backstop. **The dominant fixed cost.** |
| Agent first token | ~300–700 ms | streaming agent; non-streaming pays *full* think-time here instead. |
| TTS first audio | ~80–150 ms | Cartesia SSE first-byte. |
| Network / transport | ~50–120 ms | WS round-trips. |

The budget's headline: **the endpointing wait alone is larger than the agent and
TTS first-response legs combined.** That is why this ADR exists as a programme
rather than a single fix — and why **Phase 0 is "measure," not "tune."**

---

## Decision

### D1 — Adopt the latency budget as the shared frame; measure before tuning.

The budget table above is the canonical decomposition. Every proposed latency
change states *which leg it moves* and is justified against a **bench delta**
(ADR 049 `mdk voice bench`) — not a vibe. No `endpointing_ms` change ships
without a before/after on the validation runbook (`docs/voice-validation-runbook.md`).
This makes "measure-first" a gate, not a guideline.

### D2 — Rank the levers by (headroom recovered ÷ risk). Build in this order.

1. **Speculative agent kickoff (ADR 070)** — *recovers* the wait by starting the
   agent on a stable interim, committing/cancelling on the final. Built, opt-in
   (`speculative=False`). Highest headroom (~up to 1 s off time-to-first-audio),
   bounded risk (wasted compute on cancel, capped by the quiet-gap gate).
2. **TTS streaming on the agent stage** — start TTS on the first sentence
   instead of buffering the whole answer. Built; opt-in per agent (ADR 071
   `tts_streaming`). Only helps *streaming* agents (see D4).
3. **Keyterm prompting / nova-3 (ADR 071 D4)** — accuracy, not latency, but a
   correct first transcript avoids a re-ask round-trip (the most expensive
   latency event there is). Built.
4. **Adaptive / per-agent `endpointing_ms` (this ADR, D3)** — *shortens* the
   wait by letting deliberate-speaker agents hold longer and snappy ones cut
   shorter, instead of one global constant.
5. **Semantic turn-detection (ADR 072)** — *shortens* the wait by ending on
   "speaker is done," not "silence elapsed." Highest ceiling, highest cost
   (a model + a new optional seam). Design-only; gated.
6. **Connection / realtime reuse** — warm STT sockets, keepalive, and (for the
   demand that truly needs sub-second) the realtime full-duplex path as an
   explicit, opt-in tier — *not* the default, to preserve portability/cost.

### D3 — Adaptive endpointing as a measured, per-agent knob (not a global retune).

`endpointing_ms` becomes tunable **per agent** via the existing `agent.yaml`
voice block (ADR 071's seam — additive field, default unchanged at 1500). A
deliberate-speaker agent (support triage) can raise it; a command-style agent
(IVR menu) can lower it. This is the *safe* way to attack the constant: it never
changes the default, it's measured per agent on the runbook, and it composes
with speculation (a shorter hold + speculation recovers the most). An *adaptive*
variant (adjust within an agent from observed turn cadence) is a later step,
gated on the same bench evidence — flagged here, not built here.

### D4 — Lyzr binding guidance: prefer the streaming turn; speculation follows.

Make the streaming/non-streaming distinction an explicit deployment rule, not
folklore:

- **Default to a *streaming* `AgentTurn`** wherever the platform offers one — mdk
  `ExecutorAgentTurn`, the OpenAI tier, and **Lyzr v4 streaming HTTP**. These
  start TTS early and are `speculatable = True`, so they get both ADR 070 and
  TTS-streaming wins.
- **The Lyzr *SDK* turn (`agent.run()`, ADR 069) is non-streaming** →
  `speculatable = False`. It still works (buffered TTS), but it forfeits the two
  biggest agent-leg latency levers. Use it only when the SDK's session/memory/
  tool features are required and the v4 streaming HTTP path can't be used.
- This is a **turn-contract** property surfaced by the `speculatable` flag (ADR
  070), *not* a platform value judgement. "Native is faster" is true only insofar
  as native turns stream by default; a streaming Lyzr turn is on equal footing.

### D5 — Keep the realtime path a tier, not the answer.

Speech-to-speech realtime (already shipped, ADR 048) genuinely beats the pipeline
floor for latency, but at the cost of provider lock-in and per-minute pricing —
the exact tradeoff Deva wants to *avoid* as a default. It stays an **opt-in
tier** for latency-critical deployments; the pipeline + this roadmap is how we
get most of the win while keeping portability and cost control.

---

## The endpointing-optimization roadmap (phased)

**Phase 0 — Measure (now, no code).** Run the validation runbook against a real
deployment; capture the per-leg badge breakdown + bench numbers. Confirm the
~1.66 s endpointing headroom on the target traffic. *Exit:* a baseline table.
**This is the gate for every later phase.**

**Phase 1 — Recover the wait (built; flip on data).** Speculative kickoff (ADR
070) + TTS streaming (ADR 071) are implemented opt-in. With Phase-0 numbers in
hand, A/B speculation against badge telemetry; flip `speculative` / keyterm
defaults per the runbook verdict. *Exit:* speculation commit-ratio + latency
delta measured; defaults decided.

**Phase 2 — Shorten the wait, safely (small build).** Per-agent `endpointing_ms`
(D3) via the ADR 071 voice block; tune the two or three highest-traffic agents
on the runbook. *Exit:* per-agent holds set with bench evidence; no barge-in
regression.

**Phase 3 — Adaptive endpointing (gated).** Adjust the hold within an agent from
observed turn cadence. Build only if Phase-2 per-agent tuning shows residual
headroom worth the complexity. *Exit:* bench delta vs static per-agent.

**Phase 4 — Semantic turn-detection (ADR 072, gated).** Replace "silence
elapsed" with "speaker is done" behind the new optional seam, fixed timer as
fallback. Highest ceiling; build only when Phases 1–3 are exhausted and the ADR
072 D6 gate is met (it also *raises* speculation's commit rate, so it pairs with
Phase 1). *Exit:* the ADR 072 gate.

**Phase 5 — Connection/realtime.** Warm/keepalive STT sockets to shave transport
legs; formalize the realtime tier (D5) as the documented escape hatch for
sub-second-critical deployments. *Exit:* transport-leg delta; realtime tier
documented as opt-in.

The phases are **strictly measure-gated**: each one ships only after the prior
phase's bench delta justifies continuing. We stop when the marginal headroom no
longer beats the added complexity/cost.

---

## Consequences

**Positive**
- One shared latency budget ends per-change debate — every proposal is scored
  against the same decomposition and a bench delta.
- The Lyzr question gets a precise, non-tribal answer: it's the *turn contract*
  (streaming + `speculatable`), not the platform.
- Measure-gating prevents the classic voice failure mode — retuning
  `endpointing_ms` by feel and trading latency for barge-in bugs.
- Nothing new to build to *start*: Phase 0–1 use already-shipped levers; the ADR
  mostly *sequences and gates*.

**Negative / risks**
- Discipline cost: the bench/runbook gate is only valuable if enforced; a
  shipped `endpointing_ms` change without a before/after defeats it.
- Per-agent tuning (D3) adds an `agent.yaml` knob operators can misuse (too-low
  hold → barge-in). Mitigated: default unchanged, runbook required.
- Phases 3–5 are real engineering; the roadmap deliberately gates them behind
  evidence rather than committing now.

## New surfaces (flagged per CLAUDE.md rule 5)

- **`agent.yaml` voice block:** an additive `endpointing_ms` field (D3),
  default unchanged (1500). Backward-compatible; rides the ADR 071 seam.
- **No new** runtime API, CLI flag, `--json` shape, storage schema, or env var
  is introduced by *this* ADR — it's a programme over existing seams. (ADR 070/
  071/072 carry their own surface declarations.)

## Alternatives considered

- **Just lower the global `endpointing_ms`.** Rejected — reintroduces the
  barge-in bug the constant was raised to fix; one number can't fit all speakers.
- **Default everyone to realtime speech-to-speech.** Rejected — provider
  lock-in + per-minute cost, the exact tradeoff Deva wants to avoid; kept as an
  opt-in tier (D5).
- **Declare Lyzr "inherently slow" and steer to native.** Rejected as
  imprecise — the latency floor is endpointing (platform-independent), and the
  Lyzr v4 streaming turn is speculatable; the real rule is *prefer streaming
  turns* (D4).
- **Build semantic turn-detection first** (the biggest ceiling). Rejected as
  premature — highest cost/risk; gated behind cheaper, already-built levers.

## Boundaries (explicitly NOT in scope)

- Does **not** change the three speech Protocol signatures, the WS transport, or
  make the Executor voice-aware (it stays modality-blind behind `AgentTurn`).
- Does **not** define the speculation mechanism (ADR 070), the per-agent tuning
  seam internals (ADR 071), or the turn-detector (ADR 072) — it sequences them.
- Does **not** itself flip any default; default flips are runbook-gated outcomes
  of Phase 1.

## Cross-references / composition notes

- **ADR 070** — the *recover-the-wait* lever (speculation). This ADR schedules
  it as Phase 1 and depends on its `speculatable` flag for D4.
- **ADR 071** — the per-agent voice seam this ADR's D3 `endpointing_ms` knob
  rides on.
- **ADR 072** — the *shorten-the-wait* lever (semantic turn-detection),
  scheduled as Phase 4 and noted to raise speculation's commit rate.
- **ADR 049** — `mdk voice bench`, the measurement harness D1 makes mandatory.
- **ADR 048 / 067** — the unchanged speech seams + `AgentTurn` everything sits
  above.
- **`docs/voice-validation-runbook.md`** — the operational procedure Phase 0/1
  execute.
