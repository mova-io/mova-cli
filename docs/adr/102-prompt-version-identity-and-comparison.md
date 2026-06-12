# ADR 102 — Prompt version identity: thread prompt_hash through evals, facts, and traces; registry diff UX; deferred Langfuse prompt mirror

Status: Accepted
Date: 2026-06-12
Deciders: Engineering — touches `core` (eval/bench/executor record
construction), `storage` (two additive nullable columns), and the `mdk agent`
CLI sub-app (CLAUDE.md rule 1/2: agreement required before implementation).
Builds on: ADR 014 (durable agent registry — immutable
`(name, tenant, version)` bundle rows are the prompt's version history),
ADR 021 D2 (compare-and-publish; derived `<version>+<hash8>` local versions),
ADR 015 (self-hosted Langfuse as default trace sink), ADR 024 (step
observability — the `agent.execute` span this ADR adds an attribute to),
ADR 096 (observability facts — the bounded `attributes` escape hatch),
ADR 043 (self-improving loop — its `prompt_edit` → canary → promote/revert
pipeline is the intended consumer of this measurement substrate).
Supersedes: nothing. Rejected alternative documented here: adopting MLflow
for prompt versioning / eval tracking (see Alternatives).

## Context

The question this ADR makes answerable is: **"did prompt version A score
better than prompt version B?"** — from our own Postgres for the eval gate,
and from the Langfuse UI for live-traffic triage.

The raw material already exists:

- **Prompt identity.** The loader hashes the raw template at load time:
  `prompt_hash = sha256(prompt_text)` (`core/loader.py:357-361`), carried on
  `AgentBundle.prompt_hash` (`core/loader.py:110`). The executor stamps it on
  every `RunRecord` (`core/executor.py:2143`, model field
  `core/models.py:2180`).
- **Prompt history.** The durable registry snapshots the full prompt text
  immutably per version in `AgentBundleRecord.files` with a bundle-wide
  `content_hash` (`core/models.py:3010-3043`,
  `runtime/agent_resolver.py:72-79`). The table *is* the version history;
  `mdk agent history` already lists it (`cli/agent_cmd.py`).

What's missing is that the identity **dies at the RunRecord**:

- `EvalRecord` carries `agent_version` + `dataset_hash` but no prompt hash
  (`core/models.py:2704-2739`); same for `BenchRecord`
  (`core/models.py:2846-2863`). `agent_version` is an imprecise join key for
  prompt comparison in exactly the workflow where comparison matters most —
  local iteration, where the prompt changes *without* a version bump.
  (Publish-time collisions are handled by ADR 021's derived
  `<version>+<hash8>`, but eval runs during authoring happen *before*
  publish.)
- `BaselineDiff` flags `dataset_changed` but cannot tell the operator the
  prompt changed between baseline and current (`core/baseline.py:56-57`) —
  the single most common cause of a score delta.
- The `agent.execute` root span carries `agent` / `agent_version` attributes
  (`core/executor.py:398-407`) but **not** `prompt_hash`. The hash appears
  only inside a `log_event` alongside the rendered text
  (`core/executor.py:607-618`) — events are not filterable trace dimensions,
  so the Langfuse/ClickHouse stores cannot group traces by prompt version.
- `ObservabilityFact.attributes` carries provider / pricing_version / model
  (`runtime/facts.py:58-68`) but neither `agent_version` nor `prompt_hash` —
  the one platform integration surface (ADR 096) cannot answer the question
  at all.
- There is no comparison UX: bundles store every file per version, but the
  only consumer is `mdk agent revert`; no diff, no per-version eval rollup.

Scope note on identity: `prompt_hash` hashes the **raw template** only.
Shared contexts are prepended at render time (`core/loader.py:119-143`) and
the rendered text varies per input; the template hash is the stable identity.
A context change surfaces through `agent_version` / bundle `content_hash`,
not `prompt_hash` — documented, not hidden.

This ADR was scoped after evaluating (and rejecting) MLflow as the vehicle
for evals / regression / trace analysis / prompt comparison — the existing
`EvalRecord` + `BaselineDiff` + Langfuse architecture already covers four of
those five capabilities; prompt-version comparison is the genuine gap, and it
closes with the registry we already have plus the Langfuse we already run.

## Decision

Thread the existing `prompt_hash` through every surface that answers
"which prompt produced this number," add a read-only diff/compare command on
the bundle registry, and **defer** the Langfuse prompt-registry mirror as an
explicitly parked decision (D6). Phases 1 (D1–D4) and 2 (D5) are one PR each
(CLAUDE.md rule 3); D6 ships only if post-Phase-2 friction proves the need.

### D1 — `prompt_hash` on `EvalRecord` and `BenchRecord` (additive, nullable)

Add `prompt_hash: str | None = None` to both models, following the
`dimension_means` additive-nullable pattern verbatim
(`core/models.py:2723-2738`): `None` for legacy rows, populated for new ones.

- Eval engine: populated from `bundle.prompt_hash` where the record is built
  (`core/eval.py:595-599`, via the summary dataclass that already carries
  `agent_version` at `:536`). Eval cases execute through the executor with
  the same bundle, so the eval's hash and its per-case `RunRecord` hashes are
  consistent by construction.
- Bench engine: same, at `core/bench.py:157-161` / `:286`.
- Storage: one additive nullable column on each of the `evals` / `bench`
  tables in both providers (SQLite, Postgres), with the same migration shape
  used for `dimension_means`. No index — lookups stay keyed by
  agent/eval_id; the hash is a carried attribute, not a query plan.

### D2 — `BaselineDiff.prompt_changed`

Add a `prompt_changed: bool | None` property next to `dataset_changed`
(`core/baseline.py:56-57`): `None` when either side's `prompt_hash` is
`None` (legacy row — "unknown", rendered as such), else inequality.
**Informational only** — it does not enter `is_regression()`; changing a
prompt and regressing is exactly the case the gate must keep failing. The
eval CLI renders it alongside the existing dataset-changed flag, and
`regression_summary()` (`core/baseline.py:97-108`) appends
`prompt_changed=yes/no/unknown` so CI logs answer the first triage question
without a dashboard.

### D3 — `prompt_hash` as an `agent.execute` root-span attribute

Add `"prompt_hash": bundle.prompt_hash` to the root-span attribute dict at
`core/executor.py:398-407`, beside `agent_version`. One line; flows through
the `Tracer` Protocol unchanged to every sink — Langfuse (filterable
metadata), OTel/ClickHouse (queryable span attribute) — with zero
sink-specific code, honoring the boundary rule (tracing wired at the edges).
The existing `log_event` carrying the full rendered text
(`core/executor.py:607-618`) is untouched; the event keeps the *what*, the
span attribute adds the *filterable which*.

### D4 — `agent_version` + `prompt_hash` in fact attributes

In `fact_from_run_record` (`runtime/facts.py:58-68`), add
`"agent_version": record.agent_version` and
`"prompt_hash": record.prompt_hash` to the attributes dict — the bounded
escape hatch is the designed home for exactly this kind of key (ADR 096;
provider / pricing_version / model already live there). Derived facts
(ADR 096 D4) mean a backfill is a re-derivation, not a migration; the
fail-soft write contract (`write_fact_failsoft`, `runtime/facts.py:130-141`)
is untouched. Workflow-level facts (`fact_from_workflow_run`) are out of
scope — they carry no agent identity by design.

### D5 — `mdk agent diff` + per-version eval rollup (read-only)

A new subcommand on the **existing** `mdk agent` sub-app
(`cli/agent_cmd.py`, beside `history` / `revert` — no new top-level verb,
per the `mdk dev`-front-door convention):

- `mdk agent diff <name> [<v1>] [<v2>]` — unified diff between two bundle
  versions' `files`, defaulting to newest-vs-previous and to the prompt file
  (resolved from the bundle's own `agent.yaml` `prompt:` key), `--all-files`
  for the full bundle. Pure read of existing data via
  `get_agent_bundle(name, version=...)` / `list_agent_versions(name)`
  (`storage/base.py:1406-1452`); difflib; no storage change.
- `--evals` — joins each side's eval history (matched by `prompt_hash` when
  present, falling back to `agent_version` for legacy rows) and renders
  mean_score / pass_rate / dimension_means / cost side-by-side. Uses the
  existing eval listing surface; if the current listing lacks an agent
  filter kwarg, adding one is additive and bundled here.
- Surfaced in the `mdk dev` actions menu as a thin wrapper (the convention
  for new authoring-loop affordances), not a new resident mode.

This is the "prompt/version comparison" deliverable, answered from our own
Postgres — no external system required.

### D6 — DEFERRED: Langfuse prompt-registry mirror at the publish edge

Parked, not rejected. If, after running D1–D5 for a few weeks, the team
still wants Langfuse's prompt UI (readable version history, diff view,
`production`/`staging` labels, metrics-by-prompt-version via
generation→prompt linking), the agreed shape is:

