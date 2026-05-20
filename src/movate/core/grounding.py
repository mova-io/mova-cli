"""Runtime grounding enforcement for RAG agents.

Closes milestone M2 gap: "No runtime hallucination block".

After the executor gets a final output from the LLM, if the agent has
``grounding_enforcement: warn`` or ``strict`` set, this module's
:func:`check_grounding` function runs a structural grounding check:

1. **Grounded flag consistency** — if the output has a ``grounded``
   field and it's ``true``, there must be at least one citation.
   If ``grounded=false`` and citations are non-empty, that's internally
   inconsistent (claiming "not grounded" while pointing to sources).

2. **Citation index validity** — if the output has a ``citations``
   field (a list of 1-based integers referencing KB chunks), every
   index must be within the range returned by the KB lookup skill.
   Out-of-range indices indicate hallucinated source references.

3. **KB-skill presence check** — if the agent has at least one
   ``kb-vector-lookup`` skill call in the skill_calls log but the
   output's ``grounded`` field is ``false`` with a non-empty
   ``citations`` list (or vice versa), that's flagged as inconsistent.

Design choices
--------------
* **Structural only** — we do NOT re-embed or re-query the KB here.
  That would double the cost of every RAG run. The semantic check
  (did the answer hallucinate content not in the KB?) lives in the
  ``mdk eval-scorecard`` ``faithfulness`` + ``citation_accuracy``
  categories which use a judge LLM and are run off the hot path.
* **opt-in** — ``grounding_enforcement`` defaults to ``"off"`` so
  existing agents keep the v0.9 behavior.
* **warn vs strict** — ``warn`` is a tracer event + log line; the run
  still succeeds. ``strict`` raises ``GroundingViolationError`` which
  the executor catches and converts to ``safety_blocked``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from movate.core.failures import GroundingViolationError

log = logging.getLogger(__name__)

# Name of the KB lookup skill. We use this to count KB calls in the
# skill_calls list without importing the skill module.
_KB_LOOKUP_SKILL = "kb-vector-lookup"


@dataclass
class GroundingViolation:
    """One detected grounding violation."""

    code: str
    """Machine identifier for the violation type."""
    message: str
    """Human-readable description."""


@dataclass
class GroundingReport:
    """Result of a grounding check pass."""

    ok: bool
    violations: list[GroundingViolation] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_grounding(
    output: dict[str, Any],
    *,
    kb_call_count: int = 0,
    enforcement: str = "off",
) -> GroundingReport:
    """Run structural grounding checks on *output*.

    Parameters
    ----------
    output:
        The agent's final output dict (already schema-validated by the
        executor; this function only looks at ``grounded`` + ``citations``
        fields if present — other fields are ignored).
    kb_call_count:
        Number of successful ``kb-vector-lookup`` skill calls in this run.
        Used to detect "output claims KB-grounding but no KB was queried".
    enforcement:
        ``"off"`` → always returns a passing report (no-op).
        ``"warn"`` → runs checks, returns the report; caller logs and continues.
        ``"strict"`` → runs checks; if violations found, raises
        ``GroundingViolationError``.

    Returns
    -------
    GroundingReport
        ``ok=True`` when no violations found; ``ok=False`` + populated
        ``violations`` list otherwise.
    """
    if enforcement == "off":
        return GroundingReport(ok=True)

    violations = _run_checks(output, kb_call_count=kb_call_count)
    report = GroundingReport(ok=not violations, violations=violations)

    if violations and enforcement == "strict":
        msgs = "; ".join(v.message for v in violations)
        raise GroundingViolationError(f"grounding check failed: {msgs}")

    return report


# ---------------------------------------------------------------------------
# Internal check passes
# ---------------------------------------------------------------------------


def _run_checks(
    output: dict[str, Any],
    *,
    kb_call_count: int,
) -> list[GroundingViolation]:
    """Run all check passes; return a flat list of violations."""
    violations: list[GroundingViolation] = []

    grounded = output.get("grounded")  # bool | None
    citations = output.get("citations")  # list[int] | None

    # Check 1 — grounded=true but no citations.
    if grounded is True and not citations:
        violations.append(
            GroundingViolation(
                code="grounded_no_citations",
                message=(
                    "output declares grounded=true but citations list is empty — "
                    "either add citation indices or set grounded=false for ungrounded answers"
                ),
            )
        )

    # Check 2 — grounded=false but non-empty citations.
    if grounded is False and citations:
        violations.append(
            GroundingViolation(
                code="ungrounded_with_citations",
                message=(
                    f"output declares grounded=false but provides {len(citations)} citation(s) — "
                    "inconsistent: either set grounded=true or clear the citations list"
                ),
            )
        )

    # Check 3 — citation indices must be positive integers (1-based).
    if citations:
        bad = [c for c in citations if not isinstance(c, int) or c < 1]
        if bad:
            violations.append(
                GroundingViolation(
                    code="invalid_citation_indices",
                    message=(
                        f"citations contains non-positive or non-integer values: {bad[:5]} — "
                        "citation indices are 1-based integers referencing KB chunks returned "
                        "by the kb-vector-lookup skill"
                    ),
                )
            )

    # Check 4 — claims grounded but KB was never queried.
    if grounded is True and kb_call_count == 0:
        violations.append(
            GroundingViolation(
                code="grounded_without_kb_call",
                message=(
                    "output declares grounded=true but no kb-vector-lookup skill call was made — "
                    "the agent may be hallucinating KB-grounded answers without querying the KB. "
                    "Check that the kb-vector-lookup skill is wired in agent.yaml and invoked."
                ),
            )
        )

    return violations


# ---------------------------------------------------------------------------
# Convenience: count KB calls from a skill_calls list
# ---------------------------------------------------------------------------


def kb_call_count_from_records(
    skill_calls: list[Any],  # list[SkillCallRecord] — avoid circular import
) -> int:
    """Count successful ``kb-vector-lookup`` calls in *skill_calls*.

    Only counts calls without an ``error`` field (failed KB lookups
    shouldn't affect grounding validation).
    """
    return sum(
        1
        for sc in skill_calls
        if getattr(sc, "skill", None) == _KB_LOOKUP_SKILL
        and not getattr(sc, "error", None)
    )
