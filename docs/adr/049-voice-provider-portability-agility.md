# ADR 049 — Voice provider-portability & agility layer: make provider choice runtime, measured, and reversible

**Status:** Proposed
**Date:** 2026-05-28
**Deciders:** Engineering + Deva (Movate)
**Builds on / composes with (changes nothing in any of them):**
ADR 048 (voice agents — the three speech seams `SpeechToTextProvider` / `TextToSpeechProvider` / `RealtimeVoiceProvider`, the WS transport, the optional `voice:` block; **this ADR sits entirely ON TOP of those seams and does not alter a single one of them**),
ADR 007 (the adapter/plugin pattern — `BaseLLMProvider` / `StorageProvider` / `Tracer` are the precedent; provider portability is that same seam philosophy pushed from *swappable-in-code* to *swappable-at-runtime-and-measured*),
ADR 036 (usage metering + quotas — already meters STT-seconds / LLM-tokens / TTS-chars; the cost signals the router and the bake-off read come straight off this seam, not a new meter),
ADR 047 (observability intelligence — the drift signals, voice-turn span dashboards, and NL-queryable provider metrics ride the existing intelligence layer),
ADR 043 (self-improving agent loop — the self-tuning forward bet feeds bake-off + drift signals into the existing loop; it does not invent a new loop),
ADR 041 (agent catalog / catalog.movate.io — the forward-bet provider leaderboard extends the existing catalog channel, not a new marketplace),
ADR 045 (D-series; **semantic cache** — the TTS prompt cache pairs with the existing caching layer rather than building a second cache),
ADR 018 (per-tenant BYOK — provider keys, including any new provider added out-of-tree, slot into the same tenant key store + `ProviderKeyResolver`; routing/fallback never changes the key model).

**Defining architectural fact.** The voice **providers** (STT / TTS / realtime /
telephony) are the **volatile** layer of the voice stack: new vendors arrive
monthly, models change silently underneath a stable API name, and price /
quality / latency swing without notice. ADR 048 already made providers
*swappable in code* (the three seams). The durable product is **everything
AROUND the providers** — the manifests, the router, the bake-off, the drift
detector, the fallback chains, the per-tenant rollout. This ADR builds that
surround so that adopting a newly-launched provider is a **days-not-weeks,
measured, reversible** change instead of a code-time rewrite and a gamble.
**Provider choice becomes RUNTIME, MEASURED, and REVERSIBLE — not code-time,
assumed, and sticky.** This ADR adds **zero** new execution-plane behavior to
ADR 048's pipeline; it adds a control/measurement/routing layer on top of the
seams ADR 048 defined.

---

## Context

### The volatility problem

The speech-provider market is the fastest-churning layer of the whole stack:

- **New vendors monthly.** STT/TTS/realtime startups launch and reprice
  constantly; last quarter's best-latency provider is this quarter's
  second-best. A platform that hardcodes 2–3 providers is rewriting code every
  time the frontier moves.
- **Silent model changes.** A provider keeps the same API name
  (`whisper-large`, a named TTS voice) while quietly updating the model behind
  it — your WER or your brand voice changes overnight with **no version bump you
  can see**. This is a regression you cannot detect by reading a changelog.
- **Swinging price / quality / latency.** Per-minute and per-character pricing,
  first-byte latency, and naturalness all move independently and frequently. A
  provider that was the cost leader in January is undercut by March.

ADR 048 deliberately anticipated this — its decision drivers list
**provider-portability** (swap Whisper↔Deepgram, OpenAI-TTS↔ElevenLabs without
touching the agent) and **graceful operational failure** (a provider down must
degrade, not hard-fail). ADR 048 gave us the **seam** that makes a swap a
single-file change in code. What it explicitly did **not** build — and what this
ADR is — is the layer that makes that swap a **runtime, measured, reversible**
decision rather than a code-time, assumed, sticky one.

### The runtime / measured / reversible thesis

The seam alone makes providers *swappable in code*. But a code-time swap is
still:

- **Code-time** — it needs a deploy to change which provider serves which
  tenant, so reacting to "a better provider launched Tuesday" is an engineering
  cycle, not a config flip.
- **Assumed** — without a standing benchmark, "Provider X is better" is a
  vendor claim or a hunch; there is no apples-to-apples verdict on *your* audio,
  *your* languages, *your* latency needs.
