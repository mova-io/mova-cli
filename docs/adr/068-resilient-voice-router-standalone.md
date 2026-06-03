# ADR 068 — Self-contained resilient voice router & fallback (standalone, mdk-free)

**Status:** Accepted (2026-06-02) — see "Consolidation update" at the end of this ADR; the resilience primitives ship in-tree under `movate.voice` rather than a separate dist.
**Date:** 2026-06-01
**Deciders:** Engineering + Deva (Movate)
**Context window:** give the standalone voice SDK (ADR 067) a **robust
multi-provider fallback** that is both **high-performance and cost-efficient**,
**without** depending on the mdk runtime. ADR 049 already specified this layer —
but *as an mdk-runtime control plane*. This ADR is the **in-process realization**
of ADR 049's De-risk primitives so they work in a library with zero mdk.
**Builds on / composes with (changes nothing in their wire contracts):**
ADR 067 (the standalone `movate-voice` distribution + the `AgentTurn` seam —
this resilience ships *in* that package),
ADR 048 (the three speech seams — the router is a **composite that IS a
provider**: it implements `SpeechToTextProvider` / `TextToSpeechProvider` /
`RealtimeVoiceProvider`, so the pipeline is unchanged),
**ADR 049 (the voice agility layer — this ADR is the standalone, in-process
half of 049's *De-risk* pillar (D9 router / D10 fallback+breaker / D11 hedging /
D13 TTS cache) and the *Decouple* manifest (D1); it does NOT supersede 049 —
049 remains the mdk control-plane spec for the parts that genuinely need mdk:
per-tenant rollout flags (D12), `mdk voice bench` (D5), drift (D7), shadow/canary
(D6)),**
ADR 007 (adapter pattern — composites are adapters over adapters),
ADR 018 (BYOK — keys still flow per-call via `api_key=`; routing never changes
the key model),
ADR 036 / ADR 047 (mdk metering / observability — *optional* enrichment via a
`VoiceObserver` hook; the package emits the signals, mdk consumes them).

**Defining architectural fact.** Resilience does **not** belong in the pipeline
or the transport — it belongs in a **composite provider that satisfies the same
ADR-048 Protocol it wraps.** A `FailoverSTT(providers=[deepgram, openai, azure])`
*is* a `SpeechToTextProvider`; the pipeline (ADR 048/067) cannot tell it apart
from a single adapter. Every resilience mechanism — fallback chain, circuit
breaker, retry, latency hedge, phrase cache — is then a **decorator over a tier
list**, living strictly *above* the seam (CLAUDE.md rule 6) and reusable with or
without mdk.

---

## Context

Deva wants the standalone SDK to be "robust, with fallbacks, for cost efficiency
and high performance." Two facts shape the design:

1. **ADR 049 already designed this** — D9 (policy router), D10 (fallback chains +
   circuit breakers), D11 (latency hedging), D13 (TTS phrase cache), D1
   (capability manifests). But 049 frames them as an **mdk-runtime** control
   plane: it reads cost off ADR 036 metering, drift off ADR 047 observability,
   and rolls out via per-tenant flags. **None of that exists in a bare
   `movate-voice` install.** A Lyzr user (ADR 069) has no mdk metering seam, no
   observability-intelligence layer, no tenant flag store.

2. **mdk's existing resilience is also mdk-bound.** The Executor's retry/fallback
   (`core/retry.py`, `core/failures.py`, the executor fallback chain) is exactly
   the right *shape*, but it is wired to `MovateError`, the provider registry,
   and the run record — none of which the voice package may import (ADR 067 keeps
   it mdk-free).

So the resilience must be **re-expressed as self-contained primitives in
`movate-voice`** that (a) need nothing but the speech Protocols, and (b) expose
*optional* hooks that mdk can wire into 036/047/049 when mdk *is* present. This
ADR does that; it is 049's De-risk pillar made portable, not a new direction.

## Decision

