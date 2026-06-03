# mdk-voice — architecture

A 10-minute tour for new contributors (or a partner team kicking the tires).
Read this once and the rest of the codebase reads itself.

> **Defining fact.** Voice here is **not a new kind of agent.** It is a
> transport + adapter seams that wrap *any* text agent:
> `audio → STT → your agent → TTS → audio`. The agent stage is the small
> `AgentTurn` seam, so the same pipeline voices an mdk agent, a Lyzr ADK
> agent, a LangGraph graph, or a bare async function — with **zero
> dependency on mdk**.

## The layer map

```
┌─────────────────────────────────────────────────────────────────────┐
│  transport (your WS / WebRTC / telephony bridge)                    │
│  emits AudioChunk → consumes VoiceEvent                             │
├─────────────────────────────────────────────────────────────────────┤
│  resilient router (optional, decorator pattern)                     │
│    FailoverSTT / FailoverTTS / FailoverRealtime                     │
│    + CircuitBreaker + InMemoryVoiceCache + VoiceObserver hook       │
├─────────────────────────────────────────────────────────────────────┤
│  pipeline driver — run_voice_pipeline(stt, agent, tts, audio_in)    │
│  → emits VoiceEvent stream (transcript.* / agent.token / tts.audio  │
│    / error / done)                                                  │
├─────────────────────────────────────────────────────────────────────┤
│  THE THREE SEAMS                                                    │
│    SpeechToTextProvider │   AgentTurn   │ TextToSpeechProvider     │
│    (Protocol)           │  (Protocol)   │ (Protocol)               │
│  + the optional full-duplex RealtimeVoiceProvider                   │
├─────────────────────────────────────────────────────────────────────┤
│  adapters (one file per backend, SDK lazy-imported)                 │
│   STT: deepgram / cartesia_stt / openai_whisper / azure_speech_stt  │
│   TTS: cartesia / deepgram_aura / openai_tts /                      │
│        elevenlabs / azure_neural_tts                                │
│   Realtime: openai_realtime / azure_openai_realtime                 │
│   AgentTurn: lyzr / langgraph_adapter / (your text turn)            │
└─────────────────────────────────────────────────────────────────────┘
```

Every horizontal rule is a Protocol. Cross the rule by **implementing the
Protocol**, never by subclassing. That is the whole architectural posture.

## Module map

| Module | What it owns |
|---|---|
| `base.py` | The three Protocols (`SpeechToTextProvider`, `TextToSpeechProvider`, `RealtimeVoiceProvider`) and the chunk types (`AudioChunk`, `TranscriptChunk`, `RealtimeChunk`). |
| `agent_turn.py` | The fourth seam: the `AgentTurn` Protocol + `AgentTurnResult` / `AgentTurnError`. **This is the abstraction that lets *any* framework slot into the agent stage** (ADR 067). |
| `pipeline.py` | The driver: `run_voice_pipeline(...)` + `VoiceEvent` envelope + latency badge helpers. Transport-agnostic — it emits typed events, not WS frames. |
| `failover.py` | The router composites: `FailoverSTT`, `FailoverTTS`, `FailoverRealtime`. Implement the SAME Protocol as a single provider, so the pipeline doesn't know it's talking to a chain (ADR 068). |
| `breaker.py` | `CircuitBreaker` (closed → open → half-open → closed). Used by the failover composites to skip dead providers. |
| `cache.py` | `InMemoryVoiceCache` + `VoiceCache` Protocol + `warm_cache(...)`. Re-used phrases serve at $0, 0ms. |
| `observer.py` | `VoiceObserver` Protocol + `MetricsObserver` / `StderrObserver` / `NullObserver`. The hook the router emits structured events through. |
| `failures.py` | `VoiceFailureType` + `classify(exc)` + `DEFAULT_RETRY`. Tiny in-package failure taxonomy — mirrors mdk's shape but does **not** import from it (keeps standalone). |
| `manifest.py` | `VoiceManifest` per provider (latency tier, $/min, $/char, sovereignty). Drives the router's latency-first, cost-bounded ordering. |
| `lyzr_parity.py` | Live discovery-endpoint parity check vs Lyzr's voice menu. Strategic: every provider Lyzr lists → mdk-voice adapter (or covered via `/v4` OpenAI-compat). |
| `chunking.py` | `SentenceChunker`. Splits an agent token stream into sentences so streaming TTS can start synthesizing before the agent finishes. |
| `telephony.py` | μ-law ↔ PCM16 codec + anti-aliased resampling + 20 ms frame rechunker (`telephony_inbound` / `telephony_outbound`). |
| `vad.py` | `frame_rms(...)` / `is_silent(...)` — energy-based VAD primitives. |
| `pii.py` | `redact_pii(text)` — emitted-transcript redaction; the agent always sees raw. |
| `speakify.py` | Strip markdown / format prices and dates as a TTS engine will pronounce them. |
| `doubles.py` | `FakeSTT` / `FakeTTS` / `FakeAgentTurn` / `FakeRealtime`. Use in tests; no SDK / network / key. |
| `bench.py` | `bench_stt(...)` + WER scoring (ADR 049 D5). |

