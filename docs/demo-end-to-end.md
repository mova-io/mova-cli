# MDK end-to-end demo — clean machine to deployed Azure agent

A copy-pasteable demo script. Walks from `git clone` to a live Azure Container Apps endpoint, then layers on context blocks, skills, multi-agent workflows, and policy gating to show off the whole feature surface in one sitting.

**Total wall-clock:** ~15 minutes for the core path. ~30 minutes with every bonus section.

**Audience:** internal demos, customer walkthroughs, recorded screencasts.

> **Two recent additions used in the demo:**
> - **`mdk add <template>`** — project-aware ergonomic wrapper. `mdk add rag-qa` drops a role-agent into `./agents/rag-qa/` from the bundled template catalog. `mdk add --list` prints the catalog.
> - **`mdk deploy --notify`** — fires a Telegram message + generic webhook on successful deploy. Set `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` for Telegram, `MOVATE_DEPLOY_WEBHOOK` for Slack/Teams/Discord/custom.

> **What's real, what's stubbed:** Every command in this file runs against the shipped CLI. The one feature called out as a stub is the **knowledge-base abstraction** — there's no first-class `mdk kb` command today. RAG-style knowledge is delivered via the `contexts/` directory (manual markdown drops) plus skills for live API lookups. A vector-store integration ships in a later sprint.

---

## Table of contents

