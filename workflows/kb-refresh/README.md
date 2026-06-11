# kb-refresh — knowledge-base refresh (certification scenario #23)

Validated, auditable KB refresh, durable on Temporal:

```
TOOL ingest (sim-ingest: fixed chunking rule, ledger row {system: kb, action: ingest})
  → validate (LLM: judges the ingest SUMMARY → {ok, note})
  → DECISION quality-gate (ok eq true)
      → TOOL publish (sim-kb-publish, ledger row {system: kb, action: publish}) → notify
      → HUMAN escalate (ack → notify; fallback notify — NO retry route)      → notify
```

* **Ingest is deterministic** (ADR 097): the workflow-local `sim-ingest`
  python skill (impl.py bundled next to skill.yaml — bakes into the
  temporal-worker image) counts documents and chunks them by a FIXED rule
  (one chunk per 40 words, ceil; empty documents yield 0 chunks and count in
  `empty_docs`).
* **The LLM judges the summary, never the documents**: `validate` applies
  three explicit rules to `ingest_result` and emits the routing boolean.
* **Routing is deterministic** (ADR 094): the `quality-gate` decision node is
  a pure predicate over `ok`.
* **The failure gate cannot retry** (ADR 099): `escalate` routes ack→notify
  with fallback notify — any response acknowledges, the graph stays acyclic.
  A failed refresh is fixed at the source and submitted as a NEW run.
* **One shared tail** (ADR 098): both paths converge on ONE `notify` agent
  whose prompt GUARDS the path-exclusive keys (`publish_result`, `decision`)
  — StrictUndefined-safe on both routes.

Run locally (real LLMs on validate/notify, ingest/publish always real):

```
mdk run workflows/kb-refresh '{"documents": [{"id": "doc-1", "text": "..."}]}'
```

Mirrors `certification/scenarios/kb-refresh` (driven by
`certification/run_suite.py`) and ships as the `kb-refresh` pattern template
(`mdk init --pattern kb-refresh`).