- **Sticky** — once a provider is wired in and serving, switching is risky
  (will it regress live calls?), so teams stay on a worse provider out of fear.

The durable answer is to invest in **everything around the providers** so the
providers themselves become **disposable**. Concretely, this layer is what turns
*"a better provider launched"* into a change that is:

- **runtime** — flip a tenant flag, no redeploy (D12);
- **measured** — `mdk voice bench` over a versioned golden corpus gives an
  apples-to-apples verdict before you switch (D5), and shadow/replay prove it
  under *your* live conditions (D6/D8);
- **reversible** — circuit breakers and instant rollback mean a bad switch costs
  one config flip, not an incident (D10/D12).

That is the whole ADR: **make the providers cheap to add, cheap to evaluate, and
cheap to walk away from.**

---

## Relationship to ADR 048 (what this changes there: nothing)

This ADR **composes on top of** ADR 048 and **does not modify** any of its
decisions. Specifically:

- It does **not** touch the three Protocols (`SpeechToTextProvider`,
  `TextToSpeechProvider`, `RealtimeVoiceProvider`) — every new mechanism here
  consumes those seams as-is.
- It does **not** touch ADR 048's WS transport (`WS /api/v1/agents/{name}/voice`),
  its message protocol, the optional `voice:` `agent.yaml` block, the
  `mdk[voice]` extra, or the Executor (still unchanged, still modality-blind).
- It does **not** change BYOK (ADR 018), metering (ADR 036), Sessions/streaming
  (ADR 045), guardrails, or barge-in (R4b) — it *reads* their signals (cost,
  latency, drift) and *routes* across the seams; it adds no new execution
  behavior inside the pipeline.

Where ADR 048 says *"a tenant swaps Whisper↔Deepgram by changing a key + a
`voice.stt` value"*, this ADR is the machinery that decides **which** value to
set, **proves** it is the right value, and lets the swap happen at **runtime**
and be **undone** instantly. ADR 048 is the seam; ADR 049 is the agility around
the seam.

---

## Decision

The agility layer is organized as **three pillars** — **Decouple** (make
providers disposable), **Measure** (turn volatility into data), and **De-risk**
(keep volatility away from the customer) — followed by **forward bets** marked
future/optional. Each decision rides ADR 048's seams and the existing
metering/observability/cache/catalog layers; **none** alters them.

### Pillar 1 — Decouple (make providers disposable)

**D1 — Capability manifests.** Each provider ships a **declarative manifest** of
what it can do: streaming? supported languages? sample rates? word-level
timestamps? endpointing? barge-in support? latency tier? `$/min` (STT) / `$/char`
(TTS)? region/sovereignty (e.g. on-prem, EU-only)? The runtime **negotiates** an
agent's stated requirements against the manifests instead of hardcoding a
vendor, so adding a provider is *manifest + adapter, zero core edits*, and
queries like *"which providers do Hindi + sub-200ms first-byte + on-prem?"*
become answerable mechanically. The manifest is the data the router (D9) reads
and the thing `mdk voice providers list` (below) renders.
**New surface — flag (CLAUDE.md rule 5).** The manifest is a **new declarative
file format** (an additive provider-side artifact, e.g. `voice-provider.yaml` /
a `capabilities` field on the provider registration). It is additive — no
existing schema changes — but it is a **new surface** and is called out here per
rule 5; its exact field set is a build-time detail (Boundaries).

**D2 — Out-of-tree provider plugin SDK (entry-point discovery).** Customers and
partners add a voice provider **without forking mdk**, via the same entry-point
plugin discovery pattern the other adapter seams already use (ADR 007). A
provider package implements the ADR-048 Protocol(s) + ships a D1 manifest +
registers under a `movate.voice_providers` entry-point group; the runtime
discovers it at startup. This **removes Movate as the bottleneck** for every new
vendor and lets **regulated customers plug in on-prem / regional providers**
they cannot send audio outside of — entirely behind the existing seam, with keys
flowing through ADR 018 BYOK unchanged.

