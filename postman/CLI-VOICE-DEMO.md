# CLI → OpenWebUI voice demo (mdk `--llm`)

**The story:** *"I describe an agent in one sentence, the CLI builds it with an
LLM, I push it to Azure, and it's instantly a chat **and voice** agent in a real
UI."* ~5–6 minutes.

Runs against the live Azure runtime via the already-registered **`dev`** target
(`~/.movate/config.yaml` → `url: …movate-dev-api.bluebush…`, `key_env: MDK_DEV_KEY`).

> ✅ **VERIFIED END-TO-END (2026-06-01).** This exact flow was run live: `mdk
> init --llm` generated the agent, `mdk deploy --target dev` pushed it, it lists
> in `/api/v1/agents`, runs correctly (1.7–2s), and its **streaming output feeds
> the OpenWebUI shim cleanly** (the shim strips the JSON wrapper so voice speaks
> words, not braces). **`movate-voice-faq` is live on the runtime right now** — so
> you have a working agent to show even if you skip the live-create.
>
> ⚠️ **Use the description below verbatim.** It makes the LLM generate an input
> field named **`question`**, which the OpenWebUI shim recognizes as
> *conversational*. If you change the description and the LLM names the field
> something exotic (not one of `text/question/message/input/query/prompt/content`),
> the shim will treat the agent as "structured" and show a help message instead
> of chatting. Keep the description simple and support-flavored.

---

## Before the demo (do this once, off-camera)

```bash
# 1. The runtime key the CLI uses to push to Azure (scopes: admin,kb:write,run,read)
export MDK_DEV_KEY="mvt_live_…"          # the key we minted

# 2. A provider key so --llm can GENERATE the agent (scaffold happens locally)
export OPENAI_API_KEY="sk-…"             # or ANTHROPIC_API_KEY

# 3. Rehearse the exact flow once (see steps below) so the live run is muscle memory.
```

> **Two different keys, two different jobs.** `MDK_DEV_KEY` authenticates the
> *push to Azure*. `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` powers the local `--llm`
> *generation* of the agent. Set both before you start.

---

## The demo — step by step

### Step 1 — Describe the agent, the CLI builds it (`--llm`)
```bash
mdk init movate-voice-faq \
  --llm "A friendly customer-support assistant that answers questions about \
Movate's AI agent platform in two or three clear sentences."
```
This scaffolds a **project** `movate-voice-faq/` with an LLM-generated agent
inside it: `agents/movate-voice-faq/agent.yaml`, `prompt.md`, and the
input/output schema — all written for you.

```bash
cd movate-voice-faq
```

What it writes (verified output):
- `agent.yaml` (model `openai/gpt-4o-mini`, anthropic-haiku fallback)
- `prompt.md` with `{{ input.question }}` interpolation + a JSON output contract
- `schema/input.yaml` → required field **`question`** (shim-conversational ✓)
- `schema/output.yaml` → `{answer, confidence}` (shim extracts `answer`)
- `evals/dataset.jsonl` (2 seed cases) — generation cost ~$0.001

### Step 2 — (optional) Eyeball what it generated
```bash
mdk validate .            # static checks pass
cat agents/movate-voice-faq/prompt.md
```

### Step 2.5 — Ground it in YOUR facts (add a context) ⭐ verified
A fresh agent answers from the model's training data — which is **stale** (ask it
"the latest MacBook Pro" and it says M2). Ground it in current/owned facts with a
**context** — a knowledge fragment bundled into the agent and prepended at every
run.

```bash
cd grounded-demo            # the project dir
# create a context file (or write agents/grounded-demo/contexts/movate-facts.md yourself)
mdk contexts create movate-facts --agent grounded-demo
# → edit agents/grounded-demo/contexts/movate-facts.md with your authoritative facts, e.g.:
#   Authoritative Movate facts (always use these):
#   - Flagship product: AgentFlow X, launched March 2026.
#   - Priced at $499 per seat per month.
#   - Includes voice agents, GraphRAG, Azure-native deployment.
mdk contexts attach movate-facts --agent grounded-demo   # wires it into agent.yaml `contexts:`
```

