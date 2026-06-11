"""Deterministic PII redaction for the `redact-pii` skill — regex only, no LLM.

THE GOVERNANCE POINT OF THIS SKILL: redaction must be deterministic and
auditable. An LLM asked to "remove the PII" can miss one SSN in a thousand
runs and nobody can prove which run; three anchored regexes either match or
they don't, replay identically on Temporal, and are unit-tested character by
character (tests/test_b2_scenarios.py). stdlib ``re`` only — no new deps.

SELF-CONTAINED ON PURPOSE (ADR 097 D2): the deployed temporal-worker image
bakes ``src/`` + ``workflows/`` only, so this impl carries everything next to
its skill.yaml. Pure transform — no DB, no network, no clock — so there is no
``ctx.mock`` branch (mock runs execute it for real; the docs convention only
applies to externally-recording backends).

What is masked (and the deliberate limits, which the tests pin):

* emails  → ``[EMAIL]``  — ``local@domain.tld`` with a 2+ letter TLD.
* US SSNs → ``[SSN]``    — STRICTLY the hyphenated ``ddd-dd-dddd`` form,
  guarded against digit/hyphen neighbours so dates (``2026-06-10``), longer
  digit runs and order-ids never match. A bare 9-digit run is NOT masked —
  unseparated digits are indistinguishable from invoice numbers, and a false
  positive here silently corrupts clean documents (the worse failure mode).
* US phone numbers → ``[PHONE]`` — optional ``+1``, then ``(ddd)`` or ``ddd``
  with a ``-``/``.``/space separator, then ``ddd<sep>dddd``. Bare 10-digit
  runs and 7-digit local numbers are NOT masked, for the same
  no-false-positive reason.

Order matters and is fixed: emails first (an email's local part may contain
digit groups a later pattern could shred), then SSNs, then phones. Phones
cannot eat SSNs (their middle group is 3 digits, an SSN's is 2) but the fixed
order makes the output reproducible byte-for-byte regardless.

Contract: ``run(input_payload, ctx) -> dict`` — returns
``{redacted_text, pii_found, pii_count}``; the TOOL node raw-merges it into
workflow state (ADR 097 D1), so downstream nodes (the decision gate, the
notify agent) see ``redacted_text`` and NEVER need the original document.
"""

from __future__ import annotations

import re
from typing import Any

# local@domain.tld — word-bounded so punctuation around an address in prose
# does not leak into the match.
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# ddd-dd-dddd with digit/hyphen guards on both sides: rejects ISO dates
# (2026-06-10 — the year makes the leading group 4 digits), phone fragments,
# and anything embedded in a longer digit/hyphen run (1123-45-6789,
# 123-45-67890, SKU-style 123-45-6789-A).
_SSN = re.compile(r"(?<![0-9-])\d{3}-\d{2}-\d{4}(?![0-9-])")

# Optional +1, then (ddd) or ddd<sep>, then ddd<sep>dddd. The lookarounds
# reject matches inside longer digit/dot/hyphen runs (IPs, versions, ISO
# timestamps, account numbers).
_PHONE = re.compile(
    r"(?<![0-9.-])"
    r"(?:\+1[ .-]?)?"
    r"(?:\(\d{3}\)[ .-]?|\d{3}[ .-])"
    r"\d{3}[ .-]\d{4}"
    r"(?![0-9-])"
)

# (token, pattern) in masking order — emails BEFORE the digit patterns.
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("[EMAIL]", _EMAIL),
    ("[SSN]", _SSN),
    ("[PHONE]", _PHONE),
)


def redact(text: str) -> tuple[str, int]:
    """Mask every email/SSN/phone in ``text``; return (redacted, count)."""
    total = 0
    for token, pattern in _RULES:
        text, n = pattern.subn(token, text)
        total += n
    return text, total


def run(input_payload: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
    """Redact the document; return the masked text + detection stats."""
    document = str(input_payload.get("document", ""))
    redacted_text, pii_count = redact(document)
    return {
        "redacted_text": redacted_text,
        "pii_found": pii_count > 0,
        "pii_count": pii_count,
    }
