# kb-vector-lookup skill

Semantic search over an agent's knowledge base. Ingest documents up
front; the agent calls this skill at run time to retrieve relevant
context for the user's question.

## Setup

```bash
# 1. Scaffold a project + agent
mdk init demo && cd demo
mdk add rag-qa

# 2. Ingest knowledge into the agent's KB
mkdir -p agents/rag-qa/kb
# ... drop .md / .txt files in agents/rag-qa/kb ...
mdk kb ingest rag-qa agents/rag-qa/kb

# 3. Verify retrieval BEFORE running the agent
mdk kb search rag-qa "what's the refund policy?"
# → top-5 chunks ranked by cosine similarity

# 4. Run the agent — it'll call this skill to fetch context itself
mdk run agents/rag-qa '{"question": "what's our refund policy?"}'
```

## How it works

* The skill takes `{question, k?}` and returns
  `{chunks: [{text, source, score}], chunks_found}`.
* Under the hood: embeds the question via OpenAI
  `text-embedding-3-small` and runs cosine similarity against the
  agent's pre-ingested chunks.
* The chunks were stored at ingest time with the same embedding
  model — storage layer rejects cross-model queries with a clear
  error if you change models without re-ingesting.

## Cost

~$0.00002 per query (OpenAI's `text-embedding-3-small` price for a
few-hundred-token question). Ingest cost is one-time + scales with
KB size.

## When to use this vs `kb-lookup`

* **`kb-lookup`** — static JSON corpus, keyword matching. Best for
  small known-shape KBs (FAQs, runbooks) where exact matches matter.
* **`kb-vector-lookup`** — embedded chunks, semantic similarity.
  Best for prose corpora where the user's question may not use the
  same words as the answer.

For RAG agents with substantial document KBs, this is the right
skill. For ticket-triage with a known set of canonical resolutions,
keyword `kb-lookup` is faster + deterministic.
