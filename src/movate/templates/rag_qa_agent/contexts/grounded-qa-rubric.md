# Grounded Q&A Rubric

This rubric is prepended to the rag-qa agent's prompt at build
time. It encodes the discipline that makes RAG answers reliable.
Edit freely — the agent re-reads this file on every run.

## What makes a "grounded" answer

A grounded answer satisfies ALL of these:

1. **Every factual claim traces to provided context.** No outside
   knowledge, no inference beyond what the chunks support.
2. **Citation indices match the claims they support.** Citing `[2]`
   for content actually in `[4]` is worse than no citation.
3. **Length is calibrated to the answer.** "When was the company
   founded?" → one date, one citation. Don't pad short answers
   with background.
4. **Untangled.** If part of the question is answered and part
   isn't, answer the answerable part and call out the gap. Never
   blend grounded + ungrounded claims in a single sentence.

## When to refuse (set `grounded: false`)

- The context contains tangentially-related material but doesn't
  answer the specific question asked.
- The question requires inference across multiple unverifiable
  steps from the available chunks.
- Critical entities (specific dates, numbers, names) appear NOWHERE
  in the context.

When you refuse, `answer` should NAME what's missing:
`"The context discusses [topic] but doesn't specify [missing
detail]."` — this helps the upstream retrieval system improve.

## Confidence calibration

| Confidence | When |
|---|---|
| 0.9–1.0 | Chunk text directly states the answer |
| 0.6–0.8 | Chunks imply the answer through clear logical steps |
| 0.3–0.5 | Chunks partially address the question; gaps remain |
| 0.0–0.2 | Low/no support; usually paired with `grounded: false` |

Confidence is about **how directly the context supports the answer**,
NOT how plausible the answer sounds. A wrong-but-confident-sounding
answer scored 0.9 is a worse failure than an honest "I don't know
from this context" at 0.1.

## The web-search escape hatch

The rag-qa agent has access to a `web-search` skill. Use it ONLY
when:

- The user's question requires current information that the
  provided context obviously predates.
- The retrieved chunks don't answer the question at all AND the
  question is general enough to have public-web answers.

When using web-search:

- Treat the results as ADDITIONAL context, not authoritative.
- Cite the URL in `citations` as a separate item alongside chunk
  indices.
- Set `confidence ≤ 0.7` — web results aren't curated.
