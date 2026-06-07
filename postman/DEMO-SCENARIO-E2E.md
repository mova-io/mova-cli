# MDK End-to-End Demo Scenario

**Duration:** ~20 minutes (can cut voice section for ~12 min chat-only)
**Audience:** Technical stakeholders, product leadership, customer engineering
**Prerequisites:** Laptop with `mdk` CLI installed, Postman with `movate-core-flow` collection loaded, browser open

---

## Setup checklist (before the demo)

```bash
# Verify CLI
mdk --version                    # should print CalVer e.g. 2026.6.4.x
mdk doctor                       # green checks = good

# Verify Azure target is reachable
mdk config list-targets          # confirm 'dev' target with URL
mdk auth status                  # confirm token is valid

# Clean slate — remove any leftover demo agent
rm -rf /tmp/mdk-demo && mkdir /tmp/mdk-demo && cd /tmp/mdk-demo
```

**Postman:** Open `movate-core-flow` collection with `movate-azure` environment selected. Confirm `base_url` and `api_key` variables are set.

**Voice demo:** Open `https://mdk-voice-demo.delightfulcoast-91af3b05.eastus.azurecontainerapps.io/` in a browser tab (keep it ready for Part 3).

---

## Part 1 — CLI: Scaffold → Validate → Test locally (~8 min)

### 1.1 Scaffold a project + agent

**[SAY]** "MDK is our framework for building, evaluating, and deploying AI agents. Everything starts from the CLI. One command gives me a runnable agent."

```bash
mdk init acme-support -t faq "Acme customer support FAQ agent"
```

**[POINT OUT]**
- It created a project: `project.yaml`, `AGENTS.md`, `.env.example`
- Under `agents/acme-support/`: `agent.yaml`, `prompt.md`, `schema/input.yaml`, `schema/output.yaml`, `evals/dataset.jsonl`
- "The agent is already runnable — schema, prompt, eval dataset, all scaffolded."

```bash
tree agents/acme-support/
cat agents/acme-support/agent.yaml
cat agents/acme-support/prompt.md
```

### 1.2 Validate

**[SAY]** "Before I touch anything, let me validate — this is the gate you'd wire into CI."

```bash
mdk validate agents/acme-support
```

**[POINT OUT]** Green check — YAML valid, schemas parse, prompt resolves, no unknown fields.

### 1.3 Test locally (mock + real)

**[SAY]** "I can run the agent locally — first in mock mode (no API keys needed), then against a real model."

```bash
# Mock mode — instant, no cost
mdk run agents/acme-support "What is your refund policy?" --mock

# Real execution with streaming
mdk run agents/acme-support "What is your refund policy?" --stream
```

**[POINT OUT]** The output is structured JSON matching the output schema (answer + confidence). Streaming shows tokens as they arrive.

### 1.4 Evaluate

**[SAY]** "The scaffold includes a seed eval dataset. One command scores the agent against it."

```bash
mdk eval agents/acme-support --mock
```

**[POINT OUT]** The scorecard: pass/fail count, score, per-case breakdown. "This is what you'd gate a PR on — `mdk eval --gate 0.8` fails the build if score drops below 80%."

### 1.5 Edit the prompt + re-test

**[SAY]** "Now the dev loop: I edit the prompt and immediately see the difference."

```bash
# Quick edit — add domain knowledge to the prompt
cat >> agents/acme-support/prompt.md << 'EOF'

## Company policies
- Refunds are available within 30 days of purchase
- Support is available 24/7 via chat, email, or phone
- Premium customers get priority routing
EOF

# Re-run — the agent now answers with grounded knowledge
mdk run agents/acme-support "What is your refund policy?" --stream
```

**[POINT OUT]** "No restart, no rebuild — `mdk run` loads everything fresh from disk every time. The agent's answer now references the 30-day refund window."

---

## Part 2 — Postman: API-first on Azure (~8 min)

**[SAY]** "Everything I just did in the CLI has an API equivalent. Let me show the same flow, but hitting our live Azure runtime — the API that a product or a pipeline would call."

### 2.1 Capabilities

**[CLICK]** `Capabilities → GET /capabilities`

