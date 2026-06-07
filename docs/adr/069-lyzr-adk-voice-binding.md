# ADR 069 — Lyzr ADK voice binding: voice-enable a Lyzr-native agent with no mdk

**Status:** Accepted (2026-06-02) — see "Consolidation update" at the end of this ADR; the `LyzrAgentTurn` adapter ships under `movate.voice.lyzr` in this repo.
**Date:** 2026-06-01
**Deciders:** Engineering + Deva (Movate)
**Context window:** deliver the concrete payoff of ADR 067 — add streaming voice
(STT → agent → TTS), with the ADR 068 fallback router, to agents built on the
**Lyzr ADK** (https://docs.lyzr.ai/lyzr-adk/overview), running with **no mdk
runtime present**. This is the first cross-framework consumer of the standalone
`movate-voice` SDK.
**Builds on / composes with (changes nothing in their wire contracts):**
ADR 067 (the standalone `movate-voice` package + the `AgentTurn` seam — this ADR
is one `AgentTurn` implementation + an extra),
ADR 068 (the resilient `Failover*` router — a Lyzr deployment wires it directly
for robust, cost-bounded, low-latency voice),
ADR 048 (the speech seams + pipeline — reused verbatim; Lyzr's agent slots into
the pipeline's agent stage),
ADR 018 (BYOK — STT/TTS keys flow per-call via `api_key=`; the Lyzr agent's own
key stays Lyzr-side).

**Defining architectural fact.** With ADR 067's `AgentTurn` seam, voicing a Lyzr
agent is **a single adapter, not an integration project.** Lyzr ADK's turn is
`agent.run(message) → response.response` — synchronous, text-in/text-out. A
~30-line `LyzrAgentTurn` that wraps that call satisfies `AgentTurn`, and the
*unchanged* pipeline + the ADR-068 router do the rest. Crucially this is the
**reverse** of mdk's existing `runtime: lyzr` provider
([providers/lyzr.py](../../src/movate/providers/lyzr.py)): that invokes a Lyzr
agent *as an LLM inside mdk*; this voices a *Lyzr-native* agent with **no mdk at
all**.

---

## Context

Deva wants the voice capability to work on the Lyzr platform, not only mdk. The
Lyzr ADK is a Python library: `Studio(api_key=...)` → `studio.create_agent(name,
provider, role, goal, instructions, memory=…, …)` → `agent.run("…")`, where the
response exposes the assistant text as `response.response`. Memory, tools
(`agent.add_tool`), RAG knowledge bases, and the RAI safety policy are all
configured on the Lyzr agent and run *inside* `agent.run`.

That maps exactly onto ADR 067's `AgentTurn`: transcript in → text out. The Lyzr
turn is synchronous and (per the public overview) non-streaming — which is fine,
because `run_voice_pipeline` already tolerates a non-streaming agent: it feeds the
whole answer to TTS as one delta, and a buffered TTS adapter synthesizes it
normally (the same path the OpenAI TTS adapter already takes). No streaming is
*required* for correctness; it only sharpens latency, and we can add it the day
Lyzr exposes a token stream.

mdk's `providers/lyzr.py` is **not** the vehicle here: it is an mdk
`BaseLLMProvider` that needs the mdk runtime, the provider registry, and an
`agent.yaml` declaring `runtime: lyzr`. The goal is voice on Lyzr *without*
shipping mdk. So this binding lives in `movate-voice`, depends only on
`movate-voice` + the Lyzr SDK, and imports nothing from mdk.

## Decision

Ship a Lyzr `AgentTurn` adapter and a convenience wiring in `movate-voice`, as an
optional extra, depending on nothing in mdk.

### D1 — `LyzrAgentTurn` implements the `AgentTurn` seam

A small adapter wraps a Lyzr ADK `Agent` and satisfies ADR 067's `AgentTurn`:

```python
class LyzrAgentTurn:                       # implements AgentTurn (ADR 067 D2)
    """Voice the text turn of a Lyzr ADK agent. No mdk import."""
    name = "lyzr-adk"; version = "1"
    def __init__(self, agent: "lyzr.Agent") -> None: self._agent = agent

    async def run(self, text, *, on_token=None, language=None, session_id=None):
        # Lyzr's run() is sync → offload to a thread so we don't block the loop.
        resp = await asyncio.to_thread(self._agent.run, text)
        answer = getattr(resp, "response", None) or str(resp)
        if on_token and answer:           # non-streaming: emit one delta so the
            on_token(answer)              # latency-badge + token events still flow
        return AgentTurnResult(answer_text=answer, run_id="", status="ok")
```

The sync `agent.run` is offloaded with `asyncio.to_thread` to keep the event loop
free for concurrent STT/TTS streaming. Errors from `agent.run` map to an
`AgentTurnResult` with `status="error"`, which the pipeline surfaces as its
existing `stage="agent"` error event (graceful degrade — no audio synthesized).

### D2 — Packaging: an optional `movate-voice[lyzr]` extra, mdk-free

The binding ships in `movate-voice` behind a `lyzr` extra that pulls the Lyzr
SDK, **imported lazily** inside `LyzrAgentTurn` (the ADR 048 D9 posture — a base
`movate-voice` install never imports Lyzr). It depends on `movate-voice` + `lyzr`
and **nothing from mdk**. This ADR explicitly distinguishes it from mdk's
[providers/lyzr.py](../../src/movate/providers/lyzr.py): that is an mdk LLM
provider (Lyzr-agent-as-model, *requires* mdk); this is a voice binding
(voice-around-a-Lyzr-agent, *forbids* mdk).

### D3 — A one-call convenience wiring

For the common case, a thin helper assembles the resilient pipeline so a Lyzr
developer gets robust voice in a few lines:

```python
events = voice_agent(                     # convenience over run_voice_pipeline
    lyzr_agent,
    audio_in=mic_stream,
    stt=FailoverSTT.default(),            # ADR 068 latency-first, cost-bounded
    tts=FailoverTTS.default(),            #   T1 Deepgram/Cartesia → T2 OpenAI …
)
async for ev in events: ...               # transcript / agent.token / tts.audio
```

`voice_agent` is sugar over `run_voice_pipeline(stt=…, tts=…,
agent=LyzrAgentTurn(lyzr_agent), audio_in=…)` with the ADR-068 `Failover*`
defaults — so a Lyzr deployment gets fallback, circuit breaking, the cost budget,
and the phrase cache for free, with no mdk.

### D4 — Transport and BYOK are the embedder's, via existing seams

`movate-voice` ships the **transport-agnostic** pipeline (it emits `VoiceEvent`s;
ADR 048 D4); the Lyzr deployment brings its own transport (a WebSocket, WebRTC,
or telephony bridge) or uses a minimal reference adapter. STT/TTS keys flow
per-call through the existing `api_key=` seam (ADR 018) — resolved by the
embedder, never read from a global env when supplied. The Lyzr agent's own API
key is configured on the Lyzr `Studio`/`Agent` and stays entirely Lyzr-side; the
voice layer never sees it.

### D5 — Lyzr-native concepts stay Lyzr-side

Sessions/memory (`memory=N`), tools (`agent.add_tool`), RAG knowledge bases, and
the RAI safety policy all live on the Lyzr agent and execute inside `agent.run`.
The voice layer is **stateless per turn**: it transcribes, calls `agent.run`,
synthesizes. Multi-turn continuity is Lyzr's memory; `session_id` is passed
through `AgentTurn` for adapters that want it, but `LyzrAgentTurn` defers to
Lyzr's own session handling.

## Consequences

**Positive**
- **Voice on Lyzr is a single adapter** — ~30 lines + an extra, because ADR 067
  did the decoupling and ADR 068 did the resilience.
- **Robust + cost-bounded out of the box** — `voice_agent`'s defaults give a
  Lyzr agent the full ADR-068 fallback router with no extra wiring.
- **Zero mdk footprint** — a Lyzr customer never installs or runs mdk; the
  binding imports only `movate-voice` + `lyzr`.
- **The pattern generalizes** — the same shape voices LangGraph, a bare
  function, or a remote HTTP agent; Lyzr is just the first.

**Negative / risks**
- **Non-streaming agent → coarser latency.** `agent.run` returns the whole
  answer, so the first audio waits for the full text. *Mitigation:* the buffered
  pipeline path already handles this; add token streaming the moment Lyzr exposes
  it (the seam already carries `on_token`).
- **Coupling to Lyzr's `run()` shape.** A Lyzr SDK change to `run` / `response`
  would break the adapter. *Mitigation:* the lazy import + a single, well-tested
  adapter contains the blast radius; `getattr(resp, "response", …)` tolerates
  minor shape drift.
- **`asyncio.to_thread` per turn** — a thread-pool hop. *Mitigation:* negligible
  vs. network STT/TTS latency; keeps the loop free for streaming.

**Neutral**
- All surface is **additive**, in `movate-voice`; nothing in mdk changes
  (including `providers/lyzr.py`, which remains the unrelated LLM bridge).

## New surfaces (flagged per CLAUDE.md rule 5)

All **ADDITIVE**, all in `movate-voice`; none changes an existing
`agent.yaml`/`project.yaml` field, the `/api/v1` API, a storage schema, a
`MOVATE_*`/`MDK_*` env var, an existing `--json` shape, or deploy behavior:
- **`LyzrAgentTurn`** — a new `AgentTurn` implementation.
- **The `movate-voice[lyzr]` extra** — pulls the lazily-imported Lyzr SDK.
- **`voice_agent(...)`** — a convenience wiring over `run_voice_pipeline` with the
  ADR-068 `Failover*` defaults.

## Alternatives considered

- **(a) Build voice into Lyzr directly (a Lyzr-side feature).** Rejected — not
  portable, not Movate's to ship, and re-solves what ADR 067/068 already give for
  free across frameworks.
- **(b) Reuse mdk's `runtime: lyzr` provider.** Rejected — wrong direction (it
  runs a Lyzr agent *inside mdk* and requires the mdk runtime); the goal is voice
  on Lyzr with **no mdk**.
- **(c) Call Lyzr's HTTP inference endpoint directly from the binding.** Deferred
  — the ADK SDK is the documented Lyzr-native path and keeps tools/memory/RAI in
  Lyzr's hands; a thin HTTP `AgentTurn` is a trivial future variant if a
  deployment prefers no SDK dependency.
- **(d) Wait for Lyzr streaming before shipping.** Rejected — the buffered path
  is correct today; streaming is a latency optimization the `on_token` seam
  already anticipates. *(Resolved in implementation: Lyzr exposes an
  OpenAI-compatible `POST /v4/chat/completions` endpoint with SSE token
  streaming. The shipped Lyzr tier points `OpenAIChatAgent` at
  `https://agent-prod.studio.lyzr.ai/v4` with `send_system=False`, so a Lyzr
  agent now gets token-by-token streaming and sentence-by-sentence TTS through
  the **same** code path the OpenAI Chat tier uses — no Lyzr-specific
  streaming adapter needed. `LyzrAgentTurn` (the SDK-wrapper variant) remains
  the right binding when the deployment wants Lyzr-native memory/tools/RAI
  inside `agent.run`.)*
- **(e) Embed inside Lyzr's hosted voice runtime (LiveKit, via
  `POST /v1/sessions/start`).** Rejected — researched 2026-06-02. The endpoint
  returns `{userToken, livekitUrl, roomName, agentDispatched: true}`: Lyzr
  provisions a LiveKit room and dispatches its own worker, which runs the
  configured engine (STT → LLM → TTS) **server-side** inside that room. mdk-voice
  in that mode is reduced to a media pipe — `STT`/`TTS`/`AgentTurn` are
  bypassed, and with them every ADR-068 differentiator (failover composites,
  per-utterance circuit breaking, cost-bounded routing, the TTS phrase cache).
  Adopting it would turn the deliverable into a thin reseller of Lyzr voice
  rather than a portable, robust, framework-neutral voice layer (CLAUDE.md
  rule 1). The status-quo composition — Lyzr at the LLM stage via `/v4`, mdk-voice
  owning the audio plane — preserves every voice-layer feature and keeps Lyzr's
  brain. A narrow opt-in `LyzrLiveKitSession` *transport* adapter is acceptable
  future work for deployments that explicitly want Lyzr's PSTN/SIP plane
  (`/v1/telephony/*`) and hosted ops and accept the loss of failover; it would
  live alongside, not replace, the native pipeline.

