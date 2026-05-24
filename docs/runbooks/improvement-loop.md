# Improvement-loop runbook — harvest, continuous eval/drift, canary

The continuous-improvement loop (ADR 016) turns the lifecycle from
*author → deploy* into *author → deploy → observe → improve*. It wires three
capabilities that each ship value alone but compose into a flywheel:

```
prod runs + feedback
   └─(1) harvest ─▶ proposed eval cases (human-reviewed)
                       └─(2) continuous eval on a cadence ─▶ drift alert on regression
                                                                └─ fix → publish challenger
                                                                     └─(3) canary % traffic ─▶ compare ─▶ promote
                                                                                                              └─▶ back to prod
```

Everything is **additive and default-off** — an operator opts in per agent/env.

---

## 1. Harvest — grow the eval set from real prod runs

`mdk eval harvest <agent>` selects this project's local runs for an agent by a
prod signal and turns them into **proposed** eval-dataset cases. The dominant
safety property is **proposed-not-applied**: a harvest writes a review file
(`evals/harvested.jsonl`) and **never** touches the live `evals/dataset.jsonl`
unless you pass `--accept`. That human gate is what prevents feedback-poisoning
(noisy/adversarial thumbs-down silently corrupting the test set).

```bash
# Propose cases from thumbs-down runs → <agent>/evals/harvested.jsonl
mdk eval harvest rag-qa

# Golden cases from thumbs-up runs, to stdout
mdk eval harvest rag-qa --source thumbs-up -o -

# Review, then explicitly append to the live dataset (the human-gate step)
mdk eval harvest rag-qa --source low-score --accept
```

Options (verified against `src/movate/cli/eval_harvest_cmd.py`):

| flag | notes |
|---|---|
| `--source` | which prod signal selects candidate runs: `thumbs-down` (default; cases to fix), `thumbs-up` (golden cases), `low-score`, or `sample` (signal-agnostic). |
| `--limit` / `-n` | max proposed cases (default 20). |
| `--since` | ISO-8601 cutoff (e.g. `2026-05-01`); only runs/feedback at/after this instant. Omit for no cutoff. |
| `--output` / `-o` | write the proposals (JSONL) here. Default `<agent>/evals/harvested.jsonl`; `-` for stdout. Ignored when `--accept` is set. |
| `--accept` | APPEND the proposals to the live `<agent>/evals/dataset.jsonl`. Without it, harvesting only proposes. |
| `--format` | `table` (default) or `json`. |

The summary reports proposed / golden / need-review counts. A "need-review" case
came from a signal where a reviewer must still supply the `expected` value.

> Argument resolves to an agent directory (or a bare name → `./agents/<name>`).
> It must have an `agent.yaml`. Runs are keyed by the agent's *declared* name,
> not the directory name.

---

## 2. Continuous eval + drift alerting

Run the existing eval suite on a **cadence** against the live agent, diff vs. a
baseline, and **alert** on regression. Like the orchestration scheduler, there
is no in-process timer: set a per-agent cadence with `mdk eval-schedule set`, and
an external cron runs `mdk eval-scheduler-tick` (or the unified
`mdk scheduler-tick`, which drains eval + generic schedules together).

```bash
# Cheap mock smoke eval every 30 minutes (no tokens)
mdk eval-schedule set rag-qa --cadence 30m --mock

# Real eval every 6h, alert on a >3% drop, email on drift
mdk eval-schedule set rag-qa --cadence 6h --tolerance 0.03 --notify-email me@co.com
```

`set` options (from `src/movate/cli/eval_schedule_cmd.py`):

| flag | notes |
|---|---|
| `--cadence` (required) | int seconds or duration (`30m`, `6h`, `1d`). |
| `--mock` | use the deterministic MockProvider — cheap smoke cadence, no tokens. |
| `--runs` | runs per case (default 1; 1–10). |
| `--gate-mode` | `mean` (default), `min`, or `p10`. |
| `--gate` | per-case pass score (default 0.7; 0.0–1.0). |
| `--objective` | only eval cases for one objective id (sampling). |
| `--tolerance` | allowable `mean_score`/`pass_rate` drop vs baseline before drift fires (default 0.05; 0.0–1.0). |
| `--baseline-id` | pin a baseline `eval_id`. Default: diff against the prior eval. |
| `--notify-email` | email to alert on drift (needs SMTP configured). |
| `--disabled` | dormant. |

```bash
mdk eval-schedule list                 # agent, cadence, enabled, mock, tolerance, last enqueued
mdk eval-schedule clear rag-qa
mdk eval-scheduler-tick                 # eval-only tick (cron entrypoint)
```

The tick enqueues a `JobKind.EVAL` job per due schedule; the **worker** executes
it and, on completion, diffs against the baseline and fires the drift alert.

### Drift detection (incl. per-dimension)

Drift is a **measured regression** vs. the baseline (`src/movate/core/drift.py`):

* Always compares `mean_score` and `pass_rate` — a drop past `--tolerance`
  fires.
* Additionally compares **each shared per-dimension mean** (e.g. faithfulness,
  relevance) when both the current and baseline `EvalRecord` carry
  `dimension_means`, and flags a **per-dimension regression** when any one
  dimension drops past `tolerance`. This catches a single-dimension slide that
  holds the aggregate steady.
* When either side lacks `dimension_means` (legacy / exact-match evals), the
  per-dimension check is skipped and behaviour is unchanged.

Alerts go out via the existing `NotificationDispatcher` (email / Teams /
webhook). A drift alert **informs** — it does not auto-rollback by default.

### Troubleshoot "a drift alert fired — what now"

1. **Read the alert.** It names whether the aggregate (`mean_score`/`pass_rate`)
   or a specific dimension regressed, and by how much vs the baseline.
