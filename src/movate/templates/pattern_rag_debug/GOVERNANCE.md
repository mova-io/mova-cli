# RAG Retrieval Debugging pattern — governance

Topology: `TOOL retrieve → DECISION(top_score) → {answer | diagnose}`

RAG with the retrieval stage as an auditable, replayable step, durable on
Temporal — **zero LLM calls on the retrieval and control path**. The entry
`retrieve` TOOL node (ADR 097) runs the workflow-local `sim-retrieve` python
skill: deterministic keyword scoring over a small inline knowledge base
(stdlib only — no embeddings, no network), recording one auditable
`{system: vectorstore, action: retrieve}` ledger row per run. A deterministic
`decision` node (ADR 094) routes the score: `top_score gte 0.5` reaches the
`answer` agent (grounded STRICTLY in `retrieved_docs`); anything lower
reaches the `diagnose` agent — **low retrieval quality is a first-class
route, not an error**, so the failure mode ships with an explanation and a
suggested query reformulation instead of a hallucinated answer.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Deterministic retrieval as a step | the `retrieve` TOOL node (ADR 097) — one `dispatch_skill` call, schema-validated in and out, SKILL-gate governed, no LLM. |
| Auditable retrieval | every run records one `{system: vectorstore, action: retrieve}` ledger row — including the zero-hit miss (the audited miss is the debugging evidence). |
| Deterministic score routing | the `score-gate` `decision` node — a pure numeric predicate over `top_score`; no model in the control path. |
| Grounded answering | the `answer` agent's prompt composes STRICTLY from `retrieved_docs` and cites the document ids it used. |
| The failure mode as a route | the `diagnose` agent names the score and proposes a reformulation matching the corpus vocabulary. |
| Self-contained workflow-local skill | `skills/sim-retrieve/` carries its own `impl.py` next to `skill.yaml` — bakes into a worker image with the workflow. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| No cycles | the compiler rejects cyclic graphs at compile time | a runaway reformulate-and-retry loop is impossible by construction — the reformulation is a SUGGESTION to the caller, not an automatic retry. |
| Retrieval is deterministic | `sim-retrieve` is a pure stdlib scoring function + one ledger write | the retrieval replays identically on Temporal and its quality is provable per run. |
| The answer cannot outrun retrieval | the routing threshold is structural (the decision node) | a low-score retrieval can never reach the answering agent — the hallucination path is unreachable, not discouraged. |
| Both terminal agents are schema-pinned | `answer`/`sources` and `diagnosis`/`suggested_query` output contracts | each route's deliverable is enforced, not prompted. |

## Customize

- Point `sim-retrieve` at your real vector store: swap the impl.py body
  (return the same `{retrieved_docs, top_score}` contract), keep the ledger
  row — the auditable-retrieval property survives the swap.
- Tune the threshold: `score-gate`'s `value` is the precision/recall knob —
  raise it to send more queries to diagnosis, lower it to answer more.
- Extend the corpus: `_DOCS` in impl.py is the inline KB — replace it with
  your documents (or a loader) without touching the workflow.
- Add a re-ask loop OUTSIDE the workflow: feed `suggested_query` into a new
  run from the caller — the graph itself stays acyclic.

## Budget

Per-run LLM spend is bounded: **exactly 1 model call on every path** (answer
OR diagnose — retrieve and the score-gate routing are deterministic,
zero-cost). Cap absolute spend with the agent `budget.max_cost_usd_per_run`
field or a governance COST gate (ADR 093); the eval-gate below is the
quality budget.
