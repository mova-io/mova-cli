# Build → ground → **edit → watch it change**, by voice (mdk `--llm` FAQ agent)

The headline demo: **scaffold an FAQ agent from one sentence, ground it in your
facts, then EDIT those facts and watch the same voice agent change its answer —
live, on Azure.** ~6–8 minutes.

> **Why this lands:** it proves the agent isn't just "an LLM" — it's answering
> from *content you control*. Change the content, redeploy, and the answer
> changes. That's the difference between a chatbot and a governed knowledge agent.

Runs against the live Azure runtime via the registered **`dev`** target.
**Every step below is verified live (2026-06-01).**

---

## Before the demo (off-camera)

```bash
export MDK_DEV_KEY="mvt_live_…"      # pushes to Azure (your runtime key)
export OPENAI_API_KEY="sk-…"          # powers --llm generation + Whisper/TTS path
```
Open OpenWebUI and sign in (`mdk-user`):
`https://movate-dev-openwebui.bluebush-9aec1e70.eastus2.azurecontainerapps.io`

> Pick a fresh agent name (e.g. `tesla-faq`, or `apple-faq`) you haven't used, so
> the OpenWebUI dropdown is clean.

---

## Step 1 — Scaffold the FAQ agent from one sentence
```bash
mdk init tesla-faq --llm "A friendly Tesla product FAQ assistant that answers \
questions about Tesla vehicles in two or three concise sentences."
cd tesla-faq
```
The LLM writes the whole agent: `agent.yaml`, `prompt.md`, input/output schema,
seed evals. (Swap the sentence for Apple, your product, anything.)

## Step 2 — Ground it in your facts (add a context)
A fresh agent answers from the model's stale memory. Add a **context** — a
knowledge fragment bundled into the agent and prepended at every run.

```bash
mdk contexts create tesla-facts --agent tesla-faq
```
Edit `agents/tesla-faq/contexts/tesla-facts.md` to your **v1** facts:
```markdown
Authoritative Tesla facts (always use these):
- The Tesla Model Y starts at $44,990.
- Model Y EPA range is up to 330 miles.
- It is Tesla's best-selling vehicle.
```
```bash
mdk contexts attach tesla-facts --agent tesla-faq   # wires it into agent.yaml `contexts:`
```

## Step 3 — Deploy + hear it (v1)
```bash
mdk deploy --target dev --mode agents
```
In **OpenWebUI** → refresh → pick **`tesla-faq`** → ask *"How much is the Model Y
and what's its range?"* → hit **🎧 Call** and ask by voice.

> ✅ **Verified v1 answer:** *"The Tesla Model Y starts at **$44,990** and has an
> EPA range of up to **330 miles**."*

## Step 4 — Edit the facts ✏️
Open `agents/tesla-faq/contexts/tesla-facts.md` and change it to **v2** — drop the
price, bump the range, add a trim:
```markdown
Authoritative Tesla facts (always use these):
- The Tesla Model Y now starts at $42,990 (price reduced).
- Model Y EPA range is up to 337 miles.
- A Model Y Performance trim is available at $51,990 with a 0-60 mph time of 3.5 seconds.
- It is Tesla's best-selling vehicle.
```

## Step 5 — Redeploy + hear it CHANGE (v2) ⭐
```bash
mdk deploy --target dev --mode agents      # publishes a new version; change is detected
```
Back in **OpenWebUI** → ask the **same question** (chat or 🎧 voice).

> ✅ **Verified v2 answer:** *"The Tesla Model Y now starts at **$42,990** and has
> an EPA range of up to **337 miles**. There's also a **Model Y Performance** trim
> for **$51,990**, 0-60 in **3.5 seconds**."*

**Same agent, same model, same question — the answer changed because you changed
the knowledge.** That's the whole point.

---

## Talk track (say this to Deva / team)

**Frame (first):**
> "I'll build a product FAQ agent from one sentence, ground it in our facts, and
> then — the important part — I'll *edit* those facts and you'll watch the same
> agent, by voice, change its answer. This is what makes it a knowledge agent, not
> just a chatbot."

**Step 1 — scaffold:**
> "One sentence. The CLI's LLM generates the entire agent — config, prompt,
> schema. I described an outcome; the platform produced the engineering."

**Step 2–3 — ground + deploy + voice:**
> "Out of the box it'd answer from the model's training memory, which is stale. So
> I add a **context** — our authoritative facts — and deploy to Azure. Now listen:
> it tells me the Model Y is **$44,990, 330 miles** — *our* numbers." *(🎧 voice)*

**Step 4–5 — edit + redeploy + the reveal:**
> "Now suppose pricing changes. I edit one file — drop the price, add a
> Performance trim — and redeploy. Same question…" *(ask by voice)* "…and it now
> says **$42,990, 337 miles, plus the Performance trim**. I changed the knowledge;
> the agent's answer followed. No retraining, no prompt-hacking — governed content,
> versioned, and live in seconds."

**Close:**
> "So in a few minutes: one sentence to an agent, grounded in our content, talking
> by voice on Azure — and when the truth changes, I change one file and it's live.
> That's MDK: agents that speak for *your* business, and stay current."

---

## What's powering the voice
🎙️ OpenAI **Whisper** (STT) → 🧠 **GPT-4o-mini** agent on the mdk runtime (Claude
Haiku fallback) → 🔊 OpenAI **tts-1** voice "alloy". Full table + talk track in
[CLI-VOICE-DEMO.md](CLI-VOICE-DEMO.md#whats-powering-the-voice).

## Notes & recovery
- **Two context styles, both work:** *file-based* (above — bundled into the agent,
  simplest) or *managed* (`POST /api/v1/contexts` + attach via API — reusable across
  agents, the governed-library story; fixed 2026-06-01). For the edit-and-redeploy
  loop, **file-based is cleanest** — you edit a file in the bundle and redeploy.
- **Redeploy says `unchanged`?** You didn't save the edit, or edited the wrong
  file — confirm `agents/<name>/contexts/<name>.md`. A real edit republishes a new
  `0.1.0+<hash>` version (verified).
- **Not in OpenWebUI dropdown / shows old answer?** Hard-refresh (Cmd-Shift-R) —
  the model list and responses are fetched fresh on load.
- **`unknown_agent` after attaching a *managed* context?** Resolved on this runtime
  (worker ships ADR 060 D4). If it recurs, recover with
  `POST /api/v1/agents/<name>/revert {"to_version":"0.1.0"}`.

## Pre-flight checklist
- [ ] `MDK_DEV_KEY` + `OPENAI_API_KEY` exported
- [ ] `mdk config list-targets` shows **`dev`**
- [ ] Rehearsed init → ground → deploy → edit → redeploy once; OpenWebUI showed the change
- [ ] OpenWebUI open, signed in, fresh agent name chosen
