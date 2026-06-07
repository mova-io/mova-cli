# Movate MDK — Live API Demo Script

A presenter-ready walkthrough of the MDK runtime API, run live in Postman against the
**hosted Azure deployment**. Every step pairs the **click** with a **talk track** (what to
say) and the **value** (why it matters) so a mixed room of engineers and stakeholders
follows both the mechanics and the story.

- **How to read this:** each step has **Say** (your line), **Do** (the click), **Point out**
  (what to highlight on screen), and **Why it matters** (the value). Drive Postman top-to-bottom.
- **Reference tables** (endpoints, scopes, expected codes) are preserved in the
  [Appendix](#appendix--endpoint-reference).
- **Live runtime:** `https://movate-dev-api.bluebush-9aec1e70.eastus2.azurecontainerapps.io`

---

## The 90-second framing (say this before you click anything)

> "What you're about to see is the *same* runtime our product front-end talks to — no special
> demo backend, no mocked responses. Every button I click is a real HTTP call against a live
> deployment on Azure. The headline is this: with MDK, an AI agent is a **managed, governed
> cloud resource** — you create it, validate it, give it knowledge, run it, and watch it, all
> through one consistent API. I'm using Postman so you can see the raw calls, but a web app,
> a CI pipeline, or another service would make the exact same requests."

**The arc of the demo** — keep returning to it so the room never loses the thread:

1. **Create** an agent from a form (no code) → 2. **Validate** it before it ships →
3. Give it **knowledge** → 4. Add a reusable **skill** → 5. **Publish** a version →
6. **Run** it → 7. **Monitor** what happened.

That's the full lifecycle of an enterprise AI agent in seven moves.

---

## 0. One-time setup (do this before the room is watching — ~2 min)

1. **Import** into Postman:
   - Collection → `movate-core-flow.postman_collection.json`
   - Environment → `movate-azure.postman_environment.json` *(then select it, top-right)*
2. **Mint a bearer key** (once, in a terminal with `az` logged in):
   ```bash
   TENANT=$(uuidgen | tr 'A-Z' 'a-z')
   az containerapp exec -g movate-dev-rg -n movate-dev-api \
     --command "movate auth create-key --tenant-id $TENANT --env live \
       --scope read --scope run --scope admin --scope kb:write --scope eval \
       --label demo"
   ```
   Paste the `mvt_live_…` string into the environment's **`bearer_token`** variable and **Save**.
   > Scopes matter: `admin` alone can't *read*. Use the full set above or many GETs return `403`.
3. **Smoke test:** send **`GET /api/v1/capabilities`** → expect `200`. Leave the response on
   screen — it's your opening visual (see Step 0a).

**Presenter note:** the test scripts in the collection auto-capture IDs (`agent_name`,
`run_id`, `job_id`, …) into environment variables, so each request feeds the next. You just
keep clicking **Send**.

**Attaching files in Postman (you'll do this in steps 1b, 1c, 3a):** open the request's
**Body** tab → it's set to **form-data** → in the row you need, click the value-column
dropdown and choose **File** → click **Select files** → pick the file from the `postman/`
folder. The exact files per step are listed inline below. *(Tip: pre-attach them before the
demo so you're not file-browsing on stage.)*

---

## 1. The core flow — the seven-move story

### Step 0a — Capabilities (your opening slide)

- **Do:** Send `GET /api/v1/capabilities`.
- **Say:** "Before I do anything, the runtime tells me what it can do — the models it can
  reach, the features that are live, my tenant's limits. A client app uses this to render
  the right UI; I'm using it to prove we're talking to a real, fully-featured deployment."
- **Point out:** `voice.enabled: true`, the managed `resources` catalog, the model list.
- **Why it matters:** **Self-describing API.** Integrators don't guess what's available or
  hard-code assumptions — the platform advertises its own capabilities. That's what lets a
  front-end and a runtime upgrade independently.

### Step 0b — Whoami (trust & governance)

- **Do:** Send `GET /api/v1/auth/me`.
- **Say:** "This is who I am to the system: my tenant, my scopes, my key. Every call from
  here is authenticated and scoped — `read`, `run`, `admin`, `kb:write`. Nothing is anonymous."
- **Point out:** the `scopes` array and `tenant_id`.
- **Why it matters:** **Multi-tenant security is built in, not bolted on.** Each customer is
  isolated; each key carries least-privilege scopes. This is the table-stakes question every
  enterprise buyer asks, answered in the first 30 seconds.

### Step 1a — Create an agent from a form (the "no code" moment)

- **Do:** Send `POST /api/v1/agents/from-wizard`. Expect **201 Created**.
- **Say:** "I'm creating a working agent from nothing but structured fields — a name, a role,
  a prompt, a model. No files, no repo, no deploy pipeline. The runtime takes this form and
  synthesizes a canonical agent definition on the server."
- **Point out:** the response — `agent_dir: "faq-bot"`, `files_persisted: ["agent.yaml",
  "prompt.md"]`. "Notice it generated the `agent.yaml` and `prompt.md` *for me*, server-side,
  and persisted them durably — this agent now exists in the platform, not on my laptop."
- **Why it matters:** **Time-to-first-agent is minutes, and it's the same path our product
  wizard uses.** A business user in the UI and a developer hitting the API land in the exact
  same place. That's the "fields → live agent" promise made concrete.

### Step 1b — Create an agent from a packaged bundle (the "pro" path)

- **Do:** `POST /api/v1/agents`. In **Body → form-data**, attach `sample-agent-bundle.zip`
  to the `bundle` field. Expect `201`.
- **Say:** "Same destination, different on-ramp. When a team has already built an agent — prompt,
  schemas, evals, knowledge — they ship the whole package in one upload."
- **Point out:** the bundle persists skills *and* the agent together in one call.
- **Why it matters:** **Meets teams where they are.** Form-builders and power users are both
  supported by one API, so the platform scales from first experiment to production authoring.

### Step 1c — Edit the agent (versioning, not overwriting)

- **Do:** `PUT /api/v1/agents/{name}` — this one **uploads the edited files from disk**:
  1. Open the **Body** tab (it's already set to **form-data**).
  2. Each row's value column has a dropdown — make sure it's set to **File** (not Text).
  3. Click **Select files** on each row and pick the matching file from
     `postman/sample-agent-edit/`:
     - `agent_yaml` → `agent.yaml`
     - `prompt` → `prompt.md`
     - `input_schema` → `schema/input.json`
     - `output_schema` → `schema/output.json`
  4. Click **Send**. Expect `200`.
  > These files **are** the change — the edit lives in them (e.g. the new
  > description "v2 — friendlier, cites sources"). The `agent.yaml` targets
  > **faq-bot**; if your agent has a different name, change its `name:` line first.
- **Say:** "To edit, I update the agent's files and push them back — and the
  platform doesn't overwrite it, it cuts a new **immutable version**."
- **Point out:** the version bump in the response.
- **Why it matters:** **Auditability and rollback.** You always know what changed, when, and
  can revert. Critical when an agent is making customer-facing decisions.

### Step 2 — Validate before you ship

- **Do:** `POST /api/v1/agents/{name}/validate`. Expect `200`.
- **Say:** "Before this agent ever talks to a customer, the platform lints the prompt and
  forecasts what a run will cost. This is the pre-flight check."
- **Point out:** `passed`, any `warnings`, and the `cost_forecast`.
- **Why it matters:** **Shift-left quality and cost control.** You catch a broken prompt or a
  runaway cost *before* deploy — not from an angry customer or a surprise invoice.

### Step 3 — Give the agent knowledge (RAG in three calls)

- **3a · Ingest** — `POST /api/v1/agents/{name}/kb` (**file upload**): **Body** tab →
  **form-data** → on the **`files`** row set the dropdown to **File** → **Select files** →
  choose `postman/sample-faq.md` (or any `.md`/`.pdf`). **Send**. Expect `200`.
  - **Say:** "I'm uploading a document. The platform chunks it, embeds it, and stores it as
    the agent's private knowledge base — and, behind the scenes, extracts a knowledge graph
    from it (more on that later)."
- **3b · Stats** — `GET …/kb/stats`. **Say:** "Confirming the content actually landed —
  here are the chunks and sources."
- **3c · Search** — `POST …/kb/search`. **Say:** "And here's the semantic retrieval that will
  ground the agent's answers — ask a question, get back the most relevant passages with scores."
- **Point out:** the scored `results[]` in 3c — "this is *why* the answer in a moment will be
  grounded and not hallucinated."
- **Why it matters:** **Grounded answers, per-agent and per-tenant.** Knowledge is isolated to
  the agent that owns it. This is how you get accurate, source-backed responses instead of a
  generic chatbot.

### Step 3d — Update the knowledge (the clean-replace move)

> Optional but great: shows the knowledge is *managed*, not write-once.

- **Do (3 clicks):**
  1. **Edit** `postman/sample-faq.md` — change a fact (e.g. refund window `30 days` → `60 days`)
     and save the file.
  2. **Clear** the old knowledge — run **`3d. Clear KB (clean replace)`**
     (`DELETE /api/v1/agents/{name}/kb`). Expect `200`.
  3. **Re-ingest** — re-run **3a** (re-attach the edited `sample-faq.md` → Send).
  - *(Then re-run the agent in Step 6 to show the answer is now "60 days".)*
- **Say:** "Knowledge isn't write-once — it's managed. I edit the source, **clear** the old
  version, and re-ingest, and the agent immediately answers from the new facts. I can also
  replace a single document with `?source=faq.md` instead of clearing everything."
- **Point out:** before clearing, search shows the old fact; after clear + re-ingest, search
  shows **only** the new one.
- **Why it matters:** **Knowledge lifecycle, not a dump.** You can correct, replace, or expire
  what an agent knows — the difference between a governed enterprise knowledge base and a
  one-shot upload. ⚠️ *Note: re-uploading **without** clearing first **appends** (old + new both
  remain) — clearing is what makes it a clean replace.*

### Step 4 — Register a reusable skill (the #650 story)

- **Do:** `PUT /api/v1/skills/{name}` (JSON, no file upload). Expect `201`.
- **Say:** "Skills are capabilities — a lookup, a calculation, an API call — that *any* agent
  can reference. They're first-class, versioned, tenant-scoped resources, not code copy-pasted
  into each agent."
- **Point out:** the skill is now in the managed catalog; agents attach it by name.
- **Why it matters:** **Build once, reuse everywhere, govern centrally.** Update a skill in one
  place and every agent using it benefits. This is the difference between a platform and a pile
  of one-off scripts.

### Step 5 — Publish a version

- **Do:** `POST /api/v1/agents/{name}/publish`. Expect `200` (or `503` if GitHub publishing is
  off on this runtime — say so, it's expected on some demo deployments).
- **Say:** "Publishing promotes the agent to a released version — the platform records it as a
  commit, so there's a durable, traceable history of what went live and when."
- **Why it matters:** **Governed release process.** Promotion is deliberate and recorded, not
  a silent change to a running system.

### Step 6 — Run the agent (the payoff)

- **Do:** `POST /api/v1/agents/{name}/runs`. Expect `200` (inline with `?wait=true`) or `202`
  with a `job_id` (async).
- **Say:** "Now I actually ask the agent a question. Watch — it retrieves from the knowledge
  base we just loaded and gives a grounded answer." *(If async:)* "On Azure this is queued to a
  separate worker pod — the API stays responsive while the work happens elsewhere. I get a
  `job_id` back immediately."
- **Point out:** the grounded `output`. Tie it back: "That answer came from the doc in Step 3."
- **Why it matters:** **This is the product.** Everything before this was setup; this is the
  agent doing its job — accurately, on managed infrastructure, at scale.

### Step 7 — Monitor what happened (the trust close)

- **7a · Get run** — `GET /api/v1/runs/{id}`. **Say:** "The full record — status, timing, cost,
  output. Every run is persisted."
- **7b · Trace** — `GET /api/v1/runs/{id}/trace`. **Say:** "And the trace — the prompt, the model
  calls, the retrieval, the tool invocations. When something looks off, you can see *exactly*
  what the agent did."
- **7c · Report** — `GET /api/v1/report`. **Say:** "Zoom out: pass-rates, cost over time,
  latency percentiles, top failing cases — across every agent in the tenant."
- **Why it matters:** **Observability is the difference between a demo and production.**
  Enterprise AI fails operationally, not just logically. You can answer "is it working, what
  does it cost, and why did *that* answer happen" — for one run or the whole fleet.

> **Async pattern (runs & evals):** a `202` returns a `job_id`. Poll
> `GET /api/v1/jobs/{job_id}` until `status: succeeded`, then read the result via
> `GET /runs/{result_run_id}`. The test scripts capture the IDs, so just send the next request.
> **Talk track:** "This same queue-and-poll pattern is how every long-running operation works —
> it's why the platform stays responsive under load."

---

## 2. Going deeper (optional modules — pick based on the room)

Run these after the core flow if you have time or the audience leans technical. Each is a
self-contained "and it also does this" beat.

### Managed skills & contexts — "agents are composed, not hard-coded"

- **Talk track:** "Agents reference skills and contexts as shared, versioned resources. Update
  the resource once; every agent that uses it gets the change. That's central governance over a
  fleet of agents."
- **Show:** `GET /api/v1/skills` (the catalog), `GET /api/v1/skills/{name}/versions` (history),
  `POST /api/v1/agents/{name}/skills` (attach).
- **Value:** reuse, consistency, and one place to audit or retire a capability.

### Voice — "any agent can talk, with no prompt changes"

- **Talk track:** "The runtime wraps any text agent with speech-to-text and text-to-speech. The
  agent's logic is untouched — voice is added at the edges. The same `faq-bot` you just built can
  now hold a spoken conversation."
- **Show (fast, no mic):** `POST /api/v1/agents/{name}/voice` with form-data `text = What does
  Movate do?` → returns transcript + spoken-reply audio. For the full experience, the WebSocket
  endpoint streams full-duplex.
- **Value:** **omnichannel from one agent definition** — chat, API, and voice without rebuilding.
- **Gotcha to mention:** use a conversational agent (`faq-bot`, `movate-assistant`, `rag-qa`),
  not a structured one. Spoken brand names can mis-transcribe ("Movate" → "Move 8") — the `text`
  path is exact, so prefer it for a clean demo.

### Async eval — "quality is measured, not assumed"

- **Talk track:** "Evals score an agent against a dataset — pass-rate, per-case results, a
  gate verdict. It runs asynchronously on a worker, exactly like a production batch would."
- **Show:** `POST /api/v1/agents/{name}/evals` → `job_id`; poll `GET /jobs/{job_id}`; read
  `GET /api/v1/evals/{eval_id}`.
- **Value:** **regression-proof AI.** You can prove an agent got better (or didn't) before
  shipping a change — the CI test suite for prompts.

### Knowledge graph (GraphRAG) — "the platform understands relationships, not just text"

- **Talk track:** "When we ingested that document, the platform didn't just store text chunks —
  it extracted entities and relationships into a knowledge graph. That powers deeper, connected
  retrieval and lets you literally see how concepts relate."
- **Show:** `GET /api/v1/projects/{id}/graph` (subgraph), `GET /api/v1/graph/search?q=…`,
  `GET …/graph/analytics/centrality` (most-connected concepts).
- **Value:** **answers that reason over connections**, plus an explorable map of the knowledge
  — a differentiator over plain vector search.
- **Gotcha:** the graph is empty until content is ingested. Run Step 3 first.

### Stateful sessions — "agents remember the conversation"

- **Talk track:** "A session threads multiple runs into one conversation with memory across
  turns and a per-session cost rollup."
- **Show:** `POST /api/v1/sessions` → run with `session_id` → `GET /api/v1/sessions/{id}` (the
  growing history).
- **Value:** **real multi-turn assistants**, with cost attributed per conversation.

---

## Running it smoothly (presenter cheat sheet)

- **Run top-to-bottom.** Test scripts chain the IDs; you just keep clicking **Send**.
- **Re-runs:** the collection's `0. Reset` (`DELETE /agents/{name}`) clears the demo agent. A
  `409` on create means it already exists — harmless on a replay, just acknowledge and move on.
- **Pick conversational agents** for run/voice; structured agents (`ticket-triager`) need typed
  input and will look broken if you give them a sentence.
- **Bundle uploads (1b/1c):** attach `postman/sample-agent-bundle.zip`.
- **If a GET returns `403`:** your key is missing a scope — re-mint with the full scope set
  from setup. (Good recovery line: "scopes are enforced per call — that's the security working.")
- **If publish returns `503`:** GitHub publishing is disabled on this runtime. Expected on some
  demo deployments — say "promotion is wired; this runtime just isn't connected to a repo."

---

## Appendix — endpoint reference

### A. Core flow (run in order)

| # | Request | Method · Path | What it shows | Expect |
|---|---|---|---|---|
| 0 | **Capabilities** | `GET /capabilities` | what the runtime supports | 200 |
| 0 | **Whoami** | `GET /auth/me` | confirms your key + scopes | 200 |
| 0 | **Reset** | `DELETE /agents/{name}` | idempotent re-run (deletes the demo agent) | 200 / 404 |
| 1a | **Create agent (wizard)** | `POST /agents/from-wizard` | structured fields → a live agent (`agent.yaml` + `prompt.md`) | **201** |
| 1b | **Create agent (bundle)** | `POST /agents` | upload a packaged agent — attach **`sample-agent-bundle.zip`** (Body → form-data → `bundle`) | 201 |
| 1c | **Edit agent** | `PUT /agents/{name}` | update the agent → version bumps | 200 |
| 2 | **Validate** | `POST /agents/{name}/validate` | static checks + cost forecast before deploy | 200 |
| 3a | **KB ingest** | `POST /agents/{name}/kb` | load knowledge into the agent | 200 |
| 3b/c | **KB stats / search** | `GET …/kb/stats`, `POST …/kb/search` | retrieval over the KB | 200 |
| 4 | **Register skill** | `PUT /skills/{name}` | a **managed skill** (JSON, no file) | 201 |
| 5 | **Publish** | `POST /agents/{name}/publish` | cut a published version | 200 / 503 |
| 6 | **Run (inline)** | `POST /agents/{name}/runs` | execute the agent → output or `job_id` | 200 / 202 |
| 7 | **Monitor** | `GET /runs/{id}`, `…/trace`, `GET /report` | run record, trace, aggregate report | 200 |

> **Async note:** runs and evals return a `job_id`. Poll **`GET /jobs/{job_id}`** until
> `status: succeeded`, then read the result run via `GET /runs/{result_run_id}`. Test scripts
> auto-capture the ids into the environment, so just run the next request.

### B. Managed skills & contexts (the #650 story)

Agents reference **skills** and **contexts** as first-class, tenant-scoped, versioned resources
— not files baked into a bundle.

```
PUT  /api/v1/skills/{name}            register a skill (JSON: version, files, description)
GET  /api/v1/skills                   list skills
GET  /api/v1/skills/{name}/versions   version history
POST /api/v1/agents/{name}/skills     attach a skill to an agent
DELETE /api/v1/skills/{name}          retire it

POST /api/v1/contexts                 register a context (name, body, description, version)
GET  /api/v1/contexts                 list contexts
PUT  /api/v1/contexts/{name}          new version
POST /api/v1/agents/{name}/contexts   attach a context to an agent
```

A valid `skill.yaml` for the JSON register path:
```yaml
api_version: movate/v1
kind: Skill
name: faq-lookup
version: 0.1.0
description: Looks up FAQ answers.
schema:
  input: { query: string }
  output: { result: string }
implementation:
  kind: python
  entry: impl:run
```

### C. Voice

- **REST one-shot:** `POST /agents/{name}/voice` (multipart). Fields: `audio` (file) **or**
  `text` (shortcut), plus `stt`, `tts`, `voice_id`, `language`, `input_key`. Returns
  `transcript`, `response_text`, `audio_bytes_b64`. Quick test: form-data `text = What does
  Movate do?` → 200.
- **WebSocket:** `WS /agents/{name}/voice` — full-duplex pipeline.

### D. Async eval

```
POST /api/v1/agents/{name}/evals     kick off an eval  → job_id
GET  /api/v1/jobs/{job_id}           poll until succeeded
GET  /api/v1/evals/{eval_id}         the scorecard
```

### E. Knowledge graph (GraphRAG)

Built at KB-ingest time (entity/relation extraction). Read it via:

```
GET  /api/v1/projects/{id}/graph                        windowed subgraph
GET  /api/v1/graph/search?q=…                           semantic search
GET  /api/v1/graph/nodes/{id}/neighbors                 expand a node
GET  /api/v1/projects/{id}/graph/analytics/centrality   most-connected nodes
GET  /api/v1/projects/{id}/graph/analytics/communities  clusters
GET  /api/v1/projects/{id}/graph/stream                 live node/edge events
```

> The graph is empty until content is ingested. Ingest KB content first, then these return
> real nodes/edges.

### F. Stateful sessions

```
POST /api/v1/sessions                 start a session
POST /api/v1/agents/{name}/runs       run with session_id → memory across turns
GET  /api/v1/sessions/{id}            history
DELETE /api/v1/sessions/{id}          end it
```

---

## What this collection covers vs. the full API

The collection drives the **core lifecycle** (≈33 of 126 routes). Also exposed by the runtime,
not yet in the collection — the recommended next additions for a complete tour:

- **Managed contexts CRUD** (`/contexts`) — only *attach* is wired
- **Full skills lifecycle** (list / versions / attach / delete)
- **REST voice** (`POST …/voice`) — only the WS variant is in the collection
- **Agent versioning** (`/versions`, `/history`, `/revert`), **canary** deploys
- **Observability NL-query** (`POST /observability/ask`), insights, troubleshoot
- **Graph analytics** (centrality / communities / path)
- **Workflows API** (`/workflows` — create / validate / publish / versions)
- **Auth key management**, **jobs** (cancel / dead-letter), **catalog**, **bench**
