# Migrating Lyzr agents into MDK

If your team's agents currently live on **Lyzr Studio**, MDK's v0.7
Lyzr adapter is the bridge to evaluate them, A/B-test them against
MDK-native alternatives, and ultimately migrate them off Lyzr.

## What MDK gives you

| | Lyzr-only today | Lyzr + MDK (v0.7) | MDK-native (post-migration) |
|---|---|---|---|
| Evaluate against a dataset | Manual | `mdk eval agents/<name>` | `mdk eval agents/<name>` |
| Compare multiple models | Manual | `mdk bench` against Lyzr + LiteLLM models | `mdk bench` across providers |
| Schema-validated outputs | No | Yes (`schema/output.json`) | Yes |
| Versioned prompts (git) | Lyzr's internal | Yes (in your repo) | Yes |
| Cost tracking | Lyzr billing | Lyzr-billed (unknown to MDK) + cost-per-MDK-run | MDK-tracked |
| Policy enforcement | None | Project `policy.yaml` + `runtime.yaml` | Project `policy.yaml` + `runtime.yaml` |
| Multi-runtime fallback | No | No (Lyzr is leaf) | Yes |
| HITL / workflow composition | Lyzr-internal | Lyzr-internal | MDK workflow (v1.1) |

## Three-step migration

### Step 1 — Export the Lyzr agent JSON

In Lyzr Studio:
1. Open your agent's detail page
2. Copy the JSON definition (visible in the API/Export panel)
3. Save it as `<agent-name>.json` in your repo

### Step 2 — Import into MDK as a Lyzr-runtime agent

```bash
mdk import lyzr ./tesla-manager.json -o ./agents
```

This creates:

```
agents/tesla-customer-experience-manager/
├── agent.yaml                ← runtime: lyzr; provider: lyzr/<agent-id>
├── prompt.md                 ← agent_instructions from Lyzr (Jinja-templated)
├── schema/
│   ├── input.json            ← {"message": string}
│   └── output.json           ← {"response": string}
└── lyzr-original.json        ← preserved for audit/diff
```

Set your Lyzr API key and validate:

```bash
export LYZR_API_KEY=sk-default-...   # from Lyzr Studio → Agent → API Key
mdk validate agents/tesla-customer-experience-manager
```

Now you can run the agent through MDK while it still executes on Lyzr's
infrastructure:

```bash
mdk run agents/tesla-customer-experience-manager '{"message":"How long to charge a Model Y?"}'
```

Every call is a `RunRecord` in MDK's storage. You get cost/latency
metrics (Lyzr-tracked plus MDK-tracked invocation overhead), trace
spans, replay, and the full eval/bench machinery.

### Step 3 — Build an eval dataset, then prove parity

Create `agents/tesla-customer-experience-manager/evals/dataset.jsonl`
with representative questions + expected outputs (or LLM-judge rubric):

```jsonl
{"input": {"message": "Where is Tesla HQ?"}, "expected": {"response": "Austin, Texas..."}}
{"input": {"message": "How long to charge a Model Y?"}, "expected": {"confident": true}}
...
```

Configure the judge in
`agents/tesla-customer-experience-manager/evals/judge.yaml`. Then:

```bash
mdk eval agents/tesla-customer-experience-manager --gate 0.85 --runs 3
```

This gives you a **quality baseline** for the Lyzr-hosted version.

### Step 4 — Re-import as MDK-native, compare

```bash
mdk import lyzr ./tesla-manager.json \
    -o ./agents \
    --runtime litellm \
    --force
```

Same agent, but now:
- `runtime: litellm` (not `lyzr`)
- `provider: openai/gpt-5` (the underlying model Lyzr was calling)
- No dependence on Lyzr at runtime — purely MDK-native

Run the **same eval dataset** against this version:

```bash
mdk eval agents/tesla-customer-experience-manager
```

Or A/B-test in one go via bench:

```bash
mdk bench agents/tesla-customer-experience-manager \
    -m openai/gpt-5 \
    -m lyzr/69fe0d9890de3014e9f1cf92 \
    --runs 5
```

