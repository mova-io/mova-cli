# ADR 074 — Telephony bridge: LiveKit + Twilio transports for voice agents (Phase 3)

**Status:** Accepted — transports shipped: Phase 3a LiveKit (`voice/transports/
livekit.py`, #709) + Phase 3b Twilio (`voice/transports/twilio.py` + the
`/api/v1/agents/{name}/call/twilio*` inbound webhook/Media-Stream routes), both
behind the `mdk[telephony]` extra with tests. **Remaining: deployment + a
phone-callable demo** (no telephony infra is stood up yet) — see the "last mile"
note below. Phase 3c (Daily) deferred.
**Date:** 2026-06-03 _(status reconciled to shipped reality 2026-06-08)_
**Deciders:** Engineering + Deva (Movate)
**Builds on / composes with (changes nothing in their wire contracts):**
ADR 048 (voice agents — the three speech seams, the WS transport, the pipeline,
the `mdk[voice]` extra; **D4 names telephony as Phase 3**, D8 names the codec
matrix; this ADR delivers that phase),
ADR 050 (voice API surface & CLI parity — D2 WS + batch, D12 realtime mode; the
telephony transports produce/consume the same `AudioChunk` iterators the
pipeline already binds to),
ADR 067 (consolidated `movate.voice` + the `AgentTurn` seam — the pipeline is
transport-agnostic; telephony is a new edge, not a new pipeline),
ADR 072 (semantic turn-detection — the turn-detector seam works identically over
a phone line; no telephony-specific turn logic),
ADR 073 (voice latency budget — the endpointing-wait optimizations apply to
telephony turns unchanged; the phone adds only codec transcoding latency at the
edge),
ADR 018 (per-tenant BYOK — telephony provider keys slot into the same tenant
key store and `ProviderKeyResolver`; `LIVEKIT_*` / `TWILIO_*` credentials are
just more provider keys),
ADR 069 (Lyzr ADK voice binding — references LiveKit's hosted voice runtime as
a narrow opt-in transport, researched and rejected as the primary path; this ADR
is the mdk-native telephony bridge that keeps the full pipeline).

**Defining architectural fact.** Telephony is the **last transport mile** for
voice agents. ADR 048 designed voice as transport-agnostic: the pipeline
(`audio -> STT -> unchanged agent -> TTS -> audio`) does not know whether audio
comes from a browser WebSocket, a LiveKit room, or a Twilio Media Stream. Phase
3 delivers two telephony transports behind a `TelephonyTransport` Protocol --
one for each strategic positioning:

- **LiveKit** -- self-hostable, sovereignty-friendly, WebRTC-native, with
  built-in SIP support so inbound phone calls route directly to mdk rooms.
  Audio never leaves the customer's own infrastructure.
- **Twilio** -- enterprise PSTN reach, established brand, global phone numbers,
  SIP trunking, carrier-grade reliability. The instant-on path for enterprises
  that already hold a Twilio account.

The pipeline is **unchanged**. An agent that works over the WebSocket works over
the phone -- zero changes to `agent.yaml`, `prompt.md`, or the Executor.

---

## Context

ADR 048 Phase 1 shipped the web WebSocket transport; Phase 2 shipped the
realtime (speech-to-speech) backend. Both are browser-native: audio arrives
over a WebSocket from a web client. Phase 3 -- named in ADR 048 D4 as the
Twilio/LiveKit/Daily telephony bridge -- extends voice agents to the **phone
network**: a customer dials a number, the call routes to the mdk voice pipeline,
and the agent speaks and listens over PSTN/SIP exactly as it does over the
browser.

The prior art already exists:

- **Codec transcoding** is built and tested on `main`
  ([`src/movate/voice/telephony.py`](../../src/movate/voice/telephony.py)):
  G.711 mu-law to/from PCM16, sample-rate conversion with an anti-alias biquad,
  frame-aligned outbound chunking for Twilio's 20 ms / 160-byte media frames.
  `telephony_inbound` and `telephony_outbound` are async generators that bridge
  raw phone audio into/out of the pipeline's `AudioChunk` stream.