**D3 — Model-version pinning + trace surfacing.** A provider can be pinned to a
specific model/voice version (e.g. `whisper-large-v3`, a named TTS voice model
revision), and the **resolved version is surfaced in the voice-turn trace**
(ADR 048's span tree / ADR 024 spans). This turns **silent provider model
updates into OPT-IN, not surprise regressions**: if a provider rolls a new model
under the same name, a pinned agent is unaffected, and an unpinned one shows the
version change in its traces (and the drift detector D7 catches the metric move).
Pinning is expressed where the provider is selected (tenant default / the
optional `voice:` block) — no new execution behavior, just a version carried
through to the existing trace.

**D4 — Transport/codec layer split from providers.** The transport (WebRTC / WS
/ telephony) and codec (μ-law / Opus / PCM) concerns are kept as their **own
layer**, evolving independently of the STT/TTS adapters. ADR 048 already
transcodes **at the edge** (D8 there) and keeps codecs out of the agent; this
ADR makes that an explicit *portability* boundary: a new transport (e.g. a new
telephony bridge) or a new codec is added **without touching any STT/TTS
adapter**, and a new STT/TTS provider is added without caring how audio arrived.
This keeps the two volatile axes (who transcribes/synthesizes vs. how audio is
carried) from contaminating each other.

### Pillar 2 — Measure (turn volatility into data)

**D5 — Standing bake-off: `mdk voice bench`.** A **reproducible benchmark** over
a **versioned golden-audio corpus** that gives an apples-to-apples verdict on any
provider: for **STT** — WER, latency, cost; for **TTS** — a naturalness proxy
(MOS-style), first-byte latency, `$/char`. One command (`mdk voice bench`)
produces the verdict, so every future provider decision is **measured, not
assumed**. It **reuses the existing `mdk eval` framework** (corpus → run →
scored report) with **voice metrics** added — not a new eval engine.
**New surface — flag (CLAUDE.md rule 5).** `mdk voice bench` is a **new CLI
command** (additive; opt-in, lives behind the `mdk[voice]` extra like ADR 048's
`mdk voice test`). It changes no existing CLI shape; flagged per rule 5. (The
exact metric formulas are a build-time detail — Boundaries.)

**D6 — Shadow / canary evaluation.** Route a **percentage of real traffic** to a
**candidate** provider in **shadow** mode — *measure, don't serve*: the
incumbent's output is what the caller hears, the candidate runs in parallel and
its WER/latency/cost are recorded — so a candidate is proven **under live
conditions** before any switch. Canary is the same machinery dialed up: serve a
small live percentage with instant rollback (D12) if metrics regress. Shadow
consumes the ADR-048 seam twice for the same audio; the comparison rides the
metering (ADR 036) and observability (ADR 047) layers.

**D7 — Drift detection.** Track each provider's **measured WER / latency / cost
vs. a baseline** (the baseline established by D5's bake-off and ongoing
production telemetry) and **alert on drift**. This is the **antidote to silent
provider-side regressions** (the D3 problem from the metric side): when a
provider quietly changes its model and WER creeps up or first-byte latency
worsens, drift detection catches it. It **integrates with ADR 047** — drift is
an insight the observability-intelligence layer surfaces on the existing
voice-turn dashboards and via NL query, not a new alerting stack.

**D8 — Replay corpus from consented recordings.** Re-run **real (consented) past
calls** against a candidate provider **offline** to answer *"would Provider X
have done better on last week's actual traffic?"* — without risking a single
live call. This is the highest-signal evaluation (real audio, real
distribution) and complements D5's curated golden corpus (controlled,
reproducible). Consent + retention are a **guardrail** on this feature (only
consented, retained-per-policy recordings are replayable). **The
voice-cloning / recording ethics & consent policy itself is OUT OF SCOPE here**
— it is a separate doc (Boundaries); this ADR only states that replay is gated
on it.

### Pillar 3 — De-risk (volatility never reaches the customer)

**D9 — Voice router.** A **policy-driven provider selector** — choose STT/TTS
(and realtime, where applicable) by **latency / cost / language /
quality / region-sovereignty**, **per tenant or per agent**. It is the direct
analog of an LLM model-router, reading the D1 capability manifests to find
providers that *can* satisfy the agent's requirements and the policy to pick
*which* of them. The router is where "this tenant is cost-sensitive and EU-only"
or "this agent needs sub-200ms Hindi" becomes an automatic, declarative
selection instead of a hardcoded vendor. It sits **above** ADR 048's seams —
once it picks a provider, the unchanged ADR-048 pipeline runs.

