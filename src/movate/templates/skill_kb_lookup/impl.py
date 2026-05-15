"""Implementation for the __SKILL_NAME__ skill — knowledge-base lookup.

Naive keyword-scoring search over a small JSON corpus shipped with
the skill. The corpus is intentionally tiny (10 entries) — it's a
demo, not a production search index. Replace `corpus.json` with
your real KB, or rewrite `run()` to call a remote search service
(Elasticsearch, Algolia, pgvector, Azure AI Search, etc.) — the
input/output schema is stable, so the agent doesn't change.

Why naive keyword scoring rather than fancy embeddings:

* **Self-contained** — no extra deps (we already have stdlib).
* **Deterministic** — useful for evals; embeddings would add
  nondeterminism that fights `mdk eval`'s gating.
* **Debuggable** — a single-pass over a list of dicts. Easy to
  read, easy to fix when it's wrong.

The scoring rewards:
- Direct tag hits (highest weight)
- Title word overlap
- Symptom/resolution word overlap (lower weight)

Stopwords are filtered so a query like "the the the" doesn't match
everything. Category filter is applied BEFORE scoring.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from movate.core.skill_backend import SkillExecutionContext


# Path to the corpus JSON shipped alongside this impl. Skill scaffold
# preserves directory layout — corpus.json lives next to impl.py.
_CORPUS_PATH = Path(__file__).parent / "corpus.json"


# Words that contribute zero signal to KB matching. The list is short
# on purpose; aggressive stopword filtering would hide real query
# terms ("the system is down" → "system down" still has meaning).
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "i",
        "we",
        "my",
        "our",
        "you",
        "your",
        "it",
        "they",
        "to",
        "of",
        "in",
        "on",
        "for",
        "with",
        "at",
        "by",
        "from",
        "and",
        "or",
        "but",
        "as",
        "if",
        "this",
        "that",
        "these",
        "those",
        "how",
        "what",
        "why",
        "when",
        "where",
    }
)

_TOKEN_RE = re.compile(r"\b[a-z0-9][a-z0-9_-]*\b")

# Score weights for the three sources of evidence.
_W_TAG = 5
_W_TITLE = 3
_W_BODY = 1

# Defaults / caps for top_n.
_DEFAULT_TOP_N = 3
_MAX_TOP_N = 10


def _tokenize(text: str) -> set[str]:
    """Lowercase, split, drop stopwords. Returns set for fast membership tests."""
    return {tok for tok in _TOKEN_RE.findall(text.lower()) if tok not in _STOPWORDS}


def _score(entry: dict[str, Any], query_tokens: set[str]) -> int:
    """Score a single KB entry against the query token set.

    Higher weight on tag hits + title hits than on body word overlap;
    a query that EXACTLY matches a tag should always beat a query
    that happens to share filler words with a resolution paragraph.
    """
    if not query_tokens:
        return 0
    tag_hits = sum(1 for t in entry.get("tags", []) if t.lower() in query_tokens)
    title_hits = len(_tokenize(entry.get("title", "")) & query_tokens)
    body_hits = len(
        (_tokenize(entry.get("symptom", "")) | _tokenize(entry.get("resolution", "")))
        & query_tokens
    )
    return _W_TAG * tag_hits + _W_TITLE * title_hits + _W_BODY * body_hits


async def run(input: dict[str, Any], ctx: SkillExecutionContext) -> dict[str, Any]:
    """Search the local KB corpus; return ranked matches.

    Returns ``{"matches": [...], "corpus_size": N}``. Each match
    carries the full entry (id/category/title/symptom/resolution/tags)
    plus a ``score`` field. Empty matches list is valid (no entries
    cleared the relevance floor).

    ``category`` is a hard filter applied BEFORE scoring — useful
    when the agent already classified the ticket and wants to limit
    KB hits to the same bucket.
    """
    del ctx  # KB lookup is purely local; no budget / tracing concerns.
    query = input["query"]
    top_n = input.get("top_n") or _DEFAULT_TOP_N
    top_n = max(1, min(_MAX_TOP_N, int(top_n)))
    category_filter = input.get("category")

    try:
        corpus = json.loads(_CORPUS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        return {
            "matches": [],
            "corpus_size": 0,
            "warning": f"could not load corpus: {exc}",
        }

    # Apply category filter first.
    if category_filter:
        corpus = [e for e in corpus if e.get("category") == category_filter]

    query_tokens = _tokenize(query)
    scored = [(_score(e, query_tokens), e) for e in corpus]

    # Drop zero-scored entries (nothing matched) and sort by score desc.
    scored = [(s, e) for s, e in scored if s > 0]
    scored.sort(key=lambda pair: pair[0], reverse=True)

    matches = [{**entry, "score": score} for score, entry in scored[:top_n]]

    return {
        "matches": matches,
        "corpus_size": len(corpus),
    }