`movate-voice` ships a small **resilience layer of composite providers** plus a
declarative policy. The default policy is **latency-first, cost-bounded**. Every
piece is mdk-free; mdk enriches via hooks.

### D1 — Composite providers that *are* providers (fallback chains)

`FailoverSTT`, `FailoverTTS`, and `FailoverRealtime` each implement the
corresponding ADR-048 Protocol and wrap an **ordered tier list** of real
adapters. On a provider error/timeout they **fail over to the next tier**; the
failover applies **before a result is committed** — for STT, before the
`is_final=True` transcript is yielded; for TTS, before the first audio frame is
emitted (re-synthesizing a half-spoken answer on a different voice would be worse
than degrading). This is the multi-provider generalization of ADR 048 D8's
graceful degrade: try **another provider first**, then ADR 048's text fallback
remains the final net. The pipeline (ADR 067) is handed a `Failover*` exactly as
it would a single adapter — **zero pipeline change**.

### D2 — Latency-first, cost-bounded default policy + capability manifests

The default tiering optimizes **perceived performance first**, with cost as a
bound rather than the primary objective (the deciders' choice):

- **T1 (primary) — low-latency:** Deepgram STT / Cartesia TTS.
- **T2 (fallback) — cost/availability:** OpenAI Whisper / OpenAI TTS.
- **T3 (sovereign) — region/compliance:** Azure Speech STT / Azure Neural TTS.

Selection is driven by a lightweight **capability manifest** embedded per adapter
(ADR 049 D1, carried into the package): `latency_tier`, `$/min` (STT) / `$/char`
(TTS), `languages`, `streaming`, `sovereignty`/region. The router reads the
manifest to find providers that *can* satisfy an agent's stated requirements and
the policy to order *which*. The policy is a plain declarative object,
**configurable per agent/tenant** by the embedding application; the default above
is what a bare install gets.

### D3 — Circuit breaker + bounded retry (package-local taxonomy)

Each wrapped provider has a **circuit breaker**: consecutive failures trip it and
the router **routes away** until a cooldown elapses, so a degraded vendor is
skipped instead of retried every turn. Within a provider, transient failures get
a **bounded retry with backoff**, keyed by a **package-local** minimal failure
taxonomy — `timeout` / `rate_limit` / `unavailable` / `auth` — defined *in*
`movate-voice`. It deliberately **mirrors the shape** of `core/failures.py` /
`core/retry.py` but does **not import them** (that would re-couple to mdk, ADR
067). Terminal failures (`auth`) never retry and never fail over to a paid tier
on a credentials bug.

### D4 — Cost-bounded guard (the "cost-efficient" half)

A configurable **cost ceiling** per turn (and optionally per session): the router
prefers T1 for latency, but if the projected `$/min`·duration or `$/char`·length
would exceed the budget, it **drops to a cheaper tier** for that turn. This is
what makes "latency-first" safe to run at scale — performance by default, with a
hard cost backstop. Projected cost is computed from the D2 manifest (no mdk
metering needed); when mdk *is* present, the D7 hook can feed real metered cost
back to tighten the estimate.

### D5 — Latency hedging (opt-in, OFF by default)

ADR 049 D11, carried verbatim: an opt-in mode fires the same audio at **two
providers** and **takes whichever returns first** — buying latency with cost. It
is **off by default** (it doubles the metered surface for the hedged stage) and
is an explicit per-agent knob, never implicit. It is the ceiling of the
performance story for callers willing to pay.

### D6 — TTS phrase cache (deterministic cost + latency win)

ADR 049 D13, made self-contained: cache synthesized audio for repeated phrases
(greetings, disclaimers, IVR prompts, canned answers) keyed by
`(text, voice_id, provider, codec)`. The package ships an **in-process default
cache** behind a small `VoiceCache` Protocol, so an embedder (mdk, or a Lyzr
deployment) can plug in Redis/blob storage without changing the router. A pinned
voice-model version is part of the key, so a voice change invalidates the entry.

### D7 — Optional observability / metering hooks (`VoiceObserver`)

The router emits structured events — `provider_selected`, `failover`,
`circuit_open` / `circuit_close`, `retry`, `hedge_won`, `cache_hit`,
`cost_estimate` — through a thin `VoiceObserver` Protocol. A bare install gets a
**no-op** (optionally a stderr observer for debugging). **mdk** wires a
`VoiceObserver` that forwards to ADR 036 metering and the ADR 047
observability-intelligence layer, so the same router that runs naked on Lyzr
becomes fully measured inside mdk — **without the package importing 036/047.**

### D8 — Relationship to ADR 049 (composition, not supersession)

This ADR provides the **in-process primitives** — router, failover, breaker,
retry, hedge, cache, manifests — usable with **zero mdk dependency**. ADR 049
remains the **mdk control-plane spec** for the mechanisms that genuinely require
mdk infrastructure: per-tenant feature-flagged rollout + instant rollback (049
D12), the standing bake-off `mdk voice bench` over a golden corpus (049 D5),
drift detection on the obs layer (049 D7), and shadow/canary live evaluation
(049 D6). Those consume *these* primitives; they are not re-specified here. **No
conflict:** where 049 says "the router," this ADR is the router's portable
engine; 049 is the mdk policy/measurement surface around it.

## Consequences

**Positive**
- **Uptime decoupled from any one vendor** — a provider outage routes to the next
  tier, then to ADR 048's text fallback, with **no pipeline or transport change**
  (the router *is* a provider).
- **Performance by default, cost bounded** — T1 latency-first with a D4 budget
  backstop and a D6 phrase cache delivers Deva's "high performance *and* cost
  efficiency" in one policy.
- **Runs anywhere** — identical resilience on a bare Lyzr install and inside mdk;
  mdk just adds measurement via the D7 hook.
- **No re-coupling to mdk** — the failure taxonomy/retry mirror core's shape but
  import nothing from it, preserving ADR 067's standalone promise.

**Negative / risks**
- **A second, parallel retry/taxonomy** to `core/failures.py` (D3) — duplication
  by design (decoupling) but a drift risk. *Mitigation:* both are tiny; a
  contract test asserts they classify the same provider errors the same way.
- **Hedging (D5) doubles the metered surface** for the hedged stage — *opt-in,
  explicit, off by default* (ADR 036 visibility when run under mdk).
- **Manifest cost figures go stale** as vendors reprice (D2/D4) — the estimate
  drifts from reality. *Mitigation:* the D7 hook feeds real metered cost back
  under mdk; standalone users update the manifest with the SDK.
- **Router complexity must stay above the seam** — a selection concern leaking
  into the pipeline/Executor is a boundary violation (CLAUDE.md rule 6) to catch
  in review.

**Neutral**
- All surface is **additive** and lives in `movate-voice`; no change to the three
  Protocols, the WS transport, the `agent.yaml` schema, storage, or mdk's APIs.

## New surfaces (flagged per CLAUDE.md rule 5)

All **ADDITIVE**, all in `movate-voice`; none changes an existing
`agent.yaml`/`project.yaml` field, the `/api/v1` API, a storage schema, a
`MOVATE_*`/`MDK_*` env var, an existing `--json` shape, or deploy behavior:
- **`FailoverSTT` / `FailoverTTS` / `FailoverRealtime`** composite providers.
- **The capability-manifest fields** per adapter (latency tier, `$/min`/`$/char`,
  languages, streaming, sovereignty).
- **The routing policy object** (tiers + cost ceiling + hedging flag).
- **The `VoiceCache` and `VoiceObserver` Protocols** (pluggable cache + hooks).

## Alternatives considered

- **(a) Put fallback logic inside the pipeline driver.** Rejected — it couples
  resilience to the transport and forces every consumer to inherit it; a
  composite-that-is-a-provider keeps the pipeline a thin, unchanged driver.
- **(b) Import mdk's `core/retry.py` + `core/failures.py`.** Rejected — it
  re-couples `movate-voice` to mdk, breaking ADR 067's standalone promise; we
  mirror the shape instead.
- **(c) Single provider, no fallback (rely on ADR 048 D8 text degrade only).**
  Rejected — that degrades to *text* on any provider blip; Deva asked for
  multi-provider robustness, which means trying another *voice* provider first.
- **(d) Cost-first default (cheapest provider primary).** Considered and
  rejected as the *default* per the deciders — latency-first/cost-bounded gives
  better perceived UX with a hard budget backstop; cost-first remains a
  one-line policy change for cost-sensitive deployments.
- **(e) Re-specify 049's rollout/bench/drift here.** Rejected — those need mdk
  infrastructure; D8 keeps them in 049 and exposes only the portable engine here.

## Boundaries (explicitly NOT in scope)

- **Per-tenant rollout flags + instant rollback, `mdk voice bench`, drift
  detection, shadow/canary** — stay in ADR 049 (mdk control plane); they consume
  these primitives (D8).
- **Any change to the three speech Protocols, the WS transport, or the pipeline
  driver's contract** (ADR 048/067) — the router rides the seam unchanged.
- **The exact circuit-breaker thresholds, backoff curves, and cache eviction
  policy** — build-time defaults in `movate-voice`, tunable via the policy
  object; not fixed by this ADR.
- **Voice-cloning / recording consent policy** — out of scope here as in ADR 049.

## Cross-references / composition notes

- **ADR 067 (standalone SDK).** This resilience ships *in* `movate-voice` and is
  handed to `run_voice_pipeline` as an ordinary provider; it depends on the
  `AgentTurn` seam only insofar as it lives in the same package.
- **ADR 049 (agility layer).** D1/D9/D10/D11/D13 are the spec; this ADR is their
  portable, in-process engine. 049's D5/D6/D7/D12 (bench/shadow/drift/rollout)
  stay in mdk and consume this engine (D8).
- **ADR 048 (seams).** Composites implement the unchanged three Protocols.
- **ADR 036 / 047 (metering / observability).** Reached *only* through the
  optional `VoiceObserver` hook (D7); the package never imports them.
- **ADR 018 (BYOK).** Keys flow per-call via `api_key=`; the router selects a
  provider, then passes that provider's tenant key through unchanged.
- **ADR 069 (Lyzr binding).** A Lyzr deployment uses these composites directly to
  get resilient voice with no mdk present.

---

## Consolidation update (2026-06-02)

This ADR's resilience primitives ship under
[`src/movate/voice/`](../../src/movate/voice/) in this repo (alongside the
pipeline and adapters), not in a separate `movate-voice` distribution — see
ADR 067's consolidation update for the packaging rationale.

The architectural shape is unchanged from D1-D8:

* `FailoverSTT` / `FailoverTTS` / `FailoverRealtime` composites — implement the
  three speech Protocols, never enter the pipeline, never know about mdk
  (CLAUDE.md rule 6). See [`failover.py`](../../src/movate/voice/failover.py).
* Capability manifest (D2) — [`manifest.py`](../../src/movate/voice/manifest.py).
* Circuit breaker (D3) — [`breaker.py`](../../src/movate/voice/breaker.py),
  with the package-local failure taxonomy in
  [`failures.py`](../../src/movate/voice/failures.py) (kept separate from
  `movate.core.failures` even now, so the seam survives a future re-extraction).
* TTS phrase cache (D6) — [`cache.py`](../../src/movate/voice/cache.py).
* `VoiceObserver` (D7) — [`observer.py`](../../src/movate/voice/observer.py);
  mdk wires it to ADR 047 observability via the runtime, never via an import
  back from `movate.voice`.

**Boundary still holds.** Although the code shares a repo with mdk, no module
under `movate.voice` imports `movate.core` / `movate.runtime` / `movate.cli` —
that one-way dependency is the line that keeps a future re-extraction cheap.
