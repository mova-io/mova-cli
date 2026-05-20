"""Unit tests for movate.core.grounding.

Covers:
- check_grounding() with enforcement="off", "warn", and "strict"
- All four violation codes individually
- Multiple simultaneous violations
- Clean (passing) output
- GroundingReport.__bool__
- kb_call_count_from_records() helper
"""

from __future__ import annotations

import pytest

from movate.core.failures import GroundingViolationError
from movate.core.grounding import (
    GroundingReport,
    GroundingViolation,
    check_grounding,
    kb_call_count_from_records,
)
from movate.core.models import SkillCallRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _kb_record(*, step: int = 1, error: str | None = None) -> SkillCallRecord:
    """Return a SkillCallRecord for the kb-vector-lookup skill."""
    return SkillCallRecord(
        step=step,
        skill="kb-vector-lookup",
        input={"query": "what is X?"},
        output=None if error else {"chunks": []},
        error=error,
    )


def _other_record(*, step: int = 1) -> SkillCallRecord:
    """Return a SkillCallRecord for a non-KB skill."""
    return SkillCallRecord(
        step=step,
        skill="calculator",
        input={"expr": "1+1"},
        output={"result": 2},
    )


# ---------------------------------------------------------------------------
# enforcement="off"
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEnforcementOff:
    """When enforcement is "off", check_grounding is a no-op."""

    def test_off_returns_ok_true(self) -> None:
        report = check_grounding({}, enforcement="off")
        assert report.ok is True

    def test_off_no_violations(self) -> None:
        report = check_grounding({}, enforcement="off")
        assert report.violations == []

    def test_off_ignores_grounded_no_citations(self) -> None:
        output = {"grounded": True, "citations": []}
        report = check_grounding(output, kb_call_count=0, enforcement="off")
        assert report.ok is True
        assert report.violations == []

    def test_off_ignores_ungrounded_with_citations(self) -> None:
        output = {"grounded": False, "citations": [1, 2]}
        report = check_grounding(output, enforcement="off")
        assert report.ok is True

    def test_off_ignores_invalid_citation_indices(self) -> None:
        output = {"grounded": True, "citations": [0, -1, "bad"]}
        report = check_grounding(output, enforcement="off")
        assert report.ok is True

    def test_off_ignores_grounded_without_kb_call(self) -> None:
        output = {"grounded": True, "citations": [1]}
        report = check_grounding(output, kb_call_count=0, enforcement="off")
        assert report.ok is True


# ---------------------------------------------------------------------------
# Individual violation codes — warn mode
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestViolationGroundedNoCitations:
    """grounded_no_citations: grounded=True but citations missing or empty."""

    def test_grounded_true_empty_citations(self) -> None:
        output = {"grounded": True, "citations": []}
        report = check_grounding(output, kb_call_count=1, enforcement="warn")
        codes = [v.code for v in report.violations]
        assert "grounded_no_citations" in codes

    def test_grounded_true_missing_citations_key(self) -> None:
        output = {"grounded": True}
        report = check_grounding(output, kb_call_count=1, enforcement="warn")
        codes = [v.code for v in report.violations]
        assert "grounded_no_citations" in codes

    def test_grounded_true_none_citations(self) -> None:
        output = {"grounded": True, "citations": None}
        report = check_grounding(output, kb_call_count=1, enforcement="warn")
        codes = [v.code for v in report.violations]
        assert "grounded_no_citations" in codes

    def test_grounded_no_citations_ok_is_false(self) -> None:
        output = {"grounded": True, "citations": []}
        report = check_grounding(output, kb_call_count=1, enforcement="warn")
        assert report.ok is False

    def test_grounded_no_citations_violation_message(self) -> None:
        output = {"grounded": True, "citations": []}
        report = check_grounding(output, kb_call_count=1, enforcement="warn")
        violation = next(v for v in report.violations if v.code == "grounded_no_citations")
        assert "grounded=true" in violation.message
        assert "citations" in violation.message

    def test_grounded_false_empty_citations_no_violation(self) -> None:
        output = {"grounded": False, "citations": []}
        report = check_grounding(output, kb_call_count=0, enforcement="warn")
        codes = [v.code for v in report.violations]
        assert "grounded_no_citations" not in codes


