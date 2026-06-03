"""Turn detection seam — decide a turn is over because the speaker is *done*,
not because silence elapsed (ADR 072).

ADR 072 proposes replacing the fixed ``endpointing_ms`` silence timer with a
*semantic* decision: a small detector reads the running transcript and says
"this utterance is complete." This module is the **seam** ADR 072 designs —
the :class:`TurnDetector` Protocol plus two reference implementations:

* :class:`NullTurnDetector` — never claims completeness; the fixed timer stays
  fully in charge (the default; zero behavior change).
* :class:`HeuristicTurnDetector` — a cheap, dependency-free reference: an
  utterance is "complete" when it ends on terminal punctuation, or it is long
  enough and does **not** trail off on a continuation word ("and", "the",
  "um", …) that signals the speaker is mid-thought.

**The trained-classifier implementation ADR 072 describes remains gated** on the
ADR's D6 evidence bar — this seam exists so that classifier (or LiveKit's turn
model, or a prosody model) can drop in later without touching the pipeline.

Integration (ADR 072 ↔ ADR 070): the detector is wired as the speculator's
*trigger* — a speculation fires the moment the detector calls the interim
complete, instead of waiting out the quiet-gap debounce. This is exactly the
composition ADR 072 calls out: better turn-detection *raises* speculation's
commit rate (a semantically-complete interim is far likelier to match the
final), so the two levers reinforce rather than compete. The fixed timer
remains the fallback when the detector is unsure.
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

# Words that, when they END an utterance, signal the speaker is still going —
# articles, conjunctions, prepositions, and disfluencies/fillers. A transcript
# trailing off on one of these is almost never a finished turn ("turn on the…",
# "I want to…", "um…"). Lowercased, punctuation-stripped comparison.
_CONTINUATION_WORDS = frozenset(
    {
        # articles / determiners
        "the", "a", "an", "this", "that", "these", "those", "my", "your", "our",
        # conjunctions
        "and", "or", "but", "so", "because", "if", "when", "while", "as",
        # prepositions
        "to", "of", "for", "with", "in", "on", "at", "from", "by", "about",
        # pronoun/aux lead-ins that rarely end a turn
        "i", "we", "is", "are", "was", "were", "will", "would", "can", "could",
        # fillers / disfluencies
        "um", "uh", "er", "hmm", "like", "well",
    }
)  # fmt: skip

_WORD_RE = re.compile(r"[a-z0-9']+")
_TERMINAL_PUNCT = (".", "?", "!")


@runtime_checkable
class TurnDetector(Protocol):
    """Decide whether a (possibly interim) transcript is a complete turn."""

    def is_complete(self, text: str) -> bool: ...


class NullTurnDetector:
    """Default detector: never claims completeness (defer to the silence timer)."""

    def is_complete(self, text: str) -> bool:
        return False


class HeuristicTurnDetector:
    """Cheap, dependency-free reference detector (ADR 072 — NOT the trained model).

    ``is_complete`` returns True when the utterance looks finished:

    * it ends on terminal punctuation (``.`` / ``?`` / ``!``) — STT smart-format
      adds these at a sentence end; OR
    * it has at least ``min_words`` words AND does not end on a continuation
      word (so "reset my password" is complete, but "reset my" / "reset my
      password and" is not).

    Blank or too-short text is never complete. This is intentionally conservative:
    a false "complete" would fire (and likely cancel) a speculation, so the bar
    favours precision over recall — the silence timer always backs it up.
    """

    def __init__(self, *, min_words: int = 3) -> None:
        self._min_words = max(1, min_words)

    def is_complete(self, text: str) -> bool:
        stripped = (text or "").strip()
        if not stripped:
            return False
        if stripped.endswith(_TERMINAL_PUNCT):
            return True
        words = _WORD_RE.findall(stripped.lower())
        if len(words) < self._min_words:
            return False
        return words[-1] not in _CONTINUATION_WORDS
