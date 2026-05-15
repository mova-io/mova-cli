# `__SKILL_NAME__` — knowledge-base lookup

Searches an internal knowledge base of past tickets and resolutions,
returning the top matches for a query. Used by the `ticket-triager`
demo agent to look up "have we seen this issue before?" before
drafting a reply.

## Corpus resolution — two tiers

The skill resolves its corpus file at CALL TIME in this order:

1. **`<project>/kb/kb-lookup-corpus.json`** — the project's KB
   folder (post-MVP convention). Drop your real tickets export
   here (same JSON shape as the bundled file) and the skill picks
   it up on next run with ZERO code edits.
2. **`<skill_dir>/corpus.json`** — bundled demo corpus that ships
   with the skill template. Used as a fallback when the project
   doesn't carry an override.

Resolution happens on every call (not module-load) so operators
can edit the project KB and see results without restarting
`mdk serve`. The lookup is cheap — one stat + one `is_file()`.

## Ships with mock data

The bundled `corpus.json` carries 10 fabricated support tickets
covering billing / account / bug / how-to / feature-request
categories. The data is deliberately fake — the point is to show
the wiring + scoring, not to seed your production KB.

**To use your real KB**, two paths:

**A. Drop a file at `<project>/kb/kb-lookup-corpus.json`** in the
same shape:
```json
[
  {"id": "KB-001", "category": "billing", "title": "...",
   "symptom": "...", "resolution": "...", "tags": ["..."]},
  ...
]
```
No code edits, no skill scaffold changes — the skill resolves the
project file automatically on next run.

**B. Rewrite `impl.py:run`** to call your real search service
(Elasticsearch, Algolia, pgvector, Azure AI Search). The input
and output schemas stay identical so the agent doesn't change.

## Schema

**Input:**
```yaml
query: string         # natural-language query
top_n?: integer       # results to return (1–10, default 3)
category?: string     # hard filter (billing/bug/etc.) applied BEFORE scoring
```

**Output:**
```yaml
matches: array        # ranked hits; each includes `score`
corpus_size: integer  # how many entries were searched after filtering
warning?: string      # set on failure
```

## Scoring

Naive keyword scoring (deterministic, no embeddings):

| Match source | Weight |
|---|---|
| Tag exact-hit | 5 |
| Title token overlap | 3 |
| Symptom / resolution token overlap | 1 |

Stopwords ("the", "is", "i", etc.) are filtered. The query "system
is down" tokenizes to `{system, down}` so noise words don't dominate.

## Why no embeddings?

- **Self-contained** — no extra deps.
- **Deterministic** — embeddings introduce nondeterminism that
  fights `mdk eval`'s gating.
- **Debuggable** — single-pass scoring; failures are obvious.

Real production KB? Swap `impl.py` for a real backend. Keep the
schema. The agent doesn't need to know which world it's in.

## Cost + side effects

- `per_call_usd: 0.0` — local file read.
- `side_effects: read-only` — opens the corpus, doesn't write
  anywhere.

## Testing

```bash
mdk skills run __SKILL_NAME__ --input '{"query": "duplicate stripe charge"}'
```