@pytest.mark.unit
class TestViolationUngroundedWithCitations:
    """ungrounded_with_citations: grounded=False but citations non-empty."""

    def test_ungrounded_with_citations(self) -> None:
        output = {"grounded": False, "citations": [1, 2, 3]}
        report = check_grounding(output, enforcement="warn")
        codes = [v.code for v in report.violations]
        assert "ungrounded_with_citations" in codes

    def test_ungrounded_with_citations_ok_is_false(self) -> None:
        output = {"grounded": False, "citations": [1]}
        report = check_grounding(output, enforcement="warn")
        assert report.ok is False

    def test_ungrounded_with_citations_message_has_count(self) -> None:
        output = {"grounded": False, "citations": [1, 2]}
        report = check_grounding(output, enforcement="warn")
        violation = next(v for v in report.violations if v.code == "ungrounded_with_citations")
        assert "2" in violation.message

    def test_ungrounded_empty_citations_no_violation(self) -> None:
        output = {"grounded": False, "citations": []}
        report = check_grounding(output, enforcement="warn")
        codes = [v.code for v in report.violations]
        assert "ungrounded_with_citations" not in codes

    def test_grounded_true_with_citations_no_ungrounded_violation(self) -> None:
        output = {"grounded": True, "citations": [1]}
        report = check_grounding(output, kb_call_count=1, enforcement="warn")
        codes = [v.code for v in report.violations]
        assert "ungrounded_with_citations" not in codes


@pytest.mark.unit
class TestViolationInvalidCitationIndices:
    """invalid_citation_indices: non-positive or non-integer values in citations."""

    def test_zero_index_is_invalid(self) -> None:
        output = {"grounded": True, "citations": [0]}
        report = check_grounding(output, kb_call_count=1, enforcement="warn")
        codes = [v.code for v in report.violations]
        assert "invalid_citation_indices" in codes

    def test_negative_index_is_invalid(self) -> None:
        output = {"grounded": True, "citations": [-5]}
        report = check_grounding(output, kb_call_count=1, enforcement="warn")
        codes = [v.code for v in report.violations]
        assert "invalid_citation_indices" in codes

    def test_string_index_is_invalid(self) -> None:
        output = {"grounded": True, "citations": ["chunk-1"]}
        report = check_grounding(output, kb_call_count=1, enforcement="warn")
        codes = [v.code for v in report.violations]
        assert "invalid_citation_indices" in codes

    def test_float_index_is_invalid(self) -> None:
        # Floats are not ints, even if their value is whole
        output = {"grounded": True, "citations": [1.0]}
        report = check_grounding(output, kb_call_count=1, enforcement="warn")
        codes = [v.code for v in report.violations]
        assert "invalid_citation_indices" in codes

    def test_mixed_valid_invalid_triggers_violation(self) -> None:
        output = {"grounded": True, "citations": [1, 2, 0, "bad"]}
        report = check_grounding(output, kb_call_count=1, enforcement="warn")
        codes = [v.code for v in report.violations]
        assert "invalid_citation_indices" in codes

    def test_all_valid_positive_ints_no_violation(self) -> None:
        output = {"grounded": True, "citations": [1, 2, 3]}
        report = check_grounding(output, kb_call_count=1, enforcement="warn")
        codes = [v.code for v in report.violations]
        assert "invalid_citation_indices" not in codes

    def test_invalid_indices_violation_message_contains_bad_values(self) -> None:
        output = {"grounded": True, "citations": [1, 0]}
        report = check_grounding(output, kb_call_count=1, enforcement="warn")
        violation = next(v for v in report.violations if v.code == "invalid_citation_indices")
        assert "0" in violation.message


@pytest.mark.unit
class TestViolationGroundedWithoutKbCall:
    """grounded_without_kb_call: grounded=True but kb_call_count == 0."""

    def test_grounded_true_no_kb_call(self) -> None:
        output = {"grounded": True, "citations": [1]}
        report = check_grounding(output, kb_call_count=0, enforcement="warn")
        codes = [v.code for v in report.violations]
        assert "grounded_without_kb_call" in codes

    def test_grounded_true_with_kb_call_no_violation(self) -> None:
        output = {"grounded": True, "citations": [1]}
        report = check_grounding(output, kb_call_count=1, enforcement="warn")
        codes = [v.code for v in report.violations]
        assert "grounded_without_kb_call" not in codes

    def test_grounded_without_kb_call_ok_is_false(self) -> None:
        output = {"grounded": True, "citations": [1]}
        report = check_grounding(output, kb_call_count=0, enforcement="warn")
        assert report.ok is False

    def test_grounded_without_kb_call_message(self) -> None:
        output = {"grounded": True, "citations": [1]}
        report = check_grounding(output, kb_call_count=0, enforcement="warn")
        violation = next(v for v in report.violations if v.code == "grounded_without_kb_call")
        assert "kb-vector-lookup" in violation.message

    def test_grounded_false_no_kb_call_no_violation(self) -> None:
        output = {"grounded": False}
        report = check_grounding(output, kb_call_count=0, enforcement="warn")
        codes = [v.code for v in report.violations]
        assert "grounded_without_kb_call" not in codes