- **LiveKit's voice runtime** was researched in ADR 069 (alternative e):
  Lyzr's `POST /v1/sessions/start` provisions a LiveKit room + worker, but
  doing so reduces mdk-voice to a media pipe and forfeits the ADR 068
  differentiators (failover, circuit breaking, cost-bounded routing, TTS phrase
  cache). The decision there was to keep the native pipeline and add a narrow
  transport adapter instead. This ADR is that adapter.
- **The pipeline is transport-agnostic.** `run_voice_pipeline` consumes
  `AsyncIterator[AudioChunk]` in and emits `VoiceEvent` out. A telephony
  transport only needs to (1) decode phone audio into `AudioChunk` (already
  done by `telephony.py`), (2) feed it to the pipeline, and (3) encode the
  pipeline's TTS `AudioChunk` back to phone audio (also already done). The
  pipeline itself is untouched.

Two transports, not one, because they serve different customer profiles:

- **LiveKit** is the sovereignty play. It is Apache-2.0 open source, self-
  hostable on the customer's own infra (Azure, AWS, on-prem), WebRTC-native,
  and has built-in SIP gateway support. Audio never transits a third party
  unless the customer chooses LiveKit Cloud. For customers with data-residency
  or regulatory constraints, self-hosted LiveKit is the answer.
- **Twilio** is the enterprise-reach play. Twilio has global PSTN numbers,
  carrier-grade SIP trunking, established enterprise contracts, and a developer
  ecosystem. For customers who need phone calls working by Friday with a number
  in 40 countries, Twilio is the answer.

Both produce and consume the same `AudioChunk` iterators the pipeline already
binds to. The pipeline never learns which one is connected.

---

## Decision

### D1 -- `TelephonyTransport` Protocol

A new Protocol in `voice/transports/base.py` that abstracts a telephony
session -- connecting to a room/call, receiving audio, publishing audio, and
disconnecting:

```python
class TelephonyTransport(Protocol):
    """A bidirectional audio channel to a telephony session (a LiveKit room
    or a Twilio Media Stream). The pipeline calls it identically to the WS
    transport -- same ``AudioChunk`` in/out."""
    name: str

    async def connect(self, session_config: dict) -> AudioStream: ...
    async def publish(self, audio: AudioChunk) -> None: ...
    async def disconnect(self) -> None: ...
```

`connect` returns an `AudioStream` (an `AsyncIterator[AudioChunk]`) the
pipeline consumes as `audio_in`. `publish` sends a synthesized `AudioChunk`
back to the caller. `disconnect` tears down the session. The pipeline calls
these identically regardless of whether the backing session is a LiveKit room
or a Twilio Media Stream -- the same adapter-seam pattern as
`SpeechToTextProvider` / `TextToSpeechProvider` (ADR 048 D3, ADR 007,
CLAUDE.md rule 7).

### D2 -- LiveKit transport (`voice/transports/livekit.py`)

Connects to a LiveKit room via the `livekit-agents` SDK:

- **Subscribes** to participant audio tracks; feeds frames into the
  STT -> Executor -> TTS pipeline as `AudioChunk`s.
- **Publishes** TTS audio back to the room as an audio track.
- **SIP-native.** LiveKit has built-in SIP support: inbound phone calls route
  directly to mdk rooms via LiveKit's SIP trunk configuration. No separate SIP
  gateway is needed.
- **Sovereignty story.** Self-host LiveKit on the customer's own infra (Azure
  VMs, AKS, on-prem bare metal); audio never leaves the tenant. LiveKit Cloud
  is an option, not a requirement.
- **Codec handling.** LiveKit handles codec conversion internally -- WebRTC
  Opus to PCM at the SDK level. The transport yields `AudioChunk(codec="pcm16")`
  to the pipeline; no edge transcoding needed (unlike Twilio, D3).
