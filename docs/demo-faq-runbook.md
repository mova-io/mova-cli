# Demo runbook: build a Movate FAQ agent

Dogfood version of the demo deck. Copy-paste each block, verify the
expected output, and you'll have a working manager-plus-experts pattern
in ~30 minutes.

**Heads up — conditional `workflow.yaml` edges land in v1.1.** Until then
this runbook orchestrates the routing in **bash** (manager classifies →
bash conditional → right expert runs). Same behavior; expressed in shell
instead of YAML.

---

## Prereqs (1 min)

```bash
mdk --version    # expect: 0.5.0 or later
echo $OPENAI_API_KEY | head -c 10     # expect: sk-proj-…  (non-empty)
echo $ANTHROPIC_API_KEY | head -c 10  # expect: sk-ant-…   (non-empty)
```

If either key is empty, `export OPENAI_API_KEY=...` / `export ANTHROPIC_API_KEY=...`.
Total estimated cost for the whole runbook: **~$0.15**.

---

## Step 1 — Scaffold the three agents (2 min)

```bash
mkdir -p ~/movate-faq && cd ~/movate-faq

mdk init agents/manager  -t classifier
mdk init agents/services -t faq
mdk init agents/cli      -t faq

find . -maxdepth 3 -type f | sort
```

**Expect**: each agent gets `agent.yaml`, `prompt.md`, `schema/input.json`,
`schema/output.json`, `evals/dataset.jsonl`, `evals/judge.yaml.example`.

---

## Step 2 — Customize the manager (5 min)

The scaffolded `classifier` template has `{ text, labels }` input. We want
`{ question }` input and `{ classification }` output. Overwrite three files:

### `agents/manager/schema/input.json`

```bash
cat > agents/manager/schema/input.json <<'JSON'
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["question"],
  "properties": {
    "question": { "type": "string", "minLength": 1 }
  }
}
JSON
```

### `agents/manager/schema/output.json` — enum guarantees one of two values

```bash
cat > agents/manager/schema/output.json <<'JSON'
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["classification"],
  "properties": {
    "classification": {
      "type": "string",
      "enum": ["services", "cli"]
    }
  }
}
JSON
```

### `agents/manager/prompt.md`

```bash
cat > agents/manager/prompt.md <<'MD'
You are a router. Classify the user's question into one of two
categories so it can be answered by the right specialist agent.

- `services` — questions about Movate's offerings (consulting, managed
  services, training, contact info, company background).
- `cli` — questions about the **movate-cli** toolkit (commands,
  features, deploys, evals, agent scaffolding, schemas).

Return JSON of the shape `{"classification": "services" | "cli"}`. No
other fields, no prose.

User question:
{{ input.question }}
MD
```

### Validate the manager

```bash
mdk validate agents/manager
```

**Expect**: ✓ green verdict, prompt hash, eval cost forecast.

---

## Step 3 — Customize the role agents (5 min)

### `agents/services/prompt.md` — services KB embedded

```bash
cat > agents/services/prompt.md <<'MD'
You are a Movate services expert. Answer the user's question using
ONLY the knowledge base below. If the answer isn't in the KB, say
"I don't have that information" and set `confident` to `false`.

## Knowledge base
- Movate is a digital engineering services company.
- Core offerings: consulting, managed services, custom software, AI/ML.
- Headquartered in Plano, TX. Founded 2002.
- Industries served: telecom, retail, healthcare, fintech.

## User question
{{ input.question }}

## Output format
JSON of the shape `{"answer": "...", "confident": true | false}`.
MD
```

### `agents/cli/prompt.md` — CLI KB embedded

```bash
cat > agents/cli/prompt.md <<'MD'
You are a movate-cli expert. Answer the user's question using ONLY
the knowledge base below. If the answer isn't in the KB, say "I
don't have that information" and set `confident` to `false`.

## Knowledge base
- movate-cli is a Python toolkit for building, evaluating, and
  deploying AI agents.
- Key commands: `init` (scaffold), `validate`, `run`, `eval`, `bench`,
  `deploy`, `submit`, `jobs`, `doctor`, `config`.
- Agents are declared via `agent.yaml` (model, prompt, schemas, budget).
- Workflows wire multiple agents via `workflow.yaml`.
- Deploy target: Azure Container Apps via Bicep.

## User question
{{ input.question }}

## Output format
JSON of the shape `{"answer": "...", "confident": true | false}`.
MD
```

### Validate both

```bash
mdk validate agents/services
mdk validate agents/cli
```

**Expect**: ✓ green verdicts for both.

---

## Step 4 — Smoke test each agent with --mock (2 min)

`--mock` returns deterministic dummy data so you can verify wiring
without spending on API calls.

