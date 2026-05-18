"""In-memory retrievers for the v0.7 RAG surface.

Two backends, both pure-Python, no external deps:

* :class:`BM25Retriever` — Robertson/Sparck-Jones BM25 with default
  ``k1=1.5``, ``b=0.75``. Indexes one or more configured body fields;
  optional tag-field matches act as a high-weight bonus. The right
  default for any corpus over ~50 entries.
* :class:`SubstringRetriever` — token-overlap scorer. Cheaper than
  BM25 (no IDF, no length normalization) and useful when the corpus
  is small and IDF noise dominates.

Both implement the same :class:`Retriever` protocol so agent code +
workflow nodes don't have to branch on retriever kind. v0.8 will add
embedding-backed retrievers (pgvector, Azure AI Search) implementing
the same protocol — agent code won't change.

Scope intentionally limited per BACKLOG #127:
* No embeddings, no reranking, no chunking.
* No streaming — corpus fits in memory.
* No persistence — index rebuilt per process; that's fine since
  in-memory corpora are tiny.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Protocol

from movate.knowledge.store import InMemoryCorpus

# Tokenizer is shared across retrievers — must produce identical
# tokens at index time and query time. Word characters only, ≥2 chars
# (drops 1-char noise like "a"), lowercased. Matches the heuristic in
# the existing knowledge_cmd.py tokenizer so search results between
# `knowledge validate` and `knowledge query` stay consistent.
_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")


def _tokenize(text: str) -> list[str]:
    """Lowercase + split on non-word boundaries + drop 1-char tokens.

    Returns a list (not a set) so BM25's term-frequency calculation
    sees repeated tokens correctly; callers wanting set semantics can
    wrap the result.
    """
    return _TOKEN_RE.findall(text.lower())


@dataclass(frozen=True)
class RetrievalHit:
    """One result from a retriever query.

    ``score`` is retriever-specific (BM25 raw score, substring overlap
    count). Higher = better in both cases. ``entry`` is the original
    corpus dict, untouched — caller passes it to a renderer or skill.
    ``doc_id`` is the value of the corpus's ``id_field`` (or the
    index-as-string fallback when the entry has no id).
    """

    doc_id: str
    score: float
    entry: dict[str, Any]


class Retriever(Protocol):
    """Uniform interface that BM25 + substring + future embedding
    retrievers all implement. Agent code only ever touches this."""

    def query(self, query: str, top_k: int) -> list[RetrievalHit]:
        """Return at most ``top_k`` hits, descending by score. An
        empty query OR zero matches returns an empty list — callers
        decide whether that's an error."""
        ...


# ---------------------------------------------------------------------------
# BM25
# ---------------------------------------------------------------------------


# BM25 parameters. Defaults match the literature and rank-bm25's
# defaults — fine-tuning happens per-corpus in v0.8 once we have
# real recall data.
_BM25_K1 = 1.5
_BM25_B = 0.75
# Tag-match bonus: when a query token equals an entry tag, this many
# BM25-score-equivalent points are added. Heuristic — tags carry
# strong signal in the kb-lookup corpora we've seen, so a tag hit
# usually wants to lift a hit above body-only matches.
_TAG_BONUS = 2.0


