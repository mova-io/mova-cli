# End-to-end flow: project → agent → context/KB/skill → OpenWebUI → observability

Everything runs against the **live Azure runtime**. Verified working this
morning. Use the Postman collection (`movate-core-flow`) + `movate-azure`
environment, or the `curl` shown here. Base URL:

```
https://movate-dev-api.bluebush-9aec1e70.eastus2.azurecontainerapps.io
```
All requests need `Authorization: Bearer <your mvt_live_… key>`.

---

## 1. Create a project (remote, on Azure)  ✅
```
POST /api/v1/projects
{ "name": "Acme Demo", "description": "E2E demo project" }
```
→ `201`, returns `project_id` (e.g. `prj_…`). Persisted in Azure Postgres.

## 2. Create an agent  ✅
```
POST /api/v1/agents/from-wizard
{ "name":"acme-support", "agent_provider":"Acme", "agent_type":"Task Agent",
  "role":"Assistant", "description":"Acme support Q&A.",
  "agent_goal":"Answer support questions.", "agent_prompt":"You are Acme support. Answer concisely.",
  "reference_output":"Acme offers 24/7 support.",
  "mcp_connectors":[], "knowledge_store":[],
  "ai_model":"openai/gpt-4o-mini-2024-07-18", "ai_foundation":"Azure" }
```
→ `201`. The agent is **live immediately** and shows up in OpenWebUI (step 7).

## 3. Add a context  ✅
```
POST /api/v1/contexts
{ "name":"acme-policies", "body":"Acme refunds within 30 days. Support is 24/7.",
  "description":"Acme policies", "version":"1.0.0" }
```

## 4. Add knowledge (KB ingest)  ✅
**Body is the union member directly — NOT wrapped in `{files:[…]}`:**
```
POST /api/v1/agents/acme-support/kb
{ "kind":"text", "title":"acme-faq",
  "content":"# Acme FAQ\n\nQ: Refund window? A: 30 days.\nQ: Support hours? A: 24/7." }
```
→ `200` (chunked + embedded). This also **builds the knowledge graph** (entity
extraction). Other kinds: `{"kind":"url","url":"https://…"}`.

Check it: `GET …/kb/stats`, `POST …/kb/search {"question":"refund","k":3}`.

## 5. Register a skill  ✅
```
PUT /api/v1/skills/acme-lookup
{ "version":"0.1.0", "description":"Acme lookup",
  "files": { "skill.yaml":"api_version: movate/v1\nkind: Skill\nname: acme-lookup\nversion: 0.1.0\ndescription: Looks up Acme info.\nschema:\n  input:\n    query: string\n  output:\n    result: string\nimplementation:\n  kind: python\n  entry: impl:run\n",
             "impl.py":"def run(inp):\n    return {\"result\":\"acme\"}\n" } }
```

## 6. Attach context + skill to the agent  ✅
```
POST /api/v1/agents/acme-support/contexts   { "ref":"acme-policies", "version":"1.0.0" }
POST /api/v1/agents/acme-support/skills      { "ref":"acme-lookup",   "version":"0.1.0" }
```

## 7. "Deploy" — the agent is already live  ✅ (see note)
**No separate deploy step is needed** — the agent is runnable + in OpenWebUI the
moment it's created.
> `POST /agents/{name}/publish` returns **503** here on purpose: *publish*
> persists the agent to a **GitHub repo** (`MDK_GITHUB_ENABLED=1` + app creds),
> which isn't wired on this demo deployment. It does **not** affect running the
> agent.

## 8. Test it in OpenWebUI  ✅ (read the note)
Open OpenWebUI → **refresh** → the model dropdown now lists **`acme-support`**.
Pick it → ask a question → it **answers** (and 🎧 Call for voice). The shim
auto-maps your chat message to the agent's input field.

> ✅ **Newly-created agents now chat reliably** — the `from-wizard` prompt is
> auto-scaffolded (input interpolation + JSON output contract) so a brand-new
> agent works out of the box. Verified: a fresh wizard agent answered
> *"Our refund policy allows a full refund within 30 days…"* through OpenWebUI.

## 9. See the observability  ✅
- **Product report:** `GET /api/v1/report` → aggregate runs/cost/health.
- **Run trace:** `GET /api/v1/runs/{run_id}/trace` (after a run) → step-by-step.
- **NL query:** `POST /api/v1/observability/ask {"question":"how are my agents doing?"}`.
- **Public Grafana dashboard** (live Azure Monitor — requests/CPU/replicas):
  `https://movate-dev-grafana-oss.bluebush-9aec1e70.eastus2.azurecontainerapps.io`
- **Azure Monitor Workbooks** (portal, logged in) for the deep dive.

---

## Quick reference — verified status
| Step | Endpoint | Status |
|---|---|---|
| Create project | `POST /projects` | ✅ 201 |
| Create agent | `POST /agents/from-wizard` | ✅ 201 |
| Add context | `POST /contexts` | ✅ 201 |
| KB ingest | `POST /agents/{n}/kb` `{kind:text,…}` | ✅ 200 |
| Register skill | `PUT /skills/{n}` | ✅ 201 |
| Attach | `POST /agents/{n}/contexts` `/skills` `{ref,version}` | ✅ 200 |
| Appears in OpenWebUI | shim `/v1/models` | ✅ |
| Publish (GitHub) | `POST /agents/{n}/publish` | ⚠️ 503 (GitHub off) |
| New-agent chat | OpenWebUI | ✅ works (wizard prompt auto-scaffolded) |
| Observability | `/report`, `/trace`, Grafana | ✅ |
