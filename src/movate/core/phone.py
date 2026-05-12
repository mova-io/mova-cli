"""E.164 phone-number validation.

Lightweight, stdlib-only. We deliberately do NOT pull in the
``phonenumbers`` library — its per-country rules are valuable for
arbitrary user input, but the ``--notify-sms`` flag is operator-typed
and a one-line regex catches the realistic mistakes (forgot the ``+``,
typed dashes, included parentheses) cheaply.

E.164 is "+ followed by 1-15 digits, leading digit non-zero". That's
the whole spec. We validate that exactly. We do NOT try to validate
country code legitimacy or area-code allocation — those drift, and a
deploy-time check is the wrong place to enforce them (the SMS provider
will reject the send if the number is unallocated).

The public surface is one function and one exception. Callers that
want to NORMALIZE input (strip spaces / dashes / parens) call
:func:`normalize_e164` first; :func:`validate_e164` enforces the
final shape.
"""

from __future__ import annotations

import re

__all__ = ["InvalidPhoneError", "normalize_e164", "validate_e164"]


class InvalidPhoneError(ValueError):
    """Raised by :func:`validate_e164` when the input doesn't match
    the E.164 shape. Inherits ``ValueError`` so existing Pydantic /
    Typer error machinery picks it up without bespoke handling."""


# E.164: literal "+", then 1-15 digits, first digit non-zero.
# Source: ITU-T E.164 (2010). Length is the hard upper bound — most
# real numbers are 10-13 digits; we accept anything in spec.
_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")


def normalize_e164(raw: str) -> str:
    """Strip common cosmetic separators and return the result.

    Does NOT validate the final shape — pair with :func:`validate_e164`
    if you want a guaranteed E.164 string.

    Strips: spaces, tabs, hyphens, parentheses, dots. Leaves leading
    ``+`` and digits untouched. Operators frequently paste numbers as
    ``+1 (415) 555-1234`` from their phone's contact card; we accept
    that and normalize. We do NOT translate ``00`` international-dial
    prefix to ``+`` — the operator should type the canonical form, and
    auto-translating ``00`` would mask a real typo (someone typing a
    domestic number with a country-specific prefix expecting it to
    magically become international).
    """
    return re.sub(r"[\s\-().]", "", raw)


def validate_e164(value: str) -> str:
    """Return ``value`` unchanged if it matches E.164; else raise.

    Does NOT normalize — callers that want normalization call
    :func:`normalize_e164` first, then pass the result here. Splitting
    them keeps the function pure (no surprise rewrites of caller input).
    """
    if not isinstance(value, str):
        raise InvalidPhoneError(f"phone number must be a string; got {type(value).__name__}")
    if not _E164_RE.match(value):
        raise InvalidPhoneError(
            f"phone number {value!r} is not E.164. Expected '+' followed by "
            f"1-15 digits (first digit non-zero). Example: '+14155551234'."
        )
    return value
