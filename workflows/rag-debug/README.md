# rag-debug — RAG retrieval debugging (certification scenario #11)

Every retrieval stage as an auditable step, durable on Temporal:

```
TOOL retrieve (sim-retrieve: keyword scoring over an inline KB,
               ledger row {system: vectorstore, action: retrieve})
  → DECISION score-gate (top_score gte 0.5)
      → answer    (LLM: composes the answer STRICTLY from retrieved_docs)
      → diagnose  (LLM: explains the low score + suggests a reformulation)
```

* **Retrieval is deterministic** (ADR 097): the workflow-local `sim-retrieve`
  python skill (impl.py bundled next to skill.yaml — bakes into the
  temporal-worker image) scores lowercase content keywords against five
  inline IT-helpdesk documents and returns `retrieved_docs` + `top_score`.
  A zero-hit query is a routable outcome, never an error.
* **Routing is deterministic** (ADR 094): the `score-gate` decision node is a
  pure numeric predicate over `top_score` — no LLM in the control path.
* **The failure mode is a first-class route**: the diagnose agent is the
  debugging output the scenario certifies — it names the score and proposes
  a query the corpus vocabulary can actually match.
* **No shared tail on purpose**: the two routes share no state keys, so each
  terminates on its own agent (ADR 098 exclusive convergence kept trivial).

Run locally (real LLM on the terminal agent, retrieval always real):

```
mdk run workflows/rag-debug '{"query": "How do I reset my corporate password?"}'
mdk run workflows/rag-debug '{"query": "zebra cantaloupe spaceship maintenance"}'
```

Mirrors `certification/scenarios/rag-debug` (driven by
`certification/run_suite.py`) and ships as the `rag-debug` pattern template
(`mdk init --pattern rag-debug`).
