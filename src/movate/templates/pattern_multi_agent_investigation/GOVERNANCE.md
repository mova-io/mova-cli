# Multi-Agent Investigation pattern — governance

Topology: `plan → FAN-OUT {web-researcher ∥ kb-researcher ∥ data-analyst} → FAN-IN synthesize`

Multi-agent investigation over the canonical PARALLEL FAN-OUT/FAN-IN diamond
(ADR 092), durable on Temporal. A `plan` agent decomposes the question, three
specialist sims research it CONCURRENTLY — each from its own source of truth,
each writing its own disjoint findings key — and one `synthesize` agent joins
the branches, merging the findings into `{conclusion, confidence}` and
explicitly acknowledging any disagreement between the sources.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Parallel fan-out/fan-in (ADR 092) | the three `kind: fan_out` edges out of `plan` and the three `kind: fan_in` edges into `synthesize` — the canonical single-node-branch diamond. |
| Durable parallelism on Temporal | Phase 2 lowers the branches to `await asyncio.gather(...)` of agent activities — concurrent, durable, replayable; native runs the same diamond via the Phase 1 block executor with identical joined state. |
| Clobber-free barrier join | each branch writes its OWN key (`web_findings` / `kb_findings` / `data_findings`), so the default `last_wins` join merges all three without overwrites. |
| Calibrated specialist sims | each researcher answers ONLY from its embedded corpus, labels its findings (`[web]` / `[kb]` / `[data]`), and says when the corpus lacks coverage — no cross-contamination between perspectives. |
| Conflict-aware synthesis | the `synthesize` prompt hard-requires naming conflicting sources, stating each figure, and reconciling — with `confidence` capped at 0.6 on conflict. |
| Self-contained agents | all five bundled under `agents/` with correct schemas + JSON-instructed prompts. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| Fan-out cap is structural | exactly THREE `fan_out` edges in `workflow.yaml` (well under the engine's `max_fanout` ceiling, ADR 092 D5) | the model cannot widen the roster at runtime — adding a branch is a reviewable edit to this file. |
| Single-node branches | each branch is one agent node | keeps the diamond canonical, so it compiles to Temporal-native `asyncio.gather` (Phase 2) — no shape that silently degrades. |
| One barrier join | all branches reconverge on `synthesize` (validate_dag enforces the diamond closure) | a branch cannot dangle or exit the workflow early. |
| No cycles | the compiler rejects cyclic graphs at compile time | a runaway loop is impossible by construction. |
| Per-branch budget | every agent runs under the executor's per-run budget (default `max_cost_usd_per_run` = 1.0) and the catalog policy's MODEL + COST gates (ADR 093) | a branch cannot overspend unnoticed; the run's ceiling is the five bounded node budgets summed. |

## Customize

- Swap each specialist's embedded corpus for its real source: a web-search
  skill, `mdk kb` retrieval, a warehouse query skill — keep the
  one-labeled-findings-key contract so the join stays clobber-free.
- Add a branch: one agent dir + one `fan_out` + one `fan_in` edge (a
  reviewable diff); keep branches single-node for Temporal parity.
- Tighten the synthesis: raise the agreement bar in the synthesize prompt or
  route low-confidence conclusions into a human gate downstream.

## Budget

Per-run LLM spend is bounded: **exactly 5 model calls on every path** (plan,
the three parallel specialists, synthesize) — there is no loop and no
conditional branch, so the call count is constant; each call is capped by the
agent's per-run budget.
