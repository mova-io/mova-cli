# MDK User Guide — every command, in order of use

This guide walks you from a clean machine to a deployed agent, then catalogs every command the MDK CLI ships today (sprints O–U + 4 polish bundles + `mdk init --llm`). Aimed at engineers who want one document to keep open while they work.

The binary is `mdk` (the `movate` alias also works). Run `mdk --help` to see the same command list grouped by panel.

> **New:** `mdk init <name> --llm "<description>"` now scaffolds a complete agent from a natural-language description. See [LLM-driven scaffolding](#llm-driven-scaffolding-mdk-init---llm) below.

---

## Table of contents

1. [The five-minute walkthrough](#the-five-minute-walkthrough)
2. [Lifecycle: create → validate → run → deploy](#lifecycle)
3. [Inspection & debugging](#inspection--debugging)
4. [Eval & quality](#eval--quality)
5. [Snapshots, rollback, audit](#snapshots-rollback-audit)
6. [Profiles, secrets, config](#profiles-secrets-config)
7. [Memory, policy, tenants, auth](#memory-policy-tenants-auth)
8. [Deploy & operate](#deploy--operate)
9. [Importing from other frameworks](#importing-from-other-frameworks)
10. [Quick reference: every command](#quick-reference-every-command)

---

## The five-minute walkthrough

Two paths — pick one. Both end at the same place.

### Path A: LLM-scaffold (fastest)

```bash
# 1. Bootstrap a project
mdk init --project my-project
cd my-project
cp .env.example .env          # paste your OPENAI_API_KEY

# 2. Scaffold an agent from a natural-language description
mdk init faq-agent --llm "An FAQ agent for our SaaS product. Answers questions \
  about pricing, features, and trial limits. Returns answer + confidence."

# 3. Validate, run, eval, ship
mdk validate ./faq-agent
mdk run ./faq-agent '{"question":"do you offer SAML?"}'
mdk eval ./faq-agent --gate 0.7
mdk audit current
mdk snapshot create -d "first green eval"
```

Don't have an API key yet? Add `--mock` to the `init` command and the LLM call is replaced by a deterministic mock — useful for offline CI rehearsal.

### Path B: Template-scaffold (classic)

```bash
# 1. Bootstrap a project
mdk init --project my-project
cd my-project
cp .env.example .env          # paste your OPENAI_API_KEY

# 2. Scaffold an agent from a packaged template
mdk init faq-agent -t faq

# 3-7. Same as Path A from validate onward
mdk validate ./faq-agent
mdk run ./faq-agent --mock '{"question":"hi"}'
mdk eval ./faq-agent --mock --gate 0.7
mdk audit current
mdk snapshot create -d "first green eval"
```

You can deploy with `mdk deploy` once an Azure target is registered (`mdk config add-target …`). See [Deploy & operate](#deploy--operate).

---

## Lifecycle

### `mdk init` — scaffold an agent or project

Three modes:

- **Project mode** — bootstrap a fresh workspace.
  ```bash
  mdk init --project my-project        # creates ./my-project/
  mdk init --project                   # bootstraps current directory in place
  ```
  Drops `movate.yaml`, `.env.example`, `.gitignore`, empty `agents/`, and an initial snapshot so `mdk diff` / `mdk rollback` have a baseline from day one.

- **Agent mode** (default) — scaffold one agent from a packaged template.
  ```bash
  mdk init faq-agent                   # default template
  mdk init my-classifier -t classifier # different template
  ```

- **LLM-scaffold mode** — generate the agent from a natural-language description (see the dedicated section [below](#llm-driven-scaffolding-mdk-init---llm)).
  ```bash
  mdk init faq-agent --llm "FAQ agent for our SaaS pricing"
  ```

| Flag | Purpose |
|---|---|
| `--project` | Bootstrap project workspace instead of an agent |
| `--template / -t` | Template to scaffold from (run `mdk init --help` for the list) |
| `--llm <description>` | Generate the agent from a natural-language description |
| `--llm-model <model>` | Override the model used for `--llm` (default: `openai/gpt-4o-mini-2024-07-18`) |
| `--mock` | Use the deterministic MockProvider for `--llm` (no API key needed) |
| `--dry-run` | Preview the generated files without writing (use with `--llm`) |
| `--target` | Parent directory for the new agent/project (default `.`) |
| `--force / -f` | Overwrite an existing directory |
| `--skip-snapshot` | Skip the initial baseline snapshot (project mode only) |

#### LLM-driven scaffolding (`mdk init --llm`)

Pass `--llm "<description>"` to generate the full agent (agent.yaml + prompt.md + JSON schemas + seed eval cases) from natural language. The CLI calls an LLM with a meta-prompt + two few-shot exemplars (FAQ + classifier templates), validates the output by loading it back through the standard loader, and retries once with the error fed back to the model if validation fails. On second failure, the raw payload is stashed at `.movate/llm-init-failed-<name>.json` for inspection.

**Example:**

```bash
$ mdk init faq-agent --llm "An FAQ agent for our SaaS product. Answers questions \
  about pricing, features, and trial limits. Returns an answer string plus a \
  confidence number between 0 and 1."

⠋ scaffolding agent 'faq-agent' from description...
╭─ ✓ LLM-scaffolded agent ──────────────────────────╮
│ Agent:    faq-agent                                │
│ Path:     /Users/.../faq-agent                     │
│ Files:                                             │
│   • agent.yaml                                     │
│   • prompt.md                                      │
│   • schema/input.json                              │
│   • schema/output.json                             │
│   • evals/dataset.jsonl (2 seed cases)             │
│ Cost:     $0.000401 USD                            │
│                                                    │
│ Next steps:                                        │
│   $ mdk validate /Users/.../faq-agent              │
│   $ mdk run /Users/.../faq-agent --mock '{...}'    │
│   $ mdk eval /Users/.../faq-agent --mock --gate 0.7│
│                                                    │
│ scaffolded by --llm · review prompt.md and the     │
│ schemas before first real run.                     │
╰────────────────────────────────────────────────────╯
→ scaffolded by --llm · review prompt.md before first real run

mdk_init_summary: name=faq-agent llm=true model=openai/gpt-4o-mini-2024-07-18 \
  input_tokens=1539 output_tokens=268 cost_usd=0.000401 retried=false ok=true
```

**Key behavior:**

- **Validation loop**: write to tempdir → `load_agent()` → retry once on failure → debug artifact on second failure.
- **Name override**: the CLI forces `agent_yaml.name = <CLI arg>` after generation so a forgetful LLM that echoes the few-shot exemplar's name can't break the dir↔file-name correspondence.
- **Pre-flight dest check**: errors before the LLM call if the target dir exists and `--force` isn't set — no wasted tokens.
- **Greppable summary line**: `mdk_init_summary:` mirrors `mdk_audit_summary` / `mdk_eval_summary` / `mdk_doctor_summary` for CI parity.
- **Cost echo**: total cost in USD shown both in the success Panel and in the summary line. Missing pricing entry → `cost_usd=unknown` rather than crash.

**Failure paths:**

- Provider error or schema-mismatch on first call → exit 2 with the error.
- First load_agent failure → automatic retry with the error fed back to the model.
- Retry also fails → exit 1, raw payload saved to `.movate/llm-init-failed-<name>.json`. Inspect, fix manually, or re-run with a refined description.

**Hermetic CI mode**: pair with `--mock` to swap in the deterministic MockProvider. Set `MOVATE_MOCK_RESPONSE` to a valid `GeneratedAgent` JSON string to exercise the full path offline.

### `mdk demo` — generate a complete runnable demo

```bash
mdk demo                          # writes ./demo-faq/
mdk demo my-demo --force          # specific name, overwrite
```

Drops a fully populated FAQ-agent project (agent + dataset + working eval). Good for first-touch exploration, smoke testing, and as a known-good baseline when something else breaks.

### `mdk validate` — load + validate an agent or workflow

```bash
mdk validate ./agents/faq-agent
mdk validate ./workflows/returns --strict
```

Auto-detects agent vs workflow by file presence. With `--strict`, warnings become errors. YAML errors now report `path:line:col` so your editor jumps straight to the offending byte.

| Flag | Purpose |
|---|---|
| `--strict` | Promote warnings to errors |
| `--no-lint` | Skip the lint pass (schema validation only) |

### `mdk run` — execute an agent or workflow

```bash
mdk run ./agents/faq-agent '{"text":"hello"}'
mdk run ./agents/faq-agent --mock '{"text":"hello"}'              # no API key needed
mdk run ./agents/faq-agent -i input.json --stream                 # input from file + stream tokens
mdk run ./workflows/returns '{"order_id":"abc"}' --replay rec-id  # deterministic replay
```

After a successful run, `mdk run` echoes the `run_id` on stderr with the exact `mdk replay <id>` command. The run is persisted to SQLite (or Postgres if configured) for later inspection.

| Flag | Purpose |
|---|---|
| `--input / -i` | Read input JSON from file |
| `--mock` | Use deterministic MockProvider (no API keys) |
| `--stream` | Stream tokens as they arrive |
| `--replay <run-id>` | Re-execute a stored run with the same input |
| `--output / -o` | `json` (default), `yaml`, `table` |

### `mdk doctor` — environment sanity check

```bash
mdk doctor
mdk doctor --explain                    # per-check what/why/fix block
mdk doctor --licenses                   # per-dep SPDX license report
mdk doctor --target prod                # adds Azure preflight
```

Reports on Python version, deps, provider keys, tracer, storage, pricing table, and project config. Ends with a greppable `mdk_doctor_summary: checks=N ok=N missing=N error=N` line for CI.

If something's red, `mdk fix --list` shows what can be auto-remediated. `mdk fix --apply` runs the fixes.

---

## Inspection & debugging

### `mdk show` — print raw resolved config

```bash
mdk show ./agents/faq-agent
mdk show ./workflows/returns
mdk show ./skills/web-search
```

Prints the YAML as-resolved (relative paths absolutized, defaults filled in). Use `mdk inspect` for the deeper "what does the loader actually see" view.

### `mdk inspect agent <name>` — resolved AgentBundle view

```bash
mdk inspect agent faq-agent
mdk inspect agent faq-agent --only prompt
mdk inspect agent faq-agent --json
```

Shows the agent after defaults merge + schema compilation + context prepending — i.e. exactly what the executor sees. `--only {identity,model,prompt,schemas,skills,contexts}` lets you focus.

### `mdk replay <run-id>` — re-run a past invocation

```bash
mdk replay abc12345
mdk replay abc12345 --diff              # side-by-side with original
mdk replay abc12345 --mock              # mock-mode replay (no API spend)
```

Looks up the stored RunRecord, re-executes against the same input with the current agent definition, and (with `--diff`) shows what changed. Used by `mdk run` — the echo at end of every run tells you the exact replay command.

### `mdk diff` — snapshot vs snapshot, or working tree vs git ref

```bash
mdk diff abc12345 def67890               # two snapshots
mdk diff abc12345                        # one snapshot vs current state
mdk diff --git --ref HEAD~3              # git-style: working tree vs ref
```

Status letters are git-style: `A` added, `M` modified, `D` deleted.

### `mdk monitor` — live runs dashboard

```bash
mdk monitor                              # poll every 2s
mdk monitor --once                       # snapshot view, exit
mdk monitor --agent faq-agent --status failed --limit 50
```

Refreshes a Rich table of recent runs from storage. Use during a real-world load test or while debugging a flaky agent.

### `mdk trace replay <run-id>` — render the trace timeline

```bash
mdk trace replay abc12345
mdk trace replay abc12345 --output json
```

Like `mdk replay` but focused on the *trace* — input, output, metrics, error, per-node summary for workflows.

### `mdk logs <run-id>` — ⚠ deferred to v0.4

Stub today. Will tail run/job output once it's wired.

### `mdk watch` — TDD hot-reload loop

```bash
mdk watch ./agents/faq-agent
mdk watch ./agents/faq-agent --strict --poll-interval 0.5
```

Re-runs `validate` on every file change. Pair with a terminal split for tight prompt iteration.

### `mdk chat` — interactive REPL

```bash
mdk chat ./agents/faq-agent
mdk chat ./agents/faq-agent --no-memory --no-stream
mdk chat ./agents/faq-agent --mock
```

Multi-turn chat bound to one agent. Memory is on by default (see [`mdk memory`](#mdk-memory--list--get--set--evict--summarise--query)).

---

## Eval & quality

### `mdk eval` — score an agent against its dataset

```bash
mdk eval ./agents/faq-agent --gate 0.7
mdk eval ./agents/faq-agent --mock --gate 0.7
mdk eval ./agents/faq-agent --runs 3 --gate-mode mean
mdk eval ./agents/faq-agent --baseline-file .movate/baseline.json --regression-tolerance 0.05
mdk eval ./agents/faq-agent --objective routing-accuracy
mdk eval https://faq-runtime.example.com --agent-yaml ./faq-agent --api-key mvt_dev_...
```

Runs the eval suite, exit 0 on pass / 1 on gate-fail or regression. Shows a `✓ Eval PASSED` / `✗ Eval FAILED` banner above the table and a greppable `mdk_eval_summary: …` line at the end.

| Flag | Purpose |
|---|---|
| `--gate` | Per-case score threshold |
| `--gate-mode` | How to aggregate N runs: `mean` (default) / `min` / `p10` |
| `--runs / -r` | Runs per case (use 3+ for LLM-judge to defeat sampling variance) |
| `--mock` | Hermetic CI mode |
| `--baseline <eval-id>` | Diff against a stored EvalRecord (sqlite) |
| `--baseline-file <path>` | Diff against a JSON baseline (CI-friendly) |
| `--output-baseline <path>` | Write the current run as a JSON baseline |
| `--regression-tolerance` | Allowable score drop before flagging regression |
| `--objective <id>` | Gate on a specific objective's threshold |
| `--output / -o` | `table` (default), `json`, `markdown` |

### `mdk eval-gen <agent>` — LLM-generate eval cases

```bash
mdk eval-gen faq-agent --num 10
mdk eval-gen faq-agent --num 5 --sample-input '{"text":"sample"}'
mdk eval-gen faq-agent --num 20 --output evals/extra.jsonl
```

Uses an LLM to generate dataset entries when you don't have enough real-world traffic yet. `mdk audit`'s `missing-evals` finding embeds this exact command.

### `mdk bench` — compare an agent across multiple models

```bash
mdk bench ./agents/faq-agent -m openai/gpt-4o-mini -m anthropic/claude-sonnet-4
mdk bench ./agents/faq-agent --judge openai/gpt-4o --runs 3
mdk bench ./agents/faq-agent --mock                    # multi-model mock comparison
```

Runs the same input across the listed models and scores them side-by-side. Repeatable `--model / -m`.

### `mdk benchmark live <agent>` — shadow-traffic replay

```bash
mdk benchmark live faq-agent --candidate-model anthropic/claude-sonnet-4 --limit 100
mdk benchmark live faq-agent --since-days 7 --persist
```

Replays recorded production runs against a candidate model and reports the score / cost / latency delta. Different from `bench` — you're testing one candidate against real traffic, not picking from a slate.

### `mdk simulate <agent>` — multi-turn chatbot stress test

```bash
mdk simulate faq-agent --num 10 --max-turns 5
mdk simulate faq-agent --scenarios scenarios/edge-cases.jsonl
```

LLM-driven simulated user runs through scenarios — useful for chatbots that can't be evaluated single-turn.

### `mdk ci eval` — gate every agent in the project

```bash
mdk ci eval --mock
mdk ci eval --regression-tolerance 0.05 --summary-file github-summary.md
```

Walks every agent in `./agents/`, runs eval against its baseline (default location: `.movate/<agent>/baseline.json`), and reports pass/fail in one go. Designed to drop into a GitHub Actions step.

### `mdk audit [snapshot-or-current]` — production-readiness scanner

```bash
mdk audit current
mdk audit abc12345                       # scan a snapshot before promoting
mdk audit current --strict               # warnings fail (CI gate)
mdk audit current --json | jq .
mdk audit current --category exposed-secret
```

Findings include a category, severity, target, message, hint, AND (where applicable) the exact `mdk fix` command to remediate. Ends with a greppable `mdk_audit_summary: …` line.

### `mdk costs report` — historical cost rollup

```bash
mdk costs report
mdk costs report --since-days 7 --group-by provider
mdk costs report --output json | jq .
```

Aggregates spend per agent or per provider from recorded runs.

### `mdk tune <agent>` — deterministic knob sweep

```bash
mdk tune faq-agent '{"text":"sample"}' --sweep temperature=0.0,0.3,0.7
mdk tune faq-agent '{"text":"sample"}' --sweep model=openai/gpt-4o-mini,anthropic/claude-sonnet-4
mdk tune faq-agent '{"text":"sample"}' --sweep max_tokens=128,256,512 --runs 3
```

Vary one knob at a time across a fixed input; report score/cost/latency at each setting. Useful when you suspect "if I just lower the temperature..."

### `mdk pricing` — show the packaged price table

```bash
mdk pricing
mdk pricing --provider openai
mdk pricing --output json
```

---

## Snapshots, rollback, audit

### `mdk snapshot <verb>` — capture / inspect / delete project state

```bash
mdk snapshot create -d "before refactor"
mdk snapshot list
mdk snapshot show abc12345
mdk snapshot delete abc12345 --force
```

Snapshots are content-addressed (sha256 of the project tree). They're immutable: the same content always gets the same hash. `mdk snapshot list` shows a relative-time Age column.

### `mdk rollback <hash>` — restore a snapshot

```bash
mdk rollback abc12345 --dry-run
mdk rollback abc12345 --force
```

Restores every tracked file. Auto-creates a "pre-rollback" snapshot first so you can undo the undo.

### `mdk migrate <snapshot>` — surgical per-file restore

```bash
mdk migrate abc12345 --filter "agents/faq-agent/prompt.md" --dry-run
mdk migrate abc12345 --filter "*.yaml" --apply --backup
```

The scalpel vs `rollback`'s hammer. Use when one file regressed and you don't want to bring back everything else.

### `mdk promote` — record snapshot promotion to a profile

```bash
mdk promote abc12345 --to staging
mdk promote abc12345 --to prod --eval-pass-rate 0.95
mdk promote --list
mdk promote --current --profile prod
```

Audit trail for dev → staging → prod. Doesn't deploy — it records that this hash is approved for that environment. Pair with `mdk audit <snapshot>` first.

---

## Profiles, secrets, config

### `mdk profiles <verb>` — kubectl-context-style environments

```bash
mdk profiles create dev --tenant-id local
mdk profiles create prod --tenant-id acme-corp --target prod-runtime
mdk profiles list
mdk profiles use dev                     # switch active profile
mdk profiles current                     # print active name (shell-friendly)
mdk profiles show dev
mdk profiles delete old-staging --force
```

A profile bundles a deployment target + tenant + a secrets namespace. Switch profiles to switch all three.

### `mdk secrets <verb>` — per-profile secret values

```bash
mdk secrets set OPENAI_API_KEY                          # prompts interactively
mdk secrets set OPENAI_API_KEY --value sk-...           # non-interactive
mdk secrets list
mdk secrets get OPENAI_API_KEY
mdk secrets delete OLD_KEY --force
eval "$(mdk secrets export-shell)"                      # source as env vars
```

Per profile. The `export-shell` form is how you wire secrets into `mdk run` without putting them in your shell rc.

### `mdk config <verb>` — user-level config

```bash
mdk config add-target prod --url https://faq-runtime.example.com --key-env MDK_API_KEY \
  --azure-subscription <sub-id> --azure-resource-group movate-prod --azure-acr movateacr
mdk config list-targets
mdk config use prod                       # set default --target
mdk config show
mdk config remove-target old-target
```

Lives in `~/.movate/config.yaml`. Targets are referenced by `mdk deploy --target prod` and by remote-eval / submit / jobs commands.

---

## Memory, policy, tenants, auth

### `mdk memory <verb>` — agent memory

```bash
mdk memory list faq-agent
mdk memory list faq-agent --since-days 7
mdk memory get faq-agent user-context
mdk memory set faq-agent user-context '{"name":"alice"}' --ttl-seconds 86400
mdk memory delete faq-agent user-context --force
mdk memory evict faq-agent --before-days 30 --force
mdk memory summarise faq-agent
mdk memory query faq-agent "billing"
```

Default backend is a JSON file at `~/.movate/memory.json`; override with `MOVATE_MEMORY_FILE`. `mdk chat` writes here unless invoked with `--no-memory`.

### `mdk policy <verb>` — project policy.yaml

```bash
mdk policy export                         # normalized YAML to stdout
mdk policy export --output policy-snapshot.yaml
mdk policy import new-policy.yaml         # validates + writes to policy.yaml
mdk policy diff new-policy.yaml           # dry-run compare
```

`policy.yaml` controls promotion safety (allowed providers, model deny-list, max cost per run, fallback chain).

### `mdk auth <verb>` — runtime API keys

```bash
mdk auth create-key --tenant-id acme --env live --label ci-deployer
mdk auth list-keys
mdk auth list-keys --include-revoked
mdk auth revoke-key key_abc12345
```

Keys are `mvt_<env>_<tenant>_<keyid>_<secret>`. The secret is shown ONCE on create — there's no recovery.

### `mdk tenants <verb>` — operator tenant management

```bash
mdk tenants set-budget acme --monthly-usd 500
mdk tenants show acme
mdk tenants list
mdk tenants clear-budget acme --yes
```

Operator-only. Enforced at executor entry — runs that would exceed the budget are rejected before they hit the provider.

---

## Deploy & operate

### `mdk serve` — run the FastAPI runtime

```bash
mdk serve --host 0.0.0.0 --port 8080 --agents-path ./agents
mdk serve --rate-limit-per-minute 60 --cors-origins https://app.example.com
```

Boots `/run`, `/jobs/{id}`, `/agents`, `/healthz`. Pair with `mdk worker` to drain the queue.

### `mdk worker` — drain the job queue

```bash
mdk worker
mdk worker --tenant-id local --mock
mdk worker --agents-path ./agents --workflows-path ./workflows
```

Claim-next-job loop. Run alongside `mdk serve`; horizontally scale by booting more workers.

### `mdk deploy` — push to Azure Container Apps

```bash
mdk deploy --target prod
mdk deploy --target prod --image-tag v1.2.3 --no-wait
mdk deploy --target staging --skip-build         # reuse last image
```

Requires a registered target (`mdk config add-target …`) with the Azure fields populated. Builds the container, pushes to ACR, updates the ACA revision. Run `mdk doctor --target prod` first to catch broken auth/permissions before deploy spins.

### `mdk export oci-bundle <agent>` — portable artifact

```bash
mdk export oci-bundle faq-agent --output faq-agent.tar.gz
mdk export oci-bundle faq-agent --force
```

Creates an OCI-compatible tarball of the agent — pass it to anyone who runs movate to load it without your project layout.

### `mdk submit` — queue a job at a deployed runtime

```bash
mdk submit faq-agent '{"text":"hello"}'              # fire-and-forget
mdk submit faq-agent '{"text":"hello"}' --wait        # block until terminal
mdk submit returns '{"order_id":"x"}' --kind workflow --target prod --notify
```

The remote sibling of `mdk run`. Pairs with `mdk jobs` for polling.

### `mdk jobs <verb>` — inspect remote jobs

```bash
mdk jobs show job_abc12345 --target prod
mdk jobs wait job_abc12345 --timeout 300
mdk jobs list --status running
mdk jobs list-agents                                  # what the runtime can run
```

### `mdk teams-bot serve` — Teams Bot Framework webhook

```bash
mdk teams-bot serve --host 0.0.0.0 --port 3978 \
  --runtime-url https://faq-runtime.example.com --fleet-api-key mvt_...
```

Boots a webhook that adapts Teams messages to runtime `/run` calls.

---

## Importing from other frameworks

### `mdk import lyzr <file>` — Lyzr Studio JSON → MDK agent

```bash
mdk import lyzr ./lyzr-export.json --target ./agents --force
```

### `mdk import openapi <spec>` — OpenAPI spec → one skill per operation

```bash
mdk import openapi ./openapi.yaml --output ./skills --prefix petstore-
mdk import openapi ./openapi.yaml --only "getPetById,createPet" --dry-run
```

### `mdk import json <file>` — generic JSON → agent

```bash
mdk import json ./my-spec.json --runtime litellm
```

---

## Scaffolding & formatting

### `mdk scaffold tool <name>`
```bash
mdk scaffold tool web-search --target ./tools
```
Drops `tool.yaml` + `handler.py` + schema files.

### `mdk skills <verb>`
```bash
mdk skills list
mdk skills scaffold my-skill
mdk skills run my-skill '{"query":"hello"}' --timeout 30
```

### `mdk fmt`
```bash
mdk fmt                       # format the whole project in place
mdk fmt --check               # exit 1 if anything would change (CI gate)
mdk fmt --diff                # show the diff without applying
mdk fmt agents/faq-agent      # scoped
```

Prettier-style. Normalizes `agent.yaml`, `movate.yaml`, prompts, and JSONL evals.

### `mdk docs runbook`
```bash
mdk docs runbook --output RUNBOOK.md
mdk docs runbook --dry-run
```

Generates an ops-friendly markdown runbook for the current project.

### `mdk menu` — workspace status + next steps
```bash
mdk menu
mdk menu --auto                # auto-pick the suggested next action
```

If you're lost, run this. It reports project state and suggests the next command for where you are.

---

## Quick reference: every command

Grouped as they appear in `mdk --help`. Items marked **⚠ deferred** are stubs that error on invocation.

### Develop
- `mdk menu`, `mdk demo`, `mdk init`, `mdk validate`, `mdk show`, `mdk watch`
- `mdk inspect agent`
- `mdk fmt`, `mdk docs runbook`
- `mdk scaffold tool`
- `mdk skills {list, scaffold, run}`
- `mdk import {lyzr, json, openapi}`

### Run & evaluate
- `mdk run`, `mdk replay`, `mdk chat`, `mdk submit`
- `mdk jobs {show, wait, list, list-agents}`
- `mdk eval`, `mdk eval-gen`, `mdk bench`, `mdk benchmark live`, `mdk ci eval`, `mdk simulate`
- `mdk tune`
- `mdk trace replay`
- `mdk logs` **⚠ deferred (v0.4)**

### Diagnose
- `mdk doctor`, `mdk fix`, `mdk pricing`, `mdk monitor`
- `mdk costs report`

### Deploy & operate
- `mdk serve`, `mdk worker`, `mdk deploy`
- `mdk export oci-bundle`
- `mdk teams-bot serve`

### Manage
- `mdk snapshot {create, list, show, delete}`
- `mdk diff`, `mdk rollback`, `mdk migrate`, `mdk audit`, `mdk promote`
- `mdk profiles {create, list, show, use, delete, current}`
- `mdk secrets {set, get, list, delete, export-shell}`
- `mdk config {add-target, list-targets, use, show, remove-target}`
- `mdk policy {export, import, diff}`
- `mdk memory {list, get, set, delete, evict, summarise, query}`
- `mdk auth {create-key, list-keys, revoke-key}`
- `mdk tenants {set-budget, clear-budget, show, list}`

### Global flags (on every command)
- `--verbose / -v` — extra detail
- `--quiet / -q` — suppress stderr hints
- `--target / -t` — pick a registered deployment target
- `--version / -V` — print version

### Greppable CI signals
Three diagnostic commands emit a one-line `mdk_<command>_summary:` machine-readable footer (table mode only — JSON modes stay clean):

```
mdk_doctor_summary: checks=42 ok=36 missing=5 error=1
mdk_audit_summary:  agents=3 errors=0 warnings=2 info=1 strict=false blocks_deploy=false
mdk_eval_summary:   agent=faq-agent eval_id=abc12345 cases=10 passing=9 pass_rate=0.900 mean_score=0.85 gate=0.70 overall_pass=true regressed=false
```

Pipe to `grep mdk_.*_summary` in CI to capture them all.

---

## Where to look when…

- **"I just want a working example"** → `mdk demo`
- **"My agent isn't loading"** → `mdk validate <path>` (file:line:col errors), then `mdk inspect agent <name>`
- **"Why did this run produce that output?"** → `mdk replay <run-id> --diff`
- **"Is this safe to deploy?"** → `mdk audit current --strict` then `mdk eval --gate 0.7 --baseline-file …`
- **"How do I undo my last 10 minutes?"** → `mdk snapshot list` → `mdk rollback <hash>`
- **"Where's my Azure deploy stuck?"** → `mdk doctor --target prod`
- **"How much is this costing me?"** → `mdk costs report --since-days 7`
