# ADR 023 — Declarative pre-retrieval: opt-in auto-RAG in the shared Executor

**Status:** Proposed
**Date:** 2026-05-26
**Deciders:** Engineering + Deva (Movate)
**Context window:** v1.x inner loop — make grounding-by-default a first-class,
deterministic agent capability so RAG agents work identically in local `mdk run`,
the runtime inline path, and the worker, without each template hand-rolling a
"retrieve if empty" prompt instruction.
**Related / constrained by:** ADR 002 (skills + contexts; its "When to revisit"
explicitly defers RAG-as-a-capability to a separate ADR, and its lesson "don't
conflate skill-with-RAG"), ADR 009 (pgvector KB storage), ADR 010 (GraphRAG),
ADR 021 (content-addressed version-free iterate). Touches `core/executor.py`,
the `SkillBackend` Protocol, the `bundle.retriever` seam, and `core/models.py`
(`AgentSpec`).

## Decision

Add an **opt-in, declarative pre-retrieval phase** to the shared
`Executor.execute()`. An agent may declare a `retrieval:` block in `agent.yaml`;
when present, the executor — *after* input-schema validation and *before* prompt
render — runs the configured retrieval skill and merges the returned chunks into a
named input field. Because the phase lives in the **one shared Executor** that both
the control plane (`mdk run`) and the execution plane (runtime inline + worker) call,
grounding behaves **identically across planes** by construction.

The feature is **off unless `retrieval.auto_into` is set** — absent the block,
execution is byte-for-byte unchanged. This is additive, backward-compatible, and
does **not** turn skills into RAG: the retrieval still flows through the existing
`SkillBackend` → `StorageProvider`/retriever Protocols (ADR 002 seam). The new
surface is exactly one orchestration phase plus one optional `agent.yaml` field.

```yaml
# agent.yaml — opt in by adding this block; omit it for unchanged behavior
retrieval:
  auto_into: context          # REQUIRED to enable: the input field that receives chunks
  query_from: question        # optional; default = the agent's primary text input field
  skill: kb-vector-lookup     # optional; default = kb-vector-lookup
  top_k: 8                    # optional; passed to the retrieval skill
  when: if_empty              # optional; if_empty (default) | always
  on_error: warn              # optional; warn (default → proceed ungrounded) | fail
```

## Context

Ticket #120 was filed as a bug: *"the `kb-vector-lookup` skill auto-retrieves into
`input.context` on the deployed runtime but not on a local `mdk run`."* Investigation
(verified against the code) showed **the premise is false — neither plane
auto-retrieves**, and the observed difference is a **template asymmetry**, not a
control-plane vs. execution-plane divergence:

- Both planes converge on the identical `Executor.execute`:
  - local — `cli/run.py:_run_local_agent` → `build_local_runtime`
    (`cli/_runtime.py:122`) → `rt.executor.execute(...)` (`cli/run.py:509`);
  - runtime inline — `runtime/app.py:3526-3539`; worker — `runtime/dispatch.py:187`.
- `Executor.execute` validates `request.input` against the schema and calls
  `bundle.render_prompt(request.input)` (`core/executor.py:343-347`); rendering is
  pure Jinja with **no retrieval step** (`core/loader.py:119-143`).
- `kb-vector-lookup` only ever runs as an **LLM-emitted tool call** inside the
  tool-use loop (`core/executor.py:914-975` → `skill_backend/base.py:dispatch_skill`),
  and its result returns as a `tool_result` (`executor.py:1026-1030`) — it is
  **never written back into `request.input`**. `bundle.retriever.query(...)` is
  invoked in exactly one place in the whole codebase — the eval harness
  (`core/eval.py:1097`) — and in **no** run path.

Why two bundled templates behave differently is therefore a **template** issue:

| | `context` field | Prompt instruction | Behavior (both planes) |
|---|---|---|---|
| `rag_qa_agent` | optional (`required: [question]`) | **Step 0**: "if `input.context` is empty, call `kb-vector-lookup`" (`templates/rag_qa_agent/prompt.md:48-57`) | Model self-retrieves via the tool loop — grounded |
| `hr_policy_agent` | **required** (`required: [question, context]`) | none — only `{% if context %}…{% else %}(No context)…` | `mdk run` fails schema validation or renders "(No context)" → ungrounded |

So today there are **two** ways an agent can be grounded, and a gap:

1. **Model-driven retrieval (exists):** the agent declares `kb-vector-lookup` as a
   skill and the *prompt* tells the model to call it when context is missing
   (rag_qa). The model decides whether/what to retrieve — flexible, supports
   multi-hop, but **non-deterministic** (the model may skip it), costs an extra
   model turn, and every template must repeat the instruction.
2. **Explicit context (exists):** the caller passes `context` in the input (the
   #121 eval dataset does this) — deterministic but requires the caller to retrieve.
3. **Deterministic engine retrieval (does NOT exist):** "for this agent, always
   fetch grounding for me before the model sees the prompt." This is what #120's
   author expected and what every from-scratch RAG agent (`mdk init`/`--llm`) wants
   by default. There is no declarative way to get it.

This ADR adds (3). ADR 002 anticipated it directly — its "When to revisit" lists
"RAG/knowledge integration wants to surface as a skill" as a trigger for a separate
ADR, and warns against conflating the skill *contract* with RAG. We honor both: the
skill stays a pure tool; the Executor gains a directive to *pre-invoke* it.

## Decisions in detail

### D1 — Opt-in `agent.yaml: retrieval:` block, off by default
A new optional `AgentSpec.retrieval` field (`core/models.py`). The single field that
**enables** the feature is `auto_into` (the input field name to populate). With no
`retrieval:` block, `Executor.execute` runs exactly as today — no new model turn, no
embedding call, no behavior change. This keeps the dominant non-RAG path untouched
(CLAUDE.md compat rule 5) and makes auto-RAG a deliberate, discoverable opt-in.
Adding the block changes the agent's content hash → a normal content-addressed
version bump (ADR 021); no special handling needed.

### D2 — The phase lives in the shared Executor, between validate and render
The retrieval phase is inserted in `Executor.execute` immediately **after** input
validation and **before** `render_prompt` (`core/executor.py:343-347`). It is in the
**one** code path both planes call, so "local and runtime match" is true *by
construction* rather than by keeping two implementations in sync — directly
addressing the class of confusion that produced #120. The phase reuses the existing
`SkillBackend` dispatch (`skill_backend/base.py:dispatch_skill`) to invoke
`retrieval.skill`; it does **not** add a second retrieval code path. Boundaries
hold: `core` calls the `SkillBackend`/retriever **Protocols**, never a concrete
`postgres`/`sqlite` backend (CLAUDE.md §6/§7).

### D3 — Merge semantics, `when`, and `query_from`
- The executor builds the retrieval skill's input from `query_from` (default: the
  agent's primary string input field; explicit override required when ambiguous),
  invokes the skill, and writes the returned chunk list into `request.input[auto_into]`.
- `when: if_empty` (default) only retrieves when `auto_into` is absent/empty — so an
  explicitly-passed `context` (eval/test path #2) is respected and retrieval is
  skipped, preserving eval determinism. `when: always` re-retrieves unconditionally.
- After merge, the input is **re-validated** against the schema so the populated
  field still conforms. At load time, `mdk validate` checks that `auto_into` names a
  field that accepts the skill's output shape (e.g. `list[string]`) and that
  `retrieval.skill` resolves in the skill registry — fail-loud, like the existing
  agent→skill cross-link check (ADR 002 §5).
- This composes with model-driven retrieval (path 1): `auto_into` populates context
  *before* turn 0; the model may still call the skill again as a tool for follow-up
  hops. They are not mutually exclusive.

### D4 — Mock mode + failure modes (CLAUDE.md §10)
- **No retriever / KB wired:** the phase is a **no-op with one stderr notice**, not
  a hard failure — the prompt's empty-context branch handles it. This keeps
  `--mock` smoke runs and KB-less environments working.
- **`--mock` / MockProvider:** retrieval still runs against whatever
  `StorageProvider` is configured (so a local mock run against a seeded in-memory/
  sqlite KB is genuinely grounded); with no KB, see above.
- **Retrieval/embedding error:** surfaced via the ADR 002 `SkillError` taxonomy.
  `on_error: warn` (default) → proceed ungrounded with a notice; `on_error: fail` →
  abort the run with a typed error. Operators choose per agent.
- **Empty results:** populate `auto_into` with `[]` and proceed (deterministic; the
  template's "no context" branch fires).
- **Cost/latency:** one embedding+search per run for opted-in agents only; the
  skill's `cost.per_call_usd` already participates in budget accounting (ADR 002).

### D5 — Relationship to skills, contexts, and KB (no conflation)
This does **not** make `kind: Skill` into RAG (the ADR 002 anti-pattern). Retrieval
remains a skill invoked through the skill Protocol; `contexts/` remains static
prompt fragments; the KB remains behind `StorageProvider`. The new thing is purely
an **orchestration directive on the agent** that says "pre-invoke this retrieval
skill into this field." If a future need emerges for a first-class `kind:
KnowledgeBase` (ADR 002's other revisit trigger), this field layers cleanly on top
(its `skill:` would target the KB-query skill).

### D6 — Scope: this ADR decides, a follow-up task implements
This ADR is the *decision*; implementation is a separate task (not now). The impl
touches `core/models.py` (`AgentSpec.retrieval` + validation), `core/executor.py`
(the pre-retrieval phase), `cli/validate.py` (load-time field/skill checks), the
`--llm`/template scaffolds (so generated RAG agents opt in by default), and docs. It
must land with the D-matrix tests below. **Until it ships, the pragmatic stopgap for
the specific #120 symptom is the template fix recorded as Alternative (a)** — it can
be done independently and is not blocked by this ADR.

## Consequences

**Positive**
- Closes the real gap behind #120: a declarative, **deterministic** way to ground an
  agent that behaves **identically** in `mdk run`, runtime inline, and the worker —
  because it's one shared phase, not three.
- RAG-by-default for scaffolded agents: `mdk init`/`--llm` can emit `retrieval:
  auto_into: context` so a from-scratch RAG agent is grounded without the author
  hand-writing a "retrieve if empty" prompt block.
- Deterministic + cheaper than model-driven retrieval for the common single-shot
  case (no extra model turn; retrieval always happens), while still composing with
  the model-driven tool-loop path for multi-hop.
- Reuses existing seams (SkillBackend, retriever, budget) — small new surface,
  no boundary violations, no new YAML dialect.

**Negative / risks**
- A new `agent.yaml` field = a backward-compat surface (CLAUDE.md rule 5). Mitigated:
  fully opt-in, absent-block = unchanged behavior, additive schema, CHANGELOG + docs.
- A new execution phase in `core/executor.py` — the hottest, most-tested code. Risk
  of perturbing the non-RAG path; mitigated by gating the entire phase behind
  `retrieval.auto_into` presence and the D-matrix tests.
- Two grounding mechanisms now coexist (model-driven tool loop vs. declarative
  pre-retrieval). Mitigated by D3 (`when: if_empty` makes them compose, not fight)
  and docs that state when to use which.
- `query_from` default-resolution can be ambiguous for multi-field inputs; mitigated
  by requiring an explicit `query_from` when the primary text field isn't
  unambiguous, enforced at `mdk validate`.

**Test matrix (must all be covered by the impl task):**
no `retrieval:` block → behavior byte-for-byte unchanged (non-RAG regression guard);
`auto_into` + empty input field → retrieves + merges + grounded (MockProvider +
in-memory KB, no API keys); `auto_into` + explicitly-passed field + `when: if_empty`
→ retrieval skipped, explicit value used (eval-determinism guard); `when: always` →
re-retrieves; no retriever configured → no-op + notice, run succeeds; retrieval
error + `on_error: warn` → ungrounded + notice; `on_error: fail` → typed error;
load-time `mdk validate` rejects an `auto_into` whose field can't hold the chunk
shape and an unresolved `retrieval.skill`.

## Alternatives considered

- **(a) Fix the `hr_policy` template only (no engine change).** Mirror `rag_qa`: drop
  `context` from `required` and add a "Step 0 — retrieve if empty" prompt block, so
  the model self-retrieves via the existing tool loop. *Lowest cost, no ADR strictly
  needed, fixes the #120 symptom in both planes today.* **Recorded as the stopgap
  (D6), not the end-state:** it leaves grounding model-driven (non-deterministic,
  costs a turn) and forces every RAG template/`--llm` output to repeat the
  instruction — the systemic gap (3) stays open. Do this now if a fix is needed
  before the engine work lands; it is not mutually exclusive with this ADR.
- **(b) Status quo — model-driven tool loop only.** *Rejected as the end-state:* it's
  non-deterministic (the model can skip retrieval), repeats per template, and is the
  exact source of the #120 confusion ("why isn't it grounded?").
- **(c) Always-on / implicit retrieval whenever an agent has a `kb-vector-lookup`
  skill.** No new field; infer intent from the skill list. *Rejected:* implicit magic
  — an agent that wants the skill as a *model-callable tool* (multi-hop) would get an
  unwanted pre-retrieval; violates "opt-in, no surprises," and couples the skill's
  presence to an execution behavior (the ADR 002 conflation trap).
- **(d) A first-class `kind: KnowledgeBase` + built-in `retrieval` skill (ADR 002's
  other revisit path).** *Deferred, not rejected:* heavier (a new kind, loader,
  registry). This ADR's `retrieval.auto_into` directive layers cleanly on top of it
  later (its `skill:` would point at the KB-query skill). Start with the directive;
  promote to a `kind` only if multiple engagements need KBs as standalone objects.
- **(e) Do the merge in the CLI local path (`cli/run.py`) to "match" the runtime.**
  *Rejected:* it would duplicate retrieval logic into the control plane, recreating
  the very local-vs-runtime drift that caused #120, and violate cli ⊥ runtime. The
  whole point of D2 is to put it in the **shared** Executor.
