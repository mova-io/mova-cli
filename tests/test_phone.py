"""E.164 phone-number validation — normalize_e164 + validate_e164.

The validator is intentionally minimal (one regex, stdlib only) — we're
not parsing arbitrary user input, we're rejecting obvious typos at the
``--notify-sms`` boundary before they round-trip through the queue and
surface as noisy worker logs hours later.
"""

from __future__ import annotations

import pytest

from movate.core.phone import InvalidPhoneError, normalize_e164, validate_e164

# ---------------------------------------------------------------------------
# normalize_e164 — strip cosmetic separators
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+14155551234", "+14155551234"),  # already canonical
        ("+1 415 555 1234", "+14155551234"),  # spaces from "share contact"
        ("+1-415-555-1234", "+14155551234"),  # dashes
        ("+1 (415) 555-1234", "+14155551234"),  # parens + dashes + spaces (US format)
        ("+1.415.555.1234", "+14155551234"),  # dots (European format)
        ("+1\t415 555-1234", "+14155551234"),  # tabs
    ],
)
def test_normalize_strips_cosmetic_separators(raw: str, expected: str) -> None:
    assert normalize_e164(raw) == expected


@pytest.mark.unit
def test_normalize_preserves_plus_and_digits() -> None:
    """No regex magic — just strip separators. ``+`` and digits remain."""
    assert normalize_e164("+44 20 7946 0958") == "+442079460958"


@pytest.mark.unit
def test_normalize_does_not_translate_double_zero_prefix() -> None:
    """``00`` (international dial prefix in many countries) is NOT
    auto-translated to ``+``. The operator should type the canonical
    form; auto-translating would mask domestic-vs-international typos."""
    assert normalize_e164("0044 20 7946 0958") == "00442079460958"
    # And validate_e164 then rejects it for missing the leading +.
    with pytest.raises(InvalidPhoneError):
        validate_e164("00442079460958")


# ---------------------------------------------------------------------------
# validate_e164 — strict E.164 shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "valid",
    [
        "+14155551234",  # US, 11 digits
        "+442079460958",  # UK, 12 digits
        "+8612345678901",  # CN, 13 digits
        "+12",  # min spec: + then 2 digits
        "+999999999999999",  # max spec: + then 15 digits
    ],
)
def test_validate_accepts_valid_e164(valid: str) -> None:
    assert validate_e164(valid) == valid


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    [
        "14155551234",  # missing +
        "+",  # + alone
        "+0",  # only one digit and it's zero (leading zero forbidden)
        "+01234",  # leading zero after +
        "+1234567890123456",  # 16 digits — over spec max
        "+abc",  # not digits
        "+1-415-555-1234",  # cosmetic separators not stripped (caller's job)
        "",  # empty
    ],
)
def test_validate_rejects_invalid_e164(bad: str) -> None:
    with pytest.raises(InvalidPhoneError, match=r"E\.164"):
        validate_e164(bad)


@pytest.mark.unit
def test_validate_rejects_non_string() -> None:
    """Pydantic / Typer normally coerces, but defense-in-depth: non-str
    in raises a clear error rather than a regex AttributeError."""
    with pytest.raises(InvalidPhoneError, match="must be a string"):
        validate_e164(14155551234)  # type: ignore[arg-type]


@pytest.mark.unit
def test_validate_error_message_includes_example() -> None:
    """Operators making a typo should see a working example in the
    error message — saves a docs trip."""
    with pytest.raises(InvalidPhoneError, match="\\+14155551234"):
        validate_e164("bad")


# ---------------------------------------------------------------------------
# Combined pipeline — normalize → validate is the canonical caller pattern
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_normalize_then_validate_accepts_common_pasted_forms() -> None:
    """The pattern used by ``movate submit --notify-sms``."""
    for raw in ["+1 (415) 555-1234", "+1-415-555-1234", "+1.415.555.1234"]:
        normalized = normalize_e164(raw)
        assert validate_e164(normalized) == "+14155551234"