**[POINT OUT]** "The runtime self-describes — models available, voice enabled, resource types. A client never has to guess."

### 2.2 Create an agent (from wizard)

**[CLICK]** `Core Flow → 1a. Create agent (from wizard JSON)`

Body (pre-filled in collection):
```json
{
  "name": "acme-support",
  "agent_provider": "Acme Corp",
  "agent_type": "Task Agent",
  "role": "Assistant",
  "description": "Acme customer support FAQ.",
  "agent_goal": "Answer support questions grounded in Acme policies.",
  "agent_prompt": "You are Acme Support. Answer concisely using company knowledge.",
  "reference_output": "Acme offers 24/7 support and 30-day refunds.",
  "ai_model": "openai/gpt-4o-mini-2024-07-18",
  "ai_foundation": "Azure"
}
```

**[POINT OUT]** `201 Created` — `files_persisted: ["agent.yaml", "prompt.md"]`. "I described intent; MDK scaffolded the canonical artifacts. The agent is live on Azure immediately."

### 2.3 Ingest knowledge (KB)

**[CLICK]** `Core Flow → 3a. KB ingest`

```json
{
  "kind": "text",
  "title": "acme-faq",
  "content": "# Acme FAQ\n\nQ: Refund window? A: 30 days.\nQ: Support hours? A: 24/7.\nQ: Premium benefits? A: Priority routing, dedicated account manager."
}
```

**[POINT OUT]** `chunks_saved`, embedding model. "MDK chunked, embedded, and indexed this. The agent now retrieves from it at runtime — RAG, not just the model's training data."

**[CLICK]** `Core Flow → 3c. KB search` — search for "refund"

**[POINT OUT]** The retrieved chunk + similarity score. "This is what the agent sees at inference time."

### 2.4 Validate remotely

**[CLICK]** `Core Flow → 2. Validate agent`

**[POINT OUT]** The same validation gate, but running on the server. "Wire this into CI or a webhook — if validation fails, the deploy doesn't happen."

### 2.5 Run the agent (chat)

**[CLICK]** `Core Flow → 6. Run agent (inline)`

```json
{
  "input": { "question": "What is your refund policy?" }
}
```

**[POINT OUT]**
- The structured response: `answer`, `confidence`
- `run_id` — every execution is tracked
- `cost_usd` — per-run cost metering
- `latency_ms` — end-to-end timing

**[SAY]** "Every run is metered, traced, and stored. I can replay it, audit it, or feed it back into eval."

### 2.6 Monitor — trace + report

**[CLICK]** `Core Flow → 7b. Monitor — run trace`

**[POINT OUT]** The trace tree: LLM call, token counts, KB retrieval step, latency per stage.

**[CLICK]** `Core Flow → 7c. Monitor — aggregate report`

**[POINT OUT]** Fleet-level metrics: runs, cost, latency percentiles, error rate. "This is your operations dashboard data."

### 2.7 Stateful session (multi-turn chat)

**[CLICK]** `Stateful Sessions → POST sessions` to create a session

**[CLICK]** `Stateful Sessions → POST agents/:name/runs (stateful)` — send a message with `session_id`

**[SAY]** "Sessions give you multi-turn memory. The agent remembers context across turns — and the history is persisted server-side in Postgres, not in the client."

---

## Part 3 — Voice: Browser + Phone (~5 min)

**[SAY]** "Now the same agent, but with voice. No code change — the voice pipeline wraps the same agent behind the AgentTurn seam."

### 3.1 Browser voice demo

**Open the voice demo tab:** `https://mdk-voice-demo.delightfulcoast-91af3b05.eastus.azurecontainerapps.io/`

1. **Click "Start"** (unlocks the audio context)
2. **Paste your Mova-iO API key** in the BYOK panel → click Apply → agents load in the dropdown
3. **Select an agent** from the dropdown
4. **Hold the Talk button** and ask: "What are your support hours?"
5. **Release** — watch the pipeline in real time:
   - STT transcribes your speech (Deepgram)
   - Agent processes the query (streaming tokens appear)
   - TTS speaks the response (Cartesia)
   - Latency breakdown appears: STT → Agent → TTS → total

