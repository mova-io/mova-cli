"""Heuristic prompt-injection detector.

Scans all string values in an input dict (recursively) for known injection
patterns using case-insensitive regex. No external dependencies — pure stdlib.

Patterns covered:
- ``ignore (previous|all|above) instructions?``
- ``forget (everything|all|what) (you|i) (said|told|know)``
- ``you are now (a|an|acting as)``
- ``pretend (you are|to be)``
- ``disregard (your|all|previous) (instructions?|rules?|constraints?)``
- ``from now on`` followed within 30 chars by a role-change indicator
- ``[SYSTEM]``, ``<system>``, ``### instruction`` at non-start positions
- ``jailbreak``, ``DAN mode``, ``developer mode``

Design choices:
- Regex scanning across all string fields (recursive dict + list walk)
- No external deps — keeps the executor's hot path free of heavy imports
- Returns the *first* match found (enough to make the decision); the caller
  can log ``matched_pattern`` + ``matched_value`` for the audit trail
- ``PromptInjectionDetector`` is a stateless class (no __init__ params) so
  callers can share a module-level singleton
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Detection patterns
# ---------------------------------------------------------------------------

# Each entry is (pattern_name, compiled_regex). We match against each string
# value in the input dict independently; a hit on any pattern is enough.
#
# All patterns are case-insensitive. The ``re.DOTALL`` flag isn't used here
# because we scan individual field values (not multi-line documents) — dots
# matching newlines would only matter for multi-line values, and the recursive
# walker already visits each string individually.

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "ignore_instructions",
        re.compile(
            r"ignore\s+(previous|all|above)\s+instructions?",
            re.IGNORECASE,
        ),
    ),
    (
        "forget_instructions",
        re.compile(
            r"forget\s+(everything|all|what)\s+(you|i)\s+(said|told|know)",
            re.IGNORECASE,
        ),
    ),
    (
        "you_are_now",
        re.compile(
            r"you\s+are\s+now\s+(a|an|acting\s+as)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "pretend_you_are",
        re.compile(
            r"pretend\s+(you\s+are|to\s+be)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "disregard_instructions",
        re.compile(
            r"disregard\s+(your|all|previous)\s+(instructions?|rules?|constraints?)",
            re.IGNORECASE,
        ),
    ),
    (
        "from_now_on",
        re.compile(
            # "from now on" within 30 chars of words that indicate a role/persona shift.
            # Using a lookahead so we capture the trigger phrase but flag the combination.
            r"from\s+now\s+on.{0,30}(act|behave|respond|be|role|persona|pretend|character)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "injected_system_block",
        re.compile(
            # [SYSTEM], <system>, or ### instruction NOT at the very start of the value.
            # At position 0 they might be legitimate system-prompt prefixes written
            # by the operator; anywhere else they're almost certainly injected.
            r"(?<!^)(\[SYSTEM\]|<system>|###\s*instruction)",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        "jailbreak_keywords",
        re.compile(
            r"\b(jailbreak|DAN\s+mode|developer\s+mode)\b",
            re.IGNORECASE,
        ),
    ),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DetectionResult:
    """Carries the pattern name and the field value that triggered it.

    ``matched_pattern`` identifies which of the eight injection families
    was hit — useful for audit logging and for writing targeted tests.

    ``matched_value`` is the raw string value that contained the match;
    callers may want to truncate it before storing.
    """

    matched_pattern: str
    matched_value: str


class PromptInjectionDetector:
    """Heuristic regex-based prompt-injection detector.

    Stateless — instantiate once (or reuse the module-level
    ``_DETECTOR`` singleton) and call :meth:`detect` for each run.

    Usage::

        detector = PromptInjectionDetector()
        result = detector.detect(input_dict)
        if result is not None:
            raise GuardrailViolationError(
                f"prompt injection detected: {result.matched_pattern}"
            )
    """

    def detect(self, input_dict: dict[str, Any]) -> DetectionResult | None:
        """Scan all string values in ``input_dict`` for injection patterns.

        Recurses into nested dicts and lists. Returns the first
        :class:`DetectionResult` found, or ``None`` if the input is clean.

        Recursion is breadth-first-ish (``_collect_strings`` is a
        generator that yields in insertion order), which means shallower
        fields are checked first — a good heuristic since injection is
        most likely in the first user-visible field.
        """
        for value in _collect_strings(input_dict):
            for pattern_name, pattern in _PATTERNS:
                if pattern.search(value):
                    return DetectionResult(
                        matched_pattern=pattern_name,
                        matched_value=value,
                    )
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _collect_strings(obj: Any) -> list[str]:
    """Recursively collect every string value from a JSON-compatible object.

    Handles ``dict`` (values only — keys are never user-controlled in
    our schema), ``list``, and scalar strings. Non-string scalars are
    ignored.
    """
    results: list[str] = []
    _walk(obj, results)
    return results


def _walk(obj: Any, out: list[str]) -> None:
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _walk(item, out)
    # int / float / bool / None — skip