## Boundaries (explicitly NOT in scope)

- **The standalone package + `AgentTurn` seam** (ADR 067) and **the resilient
  router** (ADR 068) — this ADR consumes both.
- **A transport implementation** — the deployment brings its own WS/WebRTC/
  telephony; `movate-voice` provides only the transport-agnostic pipeline + an
  optional minimal reference.
- **Any change to mdk's `providers/lyzr.py`** — it stays the unrelated LLM
  migration bridge.
- **Native Lyzr token streaming** — deferred until Lyzr exposes it; the seam is
  ready.

## Cross-references / composition notes

- **ADR 067 (`AgentTurn` seam).** `LyzrAgentTurn` is one implementation of it;
  the pipeline cannot tell it from `ExecutorAgentTurn`.
- **ADR 068 (resilient router).** `voice_agent`'s defaults wire the `Failover*`
  composites so a Lyzr deployment gets robust, latency-first/cost-bounded voice.
- **ADR 048 (seams + pipeline).** Reused verbatim; the Lyzr agent occupies the
  pipeline's agent stage; non-streaming is handled by the buffered TTS path.
- **ADR 018 (BYOK).** STT/TTS keys flow per-call via `api_key=`; the Lyzr agent's
  key stays Lyzr-side.
- **`providers/lyzr.py` (mdk).** The deliberate inverse of this binding —
  Lyzr-as-mdk-LLM vs. voice-around-Lyzr; the contrast is the point.

---

## Consolidation update (2026-06-02)

`LyzrAgentTurn` ships under [`src/movate/voice/lyzr.py`](../../src/movate/voice/lyzr.py)
inside this repo (`movate-cli`), reachable via the `mdk[voice]` extra. The
"no-mdk-needed" invariant from D2 still holds at the **import** level — nothing
in `movate.voice.lyzr` reaches into `movate.core` / `movate.runtime` — so the
adapter remains usable in a Lyzr-only deployment if/when a consumer needs the
voice tree extracted again (the seam is intact; only the distribution is shared).

The convenience entry point (D3 `voice_agent(...)`) and the optional Lyzr SDK
toggle (the "v4 streaming vs. SDK buffered" dichotomy the demo exposes) are
implemented as designed. The deliberate inverse boundary with
[`src/movate/providers/lyzr.py`](../../src/movate/providers/lyzr.py)
(Lyzr-as-mdk-LLM, the existing http provider) is unchanged: that one voices
nothing — it lets an mdk agent call a Lyzr agent over HTTP. This ADR is still
the reverse.