- **Hook**: `publish_agent_bundle` (`runtime/agent_resolver.py:441-479`),
  only on `published=True` (the compare-and-publish no-op path stays a
  no-op). Push the prompt text via the Langfuse SDK's `create_prompt`
  (same-name pushes auto-version server-side), tagged with the agent
  version + `prompt_hash`.
- **Posture**: fail-soft exactly like fact writes — a Langfuse outage must
  never fail a publish. Opt-in via a new env var (e.g.
  `MDK_PROMPT_MIRROR=langfuse`) **in addition to** the existing
  `LANGFUSE_*` credentials; absent → no behavior change.
- **Linking**: `LangfuseTracer.log_generation` gains optional
  generation→prompt-version linking (v3 SDK supports it) so Langfuse rolls
  up metrics per prompt version. Langfuse-specific, so it lives in
  `tracing/langfuse.py` only — the `Tracer` Protocol is unchanged.
- **Source of truth invariant**: the bundle registry remains canonical. The
  runtime **never fetches prompts from Langfuse** — that would invert
  ADR 014 and add a network dependency to the execution hot path. Langfuse
  is a mirror/UI, write-only from mdk's perspective.

Triggering D6 requires no new ADR — this section is the decision — but does
require its own PR and a note here flipping this D to "activated".

