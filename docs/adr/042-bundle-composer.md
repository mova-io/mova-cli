# ADR 042 — Bundle Composer: NL description → full reviewable agent bundle

**Status:** Proposed
**Date:** 2026-05-28
**Deciders:** Engineering + Deva (Movate)
**Context window:** v1.x Claude-orchestrated authoring story, Flagship 1 — turn
the existing single-file `--llm` scaffold into a **whole-bundle composer** so a
developer can describe an agent in one sentence and get back a complete,
reviewable artifact: agent + contexts + KB + evals + judge + smoke-eval result,
all `draft` until accepted.
**Builds on / related:** ADR 023 (auto-RAG; the retrieval block this composer
populates), ADR 025 (the authoring action catalog + planner; Bundle Composer is
the next surface on the same spine), ADR 028 (template metadata; the catalog
floor a composed bundle uses when no template is specified), ADR 032
(front-end API completion; D1 `/agents/preview` is the synchronous single-agent
analog this ADR generalizes to async whole-bundle), ADR 040 (projects; the
parent scope for composed bundles), ADR 041 (template catalog; an alternative
floor for "start from template" vs. "start from one sentence"). **Composes the
authoring substrate landing tonight:** the **Eval Generator** (Claude-authored
cases), the **Judge Engineer** (Claude-authored `judge.yaml` rubric), the
**Failure Pattern Diagnoser** (smoke-fail explainer), and the **Unified KB
Ingest** endpoint (URL-fetch + Claude-authored content via a single ingest
seam). **Supersedes / extends:** ADR 026 (`init` front-door — single-agent
scaffold; this ADR extends it whole-bundle when `--target` is set). **Related
backlog:** ADR 043 (Self-Improving Agent Loop — out of scope here).

## Context