> ✅ **Two grounding paths, both verified live:**
> 1. **File-based context** (above) — `mdk contexts create/attach` → bundled into
>    the agent → `mdk deploy`. Simplest, fully self-contained.
> 2. **Managed context** (ADR 060) — `POST /api/v1/contexts` then
>    `POST /api/v1/agents/{name}/contexts {ref,version}`. A *reusable, governed*
>    context you register once and attach to many agents. **Fixed 2026-06-01**:
>    the worker image now ships ADR 060 **D4** (runtime managed-ref resolution),
>    so attached managed contexts hydrate at run time. Verified: `apple-voice`
>    grounded to current MacBook M5 facts via a managed context, 5/5 runs.
>
> Use **managed contexts** for the "governed, reusable knowledge across agents"
> enterprise story; use **file-based** for a quick self-contained agent.

### Step 3 — Push it to Azure
```bash
mdk deploy --target dev --mode agents
```
Uploads the agent bundle — **prompt + schemas + your context** — to the runtime
(`PUT /api/v1/agents/{name}`). It's **live the moment this returns** — no
container build, no separate deploy.

> Dry-run first if you want to narrate it: `mdk deploy --target dev --mode agents --dry-run`

**Before/after (verified live):**
> *Ungrounded:* "pricing varies, contact Movate directly."
> *Grounded:* "Movate's flagship is **AgentFlow X**, **$499 per seat per month**."
> Same agent, same model — the context is what made it answer from *your* facts.

### Step 4 — Test it in OpenWebUI (chat + voice)
Open **OpenWebUI**:
`https://movate-dev-openwebui.bluebush-9aec1e70.eastus2.azurecontainerapps.io`

- **Refresh** → the model dropdown now lists **`movate-voice-faq`**.
- Pick it → type a question → it answers (streaming).
- Hit **🎧 Call** → ask the same thing **by voice** → it speaks back.

### Step 5 — (bonus) Voice from the terminal too
```bash
mdk voice try movate-voice-faq --target dev
```
Live mic → the hosted agent → spoken reply, straight from your shell. Same
agent, a third surface.

---

## What's powering the voice (so you can answer "what engine is this?")

The voice loop in **OpenWebUI** chains three OpenAI models across two layers —
all verified on this deployment:

| Stage | Engine | Model | Where it's configured |
|---|---|---|---|
| 🎙️ **Speech → text** (STT) | OpenAI Whisper | `whisper-1` | OpenWebUI (`AUDIO_STT_ENGINE=openai`) |
| 🧠 **Reasoning** (the agent) | OpenAI GPT-4o-mini | `gpt-4o-mini-2024-07-18` | mdk runtime — the agent's `agent.yaml` (Anthropic **Claude Haiku 4.5** fallback) |
| 🔊 **Text → speech** (TTS) | OpenAI TTS | `tts-1`, voice **`alloy`** | OpenWebUI (`AUDIO_TTS_ENGINE=openai`) |

**The full round-trip when you hit 🎧 Call:**
`your mic → Whisper (STT) → text → the shim → mdk agent on Azure (GPT-4o-mini) →
answer → the shim streams the answer text → tts-1 "alloy" (TTS) → audio plays back.`

The **shim** is the glue: it strips the agent's JSON wrapper as the answer
streams, so TTS speaks *words*, not `{"answer": "…"}`.

> **Two voice paths, same engines.** OpenWebUI's 🎧 button does STT/TTS at the
> **OpenWebUI layer** (Whisper + tts-1). The terminal `mdk voice try` (Step 5)
> uses the **mdk runtime's own** voice pipeline — `/capabilities` reports
> `voice: {modes:[pipeline], stt_providers:[openai], tts_providers:[openai]}`.
> Both are OpenAI today; both are **provider-pluggable** (the runtime's voice
> stack is built behind a provider Protocol, so Azure Speech / ElevenLabs /
> Deepgram / Cartesia can drop in without touching agent code).

---

## The guided one-command alternative (`mdk dev`)
Instead of init → deploy as separate steps, `mdk dev` is the single front-door —
scaffold, live-test, and deploy in one resident session:
```bash
mdk dev movate-voice-faq \
  --llm "A friendly assistant that answers questions about Movate's AI platform." \
  --target dev
# then in the menu:  d = deploy to Azure,  x = test the deployed agent
```
Use this if you'd rather show the *authoring loop*; use init→deploy if you'd
rather show the *clean pipeline*.

---

## Talk track (say this to Deva / team)