### D7 — Compatibility (flagged per CLAUDE.md rule 5)

- **Storage schema**: two additive nullable columns (`evals.prompt_hash`,
  `bench.prompt_hash`), both providers + migration. Legacy rows read back
  with `None`; no backfill required (facts backfill is re-derivation).
- **`--json` shapes**: eval/bench outputs gain a `prompt_hash` key and the
  baseline diff gains `prompt_changed` — additive; documented in the PR.
- **Facts**: two additive keys inside `attributes` — the escape hatch is
  explicitly schemaless for readers (ADR 096); `/api/v1` unchanged.
- **Traces**: one additive root-span attribute; no tracer Protocol change.
- **CLI**: one new subcommand (`mdk agent diff`) + one `mdk dev` menu entry —
  additive; no existing flag changes.
- **Env vars**: none in Phases 1–2; `MDK_PROMPT_MIRROR` only if D6 activates.
- **`agent.yaml` / `project.yaml`**: untouched.

### D8 — Tests

- `EvalRecord`/`BenchRecord` round-trip with and without `prompt_hash`
  (legacy-row `None` path) on both storage providers.
- Eval engine stamps the bundle's hash; an eval's `prompt_hash` equals the
  `prompt_hash` of the `RunRecord`s it produced.
- `BaselineDiff.prompt_changed`: changed / unchanged / either-side-legacy
  (`None`) — and `is_regression()` is provably unaffected by it.
- `fact_from_run_record` carries the two new attribute keys; absent fields
  degrade per the existing defaults posture.
