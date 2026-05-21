"""Tests for ``mdk eval`` CLI extensions.

Currently covers:
* ``--variant`` A/B comparison: runs the same dataset against two agent
  configurations and prints a side-by-side score table.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from movate.cli.eval import _print_variant_comparison_table
from movate.cli.main import app
from movate.core.eval import DimensionalMeans, EvalSummary
from movate.core.models import JudgeConfig, JudgeMethod

runner = CliRunner(mix_stderr=False)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def _make_summary(
    *,
    agent: str = "demo",
    mean_score: float = 0.8,
    pass_rate: float = 1.0,
    sample_count: int = 2,
    dim_accuracy: float | None = 0.8,
) -> EvalSummary:
    """Build a minimal EvalSummary for testing.

    Uses real data classes so we exercise the actual rendering path without
    mocking internal list state.
    """
    from movate.core.eval import CaseSummary, EvalCase  # noqa: PLC0415

    judge = JudgeConfig(method=JudgeMethod.EXACT)

    # Build fake cases so sample_count / pass_rate / mean_score properties
    # return the values we care about.
    from movate.core.eval import CaseRun  # noqa: PLC0415
    from movate.core.models import Metrics, RunResponse, TokenUsage  # noqa: PLC0415

    fake_response = RunResponse(
        status="success",
        metrics=Metrics(tokens=TokenUsage(input=10, output=10)),
    )

    cases = []
    for i in range(sample_count):
        run = CaseRun(response=fake_response, score=mean_score, rationale="ok")
        cs = CaseSummary(
            case=EvalCase(input={"q": f"q{i}"}, expected={"a": f"a{i}"}),
            runs=[run],
            aggregated_score=mean_score,
            passed=mean_score >= 0.7,
        )
        cases.append(cs)

    return EvalSummary(
        agent=agent,
        agent_version="0.1.0",
        dataset_hash="abc123",
        judge=judge,
        judge_provider=None,
        runs_per_case=1,
        gate_mode="mean",
        threshold=0.7,
        cases=cases,
        dimensional_means=DimensionalMeans(accuracy=dim_accuracy),
    )


# ---------------------------------------------------------------------------
# Unit: _print_variant_comparison_table
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_variant_comparison_table_renders_both_agents() -> None:
    """The comparison table shows both agent names."""
    from io import StringIO  # noqa: PLC0415

    from rich.console import Console  # noqa: PLC0415

    primary = _make_summary(agent="demo-v1", mean_score=0.8)
    variant = _make_summary(agent="demo-v2", mean_score=0.9)

    buf = StringIO()
    with mock.patch("movate.cli.eval.console", Console(file=buf, highlight=False)):
        _print_variant_comparison_table(primary, variant)

    output = _strip_ansi(buf.getvalue())
    assert "demo-v1" in output
    assert "demo-v2" in output


@pytest.mark.unit
def test_variant_comparison_table_shows_mean_scores() -> None:
    """Mean scores for both agents appear in the comparison output."""
    from io import StringIO  # noqa: PLC0415

    from rich.console import Console  # noqa: PLC0415

    primary = _make_summary(agent="demo-v1", mean_score=0.8)
    variant = _make_summary(agent="demo-v2", mean_score=0.9)

    buf = StringIO()
    with mock.patch("movate.cli.eval.console", Console(file=buf, highlight=False)):
        _print_variant_comparison_table(primary, variant)

    output = _strip_ansi(buf.getvalue())
    assert "0.800" in output
    assert "0.900" in output


@pytest.mark.unit
def test_variant_comparison_table_shows_winner() -> None:
    """The comparison output includes a winner announcement."""
    from io import StringIO  # noqa: PLC0415

    from rich.console import Console  # noqa: PLC0415

    primary = _make_summary(agent="demo-v1", mean_score=0.7)
    variant = _make_summary(agent="demo-v2", mean_score=0.9)

    buf = StringIO()
    with mock.patch("movate.cli.eval.console", Console(file=buf, highlight=False)):
        _print_variant_comparison_table(primary, variant)

    output = _strip_ansi(buf.getvalue())
    # Variant wins (0.9 > 0.7)
    assert "demo-v2" in output
    # The trophy emoji should appear; check for "wins"
    assert "wins" in output


@pytest.mark.unit
def test_variant_comparison_table_primary_wins() -> None:
    """When primary has a higher score, it is announced as the winner."""
    from io import StringIO  # noqa: PLC0415

    from rich.console import Console  # noqa: PLC0415

    primary = _make_summary(agent="demo-v1", mean_score=0.95)
    variant = _make_summary(agent="demo-v2", mean_score=0.6)

    buf = StringIO()
    with mock.patch("movate.cli.eval.console", Console(file=buf, highlight=False)):
        _print_variant_comparison_table(primary, variant)

    output = _strip_ansi(buf.getvalue())
    assert "demo-v1" in output
    assert "wins" in output


@pytest.mark.unit
def test_variant_comparison_table_equal_scores_primary_wins() -> None:
    """When scores are equal, primary is announced as the winner (tie goes to primary)."""
    from io import StringIO  # noqa: PLC0415

    from rich.console import Console  # noqa: PLC0415

    primary = _make_summary(agent="demo-v1", mean_score=0.8)
    variant = _make_summary(agent="demo-v2", mean_score=0.8)

    buf = StringIO()
    with mock.patch("movate.cli.eval.console", Console(file=buf, highlight=False)):
        _print_variant_comparison_table(primary, variant)

    output = _strip_ansi(buf.getvalue())
    # primary wins on ties (primary.mean_score >= variant.mean_score)
    assert "demo-v1" in output
    assert "wins" in output


@pytest.mark.unit
def test_variant_comparison_table_shows_pass_rate() -> None:
    """Pass Rate column appears in the comparison output."""
    from io import StringIO  # noqa: PLC0415

    from rich.console import Console  # noqa: PLC0415

    primary = _make_summary(agent="a", mean_score=0.8)
    variant = _make_summary(agent="b", mean_score=0.9)

    buf = StringIO()
    with mock.patch("movate.cli.eval.console", Console(file=buf, highlight=False)):
        _print_variant_comparison_table(primary, variant)

    output = _strip_ansi(buf.getvalue())
    assert "Pass Rate" in output


@pytest.mark.unit
def test_variant_comparison_table_dim_column_when_both_scored() -> None:
    """Dimension columns appear when both summaries have that dimension scored."""
    from io import StringIO  # noqa: PLC0415

    from rich.console import Console  # noqa: PLC0415

    primary = _make_summary(agent="a", mean_score=0.8, dim_accuracy=0.8)
    variant = _make_summary(agent="b", mean_score=0.9, dim_accuracy=0.9)

    buf = StringIO()
    with mock.patch("movate.cli.eval.console", Console(file=buf, highlight=False)):
        _print_variant_comparison_table(primary, variant)

    output = _strip_ansi(buf.getvalue())
    # accuracy dimension header should appear
    assert "Accuracy" in output


@pytest.mark.unit
def test_variant_comparison_table_no_dim_column_when_one_missing() -> None:
    """When one summary has a dimension unscored, that column is omitted."""
    from io import StringIO  # noqa: PLC0415

    from rich.console import Console  # noqa: PLC0415

    primary = _make_summary(agent="a", mean_score=0.8, dim_accuracy=0.8)
    # variant has no accuracy dimension scored
    variant = _make_summary(agent="b", mean_score=0.9, dim_accuracy=None)

    buf = StringIO()
    with mock.patch("movate.cli.eval.console", Console(file=buf, highlight=False)):
        _print_variant_comparison_table(primary, variant)

    output = _strip_ansi(buf.getvalue())
    # Accuracy column should NOT appear since variant doesn't score it
    assert "Accuracy" not in output


# ---------------------------------------------------------------------------
# Integration: test_eval_variant_comparison
# Mocks both EvalEngine.run() calls so no real provider / dataset is needed.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_eval_variant_comparison(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``mdk eval <path> --variant <path>`` triggers variant comparison.

    Mocks ``_run_eval`` and ``_run_variant_comparison`` so no real provider,
    dataset, or runtime is needed — this test just verifies the CLI wires
    the --variant flag to the variant comparison coroutine.
    """
    from movate.testing import scaffold_agent  # noqa: PLC0415

    primary_dir = scaffold_agent(tmp_path / "primary", name="demo-v1")
    variant_dir = scaffold_agent(tmp_path / "variant", name="demo-v2")

    variant_called = {"called": False}

    async def _fake_run_eval(bundle, **kwargs):
        """No-op primary eval — returns without raising so the CLI continues."""

    async def _fake_run_variant(primary_bundle, variant_bundle, **kwargs):
        variant_called["called"] = True

    monkeypatch.setattr("movate.cli.eval._run_eval", _fake_run_eval)
    monkeypatch.setattr("movate.cli.eval._run_variant_comparison", _fake_run_variant)
    # Suppress the LLM-provider preflight check.
    monkeypatch.setattr(
        "movate.cli.eval._require_llm_provider_key_or_offer_setup", lambda: None
    )

    result = runner.invoke(
        app,
        [
            "eval",
            str(primary_dir),
            "--mock",
            "--variant",
            str(variant_dir),
            "--gate",
            "0.5",
        ],
        env={"COLUMNS": "200"},
    )

    assert variant_called["called"], (
        "Expected _run_variant_comparison to be called. "
        f"Exit code: {result.exit_code}\n"
        f"stdout: {result.stdout}\n"
        f"exception: {result.exception}"
    )
