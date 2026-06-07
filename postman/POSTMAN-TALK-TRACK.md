# Movate MDK — Postman demo + talk track

A demo script for Deva / the team. Each beat: **[CLICK]** what to run,
**[SAY]** the narration (value), **[POINT]** what to highlight in the response.
Runs live against Azure. ~10–12 min for the full arc.

**The one-line frame (say this first):**
> "MDK is our framework + runtime for building, evaluating, and deploying AI
> agents — and it's **API-first**, so everything you'd do in a UI, you can do
> programmatically and embed in a product or a pipeline. Let me build an agent
> live, on Azure, end to end."

---

## 1 · Capabilities — "what can this runtime do?"
**[CLICK]** `Capabilities → GET /capabilities`
**[SAY]** "Before anything, the runtime tells you what it supports — the models
available, that voice is on, and the managed resource types. It's
self-describing, so a client never has to guess."
**[POINT]** `voice.enabled: true`, the `models` list, `resources` catalog.

## 2 · Create an agent — "describe it, and MDK builds it"
**[CLICK]** `Core Flow → 1a. Create agent (from wizard JSON)`
**[SAY]** "I don't hand-write config. I **describe** the agent I want — its role,
goal, the prompt, the model — as plain structured fields. MDK takes that and
**scaffolds the whole agent for me**: it generates the canonical `agent.yaml`
and the `prompt.md`, wires the input/output schema, and registers it on the
runtime. One call, and the agent is live on Azure."
**[POINT]** the `201`, and `files_persisted: ["agent.yaml", "prompt.md"]` —
"those are the files it wrote for me. I described intent; it produced the
artifacts."
> Value: **lower the barrier to building agents** — product teams describe
> outcomes, the platform handles the engineering shape + governance.

## 3 · Edit the agent — "every change is versioned"
**[CLICK]** `Core Flow → 1c. Edit agent (PUT bundle)`
**[SAY]** "I can update the agent and it cuts a new version — nothing is
overwritten in place. So I get a full history and can roll back."
**[POINT]** the version bump. *(Mention `GET /agents/{name}/versions`.)*

## 4 · Give it knowledge (KB) — "ground it in our content"
**[CLICK]** `Core Flow → 3a. KB ingest`
**[SAY]** "Now I feed it knowledge — a doc, a FAQ, a URL. MDK chunks it, embeds
it, and stores it for retrieval. The agent answers grounded in **our** content,
not just the model's training. And ingesting also **builds a knowledge graph**
of the entities and relationships in that content."
**[POINT]** `chunks_saved`, the embedding model. *(Then `…/kb/search` to show
retrieval.)*

## 5 · Validate — "a quality gate before it ships"
**[CLICK]** `Core Flow → 2. Validate agent`
**[SAY]** "Before I'd ever promote this, I validate it — static checks on the
bundle. This is the kind of gate you'd wire into CI so a bad agent never ships."
**[POINT]** the `200`/validation result.

## 6 · Managed skills & contexts — "reusable, versioned, governed"
**[CLICK]** `Skills & Contexts (managed) → C1 register context`, then `S1 register skill`, then `A1/A2 attach`
**[SAY]** "Skills and contexts aren't files buried in one agent — they're
**first-class, tenant-scoped, versioned resources**. I register a skill once and
attach it to any agent by reference. So a platform team can govern an approved
library of tools and knowledge, and every agent inherits updates. This is how
you scale agents across an org without copy-paste."
**[POINT]** the `201`s, then the `attach` calls binding them to the agent.
> Value: **governance + reuse** — the enterprise story, not a one-off bot.

## 7 · Run it — "execute, async, on Azure"
**[CLICK]** `Core Flow → 6. Run agent (inline)`
**[SAY]** "Now I run it. Notice it returns a **job id** — runs are asynchronous
and durable on the Azure backend, so this scales to long or bulk work, not just
a quick request. I poll the job, then read the result."
**[POINT]** the `job_id`; then `GET /jobs/{id}` → `GET /runs/{id}` for the answer.
> Value: **production execution model** — async, durable, observable.

## 8 · Evaluate — "prove quality, don't eyeball it"
**[CLICK]** `Async Eval → E1 kick off`, `E2 poll`, `E3 scorecard`
**[SAY]** "Quality isn't a vibe. I run an **eval** against a dataset and get a
scorecard — pass rate, where it failed. This is the loop that lets you ship
agents with confidence and catch regressions automatically."
**[POINT]** the scorecard.
> Value: **eval-in-the-loop** — measurable quality, CI-gateable.

## 9 · Observability — "see what every agent is doing"
**[CLICK]** `GET /report`, then `GET /runs/{id}/trace`, then `POST /observability/ask`
**[SAY]** "Operationally, I get an aggregate **report** — runs, cost, health —
and a **step-by-step trace** of any run. And I can literally **ask in plain
English** — 'how are my agents doing?' — and the runtime answers from its own
telemetry."
**[POINT]** the report numbers, the trace steps, the NL answer.
> Value: **enterprise observability** — cost, traces, and natural-language ops.

## 10 · Knowledge graph — "the GraphRAG layer"
**[CLICK]** `Graph Analytics → GET /projects/{id}/graph`, `…/analytics/centrality`
**[SAY]** "Remember the KB ingest built a graph — here it is. Entities and
relationships, with analytics like the most-connected concepts. This is the
GraphRAG layer that makes retrieval smarter than flat search."
**[POINT]** nodes/edges, centrality.

## 11 · Voice — "the same agent, by voice"
**[CLICK]** `Voice → REST voice (one-shot)` *(form-data `text` field)*
**[SAY]** "Every agent is also a **voice agent** — same runtime, full pipeline:
speech-to-text, run the agent, text-to-speech. One call in, transcript + spoken
audio back."
**[POINT]** `transcript`, `response_text`, `audio_bytes_b64`.

---

## 12 · Now the payoff — "the same agent, instantly in a chat UI"
**[SWITCH]** to OpenWebUI:
`https://movate-dev-openwebui.bluebush-9aec1e70.eastus2.azurecontainerapps.io`
**[SAY]** "Everything I just built via the API is **immediately usable** in a
real chat experience — no extra deployment. Here's the agent I created in the
dropdown. I'll just talk to it." *(Pick the agent → ask a question → hit 🎧 Call
for voice.)*
**[POINT]** the agent answering, streaming, speaking.
> Value: **API-first, surface-agnostic** — build once, use it in Postman, a
> chat UI, voice, or embedded in a product.

## 13 · And the dashboards — "all of it is observable, publicly"
**[SWITCH]** to Grafana:
`https://movate-dev-grafana-oss.bluebush-9aec1e70.eastus2.azurecontainerapps.io`
**[SAY]** "And all of this runtime activity flows into live dashboards on Azure
Monitor — requests, latency, resource use — so ops has eyes on the fleet."

---

## Closing line (say this)
> "So in ten minutes, on Azure: I **described** an agent and the platform built
> it, I **grounded** it in our knowledge, attached **governed** skills and
> contexts, **ran** and **evaluated** it, watched it through **traces and
> dashboards**, and used the exact same agent by **chat and voice** — all
> through one API. That's MDK: the operating layer for building and running
> agents at enterprise scale."

---

### Quick demo order (cheat strip)
Capabilities → **1a create** → 1c edit → 3a KB → 2 validate → skills/contexts
register+attach → 6 run → eval → report/trace/ask → graph → voice →
**OpenWebUI (chat + voice)** → Grafana.