class BM25Retriever:
    """Robertson/Sparck-Jones BM25 over a concatenated body of one or
    more configured fields. Tag-field hits get a flat bonus.

    Build-once, query-many: the inverted index + document lengths are
    computed at construction. Queries are tokenized + scored against
    that pre-computed state.

    Args:
        corpus: in-memory entries (typically loaded via
            :meth:`InMemoryCorpus.from_path`).
        body_fields: corpus entry fields concatenated for the BM25
            body. Common shapes:
            * canonical: ``["title", "body"]``
            * legacy KB-lookup: ``["title", "symptom", "resolution"]``
        tag_field: optional field whose value is a list[str]; query
            tokens matching any tag get a flat bonus (no IDF). Pass
            ``None`` to disable tag boosting.
        id_field: corpus entry field carrying the stable doc id.
    """

    def __init__(
        self,
        corpus: InMemoryCorpus,
        *,
        body_fields: list[str],
        tag_field: str | None = "tags",
        id_field: str = "id",
    ) -> None:
        self._corpus = corpus
        self._body_fields = body_fields
        self._tag_field = tag_field
        self._id_field = id_field
        self._tokenized: list[list[str]] = []
        self._doc_freq: Counter[str] = Counter()
        self._doc_len: list[int] = []
        self._avg_doc_len: float = 0.0
        self._build_index()

    def _entry_body(self, entry: dict[str, Any]) -> str:
        """Concatenate the configured body fields, joining with a space
        so token boundaries are preserved across field boundaries."""
        parts: list[str] = []
        for fname in self._body_fields:
            val = entry.get(fname, "")
            if isinstance(val, str):
                parts.append(val)
            elif isinstance(val, list):
                parts.append(" ".join(str(x) for x in val))
            else:
                parts.append(str(val))
        return " ".join(parts)

    def _entry_tags(self, entry: dict[str, Any]) -> set[str]:
        """Lowercase set of tag values for the tag-bonus calculation.
        Returns empty set when ``tag_field`` is None or absent."""
        if self._tag_field is None:
            return set()
        raw = entry.get(self._tag_field, [])
        if not isinstance(raw, list):
            return set()
        return {str(t).lower().strip() for t in raw if str(t).strip()}

    def _build_index(self) -> None:
        for entry in self._corpus.entries:
            tokens = _tokenize(self._entry_body(entry))
            self._tokenized.append(tokens)
            self._doc_len.append(len(tokens))
            # IDF needs document frequency (how many docs contain each
            # term at least once), not term frequency. Use the set.
            for term in set(tokens):
                self._doc_freq[term] += 1
        n = len(self._doc_len)
        self._avg_doc_len = (sum(self._doc_len) / n) if n > 0 else 0.0

    def _idf(self, term: str) -> float:
        """Inverse document frequency with the +1 smoothing variant
        used by Lucene + rank-bm25. Always non-negative for terms
        that appear in any doc; zero when the term is absent."""
        n = len(self._doc_len)
        df = self._doc_freq.get(term, 0)
        if df == 0:
            return 0.0
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def _doc_id(self, idx: int) -> str:
        entry = self._corpus.entries[idx]
        raw = entry.get(self._id_field)
        return str(raw) if raw is not None else str(idx)

    def query(self, query: str, top_k: int) -> list[RetrievalHit]:
        """Score every doc against ``query``; return the top ``top_k``.

        Empty query → empty result; no exception so callers don't have
        to special-case it. Ties broken by document order (stable
        sort) so results are deterministic across runs.
        """
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []
        q_token_set = set(q_tokens)

        scored: list[tuple[float, int]] = []
        for i, doc_tokens in enumerate(self._tokenized):
            if not doc_tokens:
                continue
            tf = Counter(doc_tokens)
            score = 0.0
            for term in q_token_set:
                if term not in tf:
                    continue
                idf = self._idf(term)
                if idf == 0.0:
                    continue
                f = tf[term]
                dl = self._doc_len[i]
                # BM25 numerator + denominator. b=0.75 length-normalizes;
                # b=0 would skip length normalization entirely.
                norm = (
                    1.0 - _BM25_B + _BM25_B * (dl / self._avg_doc_len if self._avg_doc_len else 0)
                )
                score += idf * (f * (_BM25_K1 + 1)) / (f + _BM25_K1 * norm)
            # Tag bonus — flat per matching tag; not affected by IDF
            # since tags are categorical, not natural-language.
            entry_tags = self._entry_tags(self._corpus.entries[i])
            tag_hits = q_token_set & entry_tags
            if tag_hits:
                score += _TAG_BONUS * len(tag_hits)
            if score > 0:
                scored.append((score, i))

        scored.sort(key=lambda x: (-x[0], x[1]))
        return [
            RetrievalHit(
                doc_id=self._doc_id(idx),
                score=s,
                entry=self._corpus.entries[idx],
            )
            for s, idx in scored[:top_k]
        ]


# ---------------------------------------------------------------------------
# Substring (token-overlap) retriever
# ---------------------------------------------------------------------------


class SubstringRetriever:
    """Cheap baseline: count query tokens that appear in each doc's
    body. No IDF, no length normalization.

    Use when:
    * Corpus is tiny (< 50 entries) — BM25's IDF behaves erratically.
    * Operators want deterministic, easily-explained ranking.

    Same interface as :class:`BM25Retriever` so agent code doesn't
    care which is configured.
    """

    def __init__(
        self,
        corpus: InMemoryCorpus,
        *,
        body_fields: list[str],
        tag_field: str | None = "tags",
        id_field: str = "id",
    ) -> None:
        self._corpus = corpus
        self._body_fields = body_fields
        self._tag_field = tag_field
        self._id_field = id_field
        self._doc_token_sets: list[set[str]] = []
        for entry in corpus.entries:
            parts: list[str] = []
            for fname in body_fields:
                val = entry.get(fname, "")
                if isinstance(val, list):
                    parts.append(" ".join(str(x) for x in val))
                else:
                    parts.append(str(val))
            self._doc_token_sets.append(set(_tokenize(" ".join(parts))))

    def query(self, query: str, top_k: int) -> list[RetrievalHit]:
        q_tokens = set(_tokenize(query))
        if not q_tokens:
            return []
        scored: list[tuple[int, int]] = []
        for i, token_set in enumerate(self._doc_token_sets):
            overlap = len(q_tokens & token_set)
            # Tag bonus mirrors BM25's behavior so the two retrievers
            # rank the same toy corpus comparably in tests.
            if self._tag_field is not None:
                raw_tags = self._corpus.entries[i].get(self._tag_field, [])
                if isinstance(raw_tags, list):
                    entry_tags = {str(t).lower().strip() for t in raw_tags if str(t).strip()}
                    overlap += int(_TAG_BONUS) * len(q_tokens & entry_tags)
            if overlap > 0:
                scored.append((overlap, i))
        scored.sort(key=lambda x: (-x[0], x[1]))

        out: list[RetrievalHit] = []
        for s, idx in scored[:top_k]:
            entry = self._corpus.entries[idx]
            raw_id = entry.get(self._id_field)
            out.append(
                RetrievalHit(
                    doc_id=str(raw_id) if raw_id is not None else str(idx),
                    score=float(s),
                    entry=entry,
                )
            )
        return out