- **Supports LiveKit Cloud + self-hosted.** `LIVEKIT_URL` points at either;
  the transport does not distinguish.

### D3 -- Twilio transport (`voice/transports/twilio.py`)

Connects via Twilio Media Streams WebSocket:

- **Receives** mu-law audio from the Twilio stream; transcodes via
  `telephony.py` (`telephony_inbound`, already on `main`) to PCM16
  `AudioChunk`s for the pipeline.
- **Publishes** TTS PCM16 `AudioChunk`s back by transcoding to mu-law via
  `telephony_outbound` (already on `main`), re-chunked into Twilio's 160-byte
  (20 ms at 8 kHz) frame format.
- **Enterprise PSTN reach.** Twilio provides global phone numbers, SIP
  trunking, carrier-grade reliability, and established enterprise billing.
- **Codec transcoding at the edge (reuse `telephony.py`, D7).** The mu-law
  to/from PCM16 + rate conversion is **already built** and tested. The Twilio
  transport uses it; the pipeline never sees raw telephony audio.

### D4 -- `POST /api/v1/agents/{name}/call` endpoint

Provisions a telephony session and dispatches the mdk voice worker into it.
Tenant-auth'd (ADR 013 `run` scope; ADR 033 hardening applies).

- **Request:** `{ "transport": "livekit" | "twilio", "agent": "<name>",
  "options": { ... } }` -- the caller selects the transport and passes
  transport-specific options (LiveKit room settings, Twilio TwiML overrides).
- **Response (LiveKit):** `{ "room_name": "...", "participant_token": "...",
  "livekit_url": "..." }` -- the caller joins the room to start the
  conversation. For inbound SIP calls, LiveKit routes the call to the
  configured mdk endpoint automatically; this endpoint is for outbound /
  programmatic session creation.
- **Response (Twilio):** `{ "stream_url": "wss://...", "call_sid": "..." }` --
  the Twilio-side webhook connects to the returned stream URL. For inbound
  calls, the Twilio webhook handler routes to the mdk worker directly.
- **Session lifecycle.** The endpoint creates the session, dispatches the
  worker (which runs `run_voice_pipeline` with the telephony transport as
  `audio_in` / audio-out), and returns the join credentials. The caller (or
  the phone network) joins; when the call ends (disconnect / hang-up), the
  transport's `disconnect` tears down the session.

**New surface -- flag (CLAUDE.md rule 5):** `POST /api/v1/agents/{name}/call`
is a **new `/api/v1` endpoint** (scope `run`). Additive; no existing endpoint
changes.

### D5 -- `[telephony]` opt-in extra

The telephony transports pull **heavy** dependencies that are never core:

```toml
[project.optional-dependencies]
telephony = [
  "livekit-agents>=0.11",   # Apache-2.0 — LiveKit server SDK for Python agents
  "livekit>=0.14",           # Apache-2.0 — LiveKit client SDK
  "twilio>=9.0",             # MIT — Twilio helper library
]
```

These are **separate from the base `[voice]` extra** (which stays lean: STT/TTS
provider SDKs only). A runtime installed with `mdk[voice]` but without
`mdk[telephony]` has web-WS voice but no phone bridge -- zero impact. Each
dependency is **permissively licensed** (`scripts/check_licenses.py --strict`)
and justified in its install PR (CLAUDE.md rule 8). The telephony package
(`src/movate/voice/transports/`) imports them **lazily**, only when a telephony
transport is actually instantiated -- exactly the posture of the existing
`langfuse`, `keychain`, `cross-encoder`, and `ocr` extras.

### D6 -- Credentials (same BYOK pattern)

Telephony provider keys slot into ADR 018's existing tenant key store and
`ProviderKeyResolver` seam with **no new credential model**:

