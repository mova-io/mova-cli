# Demo script — driving the mdk-voice walkthrough for Deva

A scene-by-scene script for the live demo. ~10–12 min if you hit every beat;
~5 min if you cut to the highlights. Every URL and toggle named below is
already wired and working as of the last deploy.

The URL: **https://mdk-voice-demo.delightfulcoast-91af3b05.eastus.azurecontainerapps.io/**
(or `https://voice.movate.io/` if you've bound the custom domain.)

---

## Scene 1 — "Voice for any framework, not a framework" (1 min)

**Open the URL.** While the page loads, set up the framing:

> "What you're about to see runs on **mdk-voice** — a standalone voice SDK
> that adds streaming voice to *any* text agent. The agent in the demo is
> currently OpenAI's GPT-4o-mini, but you can flip it to Lyzr ADK or full
> OpenAI Realtime live. The architectural point: voice isn't a kind of agent;
> it's a transport plus four small Protocols (STT, agent, TTS, optionally
> full-duplex) that wrap an existing text turn."

**Point at the header badge:** "Lyzr parity 10/19 (53%)".

> "We hit your `/v1/config/pipeline-options` endpoint on load. This badge is
> live proof that we adapt the same providers Lyzr supports — every LLM
> through your `/v4/chat/completions`, the top STT (Deepgram), the top two
> TTS (Cartesia + ElevenLabs), plus OpenAI Realtime. **Click the badge.**"

The expanded panel shows the covered + gap lists. Deva can read the gap list
himself — it's honest, not marketing.

---

## Scene 2 — "Voice working" (1 min)

**Click ▶ Start demo** (unlocks autoplay), then **click the big Talk button.**

Say something natural — *"What's today's date?"* or *"Tell me a joke."*

You'll see, in order:

- 🟢 `heard: "..."` (Deepgram streamed the partial then the final)
- ✓ `stt served by deepgram` in the trail (the **A1 failover-trail UI**)
- The agent answer streams into the right panel **token by token**
- TTS audio starts playing within ~1 sec (Cartesia, sentence-streaming)
- `done · turn 1 · openai/cartesia · ⚡ 2200ms · $0.00035`

**Point at the cost.** "Less than four ten-thousandths of a dollar per turn."

---

## Scene 3 — "Resilience: failover composites that single-pick forms can't express" (2 min)

This is the differentiator vs. a Studio dropdown. Set it up:

> "Lyzr's voice-options endpoint lets you pick one STT, one LLM, one TTS per
> agent. We compose them. When the primary fails, the router falls over
> mid-utterance to the next provider — and you'll see it happen."

**Click ⚡ break STT** (the orange button in the controls bar).

Say something. You'll see:

- 🔴 `→ stt failover from deepgram_with_fault`
- 🟢 `✓ stt served by openai_whisper`

> "Same answer, no caller-visible glitch, ~1 second slower. That's
> `FailoverSTT`, an ADR-068 composite that *implements the same Protocol* as
> a single provider. The pipeline can't tell it's talking to a chain."

**Click ⚡ break TTS** and repeat.

- 🔴 `→ tts failover from cartesia_with_fault`
- 🟢 `✓ tts served by openai`

> "Same architectural pattern. Two providers in the TTS chain, Cartesia is
> primary because it's the lowest latency, OpenAI is the fallback. Circuit
> breaker, retry budget, latency hedging — all configurable behind the same
> Protocol."

If you ask the same question twice quickly:

- ⚡ `cache hit · tts (0ms · $0)` — the phrase cache served it instantly.

---

## Scene 4 — "Pluralism: any framework" (2 min)

**Click "Lyzr ADK"** in the agent toggle.

> "Now the agent stage is a Lyzr-hosted agent. The exact same UI, same
> failover composites around it. Behind the scenes we point our
> `OpenAIChatAgent` at Lyzr's `/v4/chat/completions` — your endpoint speaks
> OpenAI-compatible SSE, so we get sentence-by-sentence TTS on Lyzr just
> like we do on OpenAI. No Lyzr-specific streaming adapter needed."

Talk to it. Show that streaming works.

> "And it's not just Lyzr. We shipped a LangGraph adapter today — same
> `AgentTurn` Protocol, ~50 lines. The pluralism story is real: name a
> framework, we have a one-file adapter, you get streaming voice + failover
> + caching on it for free."