1. [Prerequisites](#0-prerequisites)
2. [The 8-command core path](#the-8-command-core-path) — clean machine → deployed agent
3. [Bonus A: contexts for product knowledge](#bonus-a-contexts-for-product-knowledge)
4. [Bonus B: skills for tool use](#bonus-b-skills-for-tool-use)
5. [Bonus C: workflows for multi-agent composition](#bonus-c-workflows-for-multi-agent-composition)
6. [Bonus D: policy.yaml for responsible-AI gating](#bonus-d-policyyaml-for-responsible-ai-gating)
7. [Verification — what the demo proved](#verification--what-the-demo-proved)

---

## 0. Prerequisites

```bash
# Tooling
python3 --version            # 3.11+
uv --version                 # any recent
az --version                 # Azure CLI (only needed for the deploy step)

# Credentials (any one provider key works for the local steps)
export OPENAI_API_KEY=sk-...
# or: ANTHROPIC_API_KEY / AZURE_OPENAI_API_KEY / GEMINI_API_KEY

# Install MDK from the private repo
uv pip install "git+https://github.com/mova-io/mova-cli.git"
mdk --version
```

> **Hermetic mode:** every step below works with `--mock` instead of a real provider key. Use that for offline rehearsal or CI smoke. Replace `--llm "..."` with `--llm "..." --mock` and the same for `mdk run`, `mdk eval`.

---

## The 8-command core path

This is the minimum to demo "describe an agent in English, see it run, ship it to Azure."

### 1. Bootstrap a project

```bash
mdk init --project support-bot
cd support-bot
```

**What you get:** `movate.yaml`, `.env.example`, `.gitignore`, empty `agents/`, and an **initial snapshot** (a content-addressed baseline `mdk diff` / `mdk rollback` can target from minute one).

### 2. Add your provider key

```bash
cp .env.example .env
# Edit .env, paste your OPENAI_API_KEY (or another provider key)
```

### 3. Sanity-check the environment

```bash
mdk doctor
```

Shows Python version, installed deps, provider keys, tracer, storage, pricing table, and project config. Ends with a greppable `mdk_doctor_summary: checks=N ok=N missing=N error=N` line for CI.

### 4. Scaffold the agent from a natural-language description

```bash
mdk init support-bot --llm "An FAQ agent for our SaaS product. \
  Answers questions about pricing, features, trial limits, and \
  account management. Returns an answer string + a confidence score \
  between 0 and 1. Be concise and never invent product details."
```

The CLI calls an LLM with a meta-prompt + two few-shot exemplars, parses the response into a validated `GeneratedAgent`, writes the standard agent file layout to `./support-bot/`, runs it through `load_agent()` end-to-end, and retries once on failure.

You'll see a Rich Panel like:

```
╭─ ✓ LLM-scaffolded agent ──────────────────────────╮
│ Agent:    support-bot                              │
│ Files:                                             │
│   • agent.yaml                                     │
│   • prompt.md                                      │
│   • schema/input.json                              │
│   • schema/output.json                             │
│   • evals/dataset.jsonl (3 seed cases)             │
│ Cost:     $0.000401 USD                            │
╰────────────────────────────────────────────────────╯

mdk_init_summary: name=support-bot llm=true \
  model=openai/gpt-4o-mini-2024-07-18 input_tokens=1539 \
  output_tokens=268 cost_usd=0.000401 retried=false ok=true
```

### 5. Validate the generated agent

```bash
mdk validate ./support-bot
```

YAML errors (if any) surface as `path:line:col` so your editor jumps straight to the offending byte. The generated agent should already pass — the `--llm` flow validated it before writing.

### 6. Run it locally

```bash
mdk run ./support-bot '{"question": "do you offer SAML SSO?"}'
```

Output JSON shows the validated response plus `metrics.cost_usd`, `metrics.latency_ms`, and a `run_id` that's persisted to SQLite at `~/.movate/local.db`. The CLI echoes the `run_id` on stderr with the exact `mdk replay <id>` command if you want to re-run.

### 7. Gate on a baseline eval

```bash
mdk eval ./support-bot --gate 0.7
```

Runs the seed dataset through the agent, scores against the expected outputs (exact-match or LLM-as-judge with cross-family enforcement), and exits 0 on pass / 1 on fail. The output ends with `mdk_eval_summary:` for CI parsing.

### 8. Deploy to Azure Container Apps (with Telegram notification)

```bash
# One-time: register the deployment target
mdk config add-target prod \
  --url https://movate-prod-api.azurecontainerapps.io \
  --azure-subscription <sub-id> \
  --azure-resource-group movate-prod \
  --azure-acr movateprodacr \
  --azure-env prod

# One-time: wire your notification channel (any/all)
export TELEGRAM_BOT_TOKEN=...                # from @BotFather
export TELEGRAM_CHAT_ID=...                  # from getUpdates after /start
export MOVATE_DEPLOY_WEBHOOK=https://...     # optional: Slack/Teams/Discord

# Pre-flight: confirm Azure auth + permissions
mdk doctor --target prod

# Deploy: build container in ACR, update both api + worker apps,
# fire Telegram + webhook on success.
mdk deploy --target prod --notify
```

`mdk deploy` runs `az acr build` (no local Docker needed), tags as `movate:<version>-<git-sha>`, updates both `movate-prod-api` and `movate-prod-worker` Container Apps, polls `/healthz` until live, and (with `--notify`) fires a Telegram message + webhook with:

- Target, image tag, runtime URL
- Git SHA, deployer (`$USER`), wall-clock duration
- Package version

You'll see on your phone within a second of `/healthz` going green:

```
✓ Deployed to prod
movate:0.6.1-abc1234
Git SHA: abc1234
v0.6.1 · deployed by alice · 173.4s

[Open runtime](https://movate-prod-api.azurecontainerapps.io)
```

You're done. Total: 8 commands, plus a phone notification.

---

## Bonus A: contexts for product knowledge

Contexts are markdown blocks **prepended** to every prompt the agent renders. Use them for stable product facts the agent should never invent (pricing tiers, supported regions, refund windows).

### A.1. Drop a context file

```bash
mkdir -p contexts
cat > contexts/product-facts.md <<'EOF'
# Product facts

- **Tiers:** Free (10 req/day), Pro ($29/mo), Enterprise (custom).
- **Trial:** 14 days, no card required. Unlimited tier-Pro features.
- **SAML SSO:** Enterprise tier only.
- **Refund window:** 30 days from purchase, no questions asked.
- **Supported regions:** US-East, US-West, EU-West. APAC roadmap Q3.
EOF
```

### A.2. Reference it from the agent

Edit `support-bot/agent.yaml` and add a `contexts:` field:

```yaml
contexts:
  - product-facts
```

(No `.md` suffix — the loader appends it.)

### A.3. Re-run the agent

```bash
mdk run ./support-bot '{"question": "do you offer SAML SSO?"}'
```

The model now sees the product-facts block prepended to its prompt and answers from grounded knowledge instead of guessing. `mdk inspect agent support-bot --only prompt` shows the resolved prompt (context + agent body) the model actually sees.

> **Why this is the "KB" surface today:** there's no separate `mdk kb` command. RAG-style retrieval against a vector store ships in a future sprint. Contexts cover the "stable knowledge prepended at every call" use case; for live API lookups, use skills (Bonus B).

---

## Bonus B: skills for tool use

Skills are callable tools the agent can invoke during a run (Python functions, soon HTTP / MCP). Use them for live data — order status, account lookups, web searches.

### B.1. Scaffold a skill

```bash
mdk skills scaffold lookup-order
```

Creates `skills/lookup-order/` with `skill.yaml`, `impl.py`, `README.md`.

### B.2. Implement it

Edit `skills/lookup-order/impl.py`:

```python
async def run(input: dict, ctx) -> dict:
    """Look up an order's current status by ID. Stub for the demo —
    a real version would hit your order-service REST API."""
    order_id = input["order_id"]
    # Stub: pretend every order with a digit-suffix is "shipped".
    if order_id and order_id[-1].isdigit():
        return {"status": "shipped", "tracking": f"TRK-{order_id}"}
    return {"status": "not_found"}
```

Adjust the schema in `skills/lookup-order/skill.yaml` to match:

```yaml
schema:
  input: { order_id: string }
  output: { status: string, tracking: string? }
side_effects: read-only
```

### B.3. Run the skill standalone (no agent needed)

```bash
mdk skills run lookup-order '{"order_id": "ord-1234"}'
```

This is the "unit-test" path for a skill — invoke it directly to verify the contract before wiring it into an agent.

### B.4. Reference the skill from the agent

Edit `support-bot/agent.yaml`:

```yaml
skills:
  - lookup-order
```

Re-run the agent with a question that should trigger the tool:

```bash
mdk run ./support-bot '{"question": "what is the status of order ord-1234?"}'
```

The executor runs the tool-use loop: the model decides to call `lookup-order`, the executor dispatches it, the result feeds back into the next turn, and the final answer references the live data.

---

## Bonus C: workflows for multi-agent composition

Multiple agents stitched into a linear pipeline. v0.3 ships sequential workflows; conditional routing and parallel land in v1.1.

### C.1. Scaffold a second agent — classifier

```bash
# Option 1: from a bundled role template (instant, no LLM call)
mdk add classifier router

# Option 2: from a natural-language description (LLM-scaffolded)
mdk init router --llm "A two-class router. Reads a customer question \
  and decides whether it's about general FAQs ('faq') or about an \
  existing order ('order'). Returns exactly one of those two labels."
```

### C.2. Compose the workflow

```bash
mkdir -p workflows/support
cat > workflows/support/state.json <<'EOF'
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["question"],
  "properties": {
    "question": {"type": "string"},
    "label":    {"type": "string"},
    "answer":   {"type": "string"}
  }
}
EOF

cat > workflows/support/workflow.yaml <<'EOF'
api_version: movate/v1
kind: Workflow
name: support
version: 0.1.0
state_schema: ./state.json
entrypoint: classify

nodes:
  - id: classify
    type: agent
    ref: ../../router
  - id: answer
    type: agent
    ref: ../../support-bot

edges:
  - from: classify
    to: answer
EOF
```

### C.3. Run the workflow

```bash
mdk run ./workflows/support '{"question": "do you offer SAML SSO?"}'
```

Output shows both nodes executing in order, with per-node `RunRecord`s linked by a single `workflow_run_id`. `mdk trace replay <workflow_run_id>` renders the full timeline.

> **"Subagents" today** = workflow nodes of `type: agent`. There's no separate "subagent" abstraction — agents are referenced by relative path in the workflow YAML. Conditional edges (`when: ...`) land in v1.1; for v0.3 every edge is unconditional.

---

## Bonus D: policy.yaml for responsible-AI gating

Policy is the responsible-AI surface today. It gates on **cost**, **allowed providers**, **denied models**, **allowed runtimes**, and **allowed skill side-effect classes**. Content filters (PII, jailbreak detection) are a future feature, not wired today.

### D.1. Write a policy file

```bash
cat >> movate.yaml <<'EOF'

policy:
  # Only OpenAI and Anthropic — block Gemini, Lyzr, custom providers.
  allowed_providers: [openai, anthropic]
  # Block legacy models that are too cheap-and-stupid for production.
  deny_models:
    - openai/gpt-3.5-turbo
  # Hard ceiling per-run. A single agent call exceeding this is rejected.
  max_cost_per_run_usd: 0.50

runtime:
  # Only LiteLLM agents — block direct-SDK runtimes until they're audited.
  allowed: [litellm]

skills:
  # Block skills with side effects. The lookup-order skill above is
  # read-only — it stays allowed. A skill that mutates state or hits
  # the network would be rejected at validate time.
  allowed_side_effects: [read-only]
EOF
```

### D.2. Validate the project against policy

```bash
mdk validate ./support-bot
mdk validate ./workflows/support
```

If any agent or skill violates a policy rule, validation fails with the specific violation. Same gates fire at runtime: `mdk run` rejects a policy-violating request before it hits the provider.

### D.3. Audit the entire project

```bash
mdk audit current --strict
```

Runs every production-readiness scanner (missing-evals, exposed-secret, empty-prompt, missing-owner, floating-model-tag, missing-version, missing-fallback, prompt-too-long, schema-no-required, no-test-signal). Findings include the matching `mdk fix` command inline.

### D.4. Export / version / diff policy

```bash
mdk policy export --output policy-snapshot.yaml  # for git tracking
mdk policy diff new-policy.yaml                  # dry-run before applying
mdk policy import new-policy.yaml                # commit the change
```

---

## Verification — what the demo proved

| Capability | Command(s) | Section |
|---|---|---|
| Project bootstrap | `mdk init --project` | Core 1 |
| Environment sanity check | `mdk doctor` | Core 3 |
| **LLM-driven agent scaffold** | `mdk init <name> --llm "<description>"` | Core 4 |
| Schema validation | `mdk validate` | Core 5 |
| Local agent execution | `mdk run` | Core 6 |
| Eval gating | `mdk eval --gate` | Core 7 |
| Azure deploy | `mdk config add-target`, `mdk deploy` | Core 8 |
| **Context blocks (KB-lite)** | drop `contexts/*.md`, add `contexts:` to agent.yaml | Bonus A |
| **Skills (tool use)** | `mdk skills scaffold/run`, `skills:` in agent.yaml | Bonus B |
| **Multi-agent workflows** | `workflows/<name>/workflow.yaml`, `mdk run <workflow>` | Bonus C |
| **Responsible-AI policy** | `policy.yaml` blocks in `movate.yaml` | Bonus D |
| Production-readiness audit | `mdk audit current --strict` | Bonus D |

**Every command above runs against the shipped CLI** — nothing is aspirational. Each step emits a greppable `mdk_*_summary:` line that CI tooling can parse.

## What's deferred

- **First-class knowledge-base abstraction.** No `mdk kb` command. Vector store (pgvector / Azure AI Search) and embedding pipeline land in a future sprint. Today: contexts for stable knowledge, skills for live API lookups.
- **Conditional workflow routing.** v0.3 ships unconditional edges only. `when:` conditionals + parallel fan-out arrive in v1.1.
- **Content-filter policies.** Cost / model / runtime / skill gating is in. PII detection, jailbreak detection, output-content filters are not wired.
- **HTTP / MCP skill backends.** Python skill backend ships in v0.6. HTTP + MCP land in follow-up PRs.

## Recording the demo

For a screencast:

1. **Pre-record the deploy step.** It takes 2-4 minutes (ACR build + ACA revision update). Have a pre-baked target + pre-built image ready and run `mdk deploy --target prod --skip-build` for the live demo.
2. **Pre-warm `mdk init --llm`** — the LLM call adds 3-8 seconds. Show the first invocation live to demonstrate the Rich spinner, then have a pre-scaffolded agent ready for downstream steps.
3. **Pipe `mdk audit current --strict --json` into `jq`** at the end — visual proof that the CLI is CI-ready, not just demo-ware.
4. **Capture stderr too.** The greppable `mdk_*_summary:` lines + the `→ scaffolded by --llm …` hints are the strongest "engineered for operators" signal — don't let them get lost.
