# ADR 048 — Voice agents: speech adapter seams + a streaming transport that plug voice into ANY existing agent

**Status:** Proposed
**Date:** 2026-05-28
**Deciders:** Engineering + Deva (Movate)
**Builds on / depends on:**
ADR 007 (the adapter-seam pattern — `BaseLLMProvider`/`StorageProvider`/`Tracer` are the precedent; voice adds three more Protocols in exactly the same shape, no new wiring philosophy),
ADR 045 (API ergonomics + next-gen capabilities — **D10 Stateful Sessions** are voice memory; a voice turn is a session turn, the executor stays stateless; **D11 run-output token streaming** is what lets TTS start speaking before the full answer exists — voice reuses D11's `_sse_run_stream` token frames as the agent stage of the pipeline),
ADR 036 (usage metering + quotas — voice meters **three** stages: STT-seconds + LLM-tokens + TTS-characters; quotas extend to a new `voice_seconds` dimension, capped + degraded exactly like every other metered surface),
ADR 018 (per-tenant BYOK — voice provider keys (STT/TTS/realtime) live in the same tenant key store, resolved through the same `ProviderKeyResolver` seam; audio routes to the customer's chosen providers with the customer's keys, same sovereignty model as the LLM),
ADR 015 / ADR 024 (self-hosted observability + per-step spans — a voice turn is one trace with child spans per stage: STT latency, agent run, TTS latency, audio duration),
**R4b run cancellation** (item 36, #424 — `POST /api/v1/jobs/{id}/cancel` + `JobStatus.CANCELLED` + the cooperative cancel-flag the worker checks; **barge-in is run cancellation triggered by a VAD signal** — voice reuses this machinery, it does not invent a new stop path),
the **guardrails engine** (applied to the **text** plane — post-STT on the inbound transcript, pre-TTS on the outbound answer — so every existing safety policy keeps working unchanged on a voice call).

**Defining architectural fact:** Voice is **not a new kind of agent**. It is a
**transport + two adapter seams that wrap the unchanged text Executor**. An
agent's text input/output is the contract; STT turns audio into that text and
TTS turns that text back into audio. The Executor and every existing agent are
**UNCHANGED**. This is the whole ADR in one fact, and it is the reason an agent
written last month becomes voice-capable tonight with **zero edits to its
`agent.yaml` or `prompt.md`**.

---

## Context

Deva asked for voice agents on the roadmap with one explicit, load-bearing
requirement: **"an adapter that can plug into ANY existing agent"** — not only
new voice-native agents, but the agents customers already have in production
today. That requirement is the design constraint that decides the entire
architecture.

There are two ways to add voice to an agent platform, and they are not
equivalent:

1. **Voice as a new agent *type*.** A `voice-agent` kind with its own spec, its
   own executor, its own prompt conventions. This is how a lot of platforms
   ship voice, and it **fails Deva's requirement on day one**: an existing text
   agent is not a voice agent, so it cannot be one without being rewritten as
   one. Every existing deliverable would need a parallel voice rebuild.

2. **Voice as a *transport over the unchanged Executor*.** Audio comes in, an
   STT seam turns it into the text the agent already expects, the **existing,
   unchanged** text Executor runs, and a TTS seam turns the text answer back
   into audio. The agent never learns it was spoken to. **Every** existing
   agent — and every future one — is voice-capable for free, because voice
   operates entirely *around* the agent, on a transport, through seams.

This ADR chooses **(2)**, decisively, because **(2) is the only one that
satisfies "plug into ANY existing agent."** And (2) is not a new pattern for
MDK — it is *exactly* the adapter-seam pattern the codebase already lives by:

- A new model is a new `BaseLLMProvider` implementation; `core` never changes
  (ADR 007).
- A new persistence/vector backend is a new `StorageProvider`; `kb`/`runtime`
  never change.
- A new observability sink is a new `Tracer`; execution logic never changes.

Voice is the same move at the **edge of the request**: a new `SpeechToText`
implementation and a new `TextToSpeech` implementation, plus a WebSocket
transport that pipes audio through them and the **unchanged** run path between
them. No Executor surgery, no new agent kind, no schema break. This is **seams,
not a rewrite** — and stating that plainly upfront is the point of writing it
down before any code exists.

The pieces voice needs are **already built** and only need to be *composed*,
not reinvented:

- **Memory** — multi-turn voice memory is just ADR 045 D10 Sessions; a voice
  turn is a session turn. The Executor stays stateless (D10 is explicit about
  this); the session service assembles history and calls the unchanged
  Executor.
- **Low latency** — ADR 045 D11 already streams the agent's *output tokens*.
  Voice streams **every** stage (partial STT → streaming agent tokens (D11) →
  streaming TTS) so the agent starts *speaking* before the full answer is
  generated. The latency floor is a composition of existing streaming, not a
  new streaming engine.
- **Cost** — ADR 036 already meters + quotas LLM spend; voice adds two more
  metered stages (STT-seconds, TTS-characters) on the same machinery.
- **Safety** — the guardrails engine already runs on text; voice applies it on
  the **text** plane (post-STT, pre-TTS), so safety is unchanged on a voice
  call.
- **Barge-in** — interrupting the agent mid-sentence is **run cancellation**
  (R4b) fired by a voice-activity signal; the cancel path already exists.
- **BYOK** — voice provider keys slot into ADR 018's tenant key store + the
  `ProviderKeyResolver` seam with no new credential model.

In one sentence: *"Voice is a streaming transport plus two speech seams that
wrap the unchanged text Executor; STT→agent→TTS makes EVERY existing agent
voice-capable with zero changes to its `agent.yaml`/`prompt.md`, reusing
Sessions for memory, D11 streaming for latency, ADR 036 for cost, ADR 018 for
keys, the guardrails engine for safety on the text plane, and R4b for
barge-in."*

---

## Decision drivers

| Driver | Weight |
|---|---|
| **Zero change to existing agents** — Deva's explicit ask; an existing text agent MUST become voice-capable with no edit to its `agent.yaml`/`prompt.md` | HIGH |
| **Seams, not a rewrite** — voice follows ADR 007's adapter pattern exactly; `core`/Executor untouched; new providers are new files | HIGH |
| **Latency is the product** — a voice agent that pauses awkwardly is unusable; stream every stage so the agent speaks before the full answer exists | HIGH |
| **BYOK + sovereignty for audio** — audio routes to the customer's chosen providers with the customer's keys; audio never leaves the tenant except to the chosen provider (ADR 018) | HIGH |
| **Reuse, don't rebuild** — memory (Sessions D10), streaming (D11), cost (ADR 036), safety (guardrails), barge-in (R4b) all already exist; voice composes them | HIGH |
| **Opt-in, zero blast radius** — voice is an opt-in `mdk[voice]` extra (heavy audio libs + provider SDKs); agents/runtimes without it are wholly unaffected | HIGH |
| **Provider-portability** — STT/TTS/realtime are competitive, fast-moving markets; the seam must let a tenant swap Whisper↔Deepgram or OpenAI-TTS↔ElevenLabs without touching the agent | MED |
| **Graceful operational failure** — STT or TTS provider down must degrade (fall back to text), not hard-fail a live call | MED |

---

## Architecture

```
   PIPELINE MODE (D2a — the headline; works with EVERY existing + new agent, ZERO changes)
   ─────────────────────────────────────────────────────────────────────────────────────

        caller (browser / phone)
              │  audio frames in  (PCM/Opus over WS; μ-law over telephony)
              ▼
   ┌──────────────────────── WS /api/v1/agents/{name}/voice (D4) ────────────────────────┐
   │                                                                                       │
   │   audio in ──▶  D3 SpeechToTextProvider  ──▶  text  ──▶ guardrails(text)  ──┐         │
   │                 (streaming + endpointing)     (the contract)                │         │
   │                                                                             ▼         │
   │                                                            ┌────────────────────────┐ │
   │   (Sessions D10 = memory; the Executor stays stateless) ──▶│  THE UNCHANGED          │ │
   │                                                            │  TEXT EXECUTOR          │ │
   │                                                            │  + existing agent       │ │
   │                                                            │  (NO agent.yaml change) │ │
   │                                                            └───────────┬────────────┘ │
   │                                                                        │ streaming     │
   │                                                                        │ tokens (D11)  │
   │   audio out ◀── D3 TextToSpeechProvider ◀── guardrails(text) ◀─────────┘               │
   │                 (streaming synthesis)         (pre-TTS)                                │
   │                                                                                       │
   │   control plane:  barge-in / cancel  ◀── VAD ──  (R4b run cancellation: stop TTS,     │
   │                                                   cancel the run, resume listening)    │
   └───────────────────────────────────────────────────────────────────────────────────┘
        meter: STT-seconds + LLM-tokens + TTS-chars (ADR 036)   │   trace: one voice-turn span tree (ADR 024)
        keys:  STT/TTS keys from tenant Key Vault (ADR 018)     │   audio stays in-tenant except to the chosen provider


   REALTIME / SPEECH-TO-SPEECH MODE (D2b — opt-in, new voice-native agents only)
   ─────────────────────────────────────────────────────────────────────────────

        caller ─audio⇄audio─▶  D3 RealtimeVoiceProvider (OpenAI Realtime / Gemini Live)  ─▶ caller
                               full-duplex voice↔voice; lowest latency; provider-specific;
                               does NOT reuse the text Executor (Boundaries)
```

Everything in the pipeline diagram between `text` and `text` — the Executor,
the agent bundle, Sessions, guardrails, metering, tracing, BYOK — **already
exists**. The net-new code is: three Protocol definitions + their first
implementations (D3), one WebSocket route + its message protocol (D4), the
optional additive `voice:` block on `agent.yaml` (D5), an `mdk[voice]` extra
(D9), and the edge transcode/VAD glue. The `core` package and the Executor are
**not touched**.

---

## Decisions

### D1 — Voice = a transport + two seams that WRAP the unchanged Executor

The pipeline is fixed and simple:

```
audio  ──▶  STT  ──▶  [ the existing text agent, run by the unchanged Executor ]  ──▶  TTS  ──▶  audio
```

The agent's **text I/O is the contract**. STT produces the text the agent
already accepts; TTS consumes the text the agent already produces. The Executor
does not learn that the text arrived as speech, and the agent's prompt does not
change. Voice lives **entirely around** the agent, on the transport and in the
seams — never *inside* it. This is the literal application of CLAUDE.md rule 6
(control plane ⊥ execution plane; `core` depends on Protocols, not concrete
backends) and rule 7 (extend via adapters, don't hardcode). A change that
required editing the Executor to "know about voice" would be the wrong design
and would need a different ADR; this ADR deliberately makes that unnecessary.

### D2 — Two modes: Pipeline (the headline) and Realtime (opt-in premium)

**(a) Pipeline (STT → text-agent → TTS).** The headline. Works with **EVERY**
existing and future agent, **zero changes**, because the agent is just the
unchanged text run path in the middle. This is the mode that satisfies Deva's
"plug into ANY existing agent." It is the default and the bulk of the value
(Phase 1).

**(b) Realtime / speech-to-speech (voice↔voice).** An **opt-in** path for
**new voice-native agents** that want the lowest possible latency and richer
prosody/turn-taking, served by a full-duplex provider (OpenAI Realtime, Gemini
Live). It is voice↔voice — it **does not reuse the text Executor**, so it does
not voice-enable an existing text agent and it accepts provider lock-in in
exchange for the latency floor. It is a *premium, opt-in* path, not the default
(Phase 2), and is selected explicitly via `voice.mode: realtime` (D5).

The two modes share the transport (D4), the BYOK key resolution (D6), the
metering surface (D7), and tracing — but only Pipeline reuses the Executor.
This split is itself the answer to "realtime-only" (Alternatives): realtime
alone cannot voice-enable an existing text agent, so it can never be the only
mode.

### D3 — Three new adapter-seam Protocols (the seams; `core` unchanged)

Three new Protocols, defined in a new `src/movate/voice/` package (new files
only; nothing in `core` changes), in **exactly** the shape of the existing
`BaseLLMProvider` Protocol — streaming-friendly async generators of
audio/text chunks, `api_key=`-style injection for BYOK, no cost computed in the
adapter (cost is derived at the metering seam, ADR 036, the same way
`BaseLLMProvider` defers pricing to the executor's versioned table).
Illustrative shapes:

```python
# src/movate/voice/base.py  — illustrative; mirrors providers/base.py

class TranscriptChunk(BaseModel):
    """One slice of a streaming transcription. `is_final` marks an
    endpointed utterance; partial chunks stream as the caller speaks."""
    text: str
    is_final: bool
    confidence: float | None = None

class AudioChunk(BaseModel):
    """One slice of synthesized (or captured) audio. `codec`/`sample_rate`
    describe the bytes so the transport can transcode at the edge (D8)."""
    data: bytes
    codec: Literal["pcm16", "opus", "mulaw"]
    sample_rate: int

class SpeechToTextProvider(Protocol):
    """Audio → text. Whisper / Deepgram / Azure Speech / AssemblyAI.
    STREAMING + endpointing: yields partial transcripts as the caller
    speaks and a final endpointed transcript at utterance end."""
    def transcribe(self, audio: AsyncIterator[AudioChunk], *,
                   language: str | None, api_key: str | None
                   ) -> AsyncIterator[TranscriptChunk]: ...

class TextToSpeechProvider(Protocol):
    """Text → audio. OpenAI TTS / ElevenLabs / Cartesia / Azure.
    STREAMING synthesis: yields audio chunks as text arrives, so playback
    can start before the full answer is synthesized (latency, D7)."""
    def synthesize(self, text: AsyncIterator[str], *,
                   voice_id: str, codec: str, api_key: str | None
                   ) -> AsyncIterator[AudioChunk]: ...

class RealtimeVoiceProvider(Protocol):       # OPTIONAL — Phase 2, mode (b) only
    """Full-duplex voice↔voice. OpenAI Realtime / Gemini Live. Audio in,
    audio out, no intermediate text Executor (D2b). Phase 2."""
    def session(self, audio_in: AsyncIterator[AudioChunk], *,
                voice_id: str, instructions: str, api_key: str | None
                ) -> AsyncIterator[AudioChunk]: ...
```

A new STT/TTS/realtime backend is a **new file implementing the Protocol** and
a registry entry — the same extension story as adding a `BaseLLMProvider`. The
streaming-generator shape is mandatory, not optional: it is what makes D7's
stream-every-stage latency story possible. The realtime Protocol is explicitly
**optional** — Phase 1 ships only STT + TTS.

### D4 — Transport: a full-duplex WebSocket route, plus a Phase-3 telephony bridge

```
WS /api/v1/agents/{name}/voice          # full-duplex; audio in → STT → agent → TTS → audio out
```

A WebSocket (not SSE) because voice is **bidirectional** — audio flows both
ways simultaneously and the caller can interrupt (barge-in) at any moment, which
SSE's one-way model cannot express. The same `{name}` path segment as the
existing run/stream endpoints: it is the **same agent**, reached over a voice
transport. The message protocol over the socket is a small typed envelope set
(JSON control frames + binary audio frames):

| Direction | Frame | Payload |
|---|---|---|
| client→server | `audio` (binary) | a raw audio chunk (PCM/Opus; codec negotiated on connect) |
| client→server | `control:barge_in` | the caller started speaking — interrupt (D8 / R5) |
| client→server | `control:end` | end the call |
| server→client | `transcript.partial` | a streaming partial transcript (STT, not yet endpointed) |
| server→client | `transcript.final` | the endpointed user utterance (the text fed to the agent) |
| server→client | `agent.token` | a streaming agent output token (reuses D11's token frames) |
| server→client | `tts.audio` (binary) | a synthesized audio chunk to play |
| server→client | `usage` | end-of-turn STT-sec / LLM-tokens / TTS-chars (ADR 036; mirrors D11's trailing `usage` SSE frame) |
| server→client | `error` | a stage failure + the degrade taken (D8) |

The connection counts against the **same per-tenant stream concurrency cap** as
ADR 045 D9's `max_sse_streams_per_tenant` (one streaming budget, not a new one).

**Telephony bridge (Phase 3).** A separate bridge adapts a telephony provider
(Twilio Media Streams / LiveKit / Daily) onto the **same** WS pipeline so an
agent can answer a phone number. The bridge transcodes μ-law↔PCM at the edge
(D8) and is otherwise the identical STT→agent→TTS path. Telephony is **Phase 3**
and out of scope for the first implementation (Boundaries).

### D5 — Zero-change enablement: existing agents get voice with no edit at all

This is Deva's requirement, and it is realized **three** ways, in order of how
little the agent author has to do:

1. **Nothing at all (the default promise).** An existing agent becomes
   voice-capable simply by being invoked on `WS /api/v1/agents/{name}/voice`
   with STT/TTS configured **at the tenant level** (a tenant-default STT/TTS
   provider + voice, set once, applying to every agent). The agent's
   `agent.yaml` and `prompt.md` are **not touched**. This is the headline: a
   text agent shipped last month is voice-capable tonight with **zero edits**.
2. **An optional per-agent override.** An author who wants a specific voice,
   language, STT/TTS provider, or realtime mode for *one* agent adds an
   **optional** `voice:` block to that agent's `agent.yaml`:

   ```yaml
   # OPTIONAL — entirely absent on every existing agent; absence == tenant defaults
   voice:
     enabled: true
     mode: pipeline          # pipeline (default) | realtime
     stt: deepgram           # override the tenant-default STT provider
     tts: elevenlabs         # override the tenant-default TTS provider
     voice_id: "rachel"
     language: en-US
   ```
3. **(Phase 2) a voice-native agent** opts into `mode: realtime` in the same
   block.

**Compat (CLAUDE.md rule 5 — flagged surface):** the `voice:` block is an
**ADDITIVE, OPTIONAL** field on the `agent.yaml`/`AgentSpec` schema. Every
existing `agent.yaml` validates unchanged (the field is absent → defaults to
"voice off unless invoked on the voice endpoint with tenant defaults"). No
existing field is changed or removed. Because `agent.yaml`/`project.yaml`
schema is an explicit compat surface under CLAUDE.md rule 5, this addition is
**called out here and must land in its own additive schema PR** (the field
added as an `Optional` sub-model on `AgentSpec`, defaulting to `None`, so
omitting it serializes identically to today). This is the single compat-flagged
change in the ADR.

### D6 — BYOK for voice: provider keys in the tenant Key Vault, audio stays in-tenant

Voice provider keys (STT, TTS, realtime) are **just more provider keys** under
ADR 018: stored encrypted in the same per-tenant key store, resolved at run
time through the same `ProviderKeyResolver` seam, **never** returned by any API,
**redacted** in logs/traces. Audio routes to the **customer's chosen
providers** with the **customer's keys** — the same sovereignty model as the
LLM today. The hard line: **audio never leaves the tenant except to the chosen
provider** the customer configured and keyed. There is no Movate-side audio
relay, no fleet audio store, and no new credential model — voice keys slot into
the existing BYOK machinery (`mdk keys set deepgram`, `mdk keys set
elevenlabs`, resolved tenant-key-first with the existing shared-key fallback
semantics). A future on-prem/self-hosted STT/TTS slots in behind the D3 seam
without changing this.

### D7 — Reuse, don't rebuild: memory, latency, cost, tracing, safety, barge-in

Voice adds **no new** memory, streaming, cost, tracing, safety, or cancellation
engine. It **composes** the ones that already exist:

- **Memory → Sessions (ADR 045 D10).** A voice turn is a session turn. The
  session service assembles history + summary and calls the **unchanged
  stateless Executor** (D10 is explicit that the Executor stays stateless);
  voice inherits multi-turn memory, summarization, truncation, and the
  per-session cost rollup for free.
- **Latency → stream every stage (ADR 045 D11).** Partial STT streams as the
  caller speaks → the agent streams output **tokens** (D11's `_sse_run_stream`
  token frames, reused) → TTS synthesizes those tokens as they arrive. The
  agent **starts speaking before the full answer is generated**. The latency
  floor is a *composition* of existing streaming, not a new engine.
- **Cost → meter all three stages (ADR 036).** A voice turn meters **STT-seconds
  + LLM-tokens + TTS-characters** on the existing metering seam, and quotas
  extend with a new **`voice_seconds`** dimension capped + degraded per ADR 036
  exactly like every other surface. The `usage` WS frame (D4) carries the
  per-turn cost, mirroring D11's trailing `usage` SSE frame.
- **Tracing → voice-turn spans (ADR 015 / ADR 024).** A voice turn is one trace
  with child spans: STT latency, the agent run (already instrumented per ADR
  024), TTS latency, and audio duration — wired at the edge (the WS handler),
  never imported into execution logic (CLAUDE.md rule 6).
- **Safety → guardrails on the TEXT plane.** Guardrails run **post-STT** (on the
  inbound transcript) and **pre-TTS** (on the outbound answer). Because the
  agent's contract is text (D1), every existing guardrail policy applies to a
  voice call **unchanged** — safety did not become voice-aware; voice became
  text on the safety plane.
- **Barge-in → run cancellation (R4b) + VAD.** When voice-activity detection
  fires while the agent is speaking, the handler (1) stops TTS playback and (2)
  **cancels the in-flight run** via the existing R4b cooperative-cancellation
  path (`POST /api/v1/jobs/{id}/cancel` / the worker's cancel-flag checkpoint),
  then resumes listening. Barge-in is **not a new stop mechanism** — it is R4b
  triggered by a VAD signal.

### D8 — The hard problems, each with a concrete mitigation (MDK thinks in failure modes)

Voice is operationally hard. Each hard problem gets a specific, named
mitigation — none is hand-waved:

| Hard problem | Concrete mitigation |
|---|---|
| **Latency** (a pause kills a voice call) | Stream **every** stage (D7): partial STT → streaming agent tokens (D11) → streaming TTS, so the agent speaks before the full answer exists. Realtime mode (D2b) is the latency floor when even that isn't enough. Document a per-stage latency budget (Consequences). |
| **Barge-in / interruption** | VAD-driven **run cancellation (R4b)**: detect the caller speaking → stop TTS → cancel the run → resume listening. Reuses the existing cancel path, no new mechanism. |
| **Turn detection / endpointing** (when did the caller stop?) | Use the **STT provider's endpointing** where it exists (Deepgram/AssemblyAI emit `is_final`); otherwise a **VAD seam** at the edge supplies the endpoint. `TranscriptChunk.is_final` (D3) is the contract either way. |
| **Audio codecs** | PCM/Opus for web, **μ-law for telephony** — **transcode at the edge** (the WS/telephony handler), never inside the agent. `AudioChunk.codec`/`sample_rate` (D3) carry the format so the transport can convert; the agent only ever sees text. |
| **Cost (3× metered surface)** | Meter **all three** stages (D7 / ADR 036); add a `voice_seconds` quota dimension; degrade (fall back to text / end the call cleanly) when a quota is exhausted rather than running unbounded. |
| **Partial failure** (STT or TTS provider down mid-call) | **Graceful degrade.** STT down → the WS returns an `error` frame and offers a **text fallback** (the agent still runs over typed text on the same socket). TTS down → return the agent's **text** answer over the socket (the caller reads instead of hears) rather than dropping the call. A provider outage degrades the modality, it does not hard-fail the agent. |

### D9 — Opt-in `mdk[voice]` extra: heavy, never core, zero impact when unused

Voice pulls **heavy** dependencies — audio libraries (codec/resampling), VAD,
and provider SDKs (Deepgram/ElevenLabs/etc.). These are **NEVER** core. They
live in a new opt-in `pyproject.toml` extra, next to the existing `runtime`,
`langfuse`, `keychain`, `cross-encoder`, and `ocr` extras:

```toml
[project.optional-dependencies]
voice = [
  # audio I/O + codecs + VAD + provider SDKs — heavy; opt-in only.
  # Each shipped dep must pass scripts/check_licenses.py --strict
  # (permissive license) and be justified in the install PR.
]
```

The voice package (`src/movate/voice/`) imports these lazily, only when the
voice transport is actually used — exactly like the `langfuse`, `keychain`,
`cross-encoder`, and `ocr` extras do today. A runtime or CLI installed
**without** `mdk[voice]` is **wholly unaffected**: the WS route isn't
registered, the agent runs text-only, nothing imports an audio library. Every
shipped voice dependency must be **permissively licensed**
(`scripts/check_licenses.py --strict`, per CLAUDE.md rule 8) and justified in
its install PR; the SDKs go in the extra, not in `[project.dependencies]`.

---

## `agent.yaml` schema (the single additive, optional, compat-flagged change)

```yaml
# OPTIONAL block. ABSENT on every existing agent.yaml — absence is valid and
# means "no per-agent voice override; tenant defaults apply if invoked on the
# voice endpoint." Added as an Optional sub-model on AgentSpec, default None.
# CLAUDE.md rule 5 compat surface: ADDITIVE only, lands in its own schema PR.
voice:
  enabled: true               # bool; default false (opt-in per agent)
  mode: pipeline              # "pipeline" (default) | "realtime"
  stt: deepgram               # optional STT provider override (else tenant default)
  tts: elevenlabs             # optional TTS provider override (else tenant default)
  voice_id: "rachel"          # optional synthesized-voice id
  language: en-US             # optional STT/TTS language hint
```

No existing `agent.yaml` field is changed or removed. Omitting the block
serializes identically to today (the field is `None`). This is the only
schema change in the ADR and it is flagged here per CLAUDE.md rule 5.

---

## API surface (additive; ADR 033 hardening applies)

| Method | Path | Scope | Purpose |
|---|---|---|---|
| WS | `/api/v1/agents/{name}/voice` | `run` | Full-duplex voice for **any** agent (D4). Audio in → STT → unchanged agent → TTS → audio out. Pipeline mode; realtime when the agent's `voice.mode` is `realtime` (D2). Counts against the ADR 045 D9 per-tenant stream cap. |

No existing endpoint changes. The voice keys are managed through the
**existing** ADR 018 `provider-keys` endpoints + `mdk keys` CLI (a Deepgram or
ElevenLabs key is just another provider key) — no new key API.

---

## CLI

```
mdk keys set deepgram          # voice STT key — the EXISTING ADR 018 keys CLI, no new verb
mdk keys set elevenlabs        # voice TTS key — same
mdk voice test <agent>         # opt-in (mdk[voice]): a local mic→STT→agent→TTS→speaker smoke loop
```

`mdk keys *` is unchanged (voice keys are ordinary provider keys). `mdk voice
test` is a new, **opt-in** verb that only exists when `mdk[voice]` is installed;
it exercises the pipeline end-to-end against a local agent (`cli ⊥ runtime`
preserved — it talks to a runtime). No existing CLI shape changes (CLAUDE.md
rule 5).

---

## Resolved decisions (locked in upfront)

- **R1 — Pipeline-first; voice over UNCHANGED existing agents is the headline.**
  The default and the bulk of the value is Pipeline mode (D2a): STT → the
  existing text agent → TTS, with **zero changes** to the agent. Realtime
  (D2b) is the opt-in premium path, not the default. This is Deva's explicit
  requirement, locked. (D1, D2, D5.)
- **R2 — Seams, not a rewrite.** Three new Protocols (D3) in the exact shape of
  `BaseLLMProvider`; `core` and the Executor are **untouched**. A change that
  required editing the Executor to know about voice is out of bounds for this
  ADR. (D1, D3.)
- **R3 — BYOK for voice; audio stays in-tenant.** Voice keys live in the ADR 018
  tenant key store, resolved through the existing `ProviderKeyResolver`; audio
  routes only to the customer's chosen providers with the customer's keys, and
  **never leaves the tenant** except to those providers. No Movate audio relay.
  (D6.)
- **R4 — Stream every stage for latency; realtime mode for the lowest floor.**
  Partial STT → streaming agent tokens (D11) → streaming TTS, so the agent
  speaks before the answer is complete. Realtime (D2b) is the floor when the
  pipeline's composition isn't fast enough. (D7, D8.)
- **R5 — Guardrails on the text plane; barge-in via run-cancellation + VAD.**
  Guardrails run post-STT / pre-TTS so every existing policy applies unchanged.
  Barge-in is R4b cooperative run cancellation fired by a VAD signal — stop
  TTS, cancel the run, resume listening; no new stop mechanism. (D7, D8.)
- **R6 — Opt-in `mdk[voice]` extra; zero impact when unused.** Heavy audio libs
  + provider SDKs live in an opt-in extra, lazily imported, permissively
  licensed (`check_licenses.py --strict`). A runtime/CLI without it is wholly
  unaffected. (D9.)

---

## Failure modes

- **STT provider down mid-call.** The WS emits an `error` frame and offers a
  **text fallback**: the caller can type, the **same** unchanged agent runs over
  text on the **same** socket, and TTS still speaks the answer. The call
  degrades to half-duplex; it does not drop. (D8.)
- **TTS provider down mid-call.** The agent's **text** answer is returned over
  the socket (the caller reads it) and `agent.token` frames still stream. The
  caller loses audio output, not the answer. (D8.)
- **Realtime provider unavailable (mode b).** A `voice.mode: realtime` agent
  whose realtime provider is down **falls back to Pipeline mode** (STT → agent
  → TTS) for that call when STT/TTS are configured — a graceful downgrade from
  the premium path to the universal one — and notes it in an `error` frame.
- **Voice quota exhausted (`voice_seconds`).** Per ADR 036: the call is wound
  down cleanly with a spoken/emitted notice ("the voice quota for this period is
  used up") rather than cut mid-word or run unbounded. Degrade, don't fail. (D7.)
- **Latency budget exceeded (the agent is slow to first token).** The transport
  plays a short, configurable **acknowledgement/filler** (or silence) while D11
  tokens are pending, and begins TTS the instant the first token arrives — the
  caller is never left in dead air with no signal. (D8.)
- **Barge-in race (caller and agent speak at once).** VAD wins: the moment the
  caller's speech is detected, TTS stops and the run is cancelled (R4b) before
  the half-spoken answer is committed; the new utterance starts a fresh turn.
  (D7/D8.)
- **Codec mismatch (telephony μ-law vs web PCM).** Transcoded at the **edge**
  (D8); a codec the edge can't transcode is rejected at connect with a clear
  `error` frame, never passed downstream to the agent.

---

## Consequences

**Positive.**
- **Every agent becomes voice-capable with no rework** — the literal answer to
  Deva's "plug into ANY existing agent." A text agent shipped last month is
  voice-capable tonight with zero edits to its `agent.yaml`/`prompt.md` (D5/R1).
- **New voice-native agents are possible too** — the realtime path (D2b) serves
  the latency-critical, voice-first use cases without forcing every agent into
  that model.
- **No new architecture** — voice composes Sessions (memory), D11 (streaming),
  ADR 036 (cost), ADR 018 (keys), the guardrails engine (safety), and R4b
  (barge-in). Net-new is three Protocols + a WS route + an optional schema field
  + an extra. The Executor and `core` are untouched (R2).
- **Provider-portable** — a tenant swaps Whisper↔Deepgram or
  OpenAI-TTS↔ElevenLabs by changing a key + a `voice.stt`/`voice.tts` value;
  the agent never changes (D3/D5).
- **Sovereign audio** — audio routes only to the customer's keyed providers and
  never leaves the tenant otherwise (D6/R3).

**Risks / watch items.**
- **Latency budgets are the make-or-break metric.** Even with streaming every
  stage, end-to-end first-audio latency is the product. **Watch** a per-stage
  latency budget (STT first-partial, agent first-token (D11), TTS first-audio)
  in the voice-turn spans (D7) and treat a regression as a release blocker.
  Realtime (D2b) exists precisely for the cases the pipeline can't meet.
- **Voice cost is a 3× metered surface.** STT-seconds + LLM-tokens + TTS-chars
  per turn — the per-minute cost of a voice call is materially higher than a
  text run. **Watch** the aggregate `voice_seconds` spend (metered via ADR 036)
  and keep the quota defaults conservative.
- **Audio-codec matrix.** Web (PCM/Opus) and telephony (μ-law) multiply across
  providers; the edge transcode (D8) is the single chokepoint and must stay the
  *only* place codecs are handled — a codec concern leaking toward the agent is
  a boundary violation to catch in review.
- **Realtime provider lock-in (mode b).** OpenAI Realtime / Gemini Live session
  protocols are provider-specific and fast-moving; the `RealtimeVoiceProvider`
  seam (D3) contains the lock-in to a single file, but mode (b) cannot promise
  the portability mode (a) does. This is the explicit trade for the latency
  floor.
- **Barge-in correctness under load.** The VAD→cancel race (R4b) must be tested
  for the case where the cancel lands after the answer is already committed;
  the failure-modes mitigation covers it but it is the subtlest path to get
  right.

**Neutral.**
- One new package (`src/movate/voice/`) with three Protocols + first impls, one
  new WS route, one new optional `agent.yaml` field, one new `mdk[voice]` extra,
  one new opt-in `mdk voice test` CLI verb. All additive; no change to the
  Executor, `core`, existing endpoints, or existing CLI shapes.

---

## Alternatives considered

- **Voice as a new agent *type* (a `voice-agent` kind with its own
  executor/spec).** **Rejected — it fails Deva's requirement on day one.** An
  existing text agent is not a `voice-agent`, so it could not be voice-enabled
  without being rewritten as one; every existing deliverable would need a
  parallel voice rebuild. The transport-over-unchanged-Executor design (D1)
  voice-enables every existing agent for free precisely *because* voice is not
  a new type. (R1/R2.)
- **Realtime-only (speech-to-speech for everything).** **Rejected.** It (a)
  forces provider lock-in (OpenAI Realtime / Gemini Live session protocols) on
  every agent, (b) **does not reuse the text Executor**, and (c) **cannot
  voice-enable an existing text agent** — the exact thing Deva asked for.
  Realtime is kept as the opt-in premium path (D2b) for new voice-native agents,
  never the only mode. (D2/R1.)
- **Client-side STT/TTS only (do speech in the browser, send/receive text).**
  **Rejected.** Moving STT/TTS to the client forfeits **server-side
  governance**: no metering (ADR 036 can't see STT/TTS spend), no guardrails on
  the spoken plane in a controlled place, no tracing of the voice turn, and no
  BYOK routing (the customer's keys would have to ship to the browser). Voice
  must be a **server** transport for the platform's governance, metering, and
  safety to apply. (D6/D7.)
- **A bespoke voice runtime separate from the agent runtime.** **Rejected.** It
  would re-implement sessions, streaming, metering, tracing, and cancellation
  that the agent runtime already provides, and would forgo the
  "one-agent-two-transports" simplicity. Voice is a route + seams on the
  existing runtime, not a second runtime. (R2.)

---

## Boundaries (explicitly NOT in scope)

- **Building the implementation.** This is the ADR — the architectural
  decision. The three Protocols + first impls, the WS route, the schema field,
  and the extra are the *spec*; the code lands in follow-up PRs (and the
  schema field lands in its own additive PR, D5).
- **Telephony.** The Twilio/LiveKit/Daily bridge is **Phase 3** (D4); Phase 1
  is the web WebSocket only.
- **On-device / edge STT/TTS.** A future self-hosted/on-prem speech backend
  slots in behind the D3 seam, but shipping one is out of scope here.
- **Voice cloning ethics / consent policy.** Synthesizing a specific person's
  voice raises consent + likeness questions that are a **separate** policy
  decision, not this architecture ADR.
- **The realtime providers' specific session protocols.** The exact OpenAI
  Realtime / Gemini Live wire/session details are a **Phase 2** implementation
  concern behind the `RealtimeVoiceProvider` seam, not specified here.
- **Changes to the Executor, `core`, the existing `/api/v1` endpoints, or
  existing CLI shapes.** All net-new surface is additive; the Executor and
  `core` are untouched (R2), the only schema change is the optional `voice:`
  block (D5), and `cli ⊥ runtime` is preserved.

---

## Phasing

- **Phase 1 — Pipeline adapter (the bulk of the value).** The `SpeechToText` +
  `TextToSpeech` Protocols (D3) + first impls, the `WS /api/v1/agents/{name}/voice`
  transport + its message protocol (D4), streaming every stage (D7), guardrails
  on the text plane + barge-in via R4b (D7/R5), BYOK voice keys (D6), three-stage
  metering (D7), voice-turn spans (D7), and the optional `voice:` schema block
  (D5). At the end of Phase 1 **every existing agent is voice-capable** over the
  web WebSocket with zero changes. This is the headline and the majority of the
  work.
- **Phase 2 — Realtime backend.** The optional `RealtimeVoiceProvider` Protocol
  (D3) + a first impl (OpenAI Realtime / Gemini Live), selected via
  `voice.mode: realtime` (D5), for new voice-native agents that want the latency
  floor. Falls back to Pipeline when the realtime provider is down (Failure
  modes).
- **Phase 3 — Telephony bridge.** The Twilio/LiveKit/Daily bridge (D4) onto the
  same pipeline, with μ-law↔PCM transcode at the edge (D8), so an agent can
  answer a phone number.

---

## Prioritized provider integrations

D3 lists candidate STT/TTS/realtime providers without committing to an order;
this section records which ones we actually build, when, and why. The seam (D3)
makes every provider swappable, so this is a *prioritization*, not a lock-in —
it picks where the first implementation effort goes.

**Prioritization criteria (in order):**

1. **Data-sovereignty fit.** Audio is sensitive. A provider must route audio
   only to the customer's chosen endpoint with keys in the customer's Key Vault
   (D6) — which favors Azure-native or self-hostable providers that live inside
   the tenant's own cloud over ones that force audio out to a third party.
2. **Streaming latency.** Voice UX dies without low latency; a streaming-first
   provider with a low latency floor beats a batch-oriented one (D7/D8).
3. **Onboarding friction.** A provider the customer already pays for or already
   has in Azure beats a marginally better one that requires fresh procurement.
4. **Quality.** Naturalness matters most for customer-facing brand voices, and
   much less for internal task agents — so it ranks last, behind sovereignty,
   latency, and friction.

**Tiered integration plan:**

| Tier | STT | TTS | Rationale |
|---|---|---|---|
| **T1 — "wow" pair (Phase 1)** | Deepgram | Cartesia (Sonic) | Both streaming-first with the lowest latency floor; the snappy, interruptible demo pair. |
| **T1 — enterprise/sovereignty pair (Phase 1)** | Azure Speech | Azure Neural TTS | One provider covers both, lives in the customer's own Azure subscription, BYOK→Key Vault maps cleanly, strong compliance + broad language coverage; the likely default for real customer deployments. |
| **T2 — low-friction default** | OpenAI Whisper | OpenAI TTS | Most customers already hold the key (zero procurement); latency not best-in-class, so it's the on-ramp, not the demo star. |
| **T2 — premium voice** | — | ElevenLabs | Best naturalness for customer-facing brand voices; premium-priced → opt-in per agent, not a default. |
| **T3 — realtime (Phase 2)** | OpenAI Realtime + Azure OpenAI Realtime (sovereignty-preserving); Gemini Live as fast-follow | (full-duplex voice↔voice, D2b) | Lowest latency floor for new voice-native agents; Azure OpenAI Realtime keeps the sovereignty story intact. |
| **Telephony (Phase 3)** | Twilio (enterprise ubiquity) + LiveKit (open-source, self-hostable WebRTC — sovereignty-friendly) | (same, over the D4 telephony bridge) | Twilio for enterprise reach; LiveKit for a self-hostable, sovereignty-friendly transport. |

**Concrete recommendation.** Phase 1 builds exactly **two** pairs —
**Deepgram + Cartesia** (the wow demo) and **Azure Speech + Azure Neural** (the
enterprise default customers ship on). One pair to impress, one to deploy;
together they also validate the `SpeechToTextProvider`/`TextToSpeechProvider`
seams against two very different provider shapes — a streaming specialist vs. an
Azure-native suite — which is the real test that the adapter abstraction is
right. Defer OpenAI (T2 default) and ElevenLabs (T2 premium) to fast-follows.

**Caveat.** The voice-provider landscape moves quickly; treat the
*tiering/criteria* as durable and re-confirm exact latency/pricing/feature
specifics at build time.

---

## Cross-references / composition notes

### Reusing ADR 045 D10 (Sessions) as voice memory

A voice turn **is** a session turn. The session service (D10) assembles history
+ summary and calls the **unchanged stateless Executor** — D10 is explicit that
the Executor never learns sessions exist, and voice relies on exactly that: the
Executor never learns *voice* exists either. Multi-turn voice memory,
summarization, truncation, and the per-session cost rollup are inherited, not
rebuilt. **Flag:** if a future change made the Executor session-aware (or
voice-aware), the zero-change-to-existing-agents promise would break — both D10
and this ADR depend on the Executor staying stateless and modality-blind.

### Reusing ADR 045 D11 (output streaming) as the latency spine

The agent stage of the pipeline is **D11's existing token stream** — voice
consumes the same `_sse_run_stream` `token` frames D11 produces and feeds them
straight into streaming TTS, so the agent speaks before the full answer exists.
Voice does **not** add a second token-streaming engine. The `usage` WS frame
(D4) mirrors D11's trailing `usage` SSE frame. **Flag:** voice's latency story
is bounded by D11's first-token latency; a regression in D11 streaming is a
regression in voice.

### Reusing ADR 036 (metering + quotas) as the cost substrate

Voice meters three stages (STT-sec, LLM-tokens, TTS-chars) on the **existing**
metering seam and adds a `voice_seconds` quota dimension — capped + degraded per
ADR 036 like every other surface. No new metering engine; voice is three more
metered events per turn.

### Reusing ADR 018 (BYOK) as the key substrate

Voice provider keys are ordinary provider keys: stored encrypted in the ADR 018
tenant key store, resolved through the existing `ProviderKeyResolver`
(tenant-key-first, shared-key fallback), never returned, redacted in logs. No
new credential model; `mdk keys set deepgram` already works the moment the
provider is registered.

### Reusing R4b (run cancellation) as barge-in

Barge-in is **R4b cooperative run cancellation** (item 36 / #424 —
`POST /api/v1/jobs/{id}/cancel`, `JobStatus.CANCELLED`, the worker's
cancel-flag checkpoint) fired by a VAD signal: stop TTS, cancel the run, resume
listening. **Flag:** the barge-in path depends on the run being cancellable at
the moment VAD fires; the failure-modes note covers the cancel-after-commit
race, and a change to R4b's cooperative-cancel semantics would change barge-in
behavior.

### Reusing ADR 007's adapter pattern as the whole design philosophy

The three voice Protocols (D3) are the same move as `BaseLLMProvider` /
`StorageProvider` / `Tracer`: a seam `core` depends on by Protocol, with
concrete backends as swappable implementations. Voice is not a new architectural
idea — it is the existing adapter-seam pattern applied at the request edge. If a
voice change can't be expressed as a new implementation behind one of these
three Protocols, it needs its own ADR (CLAUDE.md rule 7).