## The seams

### `SpeechToTextProvider`

```python
class SpeechToTextProvider(Protocol):
    name: str
    version: str
    def transcribe(
        self,
        audio: AsyncIterator[AudioChunk],
        *, language: str | None = None,
        api_key: str | None = None,
    ) -> AsyncIterator[TranscriptChunk]: ...
```

Yield `TranscriptChunk(is_final=False)` for partial hypotheses and
`TranscriptChunk(is_final=True)` for the endpointed final. The pipeline runs
the agent when the first `is_final=True` arrives.

**Reference impls:** `DeepgramSTT` (T1 streaming), `CartesiaSTT` (T1
streaming, Ink Whisper), `OpenAIWhisperSTT` (T2 buffered), `AzureSpeechSTT`
(T1 streaming + sovereign).

### `TextToSpeechProvider`

```python
class TextToSpeechProvider(Protocol):
    name: str
    version: str
    def synthesize(
        self,
        text: AsyncIterator[str],
        *, voice_id: str = "",
        codec: AudioCodec = "pcm16",
        api_key: str | None = None,
    ) -> AsyncIterator[AudioChunk]: ...
```

The text stream is the agent's `on_token` deltas (sentence-chunked
upstream by `SentenceChunker`). Yield audio frames as they're produced —
playback begins before synthesis finishes (the latency story).

**Reference impls:** `CartesiaTTS` (T1 streaming, Sonic), `ElevenLabsTTS` (T2
streaming), `DeepgramAuraTTS` (T1 streaming, Aura 2), `OpenAITTS` (T2
buffered), `AzureNeuralTTS` (T1 streaming + sovereign).

### `AgentTurn` *— the framework-neutral seam*

```python
class AgentTurn(Protocol):
    name: str
    version: str
    async def run(
        self,
        text: str,
        *, on_token: Callable[[str], None] | None = None,
        language: str | None = None,
        session_id: str | None = None,
    ) -> AgentTurnResult: ...
```

Transcript in → text out. The pipeline awaits this; it does not import,
subclass, or know about any specific framework. **This is what makes
mdk-voice framework-neutral.**

**Reference impls:**

* `LyzrAgentTurn` (`mdk_voice.lyzr`) — wraps a Lyzr ADK `Agent.run(text)`.
  Duck-typed: never imports the `lyzr` SDK.
* `LangGraphAgentTurn` (`mdk_voice.langgraph_adapter`) — wraps a compiled
  LangGraph `.ainvoke(state)`. Duck-typed: never imports `langgraph`.
* Your own — implement `name`, `version`, and `async def run(text, ...)`.
  That is the whole contract.

### `RealtimeVoiceProvider` *— the optional, full-duplex Phase 2 seam*

```python
class RealtimeVoiceProvider(Protocol):
    name: str
    version: str
    def session(
        self,
        audio_in: AsyncIterator[AudioChunk],
        *, voice_id: str = "",
        instructions: str = "",
        language: str | None = None,
        codec: AudioCodec = "pcm16",
        api_key: str | None = None,
    ) -> AsyncIterator[RealtimeChunk]: ...
```

Voice in, voice out, no intermediate text agent. Lowest latency floor in
exchange for losing the failover-composite + bring-your-own-framework story.
Pick the right tool per use-case; both seams stay in the codebase.

**Reference impls:** `OpenAIRealtime`, `AzureOpenAIRealtime`.

## The resilient router (composite pattern)

The defining trick: a router *is* a provider. `FailoverSTT` implements
`SpeechToTextProvider`. The pipeline can't tell it from a single backend.

```python
stt = FailoverSTT(
    providers=[DeepgramSTT(), OpenAIWhisperSTT()],
    observer=MetricsObserver(),
    call_timeout=15.0,
    connect_timeout=8.0,
)
# stt now behaves exactly like SpeechToTextProvider, but...
#  - skips providers whose breaker is open
#  - times out individual providers
#  - falls over to the next on error or timeout
#  - emits structured events through the observer
```

Same shape for `FailoverTTS` (with phrase-cache short-circuit on `cache_hit`)
and `FailoverRealtime`.

**This is why the package can give Deva "robust fallbacks" without changing
the pipeline driver.** The composite is **above** the seam; never inside.

### Observer events

A non-exhaustive list of what `VoiceObserver.on_event(name, **fields)` sees,
useful when wiring a dashboard:

| Event | Fields | Meaning |
|---|---|---|
| `provider_selected` | `provider, kind` | The provider that actually served the call (after any failover). |
| `failover` | `from, kind` | One provider failed; next in chain is being tried. |
| `circuit_open` | `provider` | Breaker tripped — skipping until cooldown. |
| `circuit_close` | `provider` | Breaker reset after a successful half-open call. |
| `exhausted` | `kind` | All providers failed. The pipeline emits a stage error. |
| `cache_hit` | `kind` | The phrase cache short-circuited synthesis. |
| `hedge` / `hedge_won` | `providers, kind, provider` | Latency-hedge fired N providers in parallel; first response wins. |
| `audio_truncated` | `kind, bytes` | Audio buffer was capped to prevent unbounded memory. |

