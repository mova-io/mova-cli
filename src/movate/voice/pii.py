"""PII redaction for transcripts (enterprise / CX compliance).

Caller transcripts routinely contain card numbers, SSNs, emails, phone numbers —
data you must not leak into logs, captions, or your observability backend
(PCI/HIPAA/GDPR). :func:`redact_pii` masks the common patterns.

Important design point: redaction is for the **observability surface**, not the
agent's input. The pipeline applies it to the ``transcript.*`` events it emits
(captions/logs) while the agent stage still runs on the *raw* transcript — an
agent that needs the real card number to act on it still gets it; your logs
don't. (Wire it via ``run_voice_pipeline(pii_redactor=redact_pii)``.)

Conservative + dependency-free regexes — they aim to avoid false positives on
ordinary speech. For stricter guarantees, plug a dedicated PII/NER service in
behind the same ``Callable[[str], str]`` shape.
"""

from __future__ import annotations

import re

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# Phone: + and 7+ digits with optional separators.
_PHONE = re.compile(r"\b\+?\d[\d().\- ]{7,}\d\b")
# Long digit run inside ONE word (no separators) — account/reference numbers.
_LONG_DIGITS = re.compile(r"\b\d{6,}\b")
# Multi-group digit "thing" with arbitrary internal separators (- / . space) —
# 11+ total digits. Catches card numbers in any grouping the model produces
# ("4111 1111 1111 1111", "4111-222-2333-334-444", "4111.1111.1111.1111").
# Live finding: Whisper invented uneven groupings my old 4-4-4-4 regex missed.
_DIGITS_WITH_SEPS = re.compile(r"\d[\d \-./]{9,}\d")

MASK = "[redacted]"


def _has_min_digits(span: str, n: int) -> bool:
    return sum(c.isdigit() for c in span) >= n


def redact_pii(text: str, *, mask: str = MASK) -> str:
    """Mask emails, SSNs, card/phone/long-digit sequences in ``text``.

    Order matters: the most specific patterns run first so a card number isn't
    half-matched by the generic digit rule. Returns ``text`` unchanged when it
    contains nothing that looks like PII.
    """
    if not text:
        return text
    out = _EMAIL.sub(mask, text)
    out = _SSN.sub(mask, out)
    # Multi-group digit sequences (11+ digits across separators): the real-world
    # card-number guard. Filter so a date like "1 2 3 4 5 6 7 8 9 10 11" doesn't
    # match — require ≥11 actual digits in the span.
    out = _DIGITS_WITH_SEPS.sub(
        lambda m: mask if _has_min_digits(m.group(0), 11) else m.group(0), out
    )
    out = _PHONE.sub(mask, out)
    out = _LONG_DIGITS.sub(mask, out)
    return out
