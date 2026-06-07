# Mova-iO Quick Demo — API + Voice (~15 min)

**Audience:** Technical stakeholders
**Prereqs:** Postman with `movate-core-flow` collection, `movate-azure` environment selected, browser open

---

## Endpoints

| Service | URL |
|---------|-----|
| **API Runtime (dev)** | `https://movate-dev-api.bluebush-9aec1e70.eastus2.azurecontainerapps.io` |
| **Voice Playground** | `https://mdk-voice-demo.delightfulcoast-91af3b05.eastus.azurecontainerapps.io` |
| **Phone (Twilio)** | `(217) 919-5393` |

---

## Generate an API key (one-time)

Run this in your terminal to mint a key for the demo:

```bash
# 1. Verify the CLI sees the dev target
mdk config list-targets

# 2. Generate a full-access API key
mdk auth create-key \
  --tenant-id movate-demo \
  --env live \
  --label "postman-demo" \
  --scope admin,read,run,eval,kb:write

# ⚠️  The key prints ONCE — copy it immediately!
# It looks like: mdk_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 3. Verify it works
mdk auth whoami --target dev
```

---

## Setup (before the demo)

1. **Postman — import collection + environment:**
   - File → Import → `postman/mdk-quick-demo.postman_collection.json`
   - File → Import → `postman/mdk-quick-demo.postman_environment.json`
   - Select **"MDK Quick Demo"** environment (top-right dropdown)

2. **Set the API key:** Click the eye icon → edit `api_key` → paste the key from the step above.

3. **Verify:** The `runtime_url` is pre-filled to `https://movate-dev-api.bluebush-9aec1e70.eastus2.azurecontainerapps.io`. The `agent_name` defaults to `demo-support`.

4. **Voice tab:** Open `https://mdk-voice-demo.delightfulcoast-91af3b05.eastus.azurecontainerapps.io/` — keep ready for Part 2.