**D10 — Fallback chains + circuit breakers.** A provider selection is a
**chain**, not a single point: if the primary STT times out or errors
**mid-stream**, the handler **fails over to a secondary** provider; a **circuit
breaker** trips on a provider that is degraded/erroring and **routes away from it
automatically** until it recovers. This **decouples uptime from any one vendor**
and is the operational, multi-provider generalization of ADR 048's D8 graceful
degrade (which falls back to *text*); here we first try **another provider**,
then ADR 048's text fallback remains the final safety net. Failover reuses the
ADR-048 seam — it is the same `transcribe` / `synthesize` contract on a
different implementation.

**D11 — Latency hedging (opt-in knob, NOT default).** For premium tenants, an
**opt-in** mode fires the same audio at **two providers** and **takes whichever
returns first** — *buying latency with cost*. This is **off by default** (it
doubles the metered STT/TTS surface for the hedged stage, ADR 036) and is an
explicit per-tenant/per-agent knob, never an implicit behavior. It is the
ceiling of the latency story for tenants willing to pay for it.

**D12 — Per-tenant feature-flagged rollout + instant rollback.** Enabling a new
provider is a **feature flag** per tenant: enable for **one** tenant, **ramp**
the percentage, and **revert in one config flip** with **no redeploy**. This is
what makes a switch **reversible** — a bad provider is undone instantly, and a
good one is ramped with confidence. It pairs with D6 (canary) and D7 (drift): a
rollout that drifts is auto-flagged and can be rolled back before it spreads.

**D13 — TTS prompt cache.** Cache **synthesized audio for repeated phrases** —
greetings, disclaimers, IVR prompts, common canned responses — keyed by
(text, voice, provider, codec). This is a large **deterministic** cost +
latency win: the same disclaimer spoken on every call is synthesized once. It
**pairs with ADR 045's semantic/caching layer** (the same cache substrate,
extended to an audio artifact) rather than introducing a separate cache, and it
respects D3 pinning (a voice-model version change invalidates the cache key).

### Forward bets (future / optional — NOT all v1)

**D14 — Self-tuning voice loop (forward bet).** Feed the D5 bake-off + D7 drift
signals into the **self-improving loop (ADR 043)**: continuously benchmark
providers against **each tenant's profile** (language mix, latency needs,
budget) and **recommend** the best fit — or, **guardrailed**, auto-switch via
D12's rollout machinery. This is a *forward bet*, not v1: the loop and its
guardrails (recommend-first, human-approve, then optionally automate) are
deferred until the measurement (D5/D7) and rollout (D12) primitives are proven.

**D15 — Voice provider catalog + live leaderboard (forward bet).** Extend the
**agent catalog (ADR 041 / catalog.movate.io)** to host **provider adapters**
alongside their **standing benchmark scores** — a **self-updating leaderboard**
fed by D5/D7, with community/partner adapters flowing through the **same
channel** as catalog entries today. This is a *forward bet*; a **full public
provider marketplace is explicitly NOT v1** (Alternatives (c)) — the leaderboard
is a read surface on the existing catalog, deferred until the bake-off corpus
and scores are mature.

**D16 — Capability-gated new modalities (forward bet).** New cutting-edge
features — emotion control, voice cloning, diarization, real-time translation —
arrive as **OPTIONAL Protocol methods**, **capability-gated via the D1
manifest**, so adopting a frontier feature on one provider **never breaks plain
pipeline agents** that don't use it. The manifest declares "this provider
supports diarization"; an agent that asks for diarization is routed (D9) only to
providers whose manifest advertises it; everyone else is unaffected. This keeps
the seam open to modality growth without a schema break — a *forward bet* on how
new capabilities land, not a v1 deliverable.

---

## Build-first sequencing (agility-per-unit-effort)

Ordered so each step maximizes agility gained per unit of effort and each later
step clips onto the earlier ones:

1. **Capability manifests (D1) + voice router (D9) + fallback chains (D10).**
   The **spine** — everything else clips onto it. The manifest is the data, the
   router is the decision, the fallback chain is the resilience; together they
   make providers genuinely interchangeable at runtime.