If the MDK-native version meets the same quality bar, you have a
data-driven migration story.

## What does NOT port automatically

The Lyzr adapter is a migration bridge — not a feature-parity
reimplementation. The following Lyzr concepts are intentionally
**dropped** during import:

| Lyzr concept | Why dropped | MDK equivalent (eventually) |
|---|---|---|
| `tools` / `tool_configs` | MDK tool registry lands in v1.1 | `movate.tools` (v1.1) |
| `mcp_resources` / `mcp_prompts` | MCP not yet in MDK | tracked for post-v1.1 |
| `voice_config` / `image_output_config` | Out of v1.x scope | TBD |
| `git_agent`, `proxy_config` | Lyzr-specific infrastructure | N/A |
| `a2a_tools` | Lyzr-specific agent-to-agent calls | MDK workflow nodes (v1.1) |
| `max_iterations` | Lyzr-specific ReAct loop control | MDK has direct schema-validated outputs; no loop needed |
| `managed_agents` | Lyzr handles routing internally | **Manual migration to MDK workflow** (see below) |

### Managed agents = your manager + role agents pattern

If your Lyzr agent has `managed_agents` (sub-agents that the manager
routes to), Lyzr handles that orchestration internally. MDK currently
calls the manager as a single black-box invocation — you don't see
the sub-agents.

Once MDK v1.1 ships conditional workflow edges, you can replicate
this pattern explicitly:

```yaml
# workflow.yaml (v1.1)
kind: Workflow
nodes:
  - { id: manager, type: agent, ref: ./agents/tesla-manager }
  - { id: vehicle, type: agent, ref: ./agents/r-vehicle-support }
  - { id: charging, type: agent, ref: ./agents/r-charging }
  # ...
edges:
  - { from: manager, to: vehicle, when: "$.intent == 'vehicle'" }
  - { from: manager, to: charging, when: "$.intent == 'charging'" }
  # ...
```

Until then, orchestrate routing in a client script (see
[`movate-faq-demo/ask.py`](https://github.com/jeremyyuAWS/movate-faq-demo/blob/main/ask.py)
for a 40-line example).

## Strategic posture

The Lyzr adapter exists so customers **don't have to wait for migration**
to start benefiting from MDK's eval/bench/observability surface. But
it's a **time-boxed** capability — once a customer's agents are migrated
to MDK-native runtimes, the Lyzr adapter becomes vestigial.

We will:
- ✅ Maintain the Lyzr adapter through v1.1
- ✅ Preserve license-clean posture (HTTP-only, no Lyzr SDK dependency)
- ❌ Not extend Lyzr feature parity beyond what's documented here
- ❌ Not build push-to-Lyzr or bidirectional sync
- ❌ Not add MDK features that only make sense under `runtime: lyzr`

## Troubleshooting

### `LYZR_API_KEY env var is not set`
You haven't exported the Lyzr API key. Copy it from Lyzr Studio → Agent
detail → API Key (the field starting with `sk-default-`).

### `Lyzr returned 401`
Either the API key is wrong, the agent ID is wrong, or the key isn't
authorized for that agent. Confirm both in Lyzr Studio.

### `Cannot map Lyzr provider_id=X to a LiteLLM family`
Your Lyzr agent uses a provider MDK doesn't yet know about. Edit
`agent.yaml` after import to set `model.provider` manually (LiteLLM
supports many providers; see `mdk pricing` for the catalog). Or open
an issue with the provider name to add a mapping.

### My imported agent's prompt doesn't reference `{{ input.message }}`
The import always adds the Jinja reference at the bottom of `prompt.md`.
If you've customized the prompt, re-add `{{ input.message }}` wherever
the model should see the user's question.

### Cost shows $0 for Lyzr runs
Correct — Lyzr's inference API doesn't return token counts, so MDK
can't compute cost on its side. The actual cost is whatever Lyzr bills
you. We'll surface "Lyzr-billed; consult Lyzr Studio" explicitly in
metrics in a follow-up.