The web demo's `_TrailObserver` renders these into the live event-stream
panel so the audience sees the resilience working.

## How the pipeline runs one turn

`run_voice_pipeline(audio_in=..., stt=..., agent=..., tts=...)` returns an
async iterator of `VoiceEvent`. The shape:

```
audio_in ─┬─ STT.transcribe → transcript.partial events (yielded immediately)
          └─ STT.transcribe → transcript.final (one)
                                  └─ agent.run(text, on_token=…) → agent.token events
                                                                       └─ TTS.synthesize → tts.audio events
                                                                                              └─ done event
```

Two key concurrency tricks live inside the driver, not in the adapters:

1. **Sentence chunking.** The agent's `on_token` deltas are fed to
   `SentenceChunker`, which buffers until a sentence boundary then flushes
   that sentence's text into the TTS stream. So TTS starts synthesizing
   sentence 1 while the agent is still emitting sentence 2.
2. **Cancellation / barge-in.** A `cancel: asyncio.Event` short-circuits
   the agent + TTS tasks; the transport sets it when the caller starts
   talking again.

## How extensions land

| You want to … | Do this |
|---|---|
| Voice your existing framework | Implement `AgentTurn`. Mirror `LyzrAgentTurn` or `LangGraphAgentTurn` — both are ~50 lines and depend on nothing in your framework's SDK. |
| Add a new STT/TTS provider | Implement `SpeechToTextProvider` / `TextToSpeechProvider`. One new file. Lazy-import the SDK inside the method. Add a `VoiceManifest` entry. |
| Send router events somewhere else | Implement `VoiceObserver` (one method: `on_event(name, **fields)`). Pass to `Failover*` constructors. |
| Add a new realtime backend | Implement `RealtimeVoiceProvider`. One new file. |
| Track your own per-provider cost | Set `VoiceManifest(cost_per_min=...)` / `cost_per_char=...` per adapter (or override per call via the observer hook). |

The standard for *all* of these: one file, one class, one Protocol. No
inheritance, no framework lock-in.

## Lyzr-parity story

`mdk_voice.lyzr_parity.check_lyzr_parity(api_key=...)` hits Lyzr's two
voice-discovery endpoints (`/v1/config/{pipeline,realtime}-options`) and maps
each provider against `LYZR_PROVIDER_MAP`. The web demo surfaces the result
as a header badge — literal proof that every provider Lyzr lists is either an
mdk-voice adapter (STT/TTS/realtime) or reachable via `/v4/chat/completions`
through `OpenAIChatAgent` (every LLM).

What this enables strategically: Movate can claim *provider-parity-plus*
honestly — same menu, plus the failover composites a single-pick form can't
express. As the menu grows, the parity report grows; CI fails the moment
we drift.

## Test strategy

Three layers, all green at every commit (`pytest -q`):

1. **Protocol conformance** — `isinstance(MyAdapter(), SpeechToTextProvider)`
   for every adapter. Catches anyone breaking the seam.
2. **Adapter unit tests** — inject `client=` (or `connect=`, `to_thread=`)
   to avoid hitting the SDK or network. Each adapter has a fake-client test
   covering happy path + the protocol-specific edge cases its docstring
   calls out.
3. **Pipeline integration tests** — use `FakeSTT` / `FakeTTS` /
   `FakeAgentTurn` from `doubles.py` to assert end-to-end semantics (event
   ordering, cancellation, barge-in, observer event emission).

No test touches the network. Tests run in under a second.

## Cross-references — the ADRs

The architectural intent (and the things this codebase deliberately is NOT)
lives in three ADRs in the parent movate-cli repo:

* **[ADR 067 — Standalone voice SDK + AgentTurn seam](https://github.com/mova-io/mova-cli/tree/main/docs/adr/067-standalone-voice-sdk.md)** —
  why we have an `AgentTurn` Protocol and what it costs to add a new one.
* **[ADR 068 — Resilient voice router (standalone)](https://github.com/mova-io/mova-cli/tree/main/docs/adr/068-resilient-voice-router-standalone.md)** —
  why the router is a composite that *is* a provider, and why the cost-bounded
  latency-first ordering is the default policy.
* **[ADR 069 — Lyzr ADK voice binding](https://github.com/mova-io/mova-cli/tree/main/docs/adr/069-lyzr-adk-voice-binding.md)** —
  the first cross-framework consumer of the standalone SDK, plus the
  documented decision *against* embedding inside Lyzr's hosted LiveKit
  runtime (it would bypass everything ADR 068 buys us).

When in doubt about why a boundary exists, the ADR is the source of truth.