```bash
mdk run agents/manager  '{"question":"What is movate?"}' --mock
mdk run agents/services '{"question":"What is movate?"}' --mock
mdk run agents/cli      '{"question":"What does the eval command do?"}' --mock
```

**Expect**: each returns valid JSON. Mock data won't actually classify
correctly — its job is to prove the schema chain works.

---

## Step 5 — Real run, each agent standalone (3 min)

```bash
# Manager classifies a services question
mdk run agents/manager '{"question":"Where is Movate headquartered?"}'

# Manager classifies a CLI question
mdk run agents/manager '{"question":"How do I scaffold an agent?"}'

# Services expert answers
mdk run agents/services '{"question":"Where is Movate headquartered?"}'

# CLI expert answers
mdk run agents/cli '{"question":"How do I scaffold an agent?"}'
```

**Expect**: each prints a JSON response on stdout and a cost/latency
verdict on stderr. Cost per call ≈ $0.0005-0.002.

---

## Step 6 — Orchestrate the routing in bash (3 min)

The manager-plus-experts pattern, in 5 lines of shell. Save as
`ask.sh`:

```bash
cat > ask.sh <<'BASH'
#!/usr/bin/env bash
set -e
QUESTION="$1"
CLASS=$(mdk run agents/manager "{\"question\":\"$QUESTION\"}" \
        -o json 2>/dev/null | jq -r '.data.classification')
echo "→ router classified as: $CLASS" >&2
mdk run "agents/$CLASS" "{\"question\":\"$QUESTION\"}"
BASH
chmod +x ask.sh
```

Run it:

```bash
./ask.sh "What is movate?"
./ask.sh "How do I run a bench across multiple models?"
./ask.sh "Who founded Movate?"
```

**Expect**: each invocation prints the router's decision to stderr, then
the expert's answer (JSON) to stdout. Total cost per ask ≈ $0.001-0.003.

---

## Step 7 — Eval the manager's routing accuracy (5 min)

### Build a small eval dataset

```bash
cat > agents/manager/evals/dataset.jsonl <<'JSONL'
{"input": {"question": "What is movate?"}, "expected": {"classification": "services"}}
{"input": {"question": "Where is Movate located?"}, "expected": {"classification": "services"}}
{"input": {"question": "When was Movate founded?"}, "expected": {"classification": "services"}}
{"input": {"question": "What industries does Movate serve?"}, "expected": {"classification": "services"}}
{"input": {"question": "How do I scaffold an agent?"}, "expected": {"classification": "cli"}}
{"input": {"question": "What does mdk eval do?"}, "expected": {"classification": "cli"}}
{"input": {"question": "How do I deploy to Azure?"}, "expected": {"classification": "cli"}}
{"input": {"question": "What is mdk bench for?"}, "expected": {"classification": "cli"}}
JSONL
```

### Use exact match (deterministic; no judge call needed)

```bash
cat > agents/manager/evals/judge.yaml <<'YAML'
method: exact_match
fields: [classification]
YAML
```

### Run the eval

```bash
mdk eval agents/manager --gate 0.9 --runs 1
```

**Expect**: pass rate table + green verdict if ≥ 90% of cases routed
correctly. Exit code 0 on pass, 1 on fail. Total cost ≈ $0.008.

---

## Step 8 — Bench the manager across cheap models (5 min)

Compare three models for the manager role. Pick the cheapest one that
still hits the routing accuracy bar.

```bash
mdk bench agents/manager \
  -m openai/gpt-4o-mini-2024-07-18 \
  -m anthropic/claude-haiku-4-5-20251001 \
  --runs 1
```

**Expect**: a table comparing latency / cost / score across the two
models. Total cost ≈ $0.015.

---

## What you just exercised

- ✅ Scaffolded three agents from templates
- ✅ Customized prompts + schemas (enums as guarantees)
- ✅ Validated each agent (schema + lint + cost forecast)
- ✅ Ran each agent locally — both mock and real
- ✅ Orchestrated a router→expert pattern (in shell; later in
  `workflow.yaml` once v1.1 ships)
- ✅ Gated routing accuracy with an eval at 0.9
- ✅ Benched model choices for the cheapest viable manager

Total wall time: **~30 minutes**. Total provider spend: **~$0.15**.

## Known caveats this runbook surfaces for the deck

1. **Workflow conditional edges aren't shipped** — deck slide 18
   (`workflow.yaml` with `when:` clauses) is aspirational. Either flag
   it as "coming in v1.1" in the deck, or rewrite slide 18 to show the
   bash orchestration instead.

2. **`mdk eval` gating + datasets work today** — keep slides 22-23
   as-is.

3. **`mdk bench` works today** — keep slide 24 as-is.

4. **Deploy + production submit haven't been validated against the new
   polish PRs**. Hold slides 26-27 demos until #27 lands on main
   (lazy-imports fix).