2. **Confirm it's real, not noise.** A `--mock` smoke schedule shouldn't drive a
   real-quality decision; a too-tight `--tolerance` on a small/variable dataset
   produces false positives. Re-run the eval (or look at the eval job's record)
   to see if it persists.
3. **Localize it.** Per-dimension deltas point at the failure mode (faithfulness
   drop → KB/retrieval drift; relevance drop → prompt rot; broad drop → a model
   version shift). Harvest the offending prod runs (`mdk eval harvest <agent>
   --source low-score`) to grow coverage of the regression.
4. **Fix → publish a challenger** version, then canary it (below) instead of
   shipping straight to 100%.
5. **Cost note:** scheduled eval spends tokens. Use `--mock` for a frequent
   smoke cadence and a real cadence at a coarser interval; honor tenant budgets.

---

## 3. Canary — champion / challenger rollout

With versioned agents (ADR 014 registry), route a configurable **% of prod
traffic** to a *challenger* version, compare it live against the champion, then
promote the winner. Runs/traces are sliced by `agent_version` (no new
`RunRecord` field). Canary routing is applied identically on the sync run path
and the SSE stream path.

```bash
# Send 10% of traffic to version 2026.5.23.1
mdk canary set faq-agent --challenger 2026.5.23.1 --weight 10

# Auto-promote once the challenger clears a 0.9 thumbs-up rate
mdk canary set faq-agent --challenger 2026.5.23.1 --weight 25 \
    --auto-promote --eval-gate 0.9

# Live comparison: champion vs challenger
mdk canary status faq-agent
mdk canary compare faq-agent

# Promote the challenger to champion (concludes the canary; weight → 0)
mdk canary promote faq-agent

# Kill switch — back to 100% champion instantly
mdk canary off faq-agent
```

`set` options (from `src/movate/cli/canary_cmd.py`):

| flag | notes |
|---|---|
| `--challenger` (required) | challenger version to receive canary traffic. |
| `--weight` / `-w` | percent of traffic to the challenger (0–100). **0 = kill switch** (100% champion). |
| `--sticky` / `--no-sticky` | consistent routing per thread (default sticky — no champion↔challenger flip mid-conversation). |
| `--champion` | pin the champion to a specific version. Default: registry latest. |
| `--auto-promote` | opt-in: auto-promote once the challenger clears `--eval-gate`. **Requires `--eval-gate`** (else exits 2). |
| `--eval-gate` | min challenger quality (0–1) for auto-promote. |
| `--auto-rollback` / `--no-auto-rollback` | opt-in: a drift regression on the challenger auto-trips the kill switch (weight → 0). Default off = alert-only. |
| `--disabled` | dormant (routes to champion). |

Subcommands:

| command | does | API |
|---|---|---|
| `set` | create/update the canary | `POST /api/v1/agents/{name}/canary` (`admin`) |
| `status` | show the current config | `GET /api/v1/agents/{name}/canary` (`read`) |
| `compare` | champion-vs-challenger live quality + deltas (runs, errors, success rate, 👍/👎 rates). Accepts `--challenger`/`--champion` overrides. | `read` |
| `promote` | promote a version to champion (`--to <ver>` overrides the configured challenger); concludes the canary (weight → 0) | `POST /api/v1/agents/{name}/canary/promote` (`admin`) |
| `off` | kill switch: weight → 0, or `--delete` removes the row | `DELETE /api/v1/agents/{name}/canary` (`admin`) for `--delete` |

> The runtime also exposes `POST /api/v1/agents/{name}/canary/rollback` (scope
> `admin`) — the inverse of promote. The CLI surfaces rollback as
> **re-promoting the prior champion** (`mdk canary promote <agent> --to
> <prior-version>`) or the instant kill switch (`mdk canary off`). Agent
> versions are immutable (ADR 014); promote/rollback only moves the pointer.

### Safety defaults

* **Assisted promote is the default.** Auto-promote is opt-in **and** gated:
  the runtime **refuses** (`409`) an auto-promote when no `eval_gate` is
  configured or the challenger's thumbs-up rate is below the gate.
* **Auto-rollback is off by default** — a drift regression alerts; it only
  trips the kill switch when `--auto-rollback` is set.
* **Kill switch is instant** — `mdk canary off` (or `--weight 0`) routes 100% to
  the champion immediately.

### Troubleshoot promote / rollback

| Symptom | Cause / fix |
|---|---|
| `compare` says "no challenger to compare" | No canary is set and no `--challenger` was passed. Set one or pass `--challenger <version>`. |
| Auto-promote never happens | Auto-promote is opt-in and eval-gated. Confirm `--auto-promote` + `--eval-gate` are set and the challenger's live thumbs-up rate has cleared the gate (`mdk canary compare`). The runtime returns `409` if the gate is unmet/unconfigured. |
| Challenger looks bad in prod | `mdk canary off faq-agent` for an instant kill switch (weight → 0 = 100% champion). To make it permanent, `mdk canary off faq-agent --delete`. |
| Want to undo a promotion | `mdk canary promote faq-agent --to <prior-version>` (versions are immutable; this just re-points the champion). |
| Set fails on the challenger version | The version must be a **published** version of the agent (registry or filesystem-scanned). Publish it first. |

---

## See also

* [`orchestration.md`](orchestration.md) — the scheduler primitive the
  continuous-eval cadence reuses.
* [`serving-and-keys.md`](serving-and-keys.md) — the `admin`/`eval` scopes that
  gate harvest, canary, and promote endpoints.
* ADR 016 (`../adr/016-continuous-improvement-loop.md`), ADR 014
  (`../adr/014-durable-agent-registry.md`).
