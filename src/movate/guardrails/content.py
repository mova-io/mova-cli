"""Banned-terms content filter.

The simplest of the three guardrails: declare a list of terms (case-
insensitive substring OR ``re:<pattern>`` regex) that may not appear
in a text. If any do, the check fails.

Use cases:

* Leaked internal labels (``"INTERNAL ONLY"``, project codenames)
* Profanity (operator supplies their own list — we don't ship one
  to avoid the eternal "your list isn't comprehensive enough" debate)
* Competitor names (when policy bars discussing them)
* PII tokens that escaped the :mod:`movate.guardrails.pii` regex
  pattern (operator-specific catch-alls)

Same swap-in points as topic — content moderation APIs (Azure
Content Safety, OpenAI moderation) plug in behind
:class:`ContentVerdict`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ContentVerdict:
    """Outcome of one content check.

    ``status="pass"`` when no banned term appears; ``"violation"``
    when at least one does. ``matched_terms`` lists every term that
    fired (not just the first), so the operator-facing message can
    name them all rather than play whack-a-mole.
    """

    status: Literal["pass", "violation"]
    matched_terms: tuple[str, ...] = ()


def check(text: str, *, banned_terms: list[str] | None = None) -> ContentVerdict:
    """Return a :class:`ContentVerdict` for ``text`` against ``banned_terms``.

    Each term is either a case-insensitive substring or a regex
    pattern prefixed with ``re:``. Returns ``status="pass"`` when
    ``banned_terms`` is empty/None — permissive default.
    """
    terms = banned_terms or []
    if not terms:
        return ContentVerdict(status="pass")

    hits: list[str] = []
    for term in terms:
        if _matches(text, term):
            hits.append(term)
    if hits:
        return ContentVerdict(status="violation", matched_terms=tuple(hits))
    return ContentVerdict(status="pass")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _matches(haystack: str, term: str) -> bool:
    """Mirror of :func:`movate.guardrails.topic._matches` — substring
    (case-insensitive) by default; ``re:<pattern>`` for explicit regex.
    """
    if term.startswith("re:"):
        pattern = term[len("re:") :]
        return bool(re.search(pattern, haystack))
    return term.lower() in haystack.lower()