Today `mdk init --llm "<description>"` (ADR 023, ADR 025, PR #524) scaffolds a
**single agent** — `agent.yaml` + `prompt.md` — locally via
`movate.scaffold.generate_agent_from_description`. ADR 032 D1 added the
runtime-side analog: `POST /api/v1/agents/preview` returns a candidate
`agent.yaml`/`prompt.md` (+ schemas + seed eval cases + cost forecast) without
persisting. Both produce **one file pair**. Neither produces:

- the **contexts** an enterprise agent needs (policies, glossaries, escalation
  guides — Markdown docs authored by Claude based on the description),
- the **KB content** the agent should ground on (URL-fetched docs + auxiliary
  Claude-authored seed content, both flowing through the unified ingest seam),
- the **eval dataset** (golden cases generated from the same description by the
  Eval Generator),
- the **judge rubric** (a `judge.yaml` authored by the Judge Engineer with
  criteria derived from the agent's stated purpose),
- a **smoke-eval result** (the just-composed bundle run against `--mock` over
  the generated cases, so the user sees pass/fail before reviewing).

The product gap is the Mova iO use case driving Flagship 1: *"Describe the
agent in one sentence. Get back a working agent with everything — prompt,
contexts, KB seeded with relevant content, eval dataset, judge rubric, smoke
test — all reviewable before commit."* Closing it requires composing the
substrate landing tonight (Eval Generator, Judge Engineer, Unified Ingest,
Failure Pattern Diagnoser) into one cloud-side pipeline with **per-stage
streaming progress**, **per-stage cost caps**, and **per-stage selective
acceptance**. That pipeline is the Bundle Composer.

## Decision

Add a **cloud-side, project-scoped, async, SSE-streamed pipeline** that
composes a draft bundle from a single NL description, persists it as a `draft`
registry version (ADR 014), and lets the user review and selectively commit
piece-by-piece. The pipeline is an **orchestration over the existing
substrate** — no new execution engine, no new storage substrate, no change to
the runtime API contract beyond the additive endpoints below.

### D1 — Async compose endpoint

`POST /api/v1/projects/{project_id}/bundles/from-llm` (scope: `admin`).
Body:

```json
{
  "description": "Returns-policy assistant for our e-commerce support team",
  "shape": "rag",                              // optional; default inferred
  "model": "claude-opus-4-7",                  // optional; default tenant pref
  "target_kb_size": 12,                        // optional; default 8 docs
  "eval_case_count": 10,                       // optional; default 8
  "kb_sources": ["https://docs.example.com/returns"]  // optional URL seeds
}
```

Returns `202 Accepted` with:

```json
{
  "job_id": "bc_01HZ...",
  "stream_url": "/api/v1/projects/{id}/bundles/jobs/{job_id}/stream",
  "preview_url": "/api/v1/projects/{id}/bundles/jobs/{job_id}/preview",
  "commit_url": "/api/v1/projects/{id}/bundles/jobs/{job_id}/commit",
  "budget_usd": 5.00
}
```

The job rides the existing ADR 017 job queue (a new `JobKind.COMPOSE_BUNDLE`),
runs on the KEDA-autoscaled worker, and persists progress to a new
`bundle_compose_jobs` table — so a closed browser tab does not lose work
(see R4).

### D2 — Pipeline stages

Executed sequentially; each is an independent unit with its own input, output
contract, cost estimate, and SSE emission. A stage failure stops the pipeline
but preserves all prior outputs (see *Failure modes*).

| # | Stage              | Input                                | Output                                        | Substrate reused                                                                   | Est. cost |
|---|--------------------|--------------------------------------|-----------------------------------------------|------------------------------------------------------------------------------------|----------:|
| 1 | `plan`             | description, shape, project policy   | `BundlePlan` (chosen shape, stage budgets, sub-tasks) | ADR 025 planner                                                            | ~$0.05    |
| 2 | `generate_agent`   | `BundlePlan`                         | `agent.yaml` + `prompt.md`                    | `movate.scaffold.generate_agent_from_description` (ADR 032 D1 path)                | ~$0.30    |
| 3 | `generate_contexts`| agent + plan                         | N×Markdown contexts in `contexts/`            | catalog action `add-context` (ADR 025) × N, Claude-authored bodies                 | ~$0.60    |
| 4 | `seed_kb`          | agent + plan + `kb_sources`          | KB rows (URL-fetched + Claude-authored seeds) | **Unified KB Ingest** endpoint (tonight's PR)                                      | ~$0.80    |
| 5 | `generate_evals`   | agent + plan                         | `evals/cases.yaml` (N golden cases)           | **Eval Generator** (tonight's PR)                                                  | ~$0.70    |
| 6 | `generate_judge`   | agent + plan + eval cases            | `evals/judge.yaml`                            | **Judge Engineer** (tonight's PR)                                                  | ~$0.40    |
| 7 | `smoke_eval`       | composed bundle + judge + cases      | `BundleEvalReport` (pass/fail per case)       | existing `mdk eval` core run against `--mock`; **Failure Pattern Diagnoser** on fails | ~$1.50 |
| 8 | `package_draft`    | all stage outputs                    | `DraftBundle` registry record                 | ADR 014 registry; new `draft` lifecycle bit                                        | ~$0.00    |

Per-bundle hard ceiling: **$5.00 USD** (D5). The `plan` stage allocates a
sub-budget per remaining stage and emits a `budget.allocated` event; later
stages are admission-checked against their own sub-budget so KB-seeding can't
starve evals (R3).

### D3 — Structured SSE event taxonomy

`GET /api/v1/projects/{id}/bundles/jobs/{job_id}/stream` (scope: `read`) is an
SSE stream emitting typed events. Reuses the ADR 031 / ADR 035 D3 SSE infra —
no new transport (R1).

Event kinds:

- `job.started` — `{ job_id, project_id, plan: BundlePlan, budget_usd }`
- `stage.started` — `{ stage, sub_budget_usd, eta_seconds }`
- `stage.progress` — `{ stage, fraction: 0.0..1.0, message }` (intra-stage)
- `stage.partial` — `{ stage, partial_output }` (e.g. one context drafted of N)
- `stage.completed` — `{ stage, output_ref, cost_usd, duration_ms }`
- `stage.failed` — `{ stage, error_code, message, resumable: bool }`
- `budget.warning` — `{ stage, used_usd, sub_budget_usd }` at 80% of sub-budget
- `job.completed` — `{ draft_bundle_id, total_cost_usd, smoke_pass_rate }`
- `job.failed` — `{ failed_stage, partial_outputs: [...] }`

Example `stage.completed` payload:

```json
{
  "type": "stage.completed",
  "job_id": "bc_01HZ...",
  "stage": "generate_evals",
  "output_ref": "evals/cases.yaml@sha256:...",
  "cost_usd": 0.62,
  "duration_ms": 18400,
  "metadata": { "case_count": 10 }
}
```

The front end renders these as a live progress tree (one row per stage,
expandable to the partials).

### D4 — Draft bundle + selective commit

The composed bundle is written to the registry (ADR 014) as a `draft` version —
not yet `published`, not yet eligible for canary/promote. Reuses the existing
content-addressed versioning substrate (ADR 021) so every piece has a stable
hash for selective acceptance (R2, R4).

`GET .../jobs/{job_id}/preview` (scope `read`) returns the full draft:

```json
{
  "draft_bundle_id": "drft_01HZ...",
  "agent": { "content_hash": "sha256:...", "path": "agent.yaml", "preview": "..." },
  "prompt": { "content_hash": "sha256:...", "path": "prompt.md", "preview": "..." },
  "contexts": [{ "id": "returns-policy", "content_hash": "...", "preview": "..." }, ...],
  "kb_items": [{ "id": "...", "source": "url:https://...", "chunk_count": 14 }, ...],
  "eval_cases": [{ "id": "case_001", "input": {...}, "expected": "..." }, ...],
  "judge": { "content_hash": "...", "criteria": [...] },
  "smoke_eval": { "pass_rate": 0.7, "failures": [{ "case_id": "case_003", "diagnosis": "..." }] },
  "stage_costs_usd": { "plan": 0.04, "generate_agent": 0.28, ... }
}
```

`POST .../jobs/{job_id}/commit` (scope `admin`) — **selective acceptance**:

```json
{
  "accept": {
    "agent": true,
    "prompt": true,
    "contexts": ["returns-policy"],            // omit IDs to reject
    "kb_items": "all",                          // or list of IDs
    "eval_cases": ["case_001", "case_002", "case_004", "case_005", "case_006", "case_007"],
    "judge": true
  },
  "regenerate": {
    "contexts": ["escalation-guide"]            // re-run a stage scoped to one item
  },
  "publish": false                              // default: commit as next non-draft version, not yet published
}
```

Accepting the commit promotes the selected pieces into a new bundle version via
the existing add/validate path — every piece goes through `mdk validate`, the
registry, and content-addressed versioning. Rejected pieces are discarded. The
`regenerate` block re-runs the named stage scoped to the named items only (R2)
— costed against a fresh sub-budget capped at the original sub-budget.

### D5 — Per-stage budget caps

The default per-bundle ceiling is **$5.00 USD** (configurable per tenant via
the ADR 036 quotas pattern). The `plan` stage allocates a sub-budget per
remaining stage proportional to the row in D2's table; each subsequent stage
admission-checks against `sub_budget_usd` (returns `stage.failed` with
`error_code=budget_exhausted` rather than overrunning). Soft warning at 80% of
sub-budget via `budget.warning` (R3). Per-tenant aggregate compose-spend is
metered via ADR 036 D1.

### D6 — Project scope

Bundles live under `/api/v1/projects/{project_id}/bundles/...`, composing
cleanly with ADR 040. Tenants without explicit projects get a default project
(per ADR 040's default-project rule) so the endpoint is usable from day one.
A composed bundle inherits the project's policy/quota/key-precedence context
(ADR 022).

### D7 — Smoke-eval gates promotion

The `smoke_eval` stage runs the composed bundle against `--mock` over the
generated eval cases, judged by the generated `judge.yaml`. The result drives
draft labeling:

- pass-rate ≥ **80%** → `draft.status = ready`
- pass-rate **50–80%** → `draft.status = review_recommended`
- pass-rate **< 50%** → `draft.status = needs_review` + each failure annotated
  with a `diagnosis` from the **Failure Pattern Diagnoser** (tonight's PR) and
  proposed-fix actions from the ADR 025 catalog (e.g. *"add a policy context
  on edge cases X/Y"*).

A `needs_review` draft is **not blocked** from commit (the user may know
better) — the status is advisory and surfaced in the `/preview` payload so the
UI can render the warning + the diagnoser's proposed fixes inline.

## API surface

Endpoints (all additive; ADR 033 hardening applies; OpenAPI contract-tested):

| Method | Path                                                              | Scope   | Purpose                                |
|--------|-------------------------------------------------------------------|---------|----------------------------------------|
| POST   | `/api/v1/projects/{id}/bundles/from-llm`                          | admin   | Start a compose job (D1)               |
| GET    | `/api/v1/projects/{id}/bundles/jobs/{job_id}`                     | read    | Job status (terminal poll fallback)    |
| GET    | `/api/v1/projects/{id}/bundles/jobs/{job_id}/stream`              | read    | SSE event stream (D3)                  |
| GET    | `/api/v1/projects/{id}/bundles/jobs/{job_id}/preview`             | read    | Full draft inspection (D4)             |
| POST   | `/api/v1/projects/{id}/bundles/jobs/{job_id}/commit`              | admin   | Selective accept + optional publish    |
| POST   | `/api/v1/projects/{id}/bundles/jobs/{job_id}/resume`              | admin   | Resume from a failed stage             |
| DELETE | `/api/v1/projects/{id}/bundles/jobs/{job_id}`                     | admin   | Discard draft + free storage           |

## CLI parity

`mdk init "<description>" --target <env>` triggers the **cloud-side compose
flow** when `--target` is set: the CLI POSTs the description to the tenant's
runtime, subscribes to the SSE stream, renders progress locally, and on
completion writes the accepted pieces into the working tree (or just prints the
`draft_bundle_id` if `--no-commit`). Without `--target`, `mdk init --llm`
keeps its existing local-only single-agent behavior unchanged (CLAUDE.md rule
5; ADR 026 back-compat). New CLI flags:

- `--target <env>` — runtime to use (existing flag; this ADR adds the
  whole-bundle behavior when present + `--llm`).
- `--kb-source <url>` (repeatable) — passed to the `seed_kb` stage.
- `--no-commit` — fetch the draft + print `draft_bundle_id` without writing.
- `--budget <usd>` — override the per-bundle ceiling (still capped by tenant
  quota).

## Failure modes

The composer is an enterprise pipeline; CLAUDE.md rule 10 applies:

- **Provider/timeout failure mid-stage** → `stage.failed` with `resumable=true`;
  the partial output is preserved in the draft, and `POST .../resume` re-runs
  from the failed stage with the prior outputs as input.
- **KB ingest URL unreachable** → the URL is recorded as `skipped` with the
  HTTP error; `seed_kb` continues with remaining sources; non-fatal unless
  zero items succeed (then `stage.failed` with `error_code=no_kb_seeded`).
- **Eval generator produces zero valid cases** → `stage.failed`; user can
  resume with a different model or skip evals (`POST .../resume` body opts to
  skip).
- **Smoke eval pass-rate < 50%** → not a failure; surfaced as `needs_review`
  with diagnoses (D7).
- **Budget exhausted mid-stage** → `stage.failed` with
  `error_code=budget_exhausted` + `sub_budget_usd` + `used_usd`; user can
  resume with `--budget` increase (still under tenant quota).
- **Worker crash** → job state is durable (D1's table); a new worker resumes
  the job via the ADR 017 queue's idempotency key.
- **Browser tab closed mid-stream** → SSE reconnect with `Last-Event-Id`
  replays buffered events from the durable job state (R4).
- **Tracer unavailable** → SSE emission is at the edge, not in `core`; tracing
  failures degrade to log-only per the boundary rule (CLAUDE.md rule 6).
- **Duplicate compose job** → request idempotency key (description hash +
  project + tenant) collapses to the original `job_id` within a 1h window.

## Resolved decisions

The following four are **locked** by this ADR (decided in the conversation
that produced it):

- **R1 — SSE streaming, not polling.** Matches ADR 035 D3's precedent (the
  front end already speaks SSE). Polling does not support intra-stage progress
  or partial-output preview without a separate channel.
- **R2 — Per-piece review, not monolithic accept/reject.** The `/commit`
  surface accepts a granular `accept` map + a `regenerate` map (D4). A user
  can keep the agent, regenerate one context, accept evals 1–7 of 10, and
  reject the judge in one call.
- **R3 — Per-stage budget caps, not just per-bundle.** D2's stage-row budgets
  + D5's sub-budget admission check. Prevents one stage (typically `seed_kb`
  or `smoke_eval`) from consuming the whole ceiling before later stages run.
- **R4 — Drafts persist; resume is first-class.** The `bundle_compose_jobs`
  table + `POST .../resume` + content-addressed piece storage means the user
  can close the laptop, come back tomorrow, and finish reviewing — or resume a
  failed stage without re-running successful ones.

## Boundaries

- **`cli ⊥ runtime`.** The pipeline lives in the runtime + the worker; the
  CLI only POSTs the description, subscribes to SSE, and writes accepted
  files. The CLI does not import any compose-stage module.
- **No new execution engine.** Every stage delegates to existing substrate
  (scaffold, ADR 025 catalog, Unified Ingest, Eval Generator, Judge
  Engineer, `mdk eval` core).
- **Tracing wired at the edges.** SSE emission and per-stage spans are at the
  stage boundaries, not inside `core` execution logic (CLAUDE.md rule 6).
- **Storage via Protocol.** Draft persistence is a new table behind
  `StorageProvider` — SQLite + Postgres impls, no `postgres`/`sqlite` import
  in `core` (CLAUDE.md rule 6/7).
- **Versioning via the existing seam.** Drafts are a new lifecycle state on
  the ADR 014 registry, not a parallel registry; the content-addressed hash
  format (ADR 021) is unchanged.
- **Out of scope:**
  - The **Self-Improving Agent Loop** (ADR 043 — separate flagship).
  - Changes to the existing **local-only `mdk init --llm`** behavior (back-compat).
  - **Eval re-running on production data** — that's continuous-eval (ADR 016);
    smoke-eval here is one-shot against `--mock`.
  - **Multi-agent workflow composition** — single-agent bundle only; workflow
    composition is ADR 029 / ADR 038.
  - **Marketplace / sharing of composed bundles** — the draft is tenant-scoped.

## Alternatives considered

- **Client-side orchestration (laptop drives each stage).** Rejected: requires
  the developer's laptop to stay online and authenticated for the full
  pipeline (potentially 5+ minutes); defeats the cloud-first Mova iO use
  case; pushes the BYOK key surface to N stages instead of one runtime call;
  cannot persist a draft for review across sessions.
- **One giant LLM call returning the whole bundle.** Rejected: opaque (no
  per-stage review, no streaming progress), no per-stage cost control
  (one prompt blows the whole budget), no per-piece selective acceptance
  (R2), no Unified-Ingest reuse for KB content (the LLM would have to
  hallucinate KB instead of fetching real URLs), and a `judge.yaml`
  generated in the same call as the eval cases would judge its own
  shortcuts.
- **Synchronous endpoint (extend ADR 032 D1's `/agents/preview`).** Rejected:
  ADR 032 D1 is explicitly scoped to fast, synchronous, single-agent preview
  ("preview = generate + validate **only**"). Whole-bundle composition with KB
  seeding cannot fit in a request timeout; it needs the worker queue (ADR
  017) and SSE streaming. This ADR is the async whole-bundle analog of ADR
  032 D1.
- **Inline runs (no draft persistence).** Rejected by R4 — the user must be
  able to close the tab and resume.

## Consequences

**Positive.** Unlocks Flagship 1: a one-sentence description produces a
complete, reviewable, project-scoped agent bundle. Mova iO's demo is a
straight render of the SSE stream. Composes — does not duplicate — the
authoring substrate landing tonight (Eval Generator, Judge Engineer, Unified
Ingest, Failure Pattern Diagnoser). Every piece flows through the existing
validate + registry + content-addressed path, so a composed bundle is
indistinguishable from a hand-authored one once committed.

**Risks / watch items.**
- **Budget runaway** if per-stage caps are not enforced — D5 + R3 mitigate;
  test with adversarial descriptions (e.g. *"crawl all of wikipedia"*).
- **Hallucinated KB content** if Claude-authored seed docs are not labelled
  as such — `seed_kb` records a `source: claude-authored` tag distinguishable
  from `source: url:<url>` in the registry, so production retrieval can
  filter if desired.
- **Eval-judge collusion** — the judge is authored from the description, the
  cases are authored from the description, the prompt is authored from the
  description. Smoke-eval pass-rate is therefore an **optimistic** signal,
  not a quality gate. Documented as such in the `/preview` payload (the
  pass-rate field has a `caveat: same-source-bias` note); continuous-eval
  on real traffic (ADR 016) remains the ground truth.
- **Draft sprawl** — abandoned drafts cost storage; TTL of 30 days on
  un-committed drafts, deletable via the DELETE endpoint above.
- **Existing `scaffold_preview` path lacks per-stage hooks** — the single
  function `movate.scaffold.generate_agent_from_description` (currently
  ~150 lines of synchronous generation) does not emit stage events and has
  no progress callback. The `generate_agent` stage (D2 row 2) needs that
  function refactored to accept an `on_event` callback or yield events —
  flagged here as the **first scaffold-layer change this ADR requires**;
  the refactor itself is an implementation PR, not part of this ADR.

## Scope / rollout

This ADR commits the architecture only. Implementation breakdown (separate PRs,
each one-responsibility per CLAUDE.md rule 3):

1. `bundle_compose_jobs` table + `JobKind.COMPOSE_BUNDLE` + draft lifecycle bit
   on the ADR 014 registry.
2. `core/compose/` pipeline orchestrator (stage interface + sub-budget
   enforcement + SSE emission seam — edge-wired per CLAUDE.md rule 6).
3. Refactor `generate_agent_from_description` to emit progress events (the
   scaffold-layer change flagged above).
4. Compose endpoints (D1, D3, D4) + OpenAPI contract test.
5. CLI parity (`mdk init --target ... --llm "..."`) + the new flags above.
6. Tonight's substrate PRs land first (Eval Generator, Judge Engineer, Unified
   Ingest, Failure Pattern Diagnoser) — this ADR's D2 rows 4–7 depend on them.
