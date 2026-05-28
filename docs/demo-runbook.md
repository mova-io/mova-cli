# MDK demo runbook — architecture review (Wednesday)

**Status:** Live demo script (companion to [`docs/architecture-review.md`](architecture-review.md))
**Audience:** Architecture reviewers + the demo driver
**Duration:** ~15 minutes (10 segments x ~1 min, plus a short close)
**Date:** 2026-05-27

This runbook walks reviewers through MDK's end-to-end story — **scaffold ->
eval -> deploy -> observe** — using only commands the driver can run live on
`origin/main`. Total time: ~15 min. Every segment cross-links the
[architecture-review packet](architecture-review.md) (PR #511) so the demo
*shows* what the packet *argues*: the packet is the long-form architectural
case; this runbook is the live proof.

Convention: each segment lists **what you'll see**, the **exact command(s)**,
**expected output** (terse), and **what this proves** (the cross-link into the
packet's section / ADR). Features still on unmerged PRs are marked
`(future, PR #NNN)` and skipped during the demo — call them out verbally
only.

Pre-flight: run the night-before checklist at the end of this doc first. If
anything red, fix it before the meeting.

---

## 1. The layered command surface — `mdk --help` (~60s)

**What you'll see.** Typer prints the `mdk` help with five Rich panels:
**Develop**, **Run & evaluate**, **Deploy & operate**, **Diagnose**,
**Manage**. The panels are the contract between the driver and the system.

```bash
mdk --help
```

Expected output (abridged):

```
 Develop          init, dev, demo, add, compose, plan, validate, ...
 Run & evaluate   run, eval, replay, tune, simulate, benchmark, chat, ...
 Deploy & operate deploy, promote, rollback, canary, infra, ...
 Diagnose         doctor, fix, explain, trace, logs, ...
 Manage           tenants, keys, secrets, profiles, policy, ...
```

**What this proves for the architecture review.** Separation of concerns at
the surface level. The Develop panel is **control plane**; Run & evaluate +
Deploy & operate are **control-plane verbs that *drive* the execution plane**;
nothing in the CLI imports `runtime`. See
[`docs/architecture-review.md` §2 "The two planes + the seams"](architecture-review.md#2-the-two-planes--the-seams)
and the boundary rule in `CLAUDE.md` #6.

---

## 2. Natural-language scaffold — `mdk init` (~90s)

**What you'll see.** A single command takes a free-text description and
yields a runnable project: `project.yaml` + `.env.example` + `AGENTS.md` +
`agents/faq-bot/` with `agent.yaml`, `prompt.md`, an `evals/` dataset, an
initial snapshot, and a post-scaffold `--mock` eval baseline.

```bash
cd /tmp && rm -rf mdk-demo && mkdir mdk-demo && cd mdk-demo
mdk init faq-bot --llm "an FAQ bot for our pricing tiers" --mock
```

Expected output: scaffold tree printed, baseline eval row ("post-scaffold
baseline: PASS"), and a "Next steps" panel pointing at `mdk run faq-bot
--mock '{}'` and `mdk dev faq-bot`.

**What this proves.** ADR 023 (auto-retrieval block lands in `agent.yaml`
when the description warrants it), ADR 025 (the **Action Catalog** drove the
scaffold), ADR 026 (`init` always yields a runnable project, `--bare` would
yield a standalone single-dir agent), ADR 028 (template gallery + use-case
matcher). The same describe-pipeline will be exposed at
`POST /api/v1/agents/preview` for the front end — *(future, PR #524 — ADR 032
D1)*. See [§3 "Authoring" pillar](architecture-review.md#3-the-pillars) and
[§6 "Front-end API readiness"](architecture-review.md#6-front-end-api-readiness).

---

## 3. Schema + pre-flight — `mdk validate` and `mdk doctor agent` (~60s)

**What you'll see.** `validate` runs schema + linter against `agent.yaml`
and friends; `doctor agent <name>` does an env/secrets/runtime-key pre-flight
specific to one agent — friendly errors with fix hints, not stack traces.

```bash
mdk validate faq-bot
mdk doctor agent faq-bot
```

Expected output: a Rich check-list, all green for the fresh scaffold; under
`doctor agent` you'll see provider-key probes, runtime-key precedence, and
schema sanity.

**What this proves.** Friendly errors + `agent.yaml` schema invariants are
the *user-facing* compat contract (`CLAUDE.md` #5). See [§3
"Authoring" pillar](architecture-review.md#3-the-pillars) and
[`docs/adr/022-runtime-key-precedence.md`](adr/022-runtime-key-precedence.md).

---

## 4. Local execution — `mdk run --mock` (~60s)

**What you'll see.** A local invocation of the same `core.Executor` that
runs inline in the runtime and the worker. `--mock` swaps in the
deterministic mock provider — zero spend, hermetic.

```bash
mdk run faq-bot --mock '{"question": "what is the pro tier?"}'
```

Expected output: agent response, a one-line `metrics` summary (tokens / cost
/ latency / turns), and a `mdk explain <run-id>` hint.

**What this proves.** *One executor, three planes.* The same shared engine
(`core/executor.py`) runs locally, in the FastAPI runtime, and on the KEDA
worker — so demo behavior is production behavior. Per-step accounting is in
the persisted `RunRecord`, readable offline. See [§3 "Execution"
pillar](architecture-review.md#3-the-pillars), ADRs 015 (self-hosted
observability) and 024 (per-step observability).

---

## 5. Continuous-eval loop — `mdk eval` (~90s)

**What you'll see.** The agent's dataset runs against the agent + the LLM
judge. Output is a Rich scorecard with per-case pass/fail and aggregate
cost/latency/judge-score.

```bash
mdk eval faq-bot --mock
```

Expected output: scorecard table, `passed: N/N`, total cost (mock = 0),
total tokens, p50/p95 latency.

**What this proves.** ADR 016 is the central governance pillar — harvest
prod -> dataset -> continuous-eval -> drift -> canary gate. Cost/latency are
**retained per turn** (ADR 024), so an eval gate can fail on regression, not
just on accuracy. See [§3 "Governance"
pillar](architecture-review.md#3-the-pillars), ADR 016, and ADR 024.

---

## 6. Authoring inner loop — `mdk dev` (~90s)

**What you'll see.** A foreground REPL-ish loop: edit `agents/faq-bot/prompt.md`
or drop a file into `contexts/` and `mdk dev` re-runs the agent and shows the
new output (diff vs. last run). The conversational copilot is the same Action
Catalog as `init` — typed, validated, reversible.

```bash
mdk dev faq-bot
# in another terminal: append a sentence to agents/faq-bot/prompt.md
# watch dev re-run; ask the copilot "add a context file for our tiers" and
# accept the proposed action
```

Expected output: a re-run banner with diff; the copilot prints a
plan-preview ("will create agents/faq-bot/contexts/tiers.md") and waits for
`y/n`.

**What this proves.** ADR 026 + ADR 027 (the integrated `init -> dev`
authoring UX), ADR 025 (catalog actions, never raw FS writes), and the
copilot **cost budget + audit/replay log** (ADR 025 D7e, PR #494, on `main`).
See [§3 "Authoring"](architecture-review.md#3-the-pillars).

---

## 7. Reporting — `mdk report` and the matching `/api/v1` (~90s)

**What you'll see.** The same aggregation surfaces twice: once as the
offline CLI rollup, once over HTTP as a backend-agnostic endpoint. Both call
into `core/reporting.py` — no duplicate logic.

```bash
# offline rollup
mdk report --since 1d

# matching HTTP surface (boot the runtime first in another shell:
#   mdk serve --port 8000 )
curl -s -H "Authorization: Bearer $MOVATE_API_KEY" \
  http://localhost:8000/api/v1/report | jq .

# per-agent metrics
curl -s -H "Authorization: Bearer $MOVATE_API_KEY" \
  http://localhost:8000/api/v1/agents/faq-bot/metrics | jq .
```

Expected output: aggregate rows (runs, cost, latency p50/p95, pass-rate);
the HTTP surface returns the same shape as a JSON document.

**What this proves.** ADR 031 (reporting & dashboards) + ADR 032 D2
(aggregate monitor endpoints, **shipped to `main` in PR #510**). One factored
core, two surfaces: CLI rollup for ops, API for the front end. The
per-tenant `GET /api/v1/usage` endpoint *(future, PR #519 — ADR 036 D1)*
will join this same surface. See
[§6 "Front-end API readiness"](architecture-review.md#6-front-end-api-readiness).

---

## 8. Observability — Grafana dashboards-as-code (~90s)

**What you'll see.** The Grafana / Prometheus / Azure dashboards live in
`dashboards/` and are checked-in JSON / YAML — *dashboards-as-code*. They
ride along with deployments.

```bash
# dashboards-as-code shipped on main (PR #497)
ls dashboards/
#   azure  grafana  prometheus  README.md

cat dashboards/grafana/README.md | head -40
```

Then briefly point at the standalone demo OTel + Grafana stack — *(future,
PR #518)*. The wire-up pattern when it lands:

```bash
# (future, PR #518) — local OTel collector + Grafana
# cd infra/otel-collector && docker compose up -d
# OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 mdk run faq-bot --mock '{}'
# open http://localhost:3000
```

Expected output (today): dashboards directory listing + README. Verbally
note that `OTEL_EXPORTER_OTLP_ENDPOINT` is already honored by the tracer
seam (`src/movate/tracing/__init__.py`); the demo stack is the only thing
that's on a follow-on PR.

**What this proves.** ADR 031 D2 — *real* observability assets in the repo,
not slideware. The Tracer seam (`tracing/base.py` + `composite.py`) is the
exit point; the dashboards are the entry point. See [§3
"Observability"](architecture-review.md#3-the-pillars) and ADR 015 / 019 / 020
on trace propagation.

---

## 9. Changelog automation — Tracking Issue #517 (~60s)

**What you'll see.** The pinned tracking issue
[`#517 — What's New, daily digest`](https://github.com/movate/movate-cli/issues/517)
gets a fresh comment every day with merged PRs, grouped by area. The
automation uses **only `GITHUB_TOKEN`** — no PAT, org-policy-compliant.

```bash
gh issue view 517 --comments | head -40
```

Expected output: most-recent digest comment with the day's merges
(grouped by feature / docs / fix / chore).

**What this proves.** The automation pattern itself — keeping reviewers in
the loop with zero secret-rotation overhead (PR #514 + PR #515, both merged
to `main`). Architecture-review-relevant: changelog is **first-party
infrastructure**, not a side project.

---

## 10. Deployment — `mdk deploy` with timer + verbose ACR (~120s)

**What you'll see.** A dry-run plan first (safe to show), then — if the
driver is set up live — a real `--target dev` deploy with the streaming ACR
build log and the live elapsed timer (PR #512, merged).

```bash
# 1. safe dry-run for the demo
mdk deploy --target dev --dry-run

# 2. live (only if the driver has Azure creds ready) — show --verbose
# mdk deploy --target dev --verbose
```

Expected output (dry-run): plan table — what bicep modules will run, what
image tag, what env vars, what scopes; ends with "dry_run=true ok=true".
Live: spinner with an elapsed timer; under `--verbose` the ACR `az acr
build` log streams live above the spinner.

**Close on the cross-cloud story.** "The same artifact deploys to any
customer Azure tenant; the dashboards-as-code (`dashboards/`, ADR 031 D2)
go with it. Azure Monitor Workbooks Bicep wrapper is *(future, PR #520)*,
and Movate-side Lighthouse onboarding for cross-tenant operate is
*(future, PR #525)*."

**What this proves.** ADR 001 (cloud portability), ADR 014 (durable agent
registry — publishing is a *DB write* seen by every pod, decoupled from the
image rebuild), ADR 017 (KEDA worker + Postgres queue), and the deploy
lifecycle in [§3 "Deployment"](architecture-review.md#3-the-pillars).

---

## What if a step fails

Triage table — keep this open in a second tab during the demo:

| Symptom | First action | Where |
|---|---|---|
| Provider/auth/env complaint | `mdk doctor` (or `mdk doctor agent <name>`) | `src/movate/cli/doctor.py` |
| `doctor` flagged a fixable issue | `mdk fix` (auto-remediate where safe) | `src/movate/cli/fix_cmd.py` |
| Run failed but unclear why | `mdk explain <run-id>` (offline tree from `RunRecord`) | `src/movate/cli/explain.py` |
| Need raw logs | `mdk logs --since 5m` or runtime stdout | `src/movate/cli/logs.py` |
| Eval regressed | `mdk eval <agent> --replay <failing-case>` | `src/movate/cli/eval.py` |
| Deploy hung | Ctrl-C is safe under `--dry-run`; live deploy: `mdk deploy --target <t> --verbose` to see the ACR log | `src/movate/cli/deploy.py` |
| Anything else | Escalate to the on-call architect named in `CLAUDE.md` | — |

---

## For reviewers — where the architecture argument lives

The packet, [`docs/architecture-review.md`](architecture-review.md), is the
long-form architectural case for everything this runbook demonstrates. It
synthesizes the ADR arc 023 -> 038 plus the canonical principles, with a
PENDING-DECISIONS section (§8) that names the three Deva sign-offs gating
the next milestone.

Index of where each segment's architecture argument lives:

- **Segment 1 (panels / planes):** [packet §2](architecture-review.md#2-the-two-planes--the-seams),
  [`docs/architecture-principles.md`](architecture-principles.md).
- **Segment 2 (NL scaffold):** ADRs
  [023](adr/023-auto-retrieval.md), [025](adr/025-authoring-copilot.md),
  [026](adr/026-init-front-door-ux.md), [028](adr/028-template-discoverability-workflow-starter.md).
- **Segment 3 (validate / doctor):** [packet §3 Authoring](architecture-review.md#3-the-pillars),
  ADR [022](adr/022-runtime-key-precedence.md).
- **Segment 4 (executor):** [packet §3 Execution](architecture-review.md#3-the-pillars),
  ADRs [015](adr/015-self-hosted-observability.md), [024](adr/024-step-observability.md).
- **Segment 5 (eval):** [packet §3 Governance](architecture-review.md#3-the-pillars),
  ADRs [016](adr/016-continuous-improvement-loop.md), [008](adr/008-workflow-level-evals.md).
- **Segment 6 (dev loop):** ADRs [025](adr/025-authoring-copilot.md), [026](adr/026-init-front-door-ux.md),
  [027](adr/027-dev-live-reload-loop.md).
- **Segment 7 (report / API):** [packet §6](architecture-review.md#6-front-end-api-readiness),
  ADRs [031](adr/031-reporting-and-dashboards.md), [032](adr/032-front-end-api-completion.md).
- **Segment 8 (dashboards / OTel):** [packet §3 Observability](architecture-review.md#3-the-pillars),
  ADRs [015](adr/015-self-hosted-observability.md), [019](adr/019-trace-context-propagation.md),
  [020](adr/020-otel-collector-azure-monitor.md), [031](adr/031-reporting-and-dashboards.md).
- **Segment 9 (changelog automation):** PR #514, #515 (merged); tracking
  issue [#517](https://github.com/movate/movate-cli/issues/517).
- **Segment 10 (deploy):** [packet §3 Deployment](architecture-review.md#3-the-pillars),
  ADRs [001](adr/001-cloud-portability.md), [014](adr/014-durable-agent-registry.md),
  [017](adr/017-agent-orchestration.md).

Spans + metrics catalog (for "what does the trace actually contain"):
[`docs/observability.md`](observability.md) when present; today the canonical
source is the per-step model in
[ADR 024](adr/024-step-observability.md) plus the dashboards in `dashboards/`.

Decision log: [`docs/adr/`](adr/) — ADR 023 -> 038 is the recent arc, with
status-reconciliation in [packet §9](architecture-review.md#9-adr-status-reconciliation).

---

## Pre-meeting checklist (run the night before)

Tick each box. If anything fails, fix it before the meeting — *do not* try
to debug live.

- [ ] **Segment 1 dry-run:** `mdk --help` prints all five panels (Develop /
      Run & evaluate / Deploy & operate / Diagnose / Manage).
- [ ] **Segment 2 dry-run:** `cd /tmp && rm -rf mdk-rehearsal && mkdir
      mdk-rehearsal && cd mdk-rehearsal && mdk init faq-bot --llm "an FAQ
      bot for our pricing tiers" --mock` completes with green baseline.
- [ ] **Segments 3-5 dry-run:** in `mdk-rehearsal/`, `mdk validate faq-bot
      && mdk doctor agent faq-bot && mdk run faq-bot --mock '{"question":
      "what is the pro tier?"}' && mdk eval faq-bot --mock` all pass.
- [ ] **Segment 7 dry-run:** `mdk serve --port 8000` boots in <5s; `curl
      -H "Authorization: Bearer $MOVATE_API_KEY" localhost:8000/api/v1/report`
      returns 200 JSON.
- [ ] **Segment 8 dry-run:** `ls dashboards/grafana dashboards/prometheus
      dashboards/azure` all populated; `cat dashboards/grafana/README.md`
      reads cleanly.
- [ ] **Segment 10 dry-run:** `mdk deploy --target dev --dry-run` completes
      with `dry_run=true ok=true`. If doing a live deploy, also confirm
      `az login` is valid and the target ACR / RG exist.
