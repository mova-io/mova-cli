# ADR 076 — Realtime function-calling: tool use on the voice-native (speech↔speech) seam

**Status:** Proposed
**Date:** 2026-06-05
**Deciders:** Engineering + Deva (Movate)
**Builds on / composes with (changes nothing in their wire contracts):**
ADR 048 (voice agents — the three speech seams, the WS transport, the pipeline,
the `mdk[voice]` extra; **D2b** defines the realtime / voice-native seam this ADR
extends),
ADR 050 (voice API surface — **D12** the `?mode=realtime` query on
`WS /api/v1/agents/{name}/voice`; this ADR adds events on that same wire, no new
endpoint),
ADR 002 (the Executor-owned tool-use loop — the **pipeline** path already calls
skills; this ADR gives the realtime path an equivalent tool round-trip without an
Executor),
ADR 067 (consolidated `movate.voice` + the framework-neutral seams — the
realtime provider stays a single self-contained adapter; tool dispatch is injected,
not imported),
ADR 018 (per-tenant BYOK — the realtime-provider key resolution is unchanged; the
tool dispatcher carries no new credential),
ADR 071 (per-agent voice tuning — the agent's voice block already seeds the
realtime session; this ADR adds the agent's **skills** to that same seed).

**Defining architectural fact.** The realtime (speech↔speech) seam was designed
in ADR 048 D2b as **voice-native with no intermediate text Executor**: audio in,
audio + control events out, one bidirectional `session()` async generator
([`base.py:283`](../../src/movate/voice/base.py)). That design deliberately
traded the pipeline's portability for the lowest latency floor — but it also
traded away the Executor's **tool-use loop** (ADR 002). Today a realtime agent
can *converse* fluently but cannot *act*: it has no way to call a skill. This ADR
restores tool use on the realtime seam **without** reintroducing the Executor —
the provider adapter runs the function round-trip in-process against an injected
dispatcher, keeping the speech↔speech path intact and the lock-in contained to
the one adapter file (ADR 048 D2b / CLAUDE.md rule 7).

This is the missing piece for **action-taking voice agents** — e.g. a POS-reboot
agent that must identify a store, call a reboot tool, check status, and escalate.

---

## Context

The OpenAI / Azure OpenAI Realtime wire protocol natively supports function
calling: a session declares `tools` in `session.update`, the model emits
`response.function_call_arguments.done` with a call id + JSON arguments, the
client executes the function and replies with a `conversation.item.create` of
type `function_call_output`, then triggers a fresh `response.create` so the model
speaks a reply informed by the result.

The current mdk adapter
([`realtime_openai.py`](../../src/movate/voice/realtime_openai.py), shared by the
Azure variant via `_stream_session`) implements **none** of this:

- `session.update` (`realtime_openai.py:184`) sends `modalities`, `voice`,
  `input_audio_format`, `output_audio_format`, `instructions` — **no `tools`**.
- `_translate_event` (`realtime_openai.py:247`) maps `response.audio.delta`,
  `response.audio_transcript.delta`, input-transcription, `speech_started` /
  `speech_stopped`, `response.done`, `error` — and returns `None` for everything
  else, including the function-call events.
- `RealtimeChunk` (`base.py:212`) has no `tool_call` kind, and the seam's
  `session()` signature (`base.py:283`) has no `tools` parameter and no
  back-channel for a tool **result**.

The transport consumer
([`_run_voice_realtime`](../../src/movate/runtime/app.py), `app.py:1492`) simply
streams `provider.session(...)` chunks to the socket (`app.py:1581`). There is no
place to inject a function result back into the session.

**The architectural crux.** The pipeline path gets tool use "for free" because it
runs through the Executor (ADR 002): text in → Executor's tool loop → text out.
The realtime path has no Executor and the `session()` contract is essentially
*one-directional for control* (audio in, events out). Function calling requires a
**back-channel**: when the model asks to call a tool, something must execute it
and feed the result back into the **same** live session. The decision below is
where that back-channel lives.

---

## Decision

### D1 — A tool **dispatcher** is injected into `session()`; the round-trip stays inside the adapter

Extend the `RealtimeVoiceProvider.session()` contract with two **optional**
parameters (both default to today's behavior, so the seam is back-compat):

```python
def session(
    self,
    audio_in: AsyncIterator[AudioChunk],
    *,
    voice_id: str = "",
    instructions: str = "",
    language: str | None = None,
    codec: AudioCodec = "pcm16",
    api_key: str | None = None,
    tools: list[RealtimeToolSpec] | None = None,       # NEW — function schemas
    on_tool_call: RealtimeToolDispatcher | None = None,  # NEW — async executor
) -> AsyncIterator[RealtimeChunk]: ...
```

`RealtimeToolDispatcher` is `Callable[[str, dict[str, Any]], Awaitable[str]]` —
given a tool name + parsed arguments, it returns a result string. When the
adapter surfaces a `function_call_arguments.done` event it **awaits the
dispatcher**, then sends `conversation.item.create` (`function_call_output`,
matching `call_id`) + `response.create` back over the **same socket**, all inside
`_stream_session`.

**Why the dispatcher pattern (not "surface the call and let the transport inject
it").** The alternative — emit a `tool_call` chunk, have the transport execute it,
and push the result back through a second inbound channel — would force `audio_in`
to become a bidirectional control queue and spread the Realtime wire protocol into
the transport. Injecting an `async` dispatcher keeps the entire function round-trip
**inside the one adapter file** (ADR 048 D2b's "lock-in contained to a single
adapter"), and makes the dispatcher the clean seam to mdk's skill backend — or, for
a demo, to three in-memory stubs.

### D2 — `RealtimeChunk` gains additive `tool_call` / `tool_result` kinds (observability only, not control)

Add two kinds to the `RealtimeChunk` envelope (`base.py:240`):

- `kind="tool_call"` → `tool_name` + `tool_args` (JSON string) + `tool_call_id`
- `kind="tool_result"` → `tool_name` + `text` (the result) + `tool_call_id`

These are **surfaced for observability and captions only** — the actual execution
+ result-injection is done by the dispatcher (D1) inside the adapter. The transport
yields them onto the wire as control frames so a client can render "🔧 rebooting
register…" and traces can record the call, exactly mirroring how the pipeline
surfaces tool steps. All new fields default empty (`extra="forbid"` is preserved),
so existing chunk consumers are unaffected.

### D3 — The OpenAI / Azure adapter implements the wire round-trip

In `_stream_session` (`realtime_openai.py:157`):

1. **Declare tools.** When `tools` is non-empty, add a `tools` array (and
   `tool_choice: "auto"`) to the `session.update` payload (`realtime_openai.py:184`).
   `RealtimeToolSpec` is the OpenAI function-schema shape (`name`, `description`,
   `parameters`) — the same JSON-Schema function spec the pipeline derives from a
   skill (D5), so the two paths declare a tool identically.
2. **Handle the call event.** In `_translate_event`, map
   `response.function_call_arguments.done` → `RealtimeChunk(kind="tool_call", …)`.
   The driver loop, on seeing a `tool_call`, parses arguments, **awaits
   `on_tool_call(name, args)`**, yields a `tool_result` chunk, then sends
   `conversation.item.create` (`function_call_output`) + `response.create` on the
   connection.
3. **Both providers, one place.** Because public-OpenAI and Azure-OpenAI share
   `_stream_session`, this lands once and both adapters get it (CLAUDE.md rule 4).
   Azure keeps audio in the tenant's resource — function calling does not change
   that.

### D4 — The transport builds the dispatcher from the agent's skills (reusing the existing skill backend)

In `_run_voice_realtime` (`app.py:1492`), when the agent bundle declares skills:

- derive `tools` from the bundle's skill specs (D5), and
- build `on_tool_call` as a thin async closure that routes `(name, args)` to the
  **same `dispatch_skill` backend the Executor uses** (ADR 002) — so a skill
  behaves identically whether invoked from a text turn or a voice-native turn.

A realtime agent with **no** skills passes `tools=None` and gets exactly today's
behavior. This is the only change to the transport, and it is gated on the agent
actually having skills — zero blast radius for existing voice-native agents.

### D5 — Tool specs reuse the pipeline's skill→function-schema derivation (no new agent schema)

The function schemas handed to the realtime session are derived from the agent's
existing skills via the same `BaseLLMProvider.to_tool_spec(skill)` path the
pipeline/Executor already uses (skill name + description + input JSON-Schema). A
voice-native agent therefore declares tools the **same way** a text agent does —
in `agent.yaml`'s skills, **no new field**. There is no realtime-specific tool
manifest to keep in sync.

### D6 — Tool errors degrade to the model, never crash the session

If the dispatcher raises (or a skill errors), the adapter sends a
`function_call_output` whose content is a structured error string (not an
exception up the stack) and triggers `response.create`, so the model can recover
verbally ("I wasn't able to reach the reboot service — let me get a specialist").
A malformed-arguments parse failure is treated the same way. The session stays
live; only `kind="error"` (a genuine **session** failure) tears it down, exactly
as today (ADR 048 failure modes).

---

## Phasing

- **Phase A (S) — seam + adapter.** D1 (`session()` signature + the two new
  types), D2 (`RealtimeChunk` kinds), D3 (the OpenAI/Azure wire round-trip), unit
  tests against a fake connection that scripts a `function_call_arguments.done`
  event (no socket — mirrors the existing `_translate_event` tests). This alone
  makes the seam tool-capable.
- **Phase B (S) — transport wiring.** D4 (dispatcher from the agent's skills) +
  D5 (reuse `to_tool_spec`) + `_send_realtime_chunk` serialization of the two new
  kinds (`app.py:1403`). After this, any voice-native agent with skills can call
  them over `?mode=realtime`.
- **Phase C (S) — demo skills + observability.** Three simulated skills for the
  POS-reboot demo (`reboot_register`, `check_pos_status`, `escalate_to_human`),
  operator-controlled outcome (status keys off the spoken store number), and the
  `tool_call` / `tool_result` traces wired to the `VoiceObserver` (ADR 068) so the
  playground renders the action timeline.

Phases A+B are the reusable platform capability; Phase C is the demo on top.

---

## New surfaces (flagged per CLAUDE.md rule 5)

All **ADDITIVE**; none changes an existing `agent.yaml`/`project.yaml` field, an
existing `/api/v1` endpoint's request/response shape, a storage schema, a
`MOVATE_*`/`MDK_*` env var, an existing `--json` shape, or deploy behavior:

- **`RealtimeVoiceProvider.session(tools=…, on_tool_call=…)`** (D1) — two new
  **optional** keyword params on the seam; omitted → today's behavior verbatim.
- **`RealtimeToolSpec` / `RealtimeToolDispatcher`** (D1) — new public types in
  `voice/base.py`.
- **`RealtimeChunk.kind ∈ {"tool_call","tool_result"}`** + the `tool_name` /
  `tool_args` / `tool_call_id` fields (D2) — additive enum members and fields,
  all defaulting empty.
- **Realtime wire frames `tool_call` / `tool_result`** (D2/Phase B) — new control
  frames on the **existing** `?mode=realtime` socket; existing clients ignore
  unknown frames.

No new endpoint, extra, env var, or credential. BYOK key resolution (ADR 018) is
unchanged.

---

## Consequences

**Positive.**
- **Action-taking voice agents.** The realtime seam goes from conversation-only
  to tool-capable — the prerequisite for the POS-reboot demo and any voice agent
  that must *do* something, not just talk.
- **One tool definition, both paths.** A skill declared in `agent.yaml` works
  over a text turn (Executor) and a voice-native turn (dispatcher) with no
  duplicate manifest (D5).
- **Lock-in stays in one file.** The Realtime wire protocol — including the
  function round-trip — lives entirely in the OpenAI/Azure adapter; the transport
  only supplies a dispatcher (D1).
- **Back-compat by construction.** Every new parameter and chunk kind is optional
  and defaults to current behavior; a skill-less realtime agent is byte-for-byte
  unchanged.

**Negative / costs.**
- **The realtime seam is no longer purely one-directional.** The adapter now
  writes back into the session mid-stream (result injection). This is contained
  to `_stream_session`, but it is genuinely more complex than the pure
  audio-in/events-out loop.
- **Tool latency is in the voice path.** A slow skill stalls the spoken reply;
  unlike the pipeline there is no separate text turn to absorb it. Demo skills are
  instant; real integrations must be fast or stream a "one moment…" filler. (Out
  of scope here; noted for the production ADR.)
- **Provider-specific.** Only the OpenAI-protocol realtime providers get this in
  Phase A. A future Gemini Live adapter must implement its own function-call wire
  mapping behind the same D1 seam.

---

## Alternatives considered

- **Surface the `tool_call` and let the transport execute + re-inject.**
  Rejected — forces `audio_in` into a bidirectional control queue and leaks the
  Realtime wire protocol into `app.py`. The dispatcher (D1) keeps the protocol in
  the adapter and gives mdk a clean injection seam.
- **Route realtime turns back through the Executor for tool use.** Rejected —
  defeats the entire point of the voice-native seam (ADR 048 D2b: no intermediate
  Executor, lowest latency). The dispatcher reuses the **skill backend** without
  reintroducing the text Executor in the audio path.
- **A realtime-specific tool manifest in `agent.yaml`.** Rejected — diverges from
  the pipeline's skills and creates a second source of truth to keep in sync (D5
  reuses one).
- **Skip realtime; use the STT→LLM→TTS pipeline for the demo (tool use is free
  there).** Viable, and ~3–5 days cheaper. Rejected for this demo because the
  brief prioritizes *responsiveness* (full-duplex, barge-in) over the pipeline's
  per-turn latency; this ADR buys responsiveness **and** tool use. Documented so
  the trade-off is explicit if priorities change.

---

## Boundaries (explicitly NOT in scope)

- **No streaming / long-running tools.** Phase A assumes fast, synchronous skills
  (the demo's are instant). Async tool polling, heartbeating, and "tell the user
  we're checking, come back later" are a production concern (composes with ADR
  065 Temporal), not this ADR.
- **No Temporal durability.** This is the edge voice path; durable workflow
  orchestration of the tool calls is a separate, later decision.
- **No new providers.** Gemini Live / other realtime backends are future adapters
  behind the same D1 seam.
- **No agent-schema change.** Tools come from existing skills (D5); this ADR adds
  no `agent.yaml` field.

---

## Failure modes

- **Skill raises / times out** → `function_call_output` carries a structured
  error; model recovers verbally; session stays live (D6).
- **Malformed function arguments** (bad JSON from the model) → same error path;
  no crash.
- **Dispatcher not supplied but the model calls a tool** → adapter yields the
  `tool_call` chunk for observability and replies with a "tool unavailable"
  `function_call_output`, so the model is told the tool could not run rather than
  the session hanging.
- **Session error mid tool round-trip** (`kind="error"`) → tears down as today;
  the transport degrades per ADR 048.
- **Client ignores `tool_call` / `tool_result` frames** → harmless; they are
  observability-only (D2).

---

## Cross-references / composition notes

- **Reuses ADR 002 (Executor tool loop) via the skill backend** — D4's dispatcher
  routes to the same `dispatch_skill` the Executor uses, so a skill behaves
  identically across the text and voice-native paths.
- **Reuses ADR 048 D2b's seam shape** — the new params are optional and the
  adapter stays the single point of provider lock-in.
- **Reuses ADR 050 D12's wire** — the `tool_call` / `tool_result` frames ride the
  existing `?mode=realtime` socket; no new endpoint.
- **Reuses ADR 071's per-agent seed** — the agent's skills join the voice block
  already seeded into the realtime session.
- **Feeds ADR 068 (VoiceObserver)** — `tool_call` / `tool_result` are the new
  observable events for the voice action timeline.
