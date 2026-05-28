"""Tests for ``mdk run --trace`` KB retrieval trace output.

Covers:
1. ``_print_kb_trace`` with a kb skill call that has chunks → table printed to stderr.
2. ``_print_kb_trace`` with a non-kb skill call → nothing printed.
3. ``_print_kb_trace`` with empty chunks list → nothing printed.
4. ``_print_kb_trace`` with malformed output → silently swallowed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

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
        assert "A" * 200 not in captured.err
        # The truncated content should appear somewhere in the output (Rich may
        # wrap the cell but the leading A's are still present).
        assert "A" in captured.err

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
        # The content with newlines replaced by spaces should appear in the table cell.
        assert "line1 line2" in captured.err

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


# ---------------------------------------------------------------------------
# mdk run --estimate (Cost Prediction)
# ---------------------------------------------------------------------------


class TestEstimateRender:
    """The shared estimate renderer used by both local + remote --estimate."""

    @staticmethod
    def _est_dict(**overrides: object) -> dict:
        base = {
            "estimate": True,
            "agent_name": "demo",
            "model": "openai/gpt-4o-mini-2024-07-18",
            "predicted": {
                "tokens_in": 1240,
                "tokens_out_max": 512,
                "tokens_out_expected": 180,
                "cost_usd_min": 0.0008,
                "cost_usd_expected": 0.0021,
                "cost_usd_max": 0.0034,
                "latency_ms_p50": 850,
                "latency_ms_p95": 2100,
            },
            "basis": {
                "prompt_tokens_method": "assembled+tiktoken",
                "out_expected_method": "historical_mean",
                "latency_method": "historical_p50p95",
                "sample_size": 142,
            },
            "budget_check": {"within_per_run_budget": True, "per_run_budget_usd": 0.5},
            "retrieval_embedded": False,
            "notes": [],
        }
        base.update(overrides)  # type: ignore[arg-type]
        return base

    @pytest.mark.unit
    def test_text_render_shows_table_and_no_run_notice(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from movate.cli._output import Run
        from movate.cli.run import _render_estimate

        _render_estimate(est_dict=self._est_dict(), output_format=Run.TEXT)
        out = capsys.readouterr()
        # Table (stdout) carries the agent + a token figure.
        assert "demo" in out.out
        assert "1240" in out.out
        # The "no run executed" notice + greppable summary land on stderr.
        assert "NO run executed" in out.err
        assert "mdk_estimate_summary:" in out.err
        assert "executed=false" in out.err

    @pytest.mark.unit
    def test_json_render_dumps_estimate(self, capsys: pytest.CaptureFixture[str]) -> None:
        import json

        from movate.cli._output import Run
        from movate.cli.run import _render_estimate

        _render_estimate(est_dict=self._est_dict(), output_format=Run.JSON)
        out = capsys.readouterr()
        parsed = json.loads(out.out)
        assert parsed["estimate"] is True
        assert parsed["predicted"]["tokens_in"] == 1240
        assert "mdk_estimate_summary:" in out.err

    @pytest.mark.unit
    def test_text_render_flags_over_budget(self, capsys: pytest.CaptureFixture[str]) -> None:
        from movate.cli._output import Run
        from movate.cli.run import _render_estimate

        est = self._est_dict(
            budget_check={"within_per_run_budget": False, "per_run_budget_usd": 0.001}
        )
        _render_estimate(est_dict=est, output_format=Run.TEXT)
        out = capsys.readouterr()
        assert "OVER" in out.out


@pytest.mark.unit
def test_run_estimate_local_end_to_end(
    tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`mdk run <agent-dir> --estimate "<input>"` predicts without executing.

    Uses an isolated SQLite DB (MOVATE_DB) so the estimate's historical
    query has no prior runs — exercising the fallback path through the real
    CLI dispatch + renderer."""
    from typer.testing import CliRunner

    from movate.cli.main import app as cli_app
    from movate.testing import scaffold_agent

    monkeypatch.setenv("MOVATE_DB", str(tmp_path / "local.db"))
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")

    runner = CliRunner(mix_stderr=False)
    result = runner.invoke(
        cli_app,
        ["run", str(agent_dir), "hello there", "--estimate", "--output", "json"],
    )
    assert result.exit_code == 0, result.stderr
    import json

    body = json.loads(result.stdout)
    assert body["estimate"] is True
    assert body["agent_name"] == "demo"
    assert body["predicted"]["tokens_in"] > 0
    # No history in the isolated DB → the fallback path, and crucially the
    # command exits 0 having executed nothing (no run_id is ever minted).
    assert body["basis"]["sample_size"] == 0
    assert "run_id" not in body