5. **Clean slate:** Run `0. Reset — delete demo agent` (it's idempotent — 200 or 404 are both fine).

---

## Part 1 — Postman API Flow (~10 min)

### Step 1: Create the agent

**[SAY]** "One API call creates a fully runnable agent — prompt, schema, model config, all in one shot."

**[CLICK]** `Movate — Core Flow → 1a. Create agent (from wizard JSON)`

The body is pre-filled:
```json
{
  "name": "demo-support",
  "agent_provider": "Movate",
  "agent_type": "Task Agent",
  "role": "Assistant",
  "description": "An FAQ assistant. Answers questions concisely.",
  "agent_goal": "Answer product FAQs accurately and briefly.",
  "agent_prompt": "You are an FAQ assistant. Answer the user's question concisely. If you are unsure, say so.",
  "ai_model": "openai/gpt-4o-mini-2024-07-18",
  "ai_foundation": "Azure"
}
```

**[SEND]** — expect `201 Created`

**[POINT OUT]** `files_persisted: ["agent.yaml", "prompt.md"]` — "I described the intent; MDK scaffolded the canonical artifacts. The agent is live immediately."

---

### Step 2: Talk to the agent (first conversation)

**[SAY]** "The agent is live. Let me ask it a question."

**[CLICK]** `Movate — Core Flow → 6. Run agent (inline)`

Body:
```json
{
  "input": {
    "question": "What does the standard plan include?"
  }
}
```

**[SEND]** — expect `200 OK`

**[POINT OUT]**
- The agent answers — but it's **guessing** because it has no knowledge base yet. It may say something generic.
- Note the `run_id`, `cost_usd`, `latency_ms` — every execution is metered and traced.

**[SAY]** "The agent answered, but it's hallucinating — it doesn't have any real product knowledge yet. Let's fix that."

---

### Step 3: Add knowledge (KB ingest)

**[SAY]** "Now I'll give the agent real product knowledge — one API call."

**[CLICK]** `Movate — Core Flow → 3a. KB ingest`

Body (paste this if not pre-filled):
```json
{
  "kind": "text",
  "title": "product-pricing",
  "content": "# Movate Product Plans\n\n## Starter Plan ($29/mo)\n- 3 user seats\n- 10GB storage\n- Email support (business hours)\n- 1 AI agent\n\n## Standard Plan ($99/mo)\n- 10 user seats\n- 100GB storage\n- Priority email + chat support\n- 5 AI agents\n- Knowledge base (RAG)\n- Basic analytics\n\n## Enterprise Plan (custom pricing)\n- Unlimited seats\n- Unlimited storage\n- 24/7 dedicated support + SLA\n- Unlimited AI agents\n- Knowledge graphs + advanced RAG\n- SSO / SAML\n- Custom model fine-tuning\n- On-premise deployment option\n\n## Add-ons\n- Voice agent: +$20/mo per agent\n- Phone bridge (Twilio): +$50/mo\n- Advanced analytics dashboard: +$30/mo"
}
```

**[SEND]** — expect `200` with `chunks_saved` count

**[POINT OUT]** "MDK chunked, embedded, and indexed this. The agent now retrieves from it at runtime — RAG, not just the model's training data."

---

### Step 4: Verify the knowledge landed

**[SAY]** "Let me verify the knowledge is searchable."

**[CLICK]** `Movate — Core Flow → 3c. KB search`

Body:
```json
{
  "query": "standard plan",
  "top_k": 3
}
```

**[SEND]** — expect chunks with the pricing info + similarity scores

**[POINT OUT]** "This is exactly what the agent sees at inference time — the retrieved context that grounds its answer."

---

### Step 5: Ask the same question again (now grounded)

**[SAY]** "Now let's ask the same question — but this time the agent has real knowledge."

**[CLICK]** `Movate — Core Flow → 6. Run agent (inline)` — same body as Step 2:

```json
{
  "input": {
    "question": "What does the standard plan include?"
  }
}
```

**[SEND]**

**[POINT OUT]**
- **Before KB:** The agent guessed or hallucinated.
- **After KB:** The answer references the real data — "10 user seats, 100GB storage, 5 AI agents, priority support."
- Same agent, same prompt — the only change was adding knowledge. "This is the RAG loop: ingest → retrieve → ground."

---

### Step 6: Multi-turn session (agent remembers context)

**[SAY]** "Now let me show stateful sessions — the agent remembers context across turns."

**[CLICK]** `Stateful Sessions → POST sessions`

**[SEND]** — note the `session_id` in the response (auto-saved to the `{{session_id}}` variable)

**[CLICK]** `Stateful Sessions → POST agents/:name/runs (stateful)`

First message:
```json
{
  "input": { "question": "What does the standard plan include?" },
  "session_id": "{{session_id}}"
}
```

**[SEND]** — agent answers with the grounded pricing info.

Change the body to a follow-up:
```json
{
  "input": { "question": "How much more does enterprise cost compared to that?" },
  "session_id": "{{session_id}}"
}
```

**[SEND]**

**[POINT OUT]** "The agent understood 'that' refers to the standard plan from the previous turn. Session memory is server-side in Postgres — the client doesn't need to replay history."

---

## Part 1b — CLI: Scaffold + Eval (~3 min, optional)

**[SAY]** "Let me show the developer workflow. One CLI command scaffolds a runnable agent with a built-in eval suite."

### Step 6b: Scaffold an agent from the CLI

```bash
cd /tmp && mkdir mdk-demo && cd mdk-demo

# One command → runnable agent with prompt, schema, and eval dataset
mdk init demo-faq -t faq "Product FAQ agent"
```

**[POINT OUT]**
- `agents/demo-faq/` — `agent.yaml`, `prompt.md`, `schema/`, `evals/dataset.jsonl`
- "The agent is runnable immediately — schema, prompt, eval dataset, all scaffolded."

```bash
tree agents/demo-faq/
```

### Step 6c: Run eval (mock mode — no API keys needed)

**[SAY]** "The scaffold includes a seed eval dataset. One command scores the agent against it."

```bash
mdk eval agents/demo-faq --mock
```

**[POINT OUT]**
- The scorecard: pass/fail per case, overall score
- "This is what you'd gate a PR on — `mdk eval --gate 0.8` fails the build if the score drops below 80%."

### Step 6d: Run eval against the live model

**[SAY]** "Now the same eval, but hitting the real model — GPT-4o-mini."

```bash
mdk eval agents/demo-faq
```

**[POINT OUT]**
- Real model answers vs. expected
- Cost per eval run
- "This is your regression baseline. Edit the prompt, re-run eval, see if the score goes up or down."

### Step 6e: Edit prompt + re-eval (the dev loop)

**[SAY]** "This is the dev loop — edit the prompt, re-eval, see the difference."

```bash
# Add domain knowledge to the prompt
cat >> agents/demo-faq/prompt.md << 'EOF'

## Company policies
- Refunds are available within 30 days of purchase
- Support is available 24/7 via chat, email, or phone
- Premium customers get priority routing
EOF

# Re-run eval — the agent now answers with grounded knowledge
mdk eval agents/demo-faq --mock
```

**[POINT OUT]** "No restart, no rebuild — `mdk eval` loads everything fresh from disk. The score may change because the agent now has real knowledge."

---

## Part 2 — Voice: Same Agent, Spoken (~5 min)

**[SAY]** "Now the same kind of agent — but with voice. No code change. The voice pipeline wraps the agent behind the same AgentTurn interface."

### Step 7: Browser voice demo

1. **Switch to the voice demo tab** — `https://mdk-voice-demo.delightfulcoast-91af3b05.eastus.azurecontainerapps.io/`
2. **Click Start** (unlocks audio)
3. **Don't enter a BYOK key** — the built-in OpenAI agent (Deva) is ready
4. **Hold Talk** and say: "Hi, what plans do you offer?"
5. **Release** — watch the pipeline:
   - STT transcribes your speech (Deepgram)
   - Agent streams the answer (OpenAI GPT-4o-mini)
   - TTS speaks the response (Cartesia)
   - Latency badge shows: STT → Agent → TTS → total

**[POINT OUT]**
- Responded in ~2-4 seconds total
- Clean text display (no formatting artifacts)
- Multi-turn: ask a follow-up — "What about enterprise?" — the agent remembers

### Step 8: Phone call (optional wow moment)

**[SAY]** "And the same pipeline works over a real phone call."

1. **Click Sync to Phone** in the browser
2. **Call (217) 919-5393** from your phone
3. **Watch the browser event stream** mirror the phone conversation in real time
4. **Hang up** — the event stream shows call duration and turn count

**[POINT OUT]** "Same agent, same pipeline — browser mic or PSTN phone. Sub-3-second response times on a real phone call."

---

## Closing

**[SAY]** "So that's the full loop:
1. **Create** an agent — one API call or one CLI command
2. **Talk to it** — it answers, but it's guessing
3. **Add knowledge** — one API call, RAG is automatic
4. **Talk again** — now it's grounded in real data
5. **Evaluate** — score the agent against a dataset, gate PRs on quality
6. **Sessions** — multi-turn memory, server-side
7. **Voice** — same agent, speech-enabled, browser + phone, 2-4 second latency

Everything is API-first and CLI-first. The same agent definition powers chat, API, and voice — no code changes between modalities."

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Postman 401 | Check `api_key` in the environment |
| Agent already exists (409) | Run `0. Reset` first, or change `agent_name` |
| KB ingest returns empty | Check the `content` field has actual text |
| Voice demo won't connect | Check browser console; hard-refresh (Cmd+Shift+R) |
| Voice shows "Lyzr selected but agent_id missing" | That's fine — it falls back to the built-in OpenAI agent |
| Phone call gives silence | Click "Sync to Phone" first, then call |
