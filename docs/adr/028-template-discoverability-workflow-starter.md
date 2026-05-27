# ADR 028 — Template discoverability + a workflow starter

**Status:** Accepted
**Date:** 2026-05-27 (proposed); 2026-05-27 (approved)
**Deciders:** Engineering + Deva (Movate)
**Context window:** v1.x onboarding — the agent-template gallery is rich but
hard to discover, and there is no multi-step *workflow* starter.
**Builds on / related:** ADR 026 (`mdk init -t`), F2 (shape-aware `--llm`
scaffolding), ADR 017 (the workflow engine), `src/movate/templates/`.

## Context

`src/movate/templates/` already ships a broad gallery — ~16 agent shapes (faq,
rag_qa, classifier, extractor, summarizer, ticket_triager, sql_writer, lookup,
research, code_reviewer, compliance_checker, email_responder, lead_qualifier,
meeting_summarizer, resume_screener, calc, chatbot) plus skill templates and
init scaffolds. The gap is **not** "we need templates":

1. **Discoverability.** There's no way to *browse* them — you must already know
   the name to pass `-t <name>`. The shapes are invisible at the front door.
2. **No `workflow` starter.** Every template is single-agent or skill; nothing
   scaffolds a runnable multi-step `workflow.yaml`, so the orchestration engine
   (ADR 017) has no on-ramp from `mdk init`.

## Decision

### D1 — `mdk init` interactive picker + `mdk templates`
When `mdk init <name>` is run without `-t`/`--llm` on a TTY, offer an
**interactive picker** grouped by use-case (Q&A/RAG · extraction ·
classification · tool-use · multi-step), each entry showing a one-line "what
it's for." Add a non-interactive `mdk templates` (list, `--json`) for scripting.
`-t <name>` stays unchanged (the explicit escape hatch).

### D2 — A `workflow` starter template (`workflow_init`)
A runnable multi-step starter: a 2-node `workflow.yaml` (e.g. intent-router →
two agents, or agent → human-gate) with a `state_schema`, `entrypoint`, edges,
and a workflow-eval dataset stub (ADR 008). `mdk init -t workflow <name>` yields
a pipeline that `mdk` can run immediately — the on-ramp to orchestration and the
foundation ADR 029's workflow authoring builds on.

### D3 — Use-case metadata on every template
Tag each template with use-case + a one-line summary in a single source of truth
that BOTH the D1 picker and F2's `--llm` shape-matcher consume (so the matcher
and the human picker never drift).

## Consequences

**Positive**
- The gallery becomes discoverable; the blank-page problem shrinks.
- A first runnable workflow is one command away.
- The shape taxonomy is shared by the picker and the `--llm` matcher.

**Negative / risks**
- Low. Additive content + a picker behind an existing seam; no new architecture.
  (Borderline whether this needs an ADR at all — recorded for the taxonomy +
  the workflow-starter decision.)

## Scope / rollout
Additive: new `workflow_init` template, a picker in `init`, a `mdk templates`
command, and use-case metadata. One PR. Sequence after F1′/F4′ in the
init/scaffold lane (shared files). Feeds ADR 029 (the workflow starter is what
the workflow copilot edits).
