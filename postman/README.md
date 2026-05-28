# Movate — Core Flow Postman demo

A live, top-to-bottom Postman demo of the `mdk` (`movate-cli`) runtime
`/api/v1` HTTP surface against a **deployed** Azure Container Apps runtime. It
walks the core mdk features — create (init/add), validate, KB, skills, deploy
(publish), run, monitor — as real HTTP calls so the team can demo the platform
without the CLI.

Files in this folder:

| File | What it is |
|---|---|
| `movate-core-flow.postman_collection.json` | The collection: a "Movate — Core Flow" folder of requests **ordered for a live demo**, with example bodies, descriptions, and chaining test scripts. |
| `movate.postman_environment.json` | Environment **template** — `runtime_url`, `bearer_token` (empty placeholder), `agent_name`, `run_id`. |

> Request bodies + scopes here mirror `src/movate/runtime/app.py` and
> `src/movate/runtime/schemas.py`, cross-checked against the audit in
> [`../docs/front-end-api.md`](../docs/front-end-api.md). The hermetic test
> `tests/test_postman_collection.py` asserts every request path in the
> collection maps to a real route on the app, so the demo can't drift onto a
> dead endpoint.

## Prerequisites

1. **A deployed runtime URL.** An Azure Container Apps runtime serving
   `/api/v1` (stand it up with `mdk deploy --mode runtime`). You need its base
   URL, e.g. `https://movate-runtime.<region>.azurecontainerapps.io`.
2. **A scoped bearer token** with **`admin` + `read`** (the run/KB steps also
   exercise `run` and `kb:write`). Mint it server-side with the operator CLI —
   it is **never** pasted into a committed file:
   ```
   mdk auth create-key --tenant-id <uuid> --scope admin,read,run,kb:write --quiet
   ```
   (Omitting `--scope` yields the legacy default `{read, run, eval}` — **not**
   admin. A `fleet-admin` key also passes every check if you have one.) Run this
   against the runtime's storage; in Azure you typically exec it inside the
   Container App (`az containerapp exec ... -- mdk auth create-key ...`). The
   key is shown **once** — copy it immediately.
3. **A published agent, or create it live.** The demo creates `faq-bot` in
   step 1a; if you'd rather demo against an existing agent, set `agent_name` in
   the environment to its name and skip 1a/1b.
4. **KB ingested** (for the grounded answer). Step 3a uploads a doc; have a
   small `.md`/`.txt`/`.pdf` handy (e.g. a product FAQ).

## Import

1. **Import the collection:** Postman → *Import* → drop
   `movate-core-flow.postman_collection.json`.
2. **Import the environment:** *Import* → `movate.postman_environment.json`,
   then select it in the environment dropdown (top right).
3. **Set the two values** in the environment:
   - `runtime_url` → your deployed base URL (no trailing slash).
   - `bearer_token` → the key from `mdk auth create-key` (it is a `secret`-type
     variable — Postman masks it and it does not sync). **Never** hardcode the
     token in the collection; collection-level auth reads `{{bearer_token}}`.
4. Leave `agent_name` (defaults to `faq-bot`) and `run_id` (filled in by a test
   script during the run) as-is.

Then run the **Movate — Core Flow** folder top-to-bottom (Postman *Runner*, or
send requests one at a time). The `test` scripts chain the flow: step 1a writes
`agent_name`, step 6 writes `run_id`, and the monitor steps read it back.

> **Full typed API:** Postman can also import the live spec directly —
> *Import* → *Link* → `{{runtime_url}}/api/v1/openapi.json`. That generates a
> request for **every** `/api/v1` route (75+). This curated collection is the
> *demo narrative*; the OpenAPI import is the *complete reference*.

## Demo narrative (core feature → endpoint → CLI equivalent)

