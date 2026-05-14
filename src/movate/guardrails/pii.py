"""Regex-based PII detection + redaction.

MVP scope: four common patterns that catch the bulk of "oops I pasted
a customer record into a prompt" leaks. Each pattern is a balance
between recall (catches enough variants) and precision (doesn't
false-positive on normal prose).

Swap-in points for the future:

* spaCy NER backend (catches name + address patterns regex can't)
* Microsoft Presidio (enterprise PII engine)
* Azure AI Content Safety (cloud-managed)

The :class:`PiiMatch` data class and :func:`scan` / :func:`redact`
functions are the stable contract — alternative engines plug into the
same shape so callers don't change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------
#
# Compiled at import time. Each entry is ``(pii_type, compiled_regex)``.
# Order matters only for which type wins on overlapping spans (first
# match in declaration order). Patterns are intentionally conservative
# — better to under-redact than to scribble on every number that looks
# like a phone but is actually a SKU.

PiiType = Literal["email", "phone", "ssn", "credit_card"]


# Email: RFC 5322 simplified — local part + @ + domain. Catches the
# 99% case. False-negatives: quoted-local-part forms (rare in practice).
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
)

# Phone: North American 10-digit with optional country code, allowing
# common separators (space, dash, dot, parens). Tightened to require
# at least one non-digit separator so we don't false-positive on
# 10-digit IDs.
_PHONE_RE = re.compile(
    r"(?:\+?1[\s.-]?)?"  # optional +1 or 1
    r"\(?\d{3}\)?[\s.-]"  # area code + at least one separator
    r"\d{3}[\s.-]\d{4}\b",  # exchange + line + at least one separator
)

# SSN: NNN-NN-NNNN, dash-separated. We do NOT match the
# space-separated or no-separator forms — too many false positives
# on test-data-looking digit strings.
_SSN_RE = re.compile(
    r"\b\d{3}-\d{2}-\d{4}\b",
)

# Credit card: 13-19 digit sequences with optional separators (space
# or dash). We don't Luhn-check at MVP — overkill for redaction and a
# Luhn-passing sequence in prose (e.g. an order number) would
# false-positive anyway. The 13-19 digit window covers Visa, MC,
# Amex, Discover, Diners, JCB.
_CC_RE = re.compile(
    r"\b(?:\d[ -]?){13,19}\b",
)

_PATTERNS: tuple[tuple[PiiType, re.Pattern[str]], ...] = (
    ("email", _EMAIL_RE),
    ("ssn", _SSN_RE),  # ssn before phone — both are digit-heavy
    ("phone", _PHONE_RE),
    ("credit_card", _CC_RE),
)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PiiMatch:
    """A single PII hit in some text.

    ``start`` / ``end`` are character offsets into the original
    input — callers can highlight or excise the span without
    re-scanning. ``text`` is the matched substring (useful for
    logging without re-extracting).
    """

    pii_type: PiiType
    start: int
    end: int
    text: str


# ---------------------------------------------------------------------------
# Scan + redact
# ---------------------------------------------------------------------------


def scan(text: str, *, types: list[PiiType] | None = None) -> list[PiiMatch]:
    """Return every PII match in ``text``, ordered by appearance.

    ``types`` filters which PII categories to look for; ``None``
    (default) checks all categories. Overlapping matches are deduped
    — for a given character span, the first category to claim it
    (by :data:`_PATTERNS` declaration order) wins. This means an SSN
    can't be re-flagged as a phone number, etc.
    """
    enabled = set(types) if types is not None else {t for t, _ in _PATTERNS}
    matches: list[PiiMatch] = []
    claimed: list[tuple[int, int]] = []  # (start, end) pairs already taken

    for pii_type, pattern in _PATTERNS:
        if pii_type not in enabled:
            continue
        for m in pattern.finditer(text):
            span = (m.start(), m.end())
            # Skip if any previously-claimed span overlaps this one.
            if any(_overlaps(span, c) for c in claimed):
                continue
            claimed.append(span)
            matches.append(
                PiiMatch(
                    pii_type=pii_type,
                    start=m.start(),
                    end=m.end(),
                    text=m.group(),
                )
            )

    # Sort by start offset for deterministic output regardless of the
    # order categories were declared in.
    matches.sort(key=lambda mm: mm.start)
    return matches


def redact(text: str, matches: list[PiiMatch]) -> str:
    """Return ``text`` with each match replaced by ``[REDACTED:type]``.

    Iterates in reverse so per-match index offsets stay stable as we
    splice. The marker form ``[REDACTED:email]`` is deliberately
    explicit so a downstream reader can still tell *what kind* of PII
    was scrubbed, even if not the exact value.
    """
    if not matches:
        return text
    out = text
    # Reverse so each splice doesn't invalidate later indices.
    for m in sorted(matches, key=lambda mm: mm.start, reverse=True):
        out = out[: m.start] + f"[REDACTED:{m.pii_type}]" + out[m.end :]
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """Inclusive-overlap check on two ``(start, end)`` spans."""
    return not (a[1] <= b[0] or b[1] <= a[0])