2. **`mdk voice bench` (D5) + the golden corpus (D5).** Now **every future
   provider decision becomes measured**. This pays off **every month** the
   market churns — the standing bake-off is reusable forever.
3. **Out-of-tree provider plugin SDK (D2).** Removes **Movate as the
   bottleneck** — customers/partners add providers without a fork, and regulated
   customers plug in on-prem/regional providers.
4. **Operational hardening: drift detection (D7), shadow eval (D6), TTS cache
   (D13), per-tenant rollout (D12).** The de-risking and cost/quality
   optimization that make the layer production-grade.

Forward bets (D14 self-tuning, D15 leaderboard, D16 modality gating) follow only
after the spine + measurement + plugin SDK are proven.

---

## Consequences

**Positive.**
- **Adopting a new provider becomes days-not-weeks, measured, reversible:**
  write a D1 manifest → `mdk voice bench` (D5) for an apples-to-apples verdict →
  flip a per-tenant flag (D12). No rewrite, no gamble, no redeploy.
- **Silent provider regressions are caught** by drift detection (D7) against a
  baseline — the antidote to a provider quietly swapping its model under a stable
  name (D3).
- **Uptime is decoupled from any one vendor** via fallback chains + circuit
  breakers (D10) — a provider outage routes to another provider, then to ADR
  048's text fallback as the final net.
- **Cost is continuously optimizable** — the router (D9) can prefer cheaper
  providers per policy, the TTS cache (D13) eliminates repeat synthesis, and the
  bake-off (D5) keeps the $/min and $/char picture current.
- **Movate is no longer the bottleneck** for new vendors (D2), and regulated
  customers can run sovereign on-prem/regional providers behind the same seam.

**Negative / risks.**
- **New surfaces to maintain** — flagged below: the capability-manifest file
  format (D1) and the new CLI commands (D5/D9/D6). All additive, but they are
  surfaces to version and document.
- **The golden corpus + replay corpus are assets to curate** (D5/D8) — a stale
  or unrepresentative corpus gives a misleading verdict; the corpus is
  versioned (D5) precisely so its provenance is auditable.
- **Hedging (D11) and shadow (D6) multiply the metered surface** — both run
  audio through two providers for a stage; both are opt-in and metered via ADR
  036 so the cost is visible and capped, never implicit.
- **Router/fallback complexity must stay above the seam** — the router and
  fallback logic must never leak into the ADR-048 Executor/pipeline (CLAUDE.md
  rule 6); a selection concern reaching into execution logic is a boundary
  violation to catch in review.

**Neutral.**
- All net-new surface is **additive** and either control-plane (router policy,
  rollout flags) or measurement (bench, drift) — **no change** to ADR 048's
  seams, transport, schema, the Executor, `core`, existing endpoints, or
  existing CLI shapes.

---

## New surfaces (flagged per CLAUDE.md rule 5)

All **ADDITIVE**; none changes an existing `agent.yaml`/`project.yaml` field, the
`/api/v1` runtime API, a storage schema, a `MOVATE_*`/`MDK_*` env var, an
existing `--json` shape, or deploy behavior. Called out explicitly:

- **Provider capability-manifest format (D1)** — a **new declarative file
  format** shipped alongside a provider adapter (additive provider-side
  artifact). New surface; exact fields are a build-time detail.
- **`mdk voice bench` (D5)** — a **new, opt-in CLI command** (behind the
  `mdk[voice]` extra), reusing the `mdk eval` framework with voice metrics.
- **`mdk voice providers list` (D1/D9)** — a **new, opt-in CLI command** that
  renders the discovered providers + their manifest capabilities (and, with the
  D15 forward bet, their leaderboard scores).
- **`mdk voice try` (D5/D6)** — a **new, opt-in CLI command** to try a candidate
  provider against the corpus / a sample (the interactive companion to `bench`).

(Router policy, rollout flags, and drift thresholds are control-plane config
read by the router/loop; they are additive config, not changes to any existing
flagged surface, but are noted here for completeness.)

---

## Alternatives considered

- **(a) Hardcode 2–3 providers in code.** **Rejected.** The market churns
  monthly (Context); a hardcoded set forces a code rewrite + redeploy every time
  the frontier moves, and gives no measured basis for "is the new one actually
  better." This is exactly the code-time/assumed/sticky trap the ADR exists to
  escape.
