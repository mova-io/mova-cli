# ADR 050 — Voice API surface & CLI parity: voice is an I/O modality on the existing run/agent/session resources, not a parallel API

**Status:** Accepted — shipped (voice API + CLI parity; `mdk voice`). _(status reconciled to shipped reality 2026-06-08)_
**Date:** 2026-05-29
**Deciders:** Engineering + Deva (Movate)
**Builds on / composes with (changes nothing in any of them):**
ADR 048 (voice agents — the three speech seams `SpeechToTextProvider` / `TextToSpeechProvider` / `RealtimeVoiceProvider`, the `WS /api/v1/agents/{name}/voice` transport shipped by #576, the optional `voice:` `agent.yaml` block, the `mdk[voice]` extra; **this ADR designs the API/CLI *surface* over those seams and does not alter a single one of them**),
ADR 049 (voice provider-portability & agility — the capability manifests, the voice router, `mdk voice bench`, the per-request/per-tenant provider selection; this ADR *surfaces* the manifests through the API and *routes* per-request overrides through the ADR-049 router),
ADR 045 (API ergonomics + next-gen capabilities — **D9 capability discovery** (`GET /api/v1/capabilities`), **D10 stateful sessions** (a voice turn is a session turn), **D11 run-output token streaming** (the agent stage of the voice stream), the **D1 economics headers / `?estimate`-style cost pre-flight** this ADR mirrors onto voice, the **D6 `_links`** + **D8 codegen-clean OpenAPI** contract),
ADR 036 (usage metering + quotas — voice meters three stages STT-seconds + LLM-tokens + TTS-characters and the `voice_seconds` quota dimension; the cost envelope/headers read straight off this seam, not a new meter),
ADR 035 (outbound events / SSE + the async job model — the long-poll `?wait=` and job conventions slow voice ops reuse),
ADR 032 / item #147 (front-end API completion — the `/api/v1` compat contract + the OpenAPI contract test `tests/test_front_end_api_contract.py` that enforces every CLI verb maps to an endpoint),
ADR 033 (API hardening — the scope/error/rate-limit machinery every endpoint here declares against),
ADR 013 (flat least-privilege scopes — the `run` scope a voice turn declares).

**Defining architectural principle.** **Voice is an I/O modality on the existing
`run` / `agent` / `session` resources — NOT a parallel "voice API."** A voice
turn **is** a run, with audio on the way in and audio on the way out; it threads
the **same** session, meters on the **same** three-stage cost surface (ADR 036),
appears in the **same** `mdk runs list`/traces, and is discovered through the
**same** `GET /api/v1/capabilities`. The voice endpoints must feel like *the same
API with a microphone attached*, not a second, differently-shaped API bolted on
the side. ADR 048 established that voice is "a transport + two seams that wrap the
**unchanged** Executor"; this ADR is the corollary at the **API/CLI surface**: if
the Executor doesn't get a voice silo, neither do the resources, the endpoints,
or the CLI. Every decision below exists to keep the voice surface as intuitive
and elegant as the rest of `/api/v1` — **minimal new concepts, maximal reuse**.

This ADR adds **zero** new execution-plane behavior. It is a **surface-design**
ADR: it decides the *shape* of the voice endpoints + CLI verbs and how they
reuse the existing run/agent/session resources, discovery, metering, tracing,
sessions, and job conventions. The implementation lands in follow-up PRs
(Boundaries).

---

## Context

ADR 048 made an agent voice-capable; ADR 049 made the *provider* choice runtime,
measured, and reversible. Both are about the **machinery underneath** voice.
Neither decided the question this ADR exists to answer: **what does the voice
*API surface* look like, and does it stay as intuitive and elegant as the rest of
`/api/v1`?**

That question is load-bearing for three reasons:

1. **One front end, N heterogeneous runtimes (ADR 045's defining fact).** Mova iO
   talks to many tenant runtimes on different CalVer builds with different
   extras. A voice surface that is *discovered* (capabilities) and *shaped like
   the rest of the API* is one the front end can adopt by inspection; a parallel
   voice API with its own conventions is per-tenant special-casing all over
   again. Voice must answer `GET /api/v1/capabilities` (ADR 045 D9), not require
   a separate "is voice on?" probe.

2. **The silo trap is the easy mistake.** The path of least resistance is to ship
   "voice runs," a `/voice/runs` collection, a voice-only usage endpoint, a
   voice-only trace view, voice-only sessions. Each is a *parallel* of something
   that already exists, and each is a place the two surfaces drift. ADR 048
   already rejected the silo at the *execution* layer ("voice as a new agent
   type" — rejected); this ADR rejects it at the *surface* layer for exactly the
   same reason. A voice turn that does not show up in `mdk runs list` or
   `/api/v1/usage` is a governance hole, not a feature.

3. **#576 shipped the WS transport ahead of the surface spec.** The streaming
   `WS /api/v1/agents/{name}/voice` (ADR 048 D4) is live. Its message protocol
   was specified in ADR 048 standalone; it now needs to be **reconciled** to the
   text-stream event taxonomy (D3 below) so the WS stream is *recognizably the
   run stream with audio frames added*, not a private vocabulary. There is also
   no **REST** parity to the WS for one-shot/batch turns (telephony turns, file
   transcription, automated testing) — the surface is currently WS-only.

The durable answer is a **surface contract**: voice is a modality on the existing
resources, every voice CLI verb maps to an endpoint (enforced by the #147
OpenAPI contract test), audio I/O has one clear convention, and discovery +
cost + sessions + traces + metering are the *same* surfaces text already uses.

---

## Relationship to ADR 048 and ADR 049 (what this changes there: nothing)

This ADR **composes on top of** ADR 048 and ADR 049 and **modifies neither**:

- It does **not** touch ADR 048's three Protocols, its `voice:` `agent.yaml`
  block, the `mdk[voice]` extra, the Executor, or the *existence* of the
  `WS /api/v1/agents/{name}/voice` transport. It **reconciles the WS message
  protocol's vocabulary** to the run-stream taxonomy (D3) — an additive framing
  decision about how the frames are named/typed, flagged as a compat item below,
  not a change to what the pipeline does.
- It does **not** touch ADR 049's manifests, router, bench, or rollout machinery.
  It *exposes* the manifests through `GET /api/v1/voice/providers` (D5) and
  *routes* per-request provider overrides (D6) through the **existing** ADR-049
  router. The router decides; this ADR only gives the request a place to ask.
- It does **not** change ADR 036 metering, ADR 045 sessions/streaming/economics,
  or the ADR 035 job model — it *reuses* their surfaces for voice rather than
  minting voice-only parallels.

Where ADR 048 is the seam and ADR 049 is the agility around the seam, **ADR 050
is the API/CLI surface over both** — and its whole job is to make that surface
indistinguishable in feel from the text surface it sits beside.

---

## Decision

The decisions are organized around the principle: **reuse the run/agent/session
resources; add the minimum new surface; mirror the existing conventions.**

### D1 — Voice-as-modality: a voice turn IS a run

A voice turn is **a run with audio I/O**, executed on the **same** run path over
the **same** `agent` resource. There is **no separate "voice runs" collection,
no `voice_run` entity, no parallel run lifecycle.** The audio is an input
representation (in) and an output representation (out) of a run that is otherwise
identical to a text run: same run id, same `request_id`, same cost record (ADR
024/036), same trace, same session threading (D8). This is the surface-level
restatement of ADR 048's "voice wraps the **unchanged** Executor": if the
Executor doesn't get a voice fork, the **run resource doesn't either**. Every
downstream consumer — `mdk runs list/show`, `/api/v1/usage`, traces, dashboards
— sees a voice turn as a run because it **is** one.

### D2 — Transport pair: the WS (streaming) AND a REST one-shot/batch endpoint

Voice gets **two** transports over the **same** `{name}` agent resource — the
streaming socket and its REST parity:

| Method | Path | Use |
|---|---|---|
| WS | `/api/v1/agents/{name}/voice` | **Streaming** full-duplex voice (ADR 048 D4, **shipped by #576**): live audio in → partial transcript → streaming agent tokens → streaming TTS out, with barge-in. The interactive/real-time turn. |
| POST | `/api/v1/agents/{name}/voice` | **One-shot / batch** (NEW): audio in → `{transcript, response, audio}` out in a single request/response. The REST parity to the WS for **telephony turns**, **file transcription**, and **automated testing** where a socket is overkill. |

The `POST` is the same conceptual turn as the WS, collapsed to request/response:
it runs `STT → unchanged agent → TTS` and returns the transcript (what the caller
said), the response text (what the agent answered), and the synthesized audio
(D10 governs how the audio bytes travel). It is the surface a phone-bridge turn,
a "transcribe this file and answer it," or a test harness reaches for. Both
transports share the **same path stem** (`/agents/{name}/voice`) because they are
the **same resource, two access styles** — exactly the long-poll-vs-SSE pairing
ADR 045 D4 established for jobs.
**New surface — flag (CLAUDE.md rule 5):** `POST /api/v1/agents/{name}/voice` is
a **new `/api/v1` endpoint** (scope `run`, the same as the WS). Additive; flagged
below.

### D3 — Mirror the text-stream event taxonomy on the voice WS stream

The voice WS stream is **the run-output stream (ADR 045 D11) + the run/event
stream (ADR 035 D3) with audio frames added** — not a private protocol. Concretely
the voice WS = **the existing run-stream events** plus a **minimal** set of
voice-specific frames:

| Source | Frame | Origin |
|---|---|---|
| **Run-stream (reused)** | `agent.token` (streaming agent output) | ADR 045 D11's token frames — **identical**, the agent stage of the pipeline |
| **Run-stream (reused)** | terminal `result` / `usage` carrying `request_id` + 3-stage cost | ADR 045 D11's trailing `usage` frame + ADR 036 cost — **identical** |
| **Voice-specific (minimal new)** | `transcript.partial` / `transcript.final` | STT partials + endpointed utterance (ADR 048) |
| **Voice-specific (minimal new)** | `tts.audio` (binary) | a synthesized audio chunk to play |
| **Voice-specific (control)** | `control:barge_in` / `control:cancel` | barge-in = run cancellation (ADR 048 R5 / R4b) |

The principle: **a developer who knows the text run stream already knows 80% of
the voice stream** — it is the same `agent.token` + terminal `result`/`usage`
with `request_id` and cost, and the only genuinely new concepts are
partial-transcript, TTS-audio, and barge-in/cancel control. **Minimal new
concepts.**
**Reconciliation note (flag):** #576's shipped WS protocol (ADR 048 D4 named the
frames `transcript.partial` / `transcript.final` / `agent.token` / `tts.audio` /
`usage` / `error`) is **already close** to this taxonomy. This ADR makes the
alignment **explicit and contractual**: the agent-output frame **is** D11's
`agent.token`, the terminal frame **is** D11's `usage` (carrying `request_id` +
the ADR-036 three-stage cost), and the voice-specific frames are the documented
delta. Any divergence in #576's current naming/typing is reconciled to this
taxonomy in an additive follow-up (it renames/aligns frame vocabulary, it does
not change pipeline behavior) — flagged per rule 5 because the WS protocol is a
surface the front end binds to.

### D4 — Discovery: voice answers `GET /api/v1/capabilities` and per-agent GET

Voice is **discovered, not assumed** — the same way every other ADR-045-D9
capability is, so the front end adapts to N heterogeneous runtimes by *asking*:

- **Runtime-level:** extend `GET /api/v1/capabilities` (ADR 045 D9) with a
  `voice` block advertising: **modes** (`pipeline` / `realtime`), **configured
  providers** (the STT/TTS/realtime providers this runtime has keyed +
  registered), **languages**, and **codecs** supported at the edge. A runtime
  without the `mdk[voice]` extra reports `voice: { enabled: false }` (or omits
  the block) — the front end never probes, it reads.
- **Per-agent:** surface the agent's resolved `voice:` config (ADR 048 D5 block —
  mode, stt/tts/voice_id/language defaults) on `GET /api/v1/agents/{name}`, so a
  caller can see whether *this* agent has voice defaults without parsing
  `agent.yaml`.

Both are **additive read fields** on existing endpoints (no new discovery
endpoint, no new shape) — voice slots into the discovery surface that already
exists.

### D5 — Provider catalog endpoint + CLI

```
GET /api/v1/voice/providers        # the discovered providers + ADR-049 capability manifests
mdk voice providers list           # the CLI parity (already specified in ADR 049)
```

A read endpoint returning the **ADR 049 capability manifests** (D1 there) for the
providers this runtime can use: streaming? languages? sample rates? latency tier?
`$/min` / `$/char`? region/sovereignty? This is what `mdk voice providers list`
renders (ADR 049 already named that CLI verb); D11 binds the two. It is a
**read** surface on data ADR 049 already produces — not a new registry, not a
second source of truth. The endpoint reads the same manifests the router (ADR
049 D9) reads.
**New surface — flag (CLAUDE.md rule 5):** `GET /api/v1/voice/providers` is a
**new `/api/v1` endpoint** (scope `read`). Additive; flagged below.

### D6 — Per-request provider overrides (like model selection)

`stt` / `tts` / `voice_id` / `language` are **overridable per request** — in the
`POST` body and in the WS init/connect frame — exactly the way a caller already
overrides the **model** on a text run. The override is **routed through the ADR
049 voice router**: the router validates the requested provider against the D1
manifests and the tenant policy, and selects it (or rejects it if the tenant
isn't keyed/allowed for it). The agent's `voice:` block (ADR 048 D5) sets the
**defaults**; the per-request fields **override** them for that one turn; absent
both, the **tenant defaults** apply (ADR 048 D5's three-tier resolution). No new
selection mechanism — this is the same defaults-then-override pattern the rest of
the API uses, pointed at the existing router.

### D7 — Cost / estimate parity (ADR 036 + ADR 045)

Voice carries the **same economic transparency** as every other call:

- **Three-stage cost in the response envelope/headers (ADR 036).** The `POST`
  response and the WS terminal `usage` frame (D3) carry the **STT-seconds +
  LLM-tokens + TTS-characters** cost (ADR 036's three metered stages), surfaced
  through the **same** `X-MDK-Cost-USD` / `X-MDK-Tokens-*` economics headers (ADR
  045 D1) on the `POST`, and the trailing `usage` frame on the WS (the streaming
  transport's equivalent, per ADR 045 R5). Best-effort per ADR 045 R2 — a cost
  not yet computable is omitted, never guessed.
- **`?estimate=true` pre-flight (ADR 045 cost-prediction parity).** A caller can
  ask for the **predicted** cost of a voice turn before committing it — the voice
  analog of ADR 045's dry-run/cost-prediction (D1/D3): given expected
  audio-seconds / text length, return the forecast 3-stage cost without running.
  This lets a telephony front end budget a call before placing it.

Voice cost is **the same cost surface with three stages instead of one** — not a
voice-only billing path.

### D8 — Reuse, no silos: voice turns are first-class everywhere text runs are

A voice turn appears, **unmodified and automatically**, in every place a text run
does — because it **is** a run (D1):

- **`mdk runs list` / `mdk runs show`** — voice turns are listed and shown like
  any run (with a modality marker), not in a separate `mdk voice runs`.
- **Traces (ADR 015 / ADR 024)** — the voice-turn trace is the run trace with the
  STT-latency and TTS-latency spans ADR 048 D7 already defined; no separate voice
  trace view.
- **`/api/v1/usage` metering (ADR 036)** — voice's three metered stages roll into
  the **same** per-tenant usage endpoint and the **same** `voice_seconds` quota
  dimension; no voice-only usage endpoint.
- **Dashboards (ADR 031)** — voice turns feed the existing run dashboards.
- **Sessions (ADR 045 D10)** — a session threads **voice turns identically to
  text turns**: a session is **multi-modal**, mixing spoken and typed turns in
  one history with one cost rollup. A voice turn is a session turn (ADR 048's
  memory story); the session never learns the turn was spoken. There is **no
  voice-only session type.**

The test of this ADR is operational: *a voice turn that does not show up in
`mdk runs list`, `/api/v1/usage`, the traces, and the session history is a bug,
not a missing feature.*

### D9 — Async-job conventions for slow voice ops

Slow voice operations — **batch transcription** of a large file/corpus, and
**`mdk voice bench`** (ADR 049 D5, which runs a provider over a golden corpus) —
reuse the **existing async job model** (ADR 017/035), not a voice-only queue:
they **enqueue a job** and the caller **polls `GET /api/v1/jobs/{id}`**, with the
**`?wait=<duration>` long-poll** (ADR 045 D4) for simple clients and the SSE
job-stream (ADR 035 D3) for progress. This is exactly the pattern ADR 032 D3 used
for async KB ingest — a slow voice op is just another `JobKind`. No new
long-running-op convention; voice's slow paths look like every other slow path.

### D10 — Audio I/O contract: multipart or signed URL, never base64-in-JSON

Audio crosses the API boundary **one** clear way, and it is **never** base64
inside a JSON envelope:

- **Inbound audio** → a **multipart upload** (the audio as a file part alongside
  a JSON metadata part) **or** a **signed URL** the runtime fetches (for large /
  already-hosted audio, e.g. a telephony recording in blob storage).
- **Outbound audio** → a **streamed response body** (the synthesized audio bytes
  as the HTTP body with the right content-type) **or** a **short-lived signed
  URL** to fetch it (for batch results).

Base64-in-JSON is **rejected** (Alternatives): it inflates payloads ~33%, defeats
streaming, blows past JSON parser limits on real audio, and pollutes the clean,
codegen-friendly JSON shapes ADR 045 D8 requires. Audio is **binary**, so it
travels as **binary** (a body or a file part) or as a **reference** (a URL) — the
JSON envelope carries the transcript, the response text, and the metadata, but
**not the audio bytes**. This keeps the JSON surface elegant and the spec
codegen-clean.

### D11 — CLI ↔ API parity, enforced by the OpenAPI contract test

**Every voice CLI verb maps to a runtime endpoint**, and that mapping is
**enforced by the existing OpenAPI contract test** (#147 /
`tests/test_front_end_api_contract.py`, ADR 032) — voice is not exempt from the
parity contract:

| CLI verb | Endpoint | Notes |
|---|---|---|
| `mdk voice try <agent>` | WS `/api/v1/agents/{name}/voice` | interactive streaming turn (the live companion to `say`/`transcribe`) |
| `mdk voice say <agent>` | `POST /api/v1/agents/{name}/voice` | one-shot: text/audio in → spoken answer out (D2) |
| `mdk voice transcribe <file>` | `POST /api/v1/agents/{name}/voice` (or batch job, D9) | audio file → transcript (+ optional answer) |
| `mdk voice providers list` | `GET /api/v1/voice/providers` (D5) | render manifests |
| `mdk voice bench` | async job (D9) over the corpus | the ADR 049 standing bake-off |

(`mdk voice test` from ADR 048 and `mdk voice bench` from ADR 049 are
pre-existing; this ADR aligns the *full* voice verb set to endpoints.) The
parity is a **contract, not a convention**: a voice CLI verb that does not map to
a documented endpoint **fails CI** via the #147 test, the same gate that keeps
the text CLI and `/api/v1` in lockstep. This is the mechanism that keeps the CLI
and the API from drifting into two different voice products.
**New surface — flag (CLAUDE.md rule 5):** `mdk voice try` / `say` / `transcribe`
are **new, opt-in CLI verbs** (behind the `mdk[voice]` extra, like ADR 048's `mdk
voice test`). They change no existing CLI shape; flagged below.

### D12 — Realtime is the same resource, different mode

Realtime (speech-to-speech, ADR 048 D2b / the `RealtimeVoiceProvider` seam) is
**not a different endpoint** — it is the **same `WS /api/v1/agents/{name}/voice`
in a different mode**, selected with `?mode=realtime` (or the agent's
`voice.mode: realtime` default). Flipping the mode flips the transport to the
`RealtimeVoiceProvider` (full-duplex voice↔voice) instead of the
STT→agent→TTS pipeline; the **URL is identical**, the mode is **discovered via
capabilities (D4)** (`voice.modes: [pipeline, realtime]`), and a runtime/agent
without realtime simply doesn't advertise it. This mirrors ADR 048 D5's
"`voice.mode` selects the path" at the API surface: **one voice URL, two modes,
discovered — not two voice APIs.** (Realtime providers' specific session
protocols remain an ADR 048 Phase-2 concern behind the seam — Boundaries.)

---

## New surfaces (flagged per CLAUDE.md rule 5)

All **ADDITIVE**; none changes an existing `agent.yaml`/`project.yaml` field, an
existing `/api/v1` endpoint's request/response shape, a storage schema, a
`MOVATE_*`/`MDK_*` env var, an existing `--json` shape, or deploy behavior.
Called out explicitly:

- **`POST /api/v1/agents/{name}/voice` (D2)** — a **new `/api/v1` endpoint**
  (scope `run`), the REST one-shot/batch parity to the shipped WS.
- **`GET /api/v1/voice/providers` (D5)** — a **new `/api/v1` endpoint** (scope
  `read`) rendering the ADR-049 manifests.
- **`voice` block on `GET /api/v1/capabilities` + `voice:` echo on
  `GET /api/v1/agents/{name}` (D4)** — **additive read fields** on existing
  endpoints; absent/`enabled:false` on a non-voice runtime. No existing field
  changes.
- **`?estimate=true` on the voice `POST` + `?mode=realtime` on the voice WS
  (D7/D12)** — **additive query params**; inert/absent on existing behavior.
- **Voice WS message-protocol reconciliation to the run-stream taxonomy (D3)** —
  an **additive framing** of #576's already-shipped WS frames to the ADR-045-D11
  / ADR-035-D3 event vocabulary. It aligns frame naming/typing (the front end
  binds to the WS protocol, so it is flagged) and changes **no** pipeline
  behavior.
- **`mdk voice try` / `say` / `transcribe` (D11)** — **new, opt-in CLI verbs**
  behind the `mdk[voice]` extra. They change no existing CLI shape.

(Per-request `stt`/`tts`/`voice_id`/`language` overrides (D6) are additive
request fields routed through the existing ADR-049 router — additive input, not a
change to any existing flagged surface, noted for completeness.)

---

## Consequences

**Positive.**
- **The voice surface feels like the rest of `/api/v1`** — a developer who knows
  the run/agent/session resources, capability discovery, the economics headers,
  the job long-poll, and the CLI↔API parity contract already knows the voice
  surface, because it is *those same surfaces with audio*. Minimal new concepts.
- **No silos to drift.** Voice turns are runs (D1), appear in `mdk runs
  list`/usage/traces/sessions automatically (D8), and the CLI↔API parity is
  CI-enforced (D11) — there is no second voice product to keep in sync.
- **The WS gets a REST sibling (D2)** — telephony turns, file transcription, and
  automated tests get a request/response surface without standing up a socket.
- **Voice is discovered, not assumed (D4)** — one Mova iO adapts to N runtimes by
  reading capabilities, the same way it adapts to every other ADR-045 feature.
- **Economic transparency is uniform (D7)** — voice's three-stage cost rides the
  same headers/envelope + estimate pre-flight as every other call; no voice-only
  billing surface.
- **The JSON surface stays clean + codegen-friendly (D10)** — audio travels as
  binary or a URL, never bloating envelopes or defeating the codegen-clean spec
  (ADR 045 D8).

**Negative / risks.**
- **New surfaces to maintain** — flagged above: one new REST endpoint (D2), one
  new provider-catalog endpoint (D5), additive capability/agent read fields (D4),
  two query params (D7/D12), the WS-protocol reconciliation (D3), and three new
  CLI verbs (D11). All additive, but they are surfaces to version, document, and
  keep in the OpenAPI spec.
- **#576's WS protocol must be reconciled (D3)** — the shipped frames are close to
  the taxonomy but the alignment must be made explicit and contractual; a
  rename/retyping of frames is a front-end-visible change even though pipeline
  behavior is unchanged, so it is flagged and lands as an additive follow-up with
  the contract test asserting the taxonomy.
- **`POST` voice turns must NOT become a second run path (D1).** The REST
  one-shot endpoint must execute the **same** run as the WS — same run record,
  same cost, same trace, same session threading. A separate code path that
  produced a "voice run" not visible in `mdk runs list` would re-introduce the
  silo this ADR exists to prevent; this is a boundary to catch in review.
- **Audio-reference lifecycle (D10).** Signed URLs (in and out) need short TTLs
  and tenant-scoped access; a leaked or over-broad URL is an audio-disclosure
  risk. The audio-retention/consent policy (ADR 049 D8 / a separate policy doc)
  governs how long any stored audio lives — out of scope here, flagged as a
  dependency.

**Neutral.**
- All net-new surface is **additive** and either a read surface (providers,
  capabilities) or a transport parity (the REST `POST`) — **no change** to ADR
  048's seams/transport/schema/Executor, ADR 049's machinery, ADR 036 metering,
  ADR 045 sessions/streaming, `core`, existing endpoints, or existing CLI shapes.

---

## Alternatives considered

- **(a) Voice as a new resource type (a `/api/v1/voice/runs` collection / a
  `voice_run` entity with its own lifecycle).** **Rejected.** It is the silo trap
  (Context): a parallel of the run resource that drifts from it, a voice turn
  that doesn't show up in `mdk runs list` / `/api/v1/usage` / the traces, a second
  cost path, a second session type. It is the **surface-layer version of ADR
  048's rejected "voice as a new agent type."** A voice turn **is** a run (D1);
  giving it a parallel resource forfeits all the reuse (D8) and doubles the
  surface area for no benefit. Voice is a **modality on the existing resources**,
  not a new resource.
- **(b) Base64-encoded audio inside JSON envelopes.** **Rejected.** It inflates
  payloads ~33%, defeats streaming, exceeds JSON parser limits on real audio
  files, and pollutes the codegen-clean JSON shapes ADR 045 D8 requires. Audio is
  binary and travels as binary (a body / a multipart part) or as a reference (a
  signed URL); the JSON carries the transcript + response text + metadata, never
  the audio bytes (D10).
- **(c) WS-only, no batch/REST surface.** **Rejected.** A socket-only voice
  surface forces a telephony-turn bridge, a "transcribe this file" job, and an
  automated test to stand up and drive a WebSocket for what is conceptually a
  single request/response. The `POST` parity (D2) serves those cases cleanly; the
  WS remains the right surface for live, interruptible, streaming turns. **Both**
  transports over the **same** resource (the long-poll-vs-SSE pattern, ADR 045
  D4) is the elegant answer, not one or the other.
- **(d) A voice-only capability/usage/trace surface** (a `GET /api/v1/voice/status`,
  a voice-only usage endpoint, a voice-only trace view). **Rejected.** Voice
  discovery belongs in `GET /api/v1/capabilities` (D4), voice cost belongs in
  `/api/v1/usage` (D8), and voice traces belong in the run trace (D8) — voice
  rides the surfaces that already exist rather than minting parallel ones the
  front end has to special-case.

---

## Boundaries (explicitly NOT in scope)

- **The implementation.** This is a **surface-design** ADR — it decides the shape
  of the voice endpoints, frames, and CLI verbs and how they reuse the existing
  resources. The endpoints, the WS-protocol reconciliation, the CLI verbs, and
  the contract-test assertions land in **follow-up PRs**.
- **Any change to ADR 048's seams / transport / `voice:` schema / Executor, or
  ADR 049's router / manifests / bench / rollout machinery.** This ADR **does NOT
  modify** them — it designs the API/CLI surface **over** them and consumes them
  as-is (CLAUDE.md rules 6/7).
- **The provider implementations.** Which STT/TTS/realtime providers exist and
  their tiering is ADR 048 / ADR 049 territory; this ADR only *surfaces* whatever
  is configured.
- **The exact OpenAPI schemas + frame field sets.** The precise request/response
  models, the multipart part names, the signed-URL TTL, and the exact WS frame
  field shapes are **build-time details** fixed in the implementation PRs (kept
  codegen-clean per ADR 045 D8), not pinned here.
- **Realtime providers' session protocols.** The OpenAI Realtime / Gemini Live
  wire/session specifics remain an **ADR 048 Phase-2** concern behind the
  `RealtimeVoiceProvider` seam (D12); this ADR routes to realtime via `?mode=`
  but does not specify those protocols.
- **Audio retention / consent / voice-cloning ethics policy.** How long stored
  audio (and signed-URL targets, D10) lives, and the consent model around it, is
  the **separate policy doc** ADR 048/049 already deferred — this ADR only states
  that the audio-reference lifecycle (D10) is gated on it.
- **Telephony bridge + on-prem speech.** The Twilio/LiveKit/Daily bridge (ADR 048
  Phase 3) is a *consumer* of the `POST`/WS surface, not specified here.

---

## Cross-references / composition notes

- **ADR 048 (the seams + WS transport this surfaces).** D1/D2 run STT→agent→TTS
  over the *unchanged* Executor; D3 reconciles #576's WS frames to the run-stream
  taxonomy; D6 routes per-request provider overrides through the ADR-048 `voice:`
  three-tier resolution; D12 flips the *same* WS to the `RealtimeVoiceProvider`.
  **The three Protocols, the transport's existence, and the schema are untouched.**
- **ADR 049 (provider agility).** D5 surfaces ADR 049's capability manifests
  through `GET /api/v1/voice/providers`; D6's per-request overrides are *validated
  + selected by the ADR-049 voice router*, not a new selector; `mdk voice
  providers list`/`bench` (D9/D11) are the ADR-049 verbs given their endpoint
  mappings.
- **ADR 045 (ergonomics).** D3 reuses D11's `agent.token` + trailing `usage`
  frames; D4 extends D9 capability discovery; D7 mirrors D1 economics headers +
  the cost-prediction pre-flight; D8 threads voice turns through D10 sessions;
  D9 reuses D4's `?wait=` long-poll; D10 keeps the spec codegen-clean per D8.
- **ADR 036 (metering).** D7's three-stage cost + D8's `/api/v1/usage` +
  `voice_seconds` quota read off the existing metering seam — no new meter.
- **ADR 035 (jobs/SSE).** D9's slow voice ops reuse the async job model + the SSE
  job-stream; D3's stream taxonomy aligns with D3-there's event stream.
- **ADR 032 / #147 (API↔CLI parity + OpenAPI contract test).** D11's CLI↔API
  parity is enforced by the **same** `tests/test_front_end_api_contract.py` gate
  that keeps the text CLI and `/api/v1` in lockstep — voice is not exempt.
- **ADR 033 / ADR 013 (hardening + scopes).** The voice `POST` declares `run`
  scope and the providers endpoint `read`, under the existing error/rate-limit
  machinery — voice endpoints are ordinary `/api/v1` endpoints on the hardening
  surface. If a voice surface change can't be expressed as additive endpoints/CLI
  verbs reusing the existing resources, discovery, metering, and parity contract,
  it needs its own ADR (CLAUDE.md rule 7).