# ---------------------------------------------------------------------------
# Multiple simultaneous violations
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMultipleViolations:
    """grounded=True + empty citations + kb_call_count=0 → 2 violations."""

    def test_grounded_no_citations_and_no_kb_call(self) -> None:
        output = {"grounded": True, "citations": []}
        report = check_grounding(output, kb_call_count=0, enforcement="warn")
        codes = {v.code for v in report.violations}
        assert "grounded_no_citations" in codes
        assert "grounded_without_kb_call" in codes
        assert len(report.violations) == 2

    def test_report_not_ok_with_multiple_violations(self) -> None:
        output = {"grounded": True, "citations": []}
        report = check_grounding(output, kb_call_count=0, enforcement="warn")
        assert report.ok is False

    def test_grounded_missing_key_and_no_kb_call_two_violations(self) -> None:
        # No "citations" key at all → grounded_no_citations
        # kb_call_count=0 → grounded_without_kb_call
        output = {"grounded": True}
        report = check_grounding(output, kb_call_count=0, enforcement="warn")
        codes = {v.code for v in report.violations}
        assert "grounded_no_citations" in codes
        assert "grounded_without_kb_call" in codes


# ---------------------------------------------------------------------------
# Clean output — no violations
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCleanOutput:
    """A well-formed grounded output with valid citations passes all checks."""

    def test_clean_output_ok_true(self) -> None:
        output = {"grounded": True, "citations": [1, 2]}
        report = check_grounding(output, kb_call_count=1, enforcement="warn")
        assert report.ok is True

    def test_clean_output_no_violations(self) -> None:
        output = {"grounded": True, "citations": [1, 2]}
        report = check_grounding(output, kb_call_count=1, enforcement="warn")
        assert report.violations == []

    def test_output_with_no_grounded_field_passes(self) -> None:
        """Fields are optional; an output that omits them triggers no violations."""
        output = {"answer": "The sky is blue."}
        report = check_grounding(output, kb_call_count=0, enforcement="warn")
        assert report.ok is True
        assert report.violations == []

    def test_empty_output_dict_passes(self) -> None:
        report = check_grounding({}, kb_call_count=0, enforcement="warn")
        assert report.ok is True
        assert report.violations == []

    def test_grounded_false_no_citations_passes(self) -> None:
        output = {"grounded": False}
        report = check_grounding(output, kb_call_count=0, enforcement="warn")
        assert report.ok is True

    def test_grounded_false_empty_citations_passes(self) -> None:
        output = {"grounded": False, "citations": []}
        report = check_grounding(output, kb_call_count=0, enforcement="warn")
        assert report.ok is True


# ---------------------------------------------------------------------------
# strict mode
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStrictMode:
    """strict enforcement raises GroundingViolationError on violations."""

    def test_strict_clean_output_no_raise(self) -> None:
        output = {"grounded": True, "citations": [1, 2]}
        report = check_grounding(output, kb_call_count=1, enforcement="strict")
        assert report.ok is True

    def test_strict_violation_raises(self) -> None:
        output = {"grounded": True, "citations": []}
        with pytest.raises(GroundingViolationError):
            check_grounding(output, kb_call_count=1, enforcement="strict")

    def test_strict_message_contains_violation_messages(self) -> None:
        output = {"grounded": True, "citations": []}
        with pytest.raises(GroundingViolationError) as exc_info:
            check_grounding(output, kb_call_count=1, enforcement="strict")
        error_str = str(exc_info.value)
        assert "grounding check failed" in error_str
        assert "citations" in error_str

    def test_strict_multiple_violations_joined_in_message(self) -> None:
        output = {"grounded": True, "citations": []}
        # kb_call_count=0 → grounded_without_kb_call + grounded_no_citations
        with pytest.raises(GroundingViolationError) as exc_info:
            check_grounding(output, kb_call_count=0, enforcement="strict")
        error_str = str(exc_info.value)
        # Both violation messages should appear in the joined string
        assert "grounding check failed" in error_str

    def test_strict_no_grounded_field_no_raise(self) -> None:
        output = {"answer": "Hello"}
        # Should not raise — no violations
        report = check_grounding(output, kb_call_count=0, enforcement="strict")
        assert report.ok is True

    def test_strict_ungrounded_with_citations_raises(self) -> None:
        output = {"grounded": False, "citations": [1, 2]}
        with pytest.raises(GroundingViolationError):
            check_grounding(output, enforcement="strict")

    def test_strict_invalid_indices_raises(self) -> None:
        output = {"grounded": True, "citations": [0]}
        with pytest.raises(GroundingViolationError):
            check_grounding(output, kb_call_count=1, enforcement="strict")

    def test_strict_grounded_without_kb_call_raises(self) -> None:
        output = {"grounded": True, "citations": [1]}
        with pytest.raises(GroundingViolationError):
            check_grounding(output, kb_call_count=0, enforcement="strict")