| # | Request | Endpoint | Feature it demos | CLI equivalent | Scope |
|---|---|---|---|---|---|
| 0 | Whoami | `GET /api/v1/auth/me` | Token + scopes are valid | `mdk auth whoami` | — (auth) |
| 1a | Create agent (wizard) | `POST /api/v1/agents/from-wizard` | init — structured create incl. a skill + a context | `mdk init` (agent half) | `admin` |
| 1b | Create agent (bundle) | `POST /api/v1/agents` | init/add — pre-built bundle upload | `mdk deploy --mode agents` | `admin` |
| 2 | Validate | `POST /api/v1/agents/{name}/validate` | validate — prompt lint + cost forecast | `mdk validate` | `read` |
| 3a | KB ingest | `POST /api/v1/agents/{name}/kb` | KB — ingest a doc | `mdk kb ingest` | `kb:write` |
| 3b | KB stats | `GET /api/v1/agents/{name}/kb/stats` | KB — confirm chunks landed | `mdk kb stats` | `read` |
| 3c | KB search | `POST /api/v1/agents/{name}/kb/search` | KB — semantic retrieval | `mdk kb search` | `read` |
| 4 | Register skill | `POST /api/v1/skills` | skills — register/replace a skill | `mdk skills add` | `admin` |
| 5 | Publish | `POST /api/v1/agents/{name}/publish` | deploy — promote the agent (GitHub commit) | `mdk deploy` (agent sense) | `admin` |
| 6 | Run | `POST /api/v1/agents/{name}/runs?wait=true` | run — grounded answer | `mdk run` | `run` |
| 7a | Get run | `GET /api/v1/runs/{run_id}` | monitor — fetch run + output | `mdk runs get` | `read` |
| 7b | Run trace | `GET /api/v1/runs/{run_id}/trace` | monitor — reconstructed trace | `mdk runs trace` | `read` |
| 7c | Aggregate report | `GET /api/v1/report` | monitor — cross-agent rollup (pass-rate / cost / latency) | `mdk report` | `read` |

**Suggested live ordering note:** the wizard body in 1a references a skill
(`web-search` in `mcp_connectors`) and a context (`product-docs` in
`knowledge_store`). An agent that declares `skills: [...]` / `contexts: [...]`
**422s** if those don't already exist on the runtime. So either (a) run **step
4** (register the skill) *before* 1a and ensure the context exists, or (b) drop
the `mcp_connectors` / `knowledge_store` arrays from the 1a body for a
no-dependency create and demo skills purely via step 4.

## CLI ↔ API caveats (important for the narrative)

The CLI verbs don't map one-to-one onto runtime endpoints — the control plane
(`cli`) and execution plane (`runtime`) are deliberately separate
(CLAUDE.md rule 6). Call these out when demoing:

- **`init` = create-in-registry, not local scaffold.** `mdk init` on the
  operator's disk *also* writes `project.yaml` + `agents/<name>/` locally —
  that filesystem authoring half is **CLI/ops-only** and has no HTTP route. The
  endpoint half (`POST /agents/from-wizard` / `POST /agents`) is the runtime
  "make this agent exist on the server" operation.
- **`deploy` is overloaded.** Step 5 (`POST .../publish`) is the *agent
  promotion* sense — push the bundle to GitHub as a commit. The *infra* sense —
  build the runtime image and roll Azure Container Apps — is
  **`mdk deploy --mode runtime`**, which shells out to `az`/bicep and stays
  CLI/ops-only (it provisions the thing that *serves* `/api/v1`, so it can't
  live behind it). `mdk deploy --mode agents` *does* talk to the runtime — it
  POSTs bundles to `POST /api/v1/agents` (step 1b).
- **No server-side LLM authoring.** There is **no** `/api/v1` endpoint that
  LLM-generates an agent from a free-text description (the `mdk init --llm`
  magic). Both create endpoints are structured/pre-built-bundle only. To use a
  generated bundle, run the planner locally (`mdk init --llm`) and upload the
  result via step 1b.
- **`/report` is the product feed, not infra metrics.** The aggregate
  `GET /api/v1/report` route (step 7c, ADR 032 D2) is the tenant-scoped,
  in-product monitor rollup the Mova iO front end renders — the same
  aggregation `mdk report` runs over the local store. *Operational* metrics
  (host/queue/latency telemetry) still surface via the
  Grafana/Prometheus/Azure dashboards-as-code path, not `/api/v1`. For a single
  agent's slice use `GET /api/v1/agents/{name}/metrics`; eval scorecards live at
  `GET /api/v1/evals/{eval_id}` for a quality view.
- **KB ingest is files-only.** `POST .../kb` takes a multipart `files` upload —
  there's no inline-text or URL body field. To ingest a URL, fetch it to an
  `.html`/`.md` file and upload that.

## Security

No secrets are committed. `bearer_token` is an **empty `secret` placeholder**
in the environment template — set it locally in your own Postman environment
and it stays out of the exported/committed file. Do not export an environment
with a populated token into the repo.
