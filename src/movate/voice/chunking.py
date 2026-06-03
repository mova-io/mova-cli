"""Sentence chunking for streaming TTS (the latency win).

The pipeline streams an agent's output tokens as they are produced. Feeding the
*whole* answer to TTS only after the agent finishes wastes the biggest latency
lever in a voice turn: we could be **synthesizing — and speaking — sentence one
while the agent is still generating sentence two**.

:class:`SentenceChunker` turns a stream of token deltas into a stream of
speakable chunks: it emits a chunk as soon as a sentence boundary is seen, and
:meth:`flush` returns whatever partial sentence remains at the end of the turn.
It is intentionally simple and punctuation-based (no NLP) — voice tolerates the
occasional over-split, and the first short chunk ("Sure!") going out
*immediately* is exactly the perceived-latency win we want.
"""

from __future__ import annotations

import re

# A complete chunk: the shortest prefix ending at sentence punctuation followed
# by whitespace (so we know the sentence really ended), or at one-or-more
# newlines (list items / paragraph breaks). DOTALL so newlines inside count.
_BOUNDARY = re.compile(r".*?(?:[.!?…]+[\"')\]]*\s+|\n+)", re.S)


class SentenceChunker:
    """Incremental token-stream → speakable-sentence-stream splitter."""

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, text: str) -> list[str]:
        """Add a token delta; return any newly-complete sentence chunks."""
        self._buf += text
        out: list[str] = []
        while True:
            match = _BOUNDARY.match(self._buf)
            if match is None:
                break
            chunk = match.group(0).strip()
            self._buf = self._buf[match.end() :]
            if chunk:
                out.append(chunk)
        return out

    def flush(self) -> str:
        """Return the trailing partial sentence (no terminator), and reset."""
        rest = self._buf.strip()
        self._buf = ""
        return rest
