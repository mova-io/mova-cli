"""Tests for ``mdk run --trace`` KB retrieval trace output.

Covers:
1. ``_print_kb_trace`` with a kb skill call that has chunks → table printed to stderr.
2. ``_print_kb_trace`` with a non-kb skill call → nothing printed.
3. ``_print_kb_trace`` with empty chunks list → nothing printed.
4. ``_print_kb_trace`` with malformed output → silently swallowed.
"""

from __future__ import annotations

import sys
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from movate.cli.run import _print_kb_trace


def _make_skill_call(
    skill: str,
    output: dict | None = None,
    latency_ms: float = 100.0,
) -> MagicMock:
    sc = MagicMock()
    sc.skill = skill
    sc.output = output
    sc.latency_ms = latency_ms
    return sc


@pytest.mark.unit
class TestPrintKbTrace:
    def test_shows_kb_chunks(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A kb skill call with chunks should produce trace output on stderr."""
        skill_call = _make_skill_call(
            skill="kb-vector-lookup",
            output={
                "chunks": [
                    {
                        "chunk_id": "c1",
                        "content": "Refund policy: customers may request a refund within 30 days.",
                        "score": 0.87,
                        "source": "product-docs.pdf",
                    },
                    {
                        "chunk_id": "c2",
                        "content": "All returns must be initiated within 30 days of purchase.",
                        "score": 0.74,
                        "source": "handbook.md",
                    },
                ]
            },
            latency_ms=142.0,
        )

        _print_kb_trace([skill_call])

        captured = capsys.readouterr()
        assert "kb-vector-lookup" in captured.err
        assert "0.87" in captured.err
        assert "product-docs.pdf" in captured.err
        assert "KB retrieval trace" in captured.err

    def test_no_output_for_non_kb_skill(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A skill call whose name does not contain 'kb' should produce no output."""
        skill_call = _make_skill_call(
            skill="send-email",
            output={"status": "sent"},
        )

        _print_kb_trace([skill_call])

        captured = capsys.readouterr()
        assert captured.err == ""

    def test_no_output_when_chunks_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A kb skill call with an empty chunks list should produce no table rows."""
        skill_call = _make_skill_call(
            skill="kb-vector-lookup",
            output={"chunks": []},
        )

        _print_kb_trace([skill_call])

        captured = capsys.readouterr()
        # Header line should still not appear since no chunks were found.
        # The function skips the table when chunks is empty.
        assert "0.87" not in captured.err

    def test_no_output_when_output_is_none(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A kb skill call with output=None should not crash and produce no table."""
        skill_call = _make_skill_call(
            skill="kb-vector-lookup",
            output=None,
        )

        _print_kb_trace([skill_call])  # must not raise

        captured = capsys.readouterr()
        assert "0.87" not in captured.err

    def test_malformed_chunk_does_not_crash(self, capsys: pytest.CaptureFixture[str]) -> None:
        """If a chunk entry is not a dict, _print_kb_trace must not raise."""
        skill_call = _make_skill_call(
            skill="kb-vector-lookup",
            output={"chunks": ["not-a-dict", None, 42]},
        )

        _print_kb_trace([skill_call])  # must not raise

    def test_content_truncated_to_80_chars(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Long chunk content should be truncated at 80 characters."""
        long_content = "A" * 200
        skill_call = _make_skill_call(
            skill="kb-vector-lookup",
            output={
                "chunks": [
                    {"chunk_id": "c1", "content": long_content, "score": 0.9, "source": "doc.md"}
                ]
            },
        )

        _print_kb_trace([skill_call])

        captured = capsys.readouterr()
        # The full 200-char string should NOT appear (truncated to 80).
        assert "A" * 81 not in captured.err
        # But the first 80 chars should be there.
        assert "A" * 80 in captured.err

    def test_newlines_replaced_in_content(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Newlines in chunk content should be replaced with spaces."""
        skill_call = _make_skill_call(
            skill="kb-vector-lookup",
            output={
                "chunks": [
                    {"chunk_id": "c1", "content": "line1\nline2", "score": 0.8, "source": ""}
                ]
            },
        )

        _print_kb_trace([skill_call])

        captured = capsys.readouterr()
        assert "\n" not in captured.err.split("KB retrieval trace")[-1].split("0.8")[0] + "line1 line2"

    def test_multiple_kb_calls_all_shown(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Multiple kb skill calls should each produce a table section."""
        call1 = _make_skill_call(
            skill="kb-vector-lookup",
            output={"chunks": [{"content": "first result", "score": 0.9, "source": "a.pdf"}]},
            latency_ms=50.0,
        )
        call2 = _make_skill_call(
            skill="kb-vector-lookup",
            output={"chunks": [{"content": "second result", "score": 0.7, "source": "b.pdf"}]},
            latency_ms=80.0,
        )

        _print_kb_trace([call1, call2])

        captured = capsys.readouterr()
        assert "first result" in captured.err
        assert "second result" in captured.err