(If you have time, mention the `LyzrAgentTurn` and `LangGraphAgentTurn` files
side by side — both ~135 lines, both duck-typed so they never import the
framework's SDK.)

---

## Scene 5 — "Sub-second turns: realtime mode" (1 min)

**Click "Realtime"** in the agent toggle.

> "When the use case can tolerate provider lock-in, we drop into OpenAI's
> Realtime API. Full-duplex voice-to-voice, no intermediate text. Watch the
> latency."

Talk to it.

> "Roughly 400–800 ms turn time, vs. 1.5–2 sec on the pipeline. The trade:
> you give up the failover composites (Realtime is one provider) and pay
> ~10x in dollars per minute. We give you both modes, pick per use case."

**Click back to "OpenAI Chat"** — the UI reconnects to the pipeline path.

---

## Scene 6 — "Telephony: same pipeline, different transport" (1 min)

> "The pipeline is transport-agnostic. The browser tab is one transport. The
> phone path is another. Watch."

**Dial +1-217-919-5393** from your phone. (Or use the Azure-deployed Twilio
voice URL pointing at this same app — no ngrok.)

Once it answers, repeat one of your earlier prompts. Show:

- The phone call uses the exact same `FailoverSTT` / agent / `FailoverTTS`
  chain.
- μ-law 8 kHz ↔ PCM16 16 kHz conversion at the edge.
- Same observer events fire — the trail panel in the browser tab even shows
  the phone call's failover trail if both are connected to the same demo.

**Hang up.**

> "Twilio is shipped. Genesys and AWS Connect are the same shape — a
> transport bridge that decodes the carrier's audio format and feeds the
> identical pipeline. ~200 lines per bridge."

---

## Scene 7 — "Operational story" (1 min)

**Open a second tab to `/health`** in JSON viewer.

Point at:

- `active_sessions` — how many connections right now
- `adapters.*` — per-provider readiness map (key present + SDK importable)
- `endpoints` — service catalog
- `version` + `uptime_s` + `started_at` — pod boot info

> "Standard ops fields, no setup needed. We have GitHub Actions deploying
> every push to main, OIDC-authenticated, smoke-tested. The Container App is
> at min-1 replica — `$5/mo` warm — flips to scale-to-zero with one
> command. Per-call recording goes to Azure Blob; transcripts export to MD
> or JSON from any browser tab via the header buttons. The repo's
> `docs/architecture.md` is a 10-min read for any contributor."

---

## Scene 8 — "What's next" (1 min)

Loop back to the partnership question:

> "Three concrete asks for the partnership:
>
> 1. **Provider menu sync** — when you add a provider (you added AssemblyAI
>    STT last quarter), we get a CI failure within 24 hours and ship an
>    adapter. That's the parity badge automating the gap loop.
>
> 2. **`/v4` deepening** — your OpenAI-compatible endpoint already powers
>    every LLM Lyzr exposes through our pipeline. If you add streaming
>    tool-calling there, we surface it.
>
> 3. **Failover-aware billing** — when our router demotes from
>    Cartesia → OpenAI under outage, we'd love to surface that in your
>    Studio's cost dashboard. ADR-068's `VoiceObserver` already emits the
>    events; we just need a webhook target."

End there. Don't over-pitch — the demo did the work.

---

## Cheat sheet — buttons + their effects

| UI element | What it does | When to use it |
|---|---|---|
| **▶ Start demo** (gate) | Unlocks mic + autoplay | First click on load |
| **Talk** | Open mic; auto-endpoints on silence | Every turn |
| **OpenAI Chat / Lyzr ADK / Realtime** | Switch agent backend; Realtime reconnects transport | Scene 4 + 5 |
| **Cartesia / OpenAI** (TTS) | Swap TTS primary; greyed in Realtime | Showing A/B speed |
| **⚡ break STT** / **⚡ break TTS** | Arm a one-shot fault for the next turn | Scene 3 (failover demo) |
| **Lyzr parity badge** | Toggle the parity detail panel | Scene 1 |
| **📥 Recording** | Per-call WAV download (requires `RECORD_CALLS=1` server) | If recording enabled |
| **📄 MD / { } JSON** | Export the conversation log | End of demo |
| **↻ Reset memory / metrics** | Clear agent context / session counters | Between dry runs |
| **VAD: auto / push-to-talk** | Endpoint-on-silence vs hold-to-talk | Noisy room → push-to-talk |
| **⏹ Interrupt** | Manual barge-in mid-answer | Show barge-in (or speak louder) |

## What to NOT do during the demo

- Don't open dev tools — distracting, breaks the magic.
- Don't switch from Realtime → OpenAI Chat mid-question (UI reconnects).
- Don't talk over the agent in Realtime mode for the first turn — the
  model's VAD calibration burns ~1 sec on initial barge-in detection.
- Don't ask the agent open-ended philosophical questions — it's prompted
  for "one or two short sentences" and will sound terse.

## Backup plan if the demo URL is down

`./scripts/deploy_azure.sh` rebuilds + rolls a fresh revision in ~90 sec.
If even that fails: `python examples/web_demo/server.py` runs the same demo
locally on `http://localhost:8765` (you'll lose the phone path, but the
browser demo is identical).
