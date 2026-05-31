# 🗂️ Movate Postman Demo — Cheat Sheet

One-page reference for running `movate-core-flow.postman_collection.json` live.
Full walkthrough: [`README.md`](README.md).

## Setup
```bash
export OPENAI_API_KEY=sk-...     # KB embed + agent run need it
mdk serve --dev                  # binds 127.0.0.1:8000, prints a dev key — copy it
mdk worker                       # 2nd terminal — ONLY for the async beats (eval E2/E3)
```
**Postman environment:** `runtime_url` = `http://127.0.0.1:8000` (no trailing slash) ·
`bearer_token` = the dev key · (`agent_name` defaults to `faq-bot`).
*(Azure: use your deployed URL + `mdk auth create-key --tenant-id <uuid> --scope admin,read,run,kb:write`.)*

## Run order

| ✓ | Folder / # | Request | Expect | Watch for |
|---|---|---|---|---|
| ☐ | **Capabilities** | GET capabilities | `200` | the **`resources`** catalog |
| ☐ | Core 0 | Reset — delete agent | `200`/`404` | **run first** (makes it re-runnable) |
| ☐ | Core 0 | Whoami | `200` | token + scopes valid |
| ☐ | Core 1a | Create agent (wizard) | `200`/`201` | **`_links`** in the body |
| ☐ | Core 1c | Edit agent (PUT bundle) | `200` | the edit story |
| ☐ | Core 2 | Validate | `200` | prompt lint + cost |
| ☐ | Core 3a | KB ingest | `200` | **form-data → `files` (File) → `sample-faq.md`** |
| ☐ | Core 3b | KB stats | `200` | chunks landed |
| ☐ | Core 3c | KB search | `200` | grounded retrieval |
| ☐ | Core 4 | Register skill | `200`/`201` | |
| ☐ | Core 5 | Publish | `200` | promote the agent |
| ☐ | Core 6 | Run agent (`?wait=true`) | `200` | the answer (sets `run_id`) |
| ☐ | Core 7a | Get run | `200` | output |
| ☐ | Core 7b | Run trace | `200` | reconstructed trace |
| ☐ | Core 7c | Aggregate report | `200` | cross-agent rollup |
| ☐ | **Eval** E1 | Kick off eval | `202` + `job_id` | **the async beat** |
| ☐ | Eval E2 | Poll job | `200` | *needs a worker; `queued` = none running* |
| ☐ | Eval E3 | Get scorecard | `200` | after E2 completes |
| ☐ | Projects | POST projects | `201` | **`id` + `_links`** envelope |

## Narrative
Capabilities (`resources`) → create / edit / validate → KB grounding → skill →
run + trace → async eval. Point at **`_links`** ("the response tells you the next
call to make") and the green ✅ test assertions.

## Gotchas
- **KB / run fails** → confirm `OPENAI_API_KEY` on the runtime (see `byok_configured` in Capabilities).
- **Re-running** → start at **`0. Reset`** (no more 409 on create).
- **Bad URL / token** → **request 0 fails loud** (every request has a "no 4xx/5xx" test).
- **Eval E2 stuck `queued`** → no worker running; E1's `202` is the async beat — that's fine.
- **Contexts + full skills-management** not in this collection yet (in review, #650/#651) — frame as "shipping this week."
