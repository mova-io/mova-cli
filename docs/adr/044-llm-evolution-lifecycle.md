# ADR 044 — Claude-orchestrated project evolution: the `--llm` lifecycle beyond create

**Status:** Proposed
**Date:** 2026-05-28
**Deciders:** Engineering + Deva (Movate)
**Context window:** v1.x Claude-orchestrated authoring story — `--llm` today
is **create-only** (`mdk init --llm "<desc>"` scaffolds one agent). This ADR
extends the lifecycle to **update an existing agent**, **reconcile a whole
project**, and **compose a whole multi-agent project** from one description.
It is the **human-driven evolution** counterpart to ADR 043's auto-driven
self-improving loop.
**Builds on / depends on:**
ADR 023 (auto-RAG — the retrieval block these flows can mutate),
ADR 025 (authoring action catalog + planner — the catalog of typed actions
each pipeline composes),
ADR 026 (`mdk init` front-door UX — the `--llm` flag this ADR generalizes
across the lifecycle),
ADR 028 (template metadata — the shape taxonomy reconcile/update consult when
suggesting a clone),
ADR 040 (projects — the scope unit for `reconcile` and `project-from-llm`),
ADR 041 (agent catalog — the suggestion source for "clone an existing pattern
instead of writing from scratch"),
ADR 042 (Bundle Composer — Flagship 1 — the **single-agent NL-to-bundle
pipeline** this ADR extends to multi-agent + cross-agent flows; the
`project-from-llm` endpoint is conceptually Bundle Composer × N agents + a
workflow + shared resources, and the implementation reuses the same pipeline
orchestrator + SSE seam + draft-lifecycle bit),
ADR 043 (Self-Improving Agent Loop — Flagship 2 — the **typed patch kinds
(D4)** are shared infrastructure; this ADR consumes the same closed enum for
all human-driven diffs, so a `prompt_edit` proposed here is the same typed
artifact a Diagnoser would propose, and the runtime applies both through one
deterministic applier),
**Substrate landing in parallel PRs tonight:** Eval Generator, Judge Engineer,
Failure Pattern Diagnoser, Unified KB Ingest — each pipeline below composes
these alongside the existing scaffold path.
**Flagship:** Companion to Flagship 1 (Bundle Composer, ADR 042) and Flagship 2
(Self-Improving Loop, ADR 043). Where ADR 042 creates a single agent bundle
and ADR 043 mutates an agent automatically in response to drift, this ADR
covers **human-driven multi-stage evolution**: update, reconcile, and
multi-agent compose.

---

## Context

The product gap is the *post-create* lifecycle. Today `--llm` ships one
verb — `mdk init --llm "<desc>"` — and produces **one agent**. Real
authoring is rarely a single-agent, single-shot exercise:

1. **Update is the most common edit.** "The returns policy changed; rewrite
   the prompt section that mentions 14-day windows." Today this is a
   hand-grep + hand-edit of `prompt.md` + a context file + maybe `agent.yaml`
   — a half-hour ritual per agent. Claude can read the existing agent,
   propose a typed `prompt_edit` + `context_remove` + `context_add`, simulate
   against the agent's own evals, and present the diff for review in 30
   seconds.
2. **Reconcile is the cross-agent edit nobody does well today.** "Pricing
   changed from $99 to $129; every agent that quotes pricing needs to update."
   Today this is `grep -r '\$99' .` across 12 agents, hand-edit each, hope
   you didn't miss any. The atomic version — "all 12 update together or none
   does" — is **impossible without tooling**, and partial application leaves
   the project mid-edit in a half-stale state visible to end users. The
   reconcile endpoint makes this a single project-scoped action with
   all-or-nothing transactional apply.
3. **Multi-agent project creation** is the natural extension of Bundle
   Composer (ADR 042) once you observe that real solutions ship as **a
   workflow plus several agents that share contexts + KB**. *"Build me a
   customer-support solution"* should produce a triager + a billing-FAQ + a
   returns-FAQ + a workflow that routes between them + shared escalation
   context + a shared policy KB — not one agent. ADR 042's pipeline already
   knows how to compose **one agent's bundle**; `project-from-llm` is that
   pipeline applied N times in parallel + a workflow-generation stage + a
   shared-context stage + a single shared KB seed + a single eval set across
   the workflow. The composition is **real reuse, not metaphor** — see the
   Boundaries + the cross-reference to ADR 042 below.

All three share the same infrastructure that the parallel PRs (Bundle
Composer, Self-Improving Loop, Eval Generator, Judge Engineer, Failure
Pattern Diagnoser) already built: an **async cloud-side job** behind a
KEDA-autoscaled worker (ADR 017), a **`review-then-commit` lifecycle** on the
durable registry (ADR 014 + ADR 042 D4), an **SSE event stream** (ADR 035 D3 /
ADR 042 D3), **per-stage budget caps** (ADR 042 D5), and the **seven typed
patch kinds** (ADR 043 D4). The architectural opportunity is that all three
new endpoints can ride **one event taxonomy, one diff schema, one review
component, and one commit surface**.

In one sentence: *"`--llm` today scaffolds one agent. ADR 044 extends it to
evolve any agent, reconcile changes across an entire project, and compose
whole multi-agent solutions from one description — all through the same
review-then-commit lifecycle, all emitting the same SSE taxonomy, all yielding
the same typed patch kinds the runtime already knows how to apply."*

---

## Decision drivers

| Driver | Weight |
|---|---|
| **Close the post-create lifecycle gap** — update/reconcile/multi-agent are the missing 90% of authoring | HIGH |
| **One review UX across the platform** — UI builds one component that drives Bundle Composer + Failure Pattern Diagnoser + all three of this ADR's endpoints | HIGH |
| **Atomic reconcile** — partial multi-file application leaves a project inconsistent mid-edit; transactional apply is non-negotiable | HIGH |
| **Reuse the typed-patch substrate from ADR 043** — no new diff language, no second applier, one audit story | HIGH |
| **Reuse the Bundle Composer pipeline (ADR 042) for `project-from-llm`** — the multi-agent flow is genuinely Composer × N + workflow + shared resources | HIGH |
| **HITL by default (matches ADR 043 D5 / R1)** — auto-apply earns trust, it is not asserted; v1 is review-then-commit only | HIGH |
| **Per-endpoint budget caps with per-stage sub-budgets** — prevents any one stage starving the rest | MED |
| **Catalog-aware suggestions (ADR 041)** — "this pattern already exists in `support-triage@2.1` — clone instead?" | MED |
| **Drafts persist for delayed review** — operators come back tomorrow and finish; matches ADR 042 R4 / ADR 043 D8 | MED |
| **Three semantics deserve three endpoints** — clean OpenAPI, clear scopes, no mode-param gymnastics | LOW |

---

## Architecture

```
                       (NL description from human)
                                  │
        ┌─────────────────────────┼─────────────────────────┐
        ▼                         ▼                         ▼
  POST .../agents/{n}/      POST .../projects/{id}/   POST .../projects/from-llm
   update/from-llm           reconcile/from-llm
        │                         │                         │
        ▼                         ▼                         ▼
  (single-agent diff)       (fleet-wide diff)        (Bundle Composer × N
                                                      + workflow
                                                      + shared resources)
        │                         │                         │
        └─────────────────────────┼─────────────────────────┘
                                  ▼
                  ADR 017 JobKind.LLM_EVOLUTION worker
                                  │
                                  ▼
                  shared pipeline scaffolding (this ADR)
                                  │
        ┌────────────────────────┼────────────────────────┐
        ▼                        ▼                        ▼
  SSE event stream      typed patch kinds            draft persistence
  (ADR 035/042 D3)        (ADR 043 D4)               (ADR 014 + ADR 042 D4)
                                  │
                                  ▼
                  GET .../jobs/{job_id}/preview
                                  │
                                  ▼
                  POST .../jobs/{job_id}/commit
                  (per-patch selective accept;
                   reconcile = all-or-nothing transaction)
                                  │
                                  ▼
                  ADR 014 registry / ADR 040 project state
```

Every shaded box is reused as-is. The net-new code is the three endpoints,
the three pipeline stage-lists, the reconcile transactional applier, and the
catalog-aware suggestion hook in the `plan` / `analyze_existing` stages.

---

## Decisions

### D1 — Three project-scoped, async, SSE-streamed endpoints

All three are additive to `/api/v1`, all are project-scoped (ADR 040), all
run on the existing ADR 017 worker (new `JobKind.LLM_EVOLUTION` —
sub-discriminated by `mode: update | reconcile | project_create`), all return
the same response shape (D2).

| Endpoint | Scope | Purpose |
|---|---|---|
| `POST /api/v1/agents/{name}/update/from-llm` | `admin` | Single-agent evolution: given an existing agent + an NL description of the desired change, propose a typed multi-patch diff. |
| `POST /api/v1/projects/{id}/reconcile/from-llm` | `admin` | Fleet-wide: scan the project for affected artifacts, propose a coordinated multi-file typed diff, commit atomically. |
| `POST /api/v1/projects/from-llm` | `admin` | Compose a whole multi-agent project (N agents + workflow + shared contexts + shared KB + workflow-level evals) from one description. |

Each accepts:

```json
{
  "description": "string — the NL change/intent",
  "model": "claude-opus-4-7",          // optional; default tenant pref
  "budget_usd": 0.50 | 2.00 | 10.00,   // optional override; capped by D6
  "target_env": "staging"               // optional; affects simulate stage
}
```

`reconcile` and `project-from-llm` additionally accept a `scope_hint` (e.g.
`{ agents: ["billing-faq", "returns-faq"] }` for reconcile, or
`{ shape: "customer-support" }` for project create) — both optional; the
`plan` / `scan_project` stage refines.

`update` additionally accepts `catalog_suggestion_opt_out: bool` (D5).

### D2 — Unified response shape across all three endpoints

```json
{
  "job_id":     "evo_01HZ...",
  "status_url": "/api/v1/jobs/evo_01HZ...",
  "stream_url": "/api/v1/jobs/evo_01HZ.../stream",
  "preview_url":"/api/v1/jobs/evo_01HZ.../preview",
  "commit_url": "/api/v1/jobs/evo_01HZ.../commit",
  "budget_usd": 0.50
}
```

The status/stream/preview/commit endpoints are **unified at the job tier**
(not per-endpoint), so the Mova iO front end builds **one diff-review
component** that drives every Claude-orchestrated job in the platform —
Bundle Composer (ADR 042), Failure Pattern Diagnoser (ADR 043), and the three
new endpoints here. (See *Review-then-commit UX* below.)

### D3 — Diffs are typed (reuses ADR 043 D4 patch-kind enum)

Every proposed change in this ADR is one of the seven typed kinds defined in
ADR 043 D4:

| Kind                | `diff` shape (jsonb)                                    |
|---------------------|---------------------------------------------------------|
| `prompt_edit`       | `{section, before, after, rationale}`                   |
| `kb_ingest`         | `{source_url_or_blob, expected_chunks, doc_id}`         |
| `context_add`       | `{key, value_template, scope}`                          |
| `context_remove`    | `{key, reason}`                                         |
| `model_swap`        | `{from_model, to_model, fallback}`                      |
| `temperature_change`| `{from, to}`                                            |
| `retrieval_k_change`| `{from, to}`                                            |

For **multi-file or fleet-wide diffs**, the response is a **list** of typed
patches, each scoped to a target:

```json
{
  "patches": [
    { "id": "p_01", "target": { "agent": "billing-faq", "file": "prompt.md" },
      "kind": "prompt_edit", "diff": { ... } },
    { "id": "p_02", "target": { "agent": "returns-faq", "file": "prompt.md" },
      "kind": "prompt_edit", "diff": { ... } },
    { "id": "p_03", "target": { "agent": "billing-faq", "file": "contexts/pricing.md" },
      "kind": "context_remove", "diff": { ... } }
  ]
}
```

The runtime applies each patch through the **same deterministic applier the
Self-Improving Loop uses** (ADR 043 D4) — no free-form text, no shell-out, no
LLM-at-apply-time. Anything outside this enum is rejected at the storage
seam. New patch kinds must extend ADR 043 D4 (one place, one ADR) so the
Diagnoser and this evolution surface always agree.

### D4 — Pipeline stages (per endpoint, sequential, each emits SSE)

#### `update` (single-agent evolution)

```
analyze_existing → propose_diff → simulate_against_evals → package_draft
```

- **`analyze_existing`** — read the agent's `agent.yaml`, `prompt.md`,
  `contexts/`, retrieval config, and (if present) `evals/` baseline. Emits
  `stage.partial` with a summary of what was read.
- **`propose_diff`** — Claude proposes a typed multi-patch diff. Emits one
  `proposed_patch.added` event per typed patch, plus a `proposed_patch.summary`
  event with the patch count + rationale.
- **`simulate_against_evals`** — run the agent's existing eval set against the
  patched bundle (mock provider unless `target_env` is set). Emits
  `simulation.case_completed` per case + `simulation.summary` at end.
- **`package_draft`** — write the draft to the registry (ADR 014 `draft`
  lifecycle bit, per ADR 042 D4). Emits `completed`.

#### `reconcile` (fleet-wide cross-agent change)

```
scan_project → identify_affected_artifacts → propose_coordinated_diff
            → simulate_against_evals → package_draft
```

- **`scan_project`** — enumerate every agent + workflow + context + KB doc in
  the project; emit `stage.partial` per artifact found.
- **`identify_affected_artifacts`** — Claude reads each artifact and decides
  which are affected by the NL intent. Emits an explicit
  `affected_artifacts` payload — the human sees the *scoping* decision
  before any diffs propose. (Critical for trust: "you missed the email
  responder" is caught at this stage, not at preview.)
- **`propose_coordinated_diff`** — one typed multi-patch diff covering every
  affected artifact. Emits `proposed_patch.added` per patch.
- **`simulate_against_evals`** — run **each affected agent's** evals against
  its patched bundle; aggregate into a project-level pass/fail summary.
- **`package_draft`** — write the coordinated draft to the registry, with
  the **atomicity contract** (D8) recorded as a single transaction
  identifier across all patches.

#### `project-from-llm` (whole multi-agent project)

```
plan → decompose_into_agents → generate_agent[N] (parallel)
     → generate_workflow → generate_shared_contexts → seed_shared_kb
     → generate_evals[N] → package_draft
```

- **`plan`** — Claude proposes the project shape: how many agents, what each
  does, what workflow connects them, what shared contexts/KB they need.
  Emits `plan.proposed`; **catalog-aware** (D5) — surfaces relevant catalog
  entries as starting points. The user can review the plan before any
  generation runs (a `stage.confirmed` event from a UI checkpoint).
- **`decompose_into_agents`** — finalize the per-agent specs (one NL
  description per agent, derived from the plan).
- **`generate_agent[N]`** — N parallel sub-jobs, **each one a Bundle
  Composer invocation** (ADR 042 D2 rows 1–4: `plan → generate_agent →
  generate_contexts → seed_kb`, scoped to this agent only). Emits
  `stage.progress` per sub-job. The composition with ADR 042 is **real
  reuse**: this stage instantiates `core/compose/` (the ADR 042
  orchestrator) N times under a parent job, with shared budget accounting.
- **`generate_workflow`** — author a `workflow.yaml` that routes between
  the N agents per the plan. Reuses ADR 029 / ADR 037 workflow authoring.
- **`generate_shared_contexts`** — author project-scoped contexts visible to
  all agents (e.g. shared escalation policy).
- **`seed_shared_kb`** — one shared KB seed at the project tier, ingested
  via the Unified KB Ingest endpoint (tonight's PR). Cheaper and more
  consistent than per-agent KB seeds for genuinely shared content.
- **`generate_evals[N]`** — one eval set per agent + one workflow-level eval
  set (ADR 008) covering the routing logic. Reuses the Eval Generator.
- **`package_draft`** — write the entire multi-agent draft (N agents +
  workflow + shared contexts + shared KB + eval sets) as **one
  project-scoped draft** under a single transaction.

### D5 — Catalog-aware suggestions (ADR 041 integration)

- **`update`** — the `analyze_existing` stage compares the existing agent
  against the ADR 041 catalog and, if a high-similarity match exists,
  **suggests cloning the relevant catalog entry instead of writing from
  scratch**. The suggestion is surfaced as a `catalog.suggestion` SSE event
  with `{ catalog_entry_id, similarity_score, rationale }`. The user can
  accept the clone (which short-circuits `propose_diff` to "clone from
  catalog + apply user's delta") or decline (default — pipeline continues
  to a Claude-authored diff). The `catalog_suggestion_opt_out` request flag
  skips this stage entirely.
- **`project-from-llm`** — the `plan` stage consults the catalog for the
  full project shape (e.g. `customer-support-bundle@1.3`) and surfaces any
  matches in the `plan.proposed` event. The user picks a catalog floor or
  proceeds with a fresh compose. Cheaper, faster, higher-quality starting
  point when a relevant catalog entry exists.
- **`reconcile`** — does not consult the catalog (the operation is intent +
  existing artifacts, not a fresh starting point).

### D6 — Budget caps per endpoint, with per-stage sub-budgets

| Endpoint            | Per-job ceiling | Notes                                                                 |
|---------------------|----------------:|-----------------------------------------------------------------------|
| `update`            |     **$0.50**   | Single-agent diff; analyze + propose dominate cost.                   |
| `reconcile`         |     **$2.00**   | Scan + coordinated diff over N agents; sim cost scales with N.        |
| `project-from-llm`  |    **$10.00**   | Composition-of-Bundle-Composer × N + workflow + shared resources; matches Bundle Composer's $5 per agent at N≈2 with overhead. |

The `plan` (or `analyze_existing` / `scan_project`) stage allocates a
**sub-budget per remaining stage** and emits a `budget.allocated` event;
later stages are admission-checked against their own sub-budget so no single
stage can starve the rest (ADR 042 R3). Soft warning at 80% of sub-budget via
`budget.warning`. Per-tenant aggregate evolution-spend is metered via ADR 036
D1; per-project quotas (ADR 040) apply on top. The ceilings above are
**defaults overridable per tenant** (ADR 036 quotas pattern).

### D7 — Drafts persist until commit or expiry (30-day default)

Every job's output is a draft on the ADR 014 registry (per ADR 042 D4 +
ADR 043 D8). Default TTL is **30 days**, configurable per tenant. A user can
start a `reconcile` Monday, leave the draft in the queue, finish reviewing
Thursday, commit Friday — every typed patch carries a stable
content-addressed hash so the diff the user reviewed is bit-identical to the
diff that commits (ADR 021). Expired drafts emit a `draft.expired` event and
are deleted on the existing scheduler tick. The `DELETE
/api/v1/jobs/{job_id}` endpoint discards a draft early.

### D8 — Atomicity for reconcile (all-or-nothing transactional apply)

A reconcile draft contains a list of typed patches across multiple
artifacts. The commit is **a single registry transaction**: the runtime
computes the full diff, validates each typed patch against each target (every
patch must apply cleanly to the current artifact state), then applies the
whole list in one transaction. **Partial application is forbidden**:

- If any patch fails validation (target moved, content drifted since the draft
  was produced, kind not in the allowed enum for that artifact), the **entire
  commit is rejected** with an explicit `commit.validation_failed` response
  listing every failing patch and why.
- If validation passes for all, the transaction either applies every patch and
  bumps every affected agent's version atomically, or — on storage error —
  rolls back and applies none. The registry never observes a half-applied
  reconcile.
- Operators who want to commit a subset must explicitly **re-run reconcile
  with a narrower `scope_hint`**, producing a new draft. The "selective
  accept" pattern from ADR 042 D4 / R2 deliberately **does not apply to
  reconcile** — the whole point of reconcile is project-level consistency,
  and per-patch acceptance would defeat it. (See `update` and
  `project-from-llm` for selective-accept semantics; `reconcile` is the
  one exception.)

This composes with — does not replace — ADR 014's existing per-agent
versioning: the transaction wraps **N atomic per-agent version bumps**, and
each bump is itself the ADR 014 immutable append. Other readers see the
project at version K (pre-reconcile) until the commit lands, then version K+1
across every affected agent simultaneously.

---

## Shared SSE event taxonomy

Every endpoint emits the same event kinds. Reuses the ADR 035 D3 / ADR 042 D3
SSE transport — no new wire format.

| Event                       | Payload (representative fields)                                                    | Notes                                       |
|-----------------------------|------------------------------------------------------------------------------------|---------------------------------------------|
| `job.started`               | `{ job_id, mode, project_id, agent_name?, plan?, budget_usd }`                     | `mode ∈ {update, reconcile, project_create}` |
| `stage.started`             | `{ stage, sub_budget_usd, eta_seconds }`                                           | Per-stage start                              |
| `stage.progress`            | `{ stage, fraction: 0.0..1.0, message }`                                           | Intra-stage progress                         |
| `stage.partial`             | `{ stage, partial_output }`                                                        | E.g. one affected artifact identified of N   |
| `stage.completed`           | `{ stage, output_ref, cost_usd, duration_ms }`                                     | Per-stage end                                |
| `stage.failed`              | `{ stage, error_code, message, resumable: bool }`                                  | E.g. `budget_exhausted`, `provider_timeout` |
| `budget.allocated`          | `{ allocations: { stage → sub_budget_usd } }`                                      | Emitted by `plan` / `analyze` / `scan` stage |
| `budget.warning`            | `{ stage, used_usd, sub_budget_usd }`                                              | Fired at 80% of sub-budget                  |
| `proposed_patch.added`      | `{ patch_id, target, kind, diff, rationale, confidence }`                          | One per typed patch                          |
| `proposed_patch.summary`    | `{ patch_count, kinds_used: {kind → n}, total_estimated_impact }`                  | End of `propose_diff` stage                 |
| `simulation.case_completed` | `{ case_id, agent_name, pass: bool, before_score, after_score }`                   | Per eval case during `simulate_against_evals` |
| `simulation.summary`        | `{ pass_rate_before, pass_rate_after, delta, per_agent_summary? }`                 | End of simulate stage                        |
| `catalog.suggestion`        | `{ catalog_entry_id, similarity_score, rationale, mode: clone | floor }`           | D5 — only when a high-similarity match is found |
| `plan.proposed`             | `{ plan: ProjectPlan, catalog_floor_suggestions?: [...] }`                         | `project-from-llm` only                      |
| `affected_artifacts`        | `{ artifacts: [{ agent, files, reason }], unaffected_acknowledged: [...] }`        | `reconcile` only — surfaces scoping decision before any diff |
| `completed`                 | `{ draft_id, total_cost_usd, simulation_summary, patch_count, atomic: bool }`      | `atomic=true` for reconcile                  |
| `error`                     | `{ error_code, message, partial_outputs?: [...] }`                                 | Terminal pipeline error                      |

The front end renders these as a live progress tree (one row per stage),
with the `proposed_patch.added` and `affected_artifacts` events folded into a
diff-review pane that becomes interactive once `completed` fires.

---

## Review-then-commit UX

The diff review is the **same component for every Claude-orchestrated job in
the platform** — Bundle Composer (ADR 042), Failure Pattern Diagnoser (ADR
043), and the three endpoints here. The component consumes the unified
preview shape (D2 / D3) and supports:

- **Per-patch acceptance** (for `update` and `project-from-llm` — matches
  ADR 042 D4 / R2). The user toggles individual patches:

  ```json
  {
    "accept": {
      "patches": ["p_01", "p_02"],          // accept these by id
      "regenerate": ["p_03"]                 // re-run the proposing sub-stage scoped to this patch
    },
    "publish": false
  }
  ```

- **All-or-nothing acceptance** (for `reconcile` — per D8). The commit body
  is `{ "accept": "all" | "reject" }`; per-patch selection is rejected with
  HTTP 400 + an error pointing to D8.

- **Selective regeneration** — for `update` and `project-from-llm`, the user
  can ask the pipeline to re-propose a specific patch (e.g. "regenerate the
  escalation context — too terse") without re-running the whole pipeline.
  Costed against a fresh sub-budget capped at the original sub-budget for
  that stage.

- **Diff diffability** — the typed patch kinds (D3) have stable, structured
  schemas, so the UI can render `prompt_edit` as a markdown diff,
  `kb_ingest` as a "new doc summary + chunk preview," `model_swap` as a
  before/after card, etc. **No free-form text patches** ever reach the UI.

---

## API surface

Endpoints (all additive; ADR 033 hardening applies; OpenAPI contract-tested):

| Method | Path                                                  | Scope   | Purpose                                |
|--------|-------------------------------------------------------|---------|----------------------------------------|
| POST   | `/api/v1/agents/{name}/update/from-llm`               | admin   | Start an `update` job (D1)             |
| POST   | `/api/v1/projects/{id}/reconcile/from-llm`            | admin   | Start a `reconcile` job (D1)           |
| POST   | `/api/v1/projects/from-llm`                           | admin   | Start a `project-from-llm` job (D1)    |
| GET    | `/api/v1/jobs/{job_id}`                               | read    | Job status + lifecycle (unified)       |
| GET    | `/api/v1/jobs/{job_id}/stream`                        | read    | SSE event stream (unified)             |
| GET    | `/api/v1/jobs/{job_id}/preview`                       | read    | Draft inspection (unified)             |
| POST   | `/api/v1/jobs/{job_id}/commit`                        | admin   | Commit with selective acceptance (D8 exception for reconcile) |
| POST   | `/api/v1/jobs/{job_id}/resume`                        | admin   | Resume from a failed stage (ADR 042 D4 + R4) |
| DELETE | `/api/v1/jobs/{job_id}`                               | admin   | Discard draft + free storage           |

The unified `/api/v1/jobs/...` surface intentionally generalizes the
per-bundle endpoints from ADR 042. ADR 042's
`/api/v1/projects/{id}/bundles/jobs/{job_id}/...` endpoints redirect to the
unified job tier in a follow-up PR (back-compat preserved via 308 redirect
for one release; deprecated in CHANGELOG).

### CLI parity

```
mdk update <agent> --llm "<description>" --target <env> [--budget <usd>]
mdk reconcile <project> --llm "<description>" --target <env> [--budget <usd>] [--dry-run]
mdk project create --llm "<description>" --target <env> [--budget <usd>] [--shape <hint>]
```

All three:
- POST the description to the runtime, subscribe to the SSE stream, render
  progress locally as a stage-tree.
- On `completed`, fetch the preview, render the diff (terminal pretty-print
  for `update`; a one-shot summary + URL to the web UI for `reconcile` and
  `project-from-llm`).
- Prompt for selective accept (or `--accept-all` / `--reject-all` for CI).
- On commit, write the accepted pieces locally + bump the project state.

Per CLAUDE.md compat rule: new commands only; no existing CLI shape changes.
Existing `mdk init --llm "..."` is **unchanged** — the create-only path
remains, ADR 026 back-compat preserved.

---

## Resolved decisions (locked in upfront)

- **R1 — Review-then-commit is REQUIRED. No auto-apply in v1.** Matches ADR
  043 D5 / R1 (HITL-by-default). Trust is earned; v2 may add policy-controlled
  auto-apply for low-risk patch kinds (`prompt_edit` to a non-customer-facing
  agent, say) once we have data on review-rejection rates from v1.
- **R2 — Typed patches only.** No free-form text diffs. Runtime applies
  deterministically through the same ADR 043 D4 applier. Anything outside the
  enum is a pipeline bug, rejected at the storage seam.
- **R3 — Atomicity for reconcile.** All-or-nothing transactional apply (D8).
  Partial application is forbidden. Per-patch selective accept does not apply
  to reconcile.
- **R4 — Catalog-aware suggestions during plan / analyze stages.** D5 —
  surfaced as `catalog.suggestion` SSE events; user accepts or declines.
  Reconcile is exempt (existing-artifact-driven, not starting-point-driven).
- **R5 — Per-endpoint budget caps with per-stage sub-budgets.** D6 — defaults
  $0.50 / $2.00 / $10.00, overridable per tenant via ADR 036 quotas. Sub-budget
  admission check prevents stage starvation (mirrors ADR 042 R3).

---

## Consequences

**Positive.**
- Prompt-drift becomes a **30-second action**: "pricing changed; reconcile" →
  the human reviews one coordinated diff instead of editing 12 files by hand.
- Multi-agent solutions ship **from one sentence**: `mdk project create --llm
  "customer-support solution"` returns a reviewable bundle that already
  composes a triager + N FAQ specialists + a workflow + shared resources.
- **One review component** drives every Claude-orchestrated job — Bundle
  Composer (ADR 042), Failure Pattern Diagnoser (ADR 043), and the three new
  endpoints. The Mova iO front end builds this once.
- Composes **deeply with ADR 042**: `project-from-llm`'s `generate_agent[N]`
  stage is a real instantiation of the Bundle Composer orchestrator — not a
  reimplementation. The composition framework is shared infrastructure.
- Composes **deeply with ADR 043**: every patch this ADR produces is a typed
  ADR 043 D4 patch kind; the runtime applier, audit events, and registry
  lifecycle are identical for human-driven (this ADR) and auto-driven (ADR
  043) flows. Auditors see one event taxonomy across both surfaces.

**Risks / watch items.**
- **Cost of complex reconciles** — a `scope_hint`-less reconcile across 20+
  agents could approach the $2 ceiling quickly. D6's sub-budget allocation
  protects the simulate stage from being starved by scan, but the operator-
  set ceiling is still a hard cap; very large projects need explicit budget
  overrides (gated by ADR 036 quota policy). Document in the runbook.
- **Over-trust in proposed diffs** — if `update` proposals are accepted
  unread ("rubber-stamping"), drift accumulates silently. Mitigation: every
  `proposed_patch` event includes a `rationale` and a `confidence` field;
  the review UI highlights low-confidence patches. Telemetry should track
  the **time-to-commit** distribution — sub-second commits across many
  patches are a smell.
- **Atomicity ceiling for reconcile** — at very large N (50+ agents), the
  single-transaction commit may push storage transaction limits. ADR 040
  project-size guidance applies; an explicit "split this reconcile into
  smaller scopes" recommendation surfaces when the affected-artifact count
  exceeds a soft limit (configurable; default 25).
- **Catalog-suggestion bias** — if the catalog floor is consistently chosen
  over fresh compose, the project's agent population converges on a small
  set of catalog ancestors. Not necessarily bad (consistency!), but worth
  telemetering — the `catalog.suggestion` accept/decline rate is a useful
  signal for the ADR 041 catalog stewards.

**Neutral.**
- Three new endpoints, one new `JobKind` (`LLM_EVOLUTION`), one new event
  family on the unified job tier. All additive, all opt-in. No change to
  existing `mdk init --llm` semantics.

---

## Alternatives considered

- **Free-form text diffs from Claude.** Rejected — non-deterministic to
  apply, hard to audit, hard to test, and incompatible with the ADR 043 D4
  applier that already exists. Typed patch kinds give one shared
  infrastructure across human-driven and auto-driven flows. (R2.)
- **Auto-apply for low-risk patch kinds in v1.** Deferred. The v1 trust
  posture matches ADR 043 R1 — review-then-commit only. Once we have data on
  v1 review-rejection rates (especially for `prompt_edit` and `context_add`),
  v2 may introduce a per-agent `auto_apply_patch_kinds` allowlist analogous
  to ADR 043 D2's `patch_kinds_allowed`.
- **Per-agent endpoint for reconcile** (e.g. `POST
  /api/v1/agents/{name}/reconcile/from-llm`). Rejected — atomicity is the
  whole point of reconcile, and the unit of atomicity is the project (D8). A
  per-agent reconcile endpoint would re-introduce the partial-application
  failure mode this ADR exists to prevent.
- **One mega-endpoint that takes a `mode` param** (e.g. `POST
  /api/v1/evolve/from-llm { mode: "update" | "reconcile" |
  "project_create" }`). Rejected — three distinct semantics deserve three
  distinct endpoints. Cleaner OpenAPI (each endpoint has a different
  request schema and a different scope-narrowing path), clearer audit
  trails, easier per-endpoint quota/rate-limit policy.
- **Extend ADR 042's `/bundles/from-llm` to multi-agent.** Rejected — ADR
  042 is single-agent-bundle by D6 ("project scope") and *Out of scope*
  bullets ("Multi-agent workflow composition"). The `project-from-llm`
  pipeline **reuses** the ADR 042 orchestrator (D4's `generate_agent[N]`),
  but the endpoint is its own surface so ADR 042's existing scope contract
  is preserved and the multi-agent semantics live behind their own typed
  endpoint.
- **Couple update/reconcile to the Self-Improving Loop's `proposed_patches`
  table.** Considered. Decision: **reuse the typed-kind schema (D3) but
  keep separate storage**. ADR 043's `proposed_patches` is keyed by an
  auto-trigger source (`source_cluster_id`); this ADR's drafts are keyed by
  a human description. Sharing the diff schema is the right reuse;
  conflating storage would confuse the audit story. The two surfaces emit
  to overlapping event families (`patch.*` vs. `proposed_patch.*`) but
  remain distinguishable for compliance review.

---

## Boundaries (explicitly NOT in scope)

- **The auto-improvement loop** (ADR 043 — the **closed-loop** counterpart
  to this ADR's **human-driven** loop). ADR 043 produces a draft from a
  drift/harvest/eval-failure trigger; ADR 044 produces a draft from a human
  NL description. They share the typed-patch schema and the audit posture;
  they do not share triggers or storage.
- **The catalog contribution flow** (ADR 041 community namespace) — this ADR
  *consumes* catalog suggestions (D5) but does not author catalog entries.
  "Promote this composed bundle to the catalog" is a future surface.
- **Cross-tenant evolution sharing** — never. A reconcile or
  `project-from-llm` draft is tenant-scoped + project-scoped. Sharing across
  tenants is explicitly out of scope and would require a separate ADR with
  its own privacy/compliance story.
- **Changes to existing `mdk init --llm` semantics** — back-compat preserved
  per CLAUDE.md rule 5; ADR 026 contract unchanged.
- **Changes to ADR 042's Bundle Composer pipeline shape** — `project-from-llm`
  *consumes* the orchestrator; any change to the per-agent stages belongs in
  ADR 042 (or a successor), not here.
- **Changes to the ADR 043 D4 typed-kind enum** — this ADR consumes the enum
  as-is. Extensions to the enum belong in ADR 043 (or a successor) so the
  Diagnoser and this evolution surface always agree on the kind set.
- **Workflow-level reconcile** (e.g. "reroute every workflow that touches
  agent X") — out of scope for v1; the v1 `reconcile` operates on agents +
  contexts + KB. Workflow reconcile is a v2 candidate once ADR 037 + ADR
  029 land.
- **CI changes** — `cli ⊥ runtime` is unchanged; the pipelines run on the
  existing ADR 017 worker + outbox.

---

## Cross-references / composition notes

### Composition with ADR 042 (Bundle Composer — Flagship 1)

`project-from-llm` is **conceptually and structurally Bundle Composer × N +
a workflow + shared resources**. The composition is **real reuse, not
metaphor**:

- The `generate_agent[N]` stage (D4) **instantiates the ADR 042
  `core/compose/` orchestrator** N times under one parent job. Each instance
  runs ADR 042 D2 rows 1–4 (`plan → generate_agent → generate_contexts →
  seed_kb`) scoped to its agent only. The orchestrator is shared
  infrastructure; this ADR adds a parent-job wrapper, not a parallel engine.
- The shared SSE event taxonomy (this ADR) is a **superset** of ADR 042's
  taxonomy — every ADR 042 D3 event kind is preserved verbatim; this ADR
  adds `affected_artifacts`, `catalog.suggestion`, `plan.proposed`, and
  `proposed_patch.*`. ADR 042's emitters continue to work unchanged.
- The unified job tier (D2 here, `/api/v1/jobs/...`) **generalizes ADR 042's
  per-bundle job endpoints**. ADR 042's existing
  `/api/v1/projects/{id}/bundles/jobs/{job_id}/...` endpoints continue to
  function and are redirected to the unified tier via 308 in a follow-up
  PR (one-release deprecation window, CHANGELOG entry).
- The draft lifecycle is the same `draft → committed` flow on the same ADR
  014 registry with the same content-addressed hashes (ADR 021). A
  multi-agent draft from `project-from-llm` is indistinguishable from a
  hand-authored project once committed, just as a Bundle Composer draft is
  indistinguishable from a hand-authored agent.

### Composition with ADR 043 (Self-Improving Loop — Flagship 2)

ADR 043 and ADR 044 are the **auto-driven** and **human-driven** halves of
the same evolution story:

- **Shared diff schema.** The seven typed patch kinds (ADR 043 D4) are the
  same enum both surfaces emit. A `prompt_edit` proposed by a human via
  `mdk update --llm "..."` and a `prompt_edit` proposed automatically by the
  Failure Pattern Diagnoser are bit-identical typed artifacts that flow
  through the same deterministic applier.
- **Shared registry lifecycle.** Both surfaces produce drafts on the ADR 014
  registry with the same content-addressed versioning. A patch that lands
  via auto-canary (ADR 043) and a patch that lands via human review (this
  ADR) bump the same registry in the same way.
- **Distinct storage / distinct triggers.** ADR 043's `proposed_patches`
  table is keyed by an auto-trigger source (drift alert / harvest miss /
  eval-failure cluster, via `source_cluster_id`). This ADR's drafts are
  keyed by a human NL description (via `request_description`). Sharing the
  table would conflate the audit story; sharing the schema is the right
  reuse.
- **Overlapping but distinguishable event families.** ADR 043 emits
  `patch.proposed` / `patch.approved` / `patch.promoted` / `patch.reverted`
  on the existing R5 outbox (auto-lifecycle). This ADR emits
  `proposed_patch.added` / `completed` / `commit.*` on the SSE stream
  (human-lifecycle). Auditors can distinguish "Claude proposed this in
  response to a drift alert" (ADR 043) from "a human asked Claude to update
  this" (this ADR) by event family + `source_cluster_id` presence.
- **Watch-item: future convergence.** When v2 introduces policy-controlled
  auto-apply for low-risk human-initiated patch kinds (Alternatives
  considered #2 above), the two surfaces' lifecycles begin to overlap. A
  future ADR will likely unify the audit event family and possibly the
  storage table. Flagged as a known forward-compat watch-item.