- Root span attrs include `prompt_hash` (existing tracer test seam — mock
  client, `tracing/langfuse.py:61`).
- `mdk agent diff`: prompt-file default resolution from `agent.yaml`,
  full-bundle mode, eval join by hash with version fallback, and the
  two-versions-identical no-op.

## Boundary (out of scope)

- **No standalone prompt table / prompt registry service.** The bundle
  registry (ADR 014) is the version store; a parallel prompt store would be
  a second source of truth.
- **No runtime prompt fetch from any external system** (Langfuse, MLflow, or
  otherwise). Prompts load from the bundle, full stop.
- **No MLflow.** Evals/regression stay on `EvalRecord` + `BaselineDiff`;
  trace analysis stays on Langfuse/ClickHouse/facts. If an experiment-UI
  export ever earns its keep, it is a separate ADR for a one-way,
  fail-soft sink — never a store of record. (MLflow's *model registry* may
  become relevant if ADR 063's eval-to-finetune loop lands; revisit there.)
- **No prompt A/B routing or auto-promotion.** Canary → promote/revert is
  ADR 043's pipeline; this ADR only provides its measurement substrate.
- **Per-case eval persistence** (the `baseline.py` docstring's v0.4.1+ note)
  — unchanged non-goal here.

## Alternatives considered

- **Adopt MLflow (tracking server + prompt registry) self-hosted on Azure.**
  Duplicates Langfuse (both OTel-based trace stores), forks the eval source
  of truth out of Postgres, and adds a fourth stateful service (server +
  Postgres backend + Blob artifact store + an auth story — OSS MLflow
  multi-tenancy is experimental) to every customer RG, multiplying the
  retention/backup burden the unified-observability architecture (ADR
  095/096) exists to cap. Rejected for this gap; parked for ADR 063's
  model-artifact use case.
- **Use Langfuse prompt management as the source of truth** (runtime
  `get_prompt` by label). Inverts ADR 014 — the registry stops being
  canonical — and puts a network fetch on the execution hot path with a new
  availability dependency. Rejected; D6 keeps Langfuse strictly a mirror.
- **Join on `agent_version` only (no new fields).** Free, but wrong in the
  authoring loop: local prompt edits don't bump the version, so consecutive
  evals of different prompts become indistinguishable — precisely the
  comparison the feature exists for. ADR 021's `+hash8` derivation only
  covers *published* collisions.
- **A standalone `prompt_versions` table keyed by hash.** Cleaner queries,
  but a second registry to migrate, tenant-scope, and keep consistent with
  bundles — for data the bundle rows already hold byte-for-byte. The diff
  command reads bundles instead.
- **Stamp the full rendered prompt (not the hash) on records.** Rendered
  text varies per input and bloats rows; the template hash is the stable
  identity and the rendered text is already in the trace event
  (`core/executor.py:607-618`).

## Consequences

- "Did prompt A beat prompt B" becomes a first-class query: `mdk eval
  --baseline` says whether the prompt changed, `mdk agent diff --evals`
  shows the per-version scorecard from Postgres, and Langfuse/ClickHouse
  traces filter by `prompt_hash` for live-traffic comparison — all without a
  new backend service.
- ADR 043's `prompt_edit` → canary → promote/revert loop gains its
  measurement substrate: a canary's runs, evals, facts, and traces all carry
  the candidate prompt's hash.
- The facts surface (ADR 096) can now answer prompt-version questions for
  the mova-io platform without coupling to `RunRecord` internals.
- Risks accepted: `prompt_hash` identity excludes shared contexts (documented
  above); legacy rows compare as "unknown" until they age out; D6, if
  activated, adds one outbound write edge to publish (fail-soft, opt-in).
- Estimated scope: **Phase 1 (D1–D4) one PR** — two models + two engines +
  one executor line + facts builder + migrations + D8 tests. **Phase 2 (D5)
  one PR** — CLI only. **D6 deferred** — one PR if activated.