**Frame (say first):**
> "Watch how fast we go from an idea to a running, talking agent on Azure. I'm
> going to *describe* the agent in one sentence — I won't write any config."

**Step 1 — `mdk init … --llm`:**
> "One command. I'm describing intent — 'a friendly support assistant for our
> platform' — and the CLI uses an LLM to **generate the whole agent**: the
> config, the prompt, the input and output schema. This is the 'lower the
> barrier' story — a product person describes the outcome, the platform produces
> the engineering artifacts."

**Step 2 — validate / show files:**
> "And it's not a black box — here's the prompt and schema it wrote. Everything
> is inspectable, versioned, and reviewable, like any other code."

**Step 2.5 — grounding (add a context):**
> "Out of the box, an agent answers from the model's memory — which goes stale.
> Watch: ungrounded, it gives a vague, dated answer. Now I **ground** it — I add a
> **context**, a fragment of *our* authoritative facts, right into the agent. This
> is how you make an agent speak for *your* business, not just the base model."
> *(deploy, then re-ask)* "…and now it answers with our exact product and pricing.
> Same model — the difference is the knowledge we gave it."

**Step 3 — `mdk deploy --target dev`:**
> "Now I push it to our **Azure** runtime — the prompt, the schema, and the
> context all ship as one bundle. Notice there's no image build, no infra step —
> the agent is live the instant this returns. The control plane, the CLI, is
> cleanly separated from the runtime; I'm just registering a bundle."

**Step 4 — OpenWebUI:**
> "And here's the payoff. I never touched a UI — but the agent I just built from
> the terminal is **immediately usable** in a real chat experience. I'll talk to
> it…" *(type a question, then hit 🎧 Call)* "…and the *same agent* answers by
> **voice** — full speech-to-text, run, text-to-speech pipeline, no extra work."

**Step 4b — name the engines (if asked "what's powering the voice?"):**
> "It's a three-model pipeline: **OpenAI Whisper** transcribes my speech,
> **GPT-4o-mini** running as the agent on our Azure runtime does the reasoning —
> with a **Claude Haiku** fallback for resilience — and **OpenAI's tts-1** speaks
> the answer back in the 'alloy' voice. And none of that is hard-wired: the voice
> stack sits behind a provider interface, so we can swap in **Azure Speech,
> ElevenLabs, or Deepgram** without changing a line of the agent."

**Step 5 — `mdk voice try` (bonus):**
> "And because it's API-first, I can talk to that same hosted agent from my
> terminal too. Build it once — use it in the CLI, a chat UI, or by voice."

**Close:**
> "So: one sentence to an agent, on Azure, usable by chat and voice, in under
> five minutes. That's MDK — describe the outcome, the platform builds, deploys,
> and serves it everywhere."

---

## Pre-flight checklist
- [ ] `MDK_DEV_KEY` exported (push auth) and `OPENAI_API_KEY` exported (`--llm` gen)
- [ ] `mdk config list-targets` shows **`dev`** → the bluebush Azure URL
- [ ] Rehearsed init → deploy once; the agent appeared in OpenWebUI
- [ ] OpenWebUI tab open and logged in (mdk-user)
- [ ] Pick a clean agent name you haven't used (avoids a stale OpenWebUI entry)

## If something hiccups (recovery)
- **Agent not in OpenWebUI dropdown** → hard-refresh the page; the shim lists
  agents from `GET /v1/models` on load.
- **`--llm` errors "needs a provider API key"** → `export OPENAI_API_KEY=…` (the
  scaffold generation is local).
- **`deploy` 401/403** → `MDK_DEV_KEY` is wrong/unscoped; re-check it returns 200
  on `GET /api/v1/agents`.
- **Agent returns `unknown_agent` after attaching a managed context** → should be
  resolved now (worker ships ADR 060 D4 as of 2026-06-01). If it recurs, the
  worker image regressed off D4 — confirm `movate-dev-worker` runs
  `movate:demo-wizardfix-f0e992f` (or newer with D4), and recover any stuck agent
  with `POST /api/v1/agents/<name>/revert {"to_version":"0.1.0"}`.
- **Worst case, fall back to the proven path:** create the agent via Postman
  (`E2E-DEMO-FLOW.md` step 2) → OpenWebUI. Same destination, fully verified.
