"""Topic restriction — keyword/regex allow- or deny-list.

MVP scope: deterministic substring (case-insensitive) and regex
matching. Two forms an operator can use:

* **Allowlist** (``allowed_topics``) — the text must contain at least
  one term/pattern from the list. Captures "only talk about Sandisk"
  style restrictions: declare ``["sandisk", "storage", "data backup"]``
  and any input that mentions none of them is flagged.

* **Denylist** (``banned_topics``) — the text must contain none of the
  terms/patterns. Captures the inverse: declare
  ``["competitor X", "internal pricing"]`` and any input that mentions
  one is flagged.

Both lists may be used together; both must pass for the text to clear.

Future swap-ins behind the same :func:`check` contract:

* Semantic embedding match (catches paraphrases an exact match misses)
* LLM-as-judge classifier (highest fidelity, slowest)
* External moderation API (Azure Content Safety, etc.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class TopicVerdict:
    """Outcome of one topic check.

    ``status`` is ``"pass"`` when the text satisfies the constraints;
    ``"violation"`` when it doesn't. ``matched_terms`` lists which
    terms matched (allowlist hits) or violated (denylist hits) —
    useful for the operator-facing error message.

    Two-state (not three): warn vs block is a *policy* decision the
    caller makes after seeing the verdict, not a property of the
    check itself.
    """

    status: Literal["pass", "violation"]
    reason: str = ""
    matched_terms: tuple[str, ...] = ()


def check(
    text: str,
    *,
    allowed_topics: list[str] | None = None,
    banned_topics: list[str] | None = None,
) -> TopicVerdict:
    """Run topic constraints against ``text``.

    Args:
        text: The content to check (input prompt or output).
        allowed_topics: At least one of these terms/patterns must appear
            in ``text``. Empty/None means no allowlist constraint.
        banned_topics: None of these terms/patterns may appear in
            ``text``. Empty/None means no denylist constraint.

    Returns:
        :class:`TopicVerdict` — ``status="pass"`` if both constraints
        clear; ``"violation"`` if either fails.

    A term may be either:

    * A literal substring — matched case-insensitively. E.g.
      ``"sandisk"`` matches "SanDisk" or "sandisk products".
    * A regex pattern — when it starts with ``re:`` (e.g.
      ``"re:[Ss]an[Dd]isk\\s+\\w+"``). The ``re:`` prefix is stripped
      before compilation. Patterns let an operator write
      domain-specific rules (e.g. account numbers, product codes)
      without inventing a new config field.
    """
    haystack = text  # we lowercase per-term to keep the regex path case-sensitive

    # Denylist check first — banned content is the more dangerous
    # signal, so we surface that before scolding the user for being
    # off-topic.
    banned = banned_topics or []
    banned_hits = [term for term in banned if _matches(haystack, term)]
    if banned_hits:
        return TopicVerdict(
            status="violation",
            reason="contains banned topic(s)",
            matched_terms=tuple(banned_hits),
        )

    # Allowlist check — must hit at least one term.
    allowed = allowed_topics or []
    if allowed:
        allowed_hits = [term for term in allowed if _matches(haystack, term)]
        if not allowed_hits:
            return TopicVerdict(
                status="violation",
                reason="does not mention any allowed topic",
                matched_terms=(),
            )
        return TopicVerdict(
            status="pass",
            reason="matched allowed topic(s)",
            matched_terms=tuple(allowed_hits),
        )

    # No constraints declared = permissive pass.
    return TopicVerdict(status="pass", reason="no topic constraints")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _matches(haystack: str, term: str) -> bool:
    """True if ``term`` appears in ``haystack``.

    Two forms:

    * ``"re:<pattern>"`` — regex. Case-sensitive by default; the
      operator can opt into case-insensitivity via the standard
      ``(?i)`` inline flag (``"re:(?i)sandisk"``).
    * Anything else — case-insensitive substring.

    Invalid regex patterns raise ``re.error`` — let it propagate so
    a typo in the operator's config fails loud at validate time.
    """
    if term.startswith("re:"):
        pattern = term[len("re:") :]
        return bool(re.search(pattern, haystack))
    return term.lower() in haystack.lower()