# ---------------------------------------------------------------------------
# GroundingReport.__bool__
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGroundingReportBool:
    """GroundingReport truthiness matches its ok field."""

    def test_ok_true_report_is_truthy(self) -> None:
        report = GroundingReport(ok=True)
        assert bool(report) is True

    def test_ok_false_report_is_falsy(self) -> None:
        report = GroundingReport(ok=False, violations=[GroundingViolation(code="x", message="y")])
        assert bool(report) is False

    def test_truthy_used_in_if_statement(self) -> None:
        report = GroundingReport(ok=True)
        passed = False
        if report:
            passed = True
        assert passed

    def test_falsy_used_in_if_statement(self) -> None:
        report = GroundingReport(ok=False)
        passed = True
        if report:
            passed = False
        assert passed


# ---------------------------------------------------------------------------
# kb_call_count_from_records
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKbCallCountFromRecords:
    """kb_call_count_from_records counts only successful KB lookup skill calls."""

    def test_empty_list_returns_zero(self) -> None:
        assert kb_call_count_from_records([]) == 0

    def test_single_successful_kb_record(self) -> None:
        records = [_kb_record(step=1)]
        assert kb_call_count_from_records(records) == 1

    def test_multiple_successful_kb_records(self) -> None:
        records = [_kb_record(step=1), _kb_record(step=2), _kb_record(step=3)]
        assert kb_call_count_from_records(records) == 3

    def test_non_kb_skill_not_counted(self) -> None:
        records = [_other_record(step=1)]
        assert kb_call_count_from_records(records) == 0

    def test_mix_of_kb_and_non_kb(self) -> None:
        records = [
            _other_record(step=1),
            _kb_record(step=2),
            _other_record(step=3),
            _kb_record(step=4),
        ]
        assert kb_call_count_from_records(records) == 2

    def test_failed_kb_call_not_counted(self) -> None:
        records = [_kb_record(step=1, error="connection refused")]
        assert kb_call_count_from_records(records) == 0

    def test_mix_of_successful_and_failed_kb_calls(self) -> None:
        records = [
            _kb_record(step=1),
            _kb_record(step=2, error="timeout"),
            _kb_record(step=3),
        ]
        assert kb_call_count_from_records(records) == 2

    def test_only_failed_kb_calls_returns_zero(self) -> None:
        records = [
            _kb_record(step=1, error="network error"),
            _kb_record(step=2, error="auth failed"),
        ]
        assert kb_call_count_from_records(records) == 0

    def test_all_non_kb_records_returns_zero(self) -> None:
        records = [_other_record(step=i) for i in range(5)]
        assert kb_call_count_from_records(records) == 0

    def test_mixed_all_types(self) -> None:
        records = [
            _other_record(step=1),
            _kb_record(step=2),
            _kb_record(step=3, error="err"),
            _kb_record(step=4),
            _other_record(step=5),
        ]
        # steps 2 and 4 are successful KB calls
        assert kb_call_count_from_records(records) == 2


# ---------------------------------------------------------------------------
# GroundingViolation dataclass
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGroundingViolationDataclass:
    """Basic structural checks on the GroundingViolation dataclass."""

    def test_fields_are_accessible(self) -> None:
        v = GroundingViolation(code="test_code", message="test message")
        assert v.code == "test_code"
        assert v.message == "test message"

    def test_violations_in_report(self) -> None:
        v = GroundingViolation(code="grounded_no_citations", message="bad output")
        report = GroundingReport(ok=False, violations=[v])
        assert len(report.violations) == 1
        assert report.violations[0].code == "grounded_no_citations"


# ---------------------------------------------------------------------------
# Default enforcement parameter
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDefaultEnforcement:
    """check_grounding defaults to enforcement="off" when not specified."""

    def test_default_is_off_returns_ok(self) -> None:
        # grounded=True + no citations would fail under warn/strict,
        # but with the default "off" it should pass.
        output = {"grounded": True, "citations": []}
        report = check_grounding(output, kb_call_count=0)
        assert report.ok is True
        assert report.violations == []
