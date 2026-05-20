"""Runtime grounding enforcement for RAG agents.

Closes milestone M2 gap: "No runtime hallucination block".

After the executor gets a final output from the LLM, if the agent has
``grounding_enforcement: warn`` or ``strict`` set, this module's
:func:`check_grounding` function runs a structural grounding check:

1. **Grounded flag consistency** — if the output has a ``grounded``
   field and it's ``true``, there must be at least one citation.
   If ``grounded=false`` and citations are non-empty, that's internally
   inconsistent (claiming "not grounded" while pointing to sources).

2. **Citation index validity (format)** — citation indices must be
   positive integers (1-based). Non-integer or non-positive values
   indicate a model formatting error.

3. **KB-skill presence check** — if ``grounded=true`` but no
   ``kb-vector-lookup`` skill call was made, the agent may be
   hallucinating KB-grounded answers without querying the KB.

4. **Citation index range (M4)** — when the total number of chunks
   returned by the KB skill is known, every citation index must be
   ≤ that total. Indices beyond the returned set are hallucinated
   source references — the model cited a chunk that was never
   retrieved.

5. **OCR-sourced citations (M2b)** — citations that map to chunks
   extracted via OCR (``KbChunk.ocr=True``) are flagged with a
   ``ocr_sourced_citations`` violation.  This violation is *always
   warn-only* regardless of the enforcement level — OCR quality issues
   are in the source material, not in the model's reasoning, so they
   never block a run.  Operators use the warning to identify KB docs
   that need better text extraction.

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
    warn_only: bool = False
    """When True this violation is informational only — it never raises
    ``GroundingViolationError`` in strict mode.  Used for M2b OCR-source
    citations where the quality issue is in the source material rather
    than the model's reasoning."""


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
    max_valid_citation_index: int = 0,
    ocr_cited_indices: list[int] | None = None,
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
    max_valid_citation_index:
        Total chunks returned by all successful KB calls (sum of
        ``chunks_found`` across calls).  When > 0, citation indices are
        validated to be ≤ this value.  Pass 0 to skip the range check
        (e.g. when the KB skill output format does not expose a count).
    ocr_cited_indices:
        1-based citation indices that map to OCR-extracted KB chunks.
        When provided, triggers the warn-only ``ocr_sourced_citations``
        check (Check 6).  Compute via
        :func:`ocr_cited_indices_from_records`.  Pass ``None`` or ``[]``
        to skip.
    enforcement:
        ``"off"`` → always returns a passing report (no-op).
        ``"warn"`` → runs checks, returns the report; caller logs and continues.
        ``"strict"`` → runs checks; if *blocking* violations found, raises
        ``GroundingViolationError``.  ``warn_only`` violations (e.g. OCR
        source citations) never block runs regardless of this setting.

    Returns
    -------
    GroundingReport
        ``ok=True`` when no violations found; ``ok=False`` + populated
        ``violations`` list otherwise.
    """
    if enforcement == "off":
        return GroundingReport(ok=True)

    violations = _run_checks(
        output,
        kb_call_count=kb_call_count,
        max_valid_citation_index=max_valid_citation_index,
        ocr_cited_indices=ocr_cited_indices or [],
    )
    report = GroundingReport(ok=not violations, violations=violations)

    if violations and enforcement == "strict":
        # Only blocking violations raise — warn_only violations (OCR source
        # quality issues) are informational and never abort a run.
        blocking = [v for v in violations if not v.warn_only]
        if blocking:
            msgs = "; ".join(v.message for v in blocking)
            raise GroundingViolationError(f"grounding check failed: {msgs}")

    return report


# ---------------------------------------------------------------------------
# Internal check passes
# ---------------------------------------------------------------------------


def _run_checks(
    output: dict[str, Any],
    *,
    kb_call_count: int,
    max_valid_citation_index: int = 0,
    ocr_cited_indices: list[int] | None = None,
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

    # Check 5 — citation indices must not exceed the number of chunks
    # actually returned by the KB skill (M4: range enforcement).
    # Only runs when max_valid_citation_index > 0 (i.e. the executor
    # harvested a chunk count from the skill call output).
    if citations and max_valid_citation_index > 0:
        # Only check well-formed indices (bad indices caught by Check 3).
        valid_ints = [c for c in citations if isinstance(c, int) and c >= 1]
        out_of_range = [c for c in valid_ints if c > max_valid_citation_index]
        if out_of_range:
            violations.append(
                GroundingViolation(
                    code="out_of_range_citation_indices",
                    message=(
                        f"citations {out_of_range[:5]} exceed the {max_valid_citation_index} "
                        f"chunk(s) returned by the kb-vector-lookup skill — "
                        "these indices reference chunks that were never retrieved; "
                        "the model may be hallucinating source references"
                    ),
                )
            )

    # Check 6 — OCR-sourced citations (M2b, warn-only).
    # When cited chunks were extracted via OCR, the text quality may be
    # lower than natively-extracted text. The model reasoning might still
    # be correct but the source is noisy. Flag as informational (warn_only)
    # so operators know which citations came from OCR without blocking runs.
    if citations and ocr_cited_indices:
        # Only report indices that are actually cited and OCR-sourced.
        cited_ocr = [c for c in ocr_cited_indices if c in set(citations)]
        if cited_ocr:
            violations.append(
                GroundingViolation(
                    code="ocr_sourced_citations",
                    message=(
                        f"citation(s) {cited_ocr[:5]} reference chunk(s) extracted via OCR — "
                        "OCR text may contain recognition errors; consider re-ingesting "
                        "those documents with a higher-quality text extraction method. "
                        "This violation is informational and does not block the run."
                    ),
                    warn_only=True,
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


def ocr_cited_indices_from_records(
    skill_calls: list[Any],  # list[SkillCallRecord] — avoid circular import
    citations: list[int] | None,
) -> list[int]:
    """Return citation indices (1-based) that map to OCR-extracted chunks.

    Iterates successful ``kb-vector-lookup`` skill calls in order, builds
    a cumulative chunk list from their ``output["chunks"]``, then checks
    which cited 1-based indices correspond to chunks where ``ocr=True``.

    Multi-hop support: when multiple KB calls were made the chunks are
    numbered sequentially across calls (call-1 chunks are 1..N1, call-2
    chunks are N1+1..N1+N2, ...) — matching the same assumption used by
    :func:`max_valid_citation_index_from_records`.

    Returns an empty list when *citations* is None or empty, or when no
    successful KB calls with chunk data exist.
    """
    if not citations:
        return []

    # Build ordered chunk list from all successful KB calls.
    all_chunks: list[dict[str, Any]] = []
    for sc in skill_calls:
        if getattr(sc, "skill", None) != _KB_LOOKUP_SKILL:
            continue
        if getattr(sc, "error", None):
            continue
        output = getattr(sc, "output", None) or {}
        chunks = output.get("chunks", [])
        if isinstance(chunks, list):
            all_chunks.extend(chunks)

    if not all_chunks:
        return []

    ocr_indices: list[int] = []
    for idx in citations:
        if not isinstance(idx, int) or idx < 1:
            continue
        chunk_pos = idx - 1  # convert 1-based citation to 0-based list position
        if chunk_pos < len(all_chunks):
            chunk = all_chunks[chunk_pos]
            if isinstance(chunk, dict) and chunk.get("ocr"):
                ocr_indices.append(idx)
    return ocr_indices


def max_valid_citation_index_from_records(
    skill_calls: list[Any],  # list[SkillCallRecord] — avoid circular import
) -> int:
    """Return total chunks returned across all successful KB calls.

    The ``kb-vector-lookup`` skill output contains a ``chunks_found``
    integer field.  Summing across all successful calls gives the upper
    bound on valid 1-based citation indices — any citation > this value
    references a chunk that was never retrieved.

    Returns 0 when no successful KB calls exist or when ``chunks_found``
    is absent from the skill output (skips the range check gracefully).
    """
    total = 0
    for sc in skill_calls:
        if getattr(sc, "skill", None) != _KB_LOOKUP_SKILL:
            continue
        if getattr(sc, "error", None):
            continue
        output = getattr(sc, "output", None) or {}
        found = output.get("chunks_found", 0)
        if isinstance(found, int):
            total += found
    return total
