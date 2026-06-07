# ADR 063 — Eval → fine-tune loop (self-improving agents)

**Status:** Accepted
**Date:** 2026-05-31
**Deciders:** Engineering (eval / providers)
**Context window:** close the **self-improvement loop** — turn an agent's
accumulated eval results into a fine-tuned model, register it, and *prove* it
beats the base before anyone ships it. The pieces already exist in isolation
(eval harvest builds a dataset, the model catalog holds models, the eval engine
compares against a baseline); this ADR composes them into one governed loop.
**Builds on / composes with (changes nothing in any of them):**
the eval-harvest pipeline (`movate.core.harvest.ProposedCase` +
`to_dataset_row` → `evals/dataset.jsonl` — the training-set source already
grows from real runs), the model catalog (`movate.providers.model_catalog` /
`ModelInfo` — where a fine-tuned model registers so an `agent.yaml` can
reference it), the eval engine (`EvalEngine` + `baseline_id` / regression
comparison — the eval-vs-base gate already exists), the async job model
(`JobRecord` + the worker + the `POST …/evals` → 202 + `job_id` pattern this
mirrors), the `BaseLLMProvider` adapter seam (where provider-specific dispatch
lives), and BACKLOG item ADR 005 (this ADR *is* that item, made concrete).

**Defining gap.** Today eval is a *dead end*: you score an agent, harvest cases
into `dataset.jsonl`, and… that's it. There is no path from "we have 500 graded
examples" to "a model fine-tuned on them, proven better." Customers ask for
exactly this loop (it's the difference between a prompt wrapper and a platform).
Every ingredient is already built; what's missing is the **governed composition**
+ the provider fine-tune dispatch.

This is a **design** ADR for a new subsystem behind existing seams (rule 2). It
adds an **opt-in** extra and a new `JobKind`; it changes nothing in eval,
harvest, the catalog, or the providers' inference path.

---

## Decision

A four-stage loop, each stage reusing an existing component, gated end-to-end:

### D1 — Dataset prep from graded eval cases (reuse harvest)

Select cases from the agent's `evals/dataset.jsonl` + run history **above a
configurable score floor** (`--min-score`, per-dimension or overall) and format
them into the target provider's fine-tune schema (OpenAI/Together messages
JSONL; Anthropic/Bedrock equivalents). Reuses `ProposedCase.to_dataset_row` as
the source of truth so the training set is exactly the curated, human-reviewed
examples — never raw unvetted runs (anti-poisoning, same discipline as
[[feedback→harvest]]). A dataset below a minimum row count fails fast with a
clear message (rule 10).

### D2 — Hosted-job dispatch behind a `FineTuneProvider` seam

Fine-tuning is a provider call, so it lives behind an adapter — a thin
`FineTuneProvider` Protocol (or a `BaseLLMProvider.start_finetune` capability)
with impls for the providers that offer it (OpenAI, Together, Bedrock; Anthropic
when GA). Dispatch is **async**: `POST /api/v1/agents/{name}/finetune` creates a
`JobRecord(kind=FINETUNE)` and returns **202 + `job_id`**; the worker submits the
provider job and polls the provider's job id, mirroring the eval async path
(`GET /jobs/{job_id}` to follow). Uses the tenant's **BYOK** keys (never
Movate's), through the existing provider-key seam.

### D3 — Register the result in the model catalog

On success the provider returns a fine-tuned **model id** (e.g.
`openai/ft:gpt-4o-mini:tenant:abc123`). It's registered as a `ModelInfo` in the
catalog (tenant-scoped, with provenance: source agent, dataset hash, base model,
created_at) so an `agent.yaml` `model.provider` can reference it like any other
model — no special-casing downstream.

### D4 — Auto eval-vs-base + promotion gate (reuse EvalEngine)

Immediately run the agent's eval suite on the **fine-tuned** model with the base
model as `baseline_id` — the regression/improvement comparison the eval engine
already does. The loop **does not auto-promote**: it reports the scorecard delta
and gates promotion on a configurable threshold (`--promote-if-better`, default
**off** → human reviews the delta). A regression (fine-tune scored worse) is a
loud, non-promoting result, not a silent swap.

### D5 — CLI + API parity

`mdk finetune <agent> [--min-score …] [--provider …] [--promote-if-better]`
(local + `--target`), and `POST /api/v1/agents/{name}/finetune` (async, scope
`eval` + a new `finetune:write` or reuse `admin`). Status via the job; the
resulting model + eval-vs-base via the existing catalog + eval endpoints. The
CLI↔API parity gate maps the new verb.

### D6 — Backward compatibility (additive, opt-in)

Heavy provider SDKs go in an opt-in `pyproject` extra (`mdk[finetune]`,
permissively licensed per `check_licenses`); a runtime without it returns a
clean 503 (same graceful-degrade as other optional subsystems). New
`JobKind.FINETUNE`, new catalog provenance fields (additive), new routes/verbs
(flagged, rule 5). Eval, harvest, the catalog, and the inference path are
**unchanged**. No version bump in a PR (ADR 059).

## Consequences

**Positive**
- Closes the loop: **eval → dataset → fine-tune → register → eval-vs-base** —
  the self-improving-agent story, composed from parts already shipped.
- **Governed by construction**: curated dataset (D1), no auto-promote (D4),
  BYOK (D2) — improvement is *proven and reviewed*, not blind.
- Fine-tuned models are first-class catalog entries (D3) — reference them like
  any model, no downstream special-casing.

**Negative / risks**
- Provider fine-tune APIs differ + drift — bounded by the `FineTuneProvider`
  seam (rule 7); one impl per provider, the loop is provider-agnostic.
- Cost + time: a fine-tune job is minutes-to-hours and costs real money —
  hence async + the explicit `min-score` gate + the dataset-size floor so a
  tiny/garbage dataset never silently burns a job (rule 10).
- Overfitting to the eval set: the eval-vs-base (D4) uses a held-out slice +
  the existing regression tolerance so a model that memorized the train split
  doesn't look better than it is.

## Boundaries

Provider dispatch behind the adapter seam (rule 7); orchestration reuses the
job model; dataset reuses harvest; comparison reuses the eval engine; models
land in the catalog. `core` depends on the `FineTuneProvider` Protocol, never a
concrete SDK (rule 6). Opt-in extra, default-off, every existing path unchanged.

## Alternatives considered

- **Auto-promote on any improvement.** Rejected — silently swapping the model
  under a production agent on a noisy eval delta is exactly the operational
  failure mode (rule 10) we avoid; D4 gates + defaults to human review.
- **Train on all runs, not just graded cases.** Rejected — unvetted runs poison
  the set; the curated `dataset.jsonl` is the whole point of harvest.
- **A bespoke fine-tune model store separate from the catalog.** Rejected —
  duplicates the catalog + breaks `agent.yaml model:` referencing; a fine-tuned
  model is just a model (D3).
- **Synchronous dispatch.** Rejected — fine-tune jobs run minutes-to-hours;
  async + job-poll is the only sane shape (mirrors eval).

## Scope / rollout

1. `FineTuneProvider` seam + OpenAI impl (D2) + `JobKind.FINETUNE` + the async
   `POST …/finetune` route + worker handler.
2. Dataset prep from harvest above a score floor (D1) + the dataset-size guard.
3. Catalog registration with provenance (D3).
4. Auto eval-vs-base + the promotion gate (D4) + `mdk finetune` CLI + parity (D5).
5. Additional provider impls (Together / Bedrock / Anthropic) — incremental.