| Provider | Credentials | Storage |
|---|---|---|
| LiveKit | `LIVEKIT_URL` + `LIVEKIT_API_KEY` + `LIVEKIT_API_SECRET` | tenant Key Vault (ADR 018) |
| Twilio | `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN` | tenant Key Vault (ADR 018) |

Resolved tenant-key-first with the existing shared-key fallback semantics.
Never returned by any API, redacted in logs/traces.

**CLI:** `mdk auth login livekit` / `mdk auth login twilio` -- the existing
`mdk auth login` pattern (CLAUDE.md rule 5: additive provider entries, no new
CLI shape).

### D7 -- Codec transcoding at the edge (reuse `telephony.py`)

The mu-law to/from PCM16 transcoding + sample-rate conversion is **already
built** (`src/movate/voice/telephony.py`, on `main`):

- `telephony_inbound`: async generator that decodes 8 kHz mu-law frames to
  PCM16 `AudioChunk`s at the pipeline's rate (16 kHz default), with a
  Butterworth anti-alias filter on downsample.
- `telephony_outbound`: async generator that encodes PCM16 `AudioChunk`s to
  mu-law, re-chunked into Twilio's 160-byte frames.
- `pcm16_to_wav`: wraps raw PCM in a WAV container for APIs that sniff format.

The **Twilio transport** uses these directly. The **LiveKit transport** does
not need them -- LiveKit handles codec conversion internally (WebRTC Opus to
PCM at the SDK level). The pipeline never sees raw telephony audio; codec
concerns stay at the edge (ADR 048 D8, CLAUDE.md rule 6).

### D8 -- Pipeline unchanged

The pipeline's contract is `AsyncIterator[AudioChunk]` in,
`AsyncIterator[VoiceEvent]` out. LiveKit and Twilio transports just produce
and consume those iterators. The STT -> Executor -> TTS path is identical to
the browser WebSocket. Concretely:

- `run_voice_pipeline(audio_in=transport.audio_stream, stt=..., tts=...,
  agent=...)` -- the pipeline does not know whether `audio_in` comes from a
  WebSocket, a LiveKit room, or a Twilio Media Stream.
- All existing pipeline features apply unchanged: speculative kickoff (ADR
  070), semantic turn-detection (ADR 072), adaptive endpointing (ADR 073),
  streaming TTS, barge-in, PII redaction, text filtering.
- An agent that works over the WebSocket works over the phone -- **zero
  changes** to `agent.yaml`, `prompt.md`, or the Executor.

This is the literal realization of ADR 048's "voice is a transport + two seams
that wrap the unchanged Executor" promise, extended to the phone network.

### D9 -- `mdk voice call <agent> --transport livekit|twilio` CLI

Initiates an outbound call: provisions a telephony session (D4) and joins as a
participant (or connects the local mic/speaker for testing). For testing and
demo workflows.

```
mdk voice call support-agent --transport livekit    # join a LiveKit room
mdk voice call support-agent --transport twilio     # initiate a Twilio call
```