- **(b) Rely on ADR 048's adapter seam ALONE — no router, no bench, no
  manifests.** **Rejected.** The seam makes providers *swappable in code*, but a
  swap stays **code-time** (needs a deploy), **assumed** (no apples-to-apples
  verdict on your audio), and **sticky/risky** (will it regress live calls?).
  The seam is necessary but not sufficient: adoption stays a gamble and a swap
  stays scary. This layer is what makes the swap *runtime, measured, and
  reversible* — the seam's portability promise only pays off with the surround
  built on top of it.
- **(c) Build a full public provider marketplace now.** **Deferred.** The
  catalog **leaderboard (D15)** is a *forward bet* — a read surface on the
  existing ADR-041 catalog fed by the bake-off scores — **not** a v1 deliverable.
  A full marketplace (submission, ranking, monetization, trust) is premature
  before the bake-off corpus and scoring are mature; we ship the measurement and
  the plugin SDK first and let the leaderboard grow from them.

---

## Boundaries (explicitly NOT in scope)

- **The implementation.** This is the ADR — the architectural decision. The
  manifests, router, bench, drift detector, fallback chains, and rollout
  machinery are the *spec*; code lands in follow-up PRs.
- **Exact benchmark metric formulas (D5).** Which WER normalization, which MOS
  proxy, the precise latency percentiles — all **build-time details**, not fixed
  here.
- **Voice-cloning / recording consent & ethics policy (D8/D16).** The consent,
  likeness, and retention policy that *gates* replay (D8) and voice cloning
  (D16) is a **separate doc** — this ADR only states that those features are
  gated on it.
- **Realtime providers' session protocols.** The OpenAI Realtime / Gemini Live
  wire/session specifics remain an **ADR 048 Phase 2** concern behind the
  `RealtimeVoiceProvider` seam; this ADR routes/measures across realtime
  providers where applicable but does not specify their protocols.
- **Any change to ADR 048's three Protocols, transport, schema, or the
  Executor.** This ADR **does NOT modify** `SpeechToTextProvider` /
  `TextToSpeechProvider` / `RealtimeVoiceProvider`, the WS transport, the
  optional `voice:` block, or the unchanged Executor (CLAUDE.md rule 6). It
  layers **on top** — every mechanism here consumes those seams as-is.

---

## Cross-references / composition notes

- **ADR 048 (the seams this composes on).** D1/D2 add providers behind the
  *unchanged* three Protocols; D3 surfaces the resolved model version in ADR
  048's voice-turn span tree; D4 makes ADR 048's edge-transcode an explicit
  portability boundary; D9/D10/D11 select/fail-over/hedge **across** those seams
  without changing them; D13 caches the output of ADR 048's `synthesize`. **The
  three Protocols are untouched.**
- **ADR 036 (metering) — cost signals.** The `$/min` / `$/char` the router (D9)
  and bake-off (D5) read come off the existing metering seam; shadow (D6) and
  hedging (D11) are visible + capped there.
- **ADR 047 (observability intelligence) — drift signals.** Drift (D7) is an
  insight on the existing voice-turn dashboards / NL query, not a new alerting
  stack.
- **ADR 043 (self-improving loop) — self-tuning (D14, forward bet).** Bake-off +
  drift feed the existing loop, guardrailed; no new loop.
- **ADR 041 (catalog) — leaderboard (D15, forward bet).** Provider adapters +
  scores ride the existing catalog channel; not a new marketplace.
- **ADR 045 (semantic cache) — TTS cache (D13).** The audio cache extends the
  existing cache substrate, not a second cache.
- **ADR 018 (BYOK) — keys.** Any provider added out-of-tree (D2) keys through
  the same tenant store + `ProviderKeyResolver`; routing/fallback never changes
  the key model.
- **ADR 007 (adapter/plugin pattern) — the philosophy.** Provider portability is
  ADR 007's seam pattern pushed from *swappable-in-code* to
  *swappable-at-runtime-and-measured*. If an agility-layer change can't be
  expressed as control/measurement/routing **on top of** the ADR-048 seams, it
  needs its own ADR.