**[POINT OUT]**
- The latency badges: responded in ~1-2 seconds total
- The event stream at the bottom showing the full pipeline trace
- "Same agent, same knowledge base — just a different modality. The voice pipeline adds STT + TTS around it."

### 3.2 Phone call (Twilio)

**[SAY]** "And the same pipeline works over a phone call — real PSTN, not VoIP."

1. In the BYOK panel, click **"Sync to Phone"** — this pushes the browser's agent/voice settings to the phone bridge
2. **Call the Twilio number** (shown in the Phone Sync section) from your phone
3. **Watch the browser event stream** — it mirrors the phone conversation in real time:
   - `📞 incoming call · abc123…`
   - `📞 t1 caller: What are your support hours?`
   - `📞 t1 agent: Our support is available 24 hours a day, 7 days a week.`
   - `📞 t1 done · ✦ responded in 1482ms (agent 503ms · voice 384ms)`
4. **Hang up** — `📞 call ended · 2 turns`

**[POINT OUT]**
- "Same agent, same pipeline, same latency class — just delivered over a phone call instead of a browser mic."
- "The phone bridge is Twilio Media Streams WebSocket → our pipeline → μ-law audio back to the caller. Sub-2-second response times on a real phone."

### 3.3 Switch views

**Toggle to Detailed view** to show the engineering controls:
- Agent tier toggle (OpenAI Chat / Mova-iO Streaming / Mova-iO SDK / OpenAI Realtime)
- TTS provider toggle (Cartesia / OpenAI / ElevenLabs / Azure)
- Fault injection buttons ("break STT" / "break TTS" — shows failover in action)
- Hedge mode (race two providers)
- Speculative kickoff (start agent on partial transcript)
- Budget controls
- Per-turn cost metering

**[SAY]** "This is the engineering view — provider portability, failover, cost control. Switch the TTS from Cartesia to ElevenLabs with one click, no code change. Inject a fault and watch the circuit breaker failover."

---

## Part 4 — Deploy to Azure (show, don't necessarily run live) (~2 min)

**[SAY]** "Getting this to Azure is one command."

```bash
# From the project root:
mdk deploy --target dev

# What it does:
# 1. Builds a Docker image (multi-stage: runtime + worker)
# 2. Pushes to Azure Container Registry
# 3. Updates the Container App (API + worker)
# 4. Waits for /healthz to go green
# 5. Runs a smoke test against the deployed agent
```

**[POINT OUT]**
- "It validates before building — catches errors before the slow Docker step."
- "The health gate waits for the new revision to be live before declaring success."
- "Same `mdk run <agent> --target dev` command now hits the deployed version."

```bash
# Verify it's live
mdk run acme-support "What's your refund policy?" --target dev

# Check the fleet
mdk fleet
```

---

## Closing frame

**[SAY]** "So that's the full loop:
1. **Scaffold** — `mdk init`, one command, runnable agent
2. **Validate** — static checks, CI-gatable
3. **Test** — local run, mock or real, structured output
4. **Evaluate** — dataset scoring, baseline regression
5. **Deploy** — one command to Azure, health-gated
6. **Run** — API-first, metered, traced, every execution
7. **Voice** — same agent, speech-enabled, browser + phone, sub-2s latency

Everything is API-first, CLI-first, and declarative. The same agent definition powers chat, API, and voice — no code changes between modalities."

---

## Fallback / demo recovery notes

| Problem | Fix |
|---|---|
| `mdk run` fails with 401 | `mdk auth status` → if expired, `mdk auth login` or re-source the key |
| Postman 401 | Check `api_key` variable in the environment |
| Voice demo won't connect | Check browser console; ensure WS URL uses `wss://` not `ws://` |
| Phone call gives "application error" | Verify Twilio webhook URL is set to `https://<host>/twiml/voice` (POST) |
| Phone call connects but no agent | Check that "Sync to Phone" was clicked after selecting an agent |
| `mdk deploy` times out | `mdk deploy --target dev --no-wait` then manually check `mdk fleet` |
| Agent returns empty answer | KB may not be ingested yet — run `mdk kb stats <agent>` |