Requires the `[telephony]` extra. Maps to `POST /api/v1/agents/{name}/call`
(D4), so the CLI-to-API parity contract (ADR 050 D11 / ADR 032 #147) is
maintained.

**New surface -- flag (CLAUDE.md rule 5):** `mdk voice call` is a **new,
opt-in CLI verb** behind the `mdk[telephony]` extra. No existing CLI shape
changes.

### D10 -- Observability

Telephony turns are `RunRecord`s with `modality: "voice"` + a new
`transport: "livekit" | "twilio"` field (extends the runs-parity from ADR 050
D8 / #689). The voice-turn trace span (ADR 048 D7 / ADR 024) includes:

- **Call duration** -- total session time (connect to disconnect).
- **PSTN cost** -- estimated per-minute cost from the telephony provider
  (best-effort; the `usage` frame / economics headers carry it alongside the
  three-stage STT/LLM/TTS cost, ADR 036/ADR 050 D7).
- **Codec stats** -- transcode latency at the edge (mu-law to/from PCM16, D7);
  a regression here is a regression in voice latency.
- **Transport metadata** -- room name (LiveKit) or call SID (Twilio), for
  operational correlation.

Telephony turns appear in `mdk runs list`, `/api/v1/usage`, traces, and
dashboards automatically (D8 / ADR 050 D8) -- because they are runs, not a
telephony silo.

---

## Phasing

- **Phase 3a (M):** LiveKit transport (`voice/transports/livekit.py`) + the
  `[telephony]` extra + `mdk auth login livekit` + `POST /api/v1/agents/{name}/call`
  + `mdk voice call --transport livekit` CLI. LiveKit is first because it is
  self-hostable (sovereignty-first) and has built-in SIP (fewer moving parts for
  an inbound-call demo).

- **Phase 3b (M):** Twilio transport (`voice/transports/twilio.py`) + SIP trunk
  configuration + inbound call routing via Twilio webhooks + `mdk auth login
  twilio` + `mdk voice call --transport twilio`. Twilio is second because it
  requires external SIP trunk setup and Twilio account provisioning, but
  delivers enterprise PSTN reach.

- **Phase 3c (S):** Daily.co transport -- if customer demand surfaces. The same
  `TelephonyTransport` Protocol, a thin adapter (`voice/transports/daily.py`).
  Daily's API shape is similar enough to LiveKit's that the adapter is small;
  deferred because no current customer has asked for it.

---

## New surfaces (flagged per CLAUDE.md rule 5)

All **ADDITIVE**; none changes an existing `agent.yaml`/`project.yaml` field,
an existing `/api/v1` endpoint's request/response shape, a storage schema, a
`MOVATE_*`/`MDK_*` env var, an existing `--json` shape, or deploy behavior:

- **`TelephonyTransport` Protocol** (`voice/transports/base.py`) -- a new
  adapter seam for telephony sessions.
- **`POST /api/v1/agents/{name}/call`** (D4) -- a new `/api/v1` endpoint
  (scope `run`) for provisioning telephony sessions.
- **`[telephony]` extra** (D5) -- a new opt-in `pyproject.toml` extra pulling
  `livekit-agents`, `livekit`, `twilio`.
- **`mdk voice call`** (D9) -- a new opt-in CLI verb behind `mdk[telephony]`.
- **`mdk auth login livekit` / `mdk auth login twilio`** (D6) -- additive
  provider entries on the existing `mdk auth login` pattern.
- **`LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` / `TWILIO_ACCOUNT_SID` /
  `TWILIO_AUTH_TOKEN`** (D6) -- new credentials in the tenant key store.
- **`transport: "livekit" | "twilio"`** on `RunRecord` (D10) -- additive field
  for observability.

---

## Consequences

**Positive.**
- **Every mdk voice agent is phone-callable with zero agent changes.** An
  agent that works over the WebSocket works over a LiveKit room or a Twilio
  call -- same `agent.yaml`, same `prompt.md`, same Executor (D8). The
  transport is the only thing that changes.
- **Two transport options, each serving a distinct customer profile.** LiveKit
  for sovereignty (self-hosted, audio stays in-tenant); Twilio for enterprise
  PSTN reach (global numbers, carrier-grade). Customers choose by their
  constraints, not ours.
- **Pipeline and all optimizations are reused unchanged.** Speculative kickoff,
  semantic turn-detection, adaptive endpointing, streaming TTS, barge-in, PII
  redaction -- all apply to telephony turns identically (D8).
- **Codec transcoding is already built and tested** (`telephony.py` on `main`,
  D7) -- the hardest edge-level problem is solved.
- **Opt-in, zero blast radius.** The `[telephony]` extra is separate from
  `[voice]`; a runtime without it is wholly unaffected (D5).

**Negative / risks.**
- **New adapter seam to maintain** -- `TelephonyTransport` (D1) is a new
  Protocol alongside the existing STT/TTS/Realtime seams. It is thin (three
  methods), but it is a new contract.
- **Telephony provider SDK churn.** `livekit-agents` and `twilio` SDKs are
  actively developed; breaking changes in either require adapter updates. The
  adapter-file-per-transport pattern (one file per SDK) contains the blast
  radius.
- **SIP trunk configuration is operator-run** (Boundaries) -- an operator must
  configure the SIP trunk on LiveKit or Twilio to point at the mdk endpoint.
  This is not automatable by mdk and is a deployment step to document.
- **Latency budget gains a transport leg.** Phone audio adds codec transcoding
  (~1-2 ms, D7) and network hops (PSTN to SIP to mdk). These are small
  compared to the endpointing wait (ADR 073) but are a new leg in the latency
  trace to monitor (D10).
- **PSTN number provisioning is out of scope** (Boundaries) -- Twilio/LiveKit
  handle number procurement; mdk does not manage phone numbers.

**Neutral.**
- One new package (`src/movate/voice/transports/`) with the Protocol + two
  adapter files, one new REST endpoint, one new CLI verb, one new
  `pyproject.toml` extra. All additive; no change to the pipeline, the
  Executor, `core`, existing endpoints, or existing CLI shapes.

---

## Alternatives considered

- **(a) Genesys CX for telephony (CCaaS integration).** Rejected -- CCaaS
  lock-in, 8 kHz-only audio (lower STT quality), proprietary protocols, and a
  heavy vendor dependency. mdk *complements* Genesys via AudioHook (Genesys
  routes audio to the mdk pipeline) rather than embedding inside it. A future
  Genesys AudioHook adapter is a thin `TelephonyTransport` if demand arises.
- **(b) Build our own WebRTC stack.** Rejected -- LiveKit **is** the WebRTC
  stack. It is Apache-2.0, self-hostable, purpose-built for real-time media,
  and actively maintained. Building a WebRTC SFU from scratch would be a
  multi-year platform effort with no differentiation over LiveKit.
- **(c) Twilio-only (no LiveKit).** Rejected -- no self-hosted sovereignty
  story. Twilio is a hosted service; audio always transits Twilio's
  infrastructure. Customers with data-residency requirements (healthcare,
  finance, government) need a self-hosted transport. LiveKit fills that gap.
- **(d) LiveKit-only (no Twilio).** Rejected -- LiveKit's PSTN reach requires
  self-hosting + SIP trunk setup, which is operational overhead many enterprises
  will not accept for a first deployment. Twilio's instant-on phone numbers and
  established enterprise billing are the low-friction on-ramp.
- **(e) Embed inside Lyzr's hosted LiveKit runtime.** Rejected -- researched
  in ADR 069 (alternative e). Using Lyzr's `POST /v1/sessions/start` reduces
  mdk-voice to a media pipe and forfeits failover, circuit breaking,
  cost-bounded routing, and the TTS phrase cache. The narrow opt-in
  `LyzrLiveKitSession` transport adapter remains acceptable future work for
  deployments that explicitly want Lyzr's PSTN/SIP plane.

---

## Boundaries (explicitly NOT in scope)

- **SIP trunk configuration.** Configuring the SIP trunk on LiveKit or Twilio
  to route calls to the mdk endpoint is an **operator-run** deployment step,
  not automated by mdk. Documentation covers it; the ADR does not specify the
  trunk's configuration schema.
- **PSTN number provisioning.** Procuring phone numbers is done through
  Twilio's console / LiveKit's SIP configuration, not through mdk. mdk
  provisions *sessions*, not *numbers*.
- **Call recording consent / policy.** Recording calls and the consent model
  around it are a **separate policy decision** (ADR 048 Boundaries), not this
  architecture ADR. The telephony transport does not record by default.
- **The pipeline itself.** This ADR adds transports *around* the pipeline; the
  pipeline (`run_voice_pipeline`) is untouched (D8).
- **Changes to the Executor, `core`, existing `/api/v1` endpoints, or existing
  CLI shapes.** All net-new surface is additive; the Executor and `core` are
  untouched.
- **On-prem / edge STT/TTS.** A self-hosted speech backend slots in behind the
  D3 seam (ADR 048); telephony does not change the speech-provider story.
- **Daily.co transport.** Deferred to Phase 3c if customer demand surfaces
  (Phasing).

---

## Failure modes

- **LiveKit room creation fails.** `POST .../call` returns a `503` with a
  clear error; the caller retries or falls back to the WS transport. No
  partial session is left dangling (the endpoint is atomic: create + dispatch
  succeed together or not at all).
- **Twilio Media Stream disconnects mid-call.** The transport detects the
  socket close, emits a `done(status="disconnected")` event, and tears down the
  pipeline. The run is recorded as completed (not failed) with the partial
  transcript and answer -- a hang-up is normal, not an error.
- **Codec transcoding failure.** A malformed mu-law frame from Twilio is
  logged and skipped (the next frame resumes); a sustained codec error surfaces
  as an `error` event (`stage="transport"`) and the call degrades to text
  fallback (ADR 048 D8).
- **Telephony provider credentials invalid / expired.** `POST .../call` returns
  a `401` with the provider name; `mdk auth login <provider>` is the remediation.
  The call is never partially provisioned.
- **LiveKit self-hosted instance unreachable.** Same as room creation failure:
  `503`, retry or fallback. The transport does not retry internally (retry
  policy is the caller's / the orchestrator's).
- **PSTN cost spike.** PSTN minutes are metered through the `usage` frame (D10)
  and roll into the ADR 036 `voice_seconds` quota. A quota-exceeded call is
  wound down cleanly (ADR 048 D7 failure mode), not cut mid-word.

---

## Cross-references / composition notes

### Reusing `telephony.py` (already on `main`) as the codec edge

The mu-law to/from PCM16 transcoding (`mulaw_to_pcm16`, `pcm16_to_mulaw`),
sample-rate conversion (`resample_pcm16` with anti-alias biquad), and the
async bridge generators (`telephony_inbound`, `telephony_outbound`) are built,
tested, dependency-free, and 3.11--3.13 safe. The Twilio transport (D3) calls
them directly; the LiveKit transport (D2) does not need them (LiveKit's SDK
handles Opus to PCM). No codec logic is added by this ADR.

### Reusing ADR 048's pipeline contract (D8)

`run_voice_pipeline` consumes `AsyncIterator[AudioChunk]` and emits
`VoiceEvent`. Telephony transports produce/consume those iterators. The
pipeline does not learn that it is connected to a phone -- the same property
that lets an agent work over WS, LiveKit, or Twilio with zero changes.

### Reusing ADR 018 (BYOK) for telephony keys (D6)

`LIVEKIT_*` and `TWILIO_*` credentials are ordinary provider keys in the
tenant key store, resolved through `ProviderKeyResolver` (tenant-key-first,
shared-key fallback), never returned, redacted in logs. No new credential
model; `mdk auth login livekit` is the existing pattern with a new provider
name.

### Reusing ADR 050 D11 (CLI-to-API parity) for `mdk voice call` (D9)

`mdk voice call` maps to `POST /api/v1/agents/{name}/call` (D4). The mapping
is enforced by the existing OpenAPI contract test (#147 /
`tests/test_front_end_api_contract.py`, ADR 032) -- telephony is not exempt
from the parity contract.

### ADR 069 (Lyzr LiveKit research) informing the architecture

ADR 069 alternative (e) researched and rejected embedding inside Lyzr's hosted
LiveKit runtime because it forfeits the pipeline's differentiators. This ADR
is the mdk-native answer: the telephony transport feeds the full pipeline, so
failover, circuit breaking, cost-bounded routing, the TTS phrase cache, and
every latency optimization (ADRs 070/072/073) apply to phone calls unchanged.
