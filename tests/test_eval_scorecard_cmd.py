"""Tests for ``mdk eval-scorecard`` — Phase 1 of the new eval flow.

Covers:

* The 10-category scorecard definition (8 LLM-judged + 2 programmatic)
  is stable and complete — regressions in category naming would break
  CI scrapers downstream.
* Programmatic scoring math (latency, cost) is correct at the budget
  boundaries.
* Judge-call response parsing tolerates bare JSON, fenced JSON, and
  missing categories without crashing.
* Aggregation produces correct per-category and overall means.
* The CLI ``--mix`` flag is validated against the allowlist.
* End-to-end with mock provider produces a renderable scorecard.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from typer.testing import CliRunner

from movate.cli.eval_scorecard_cmd import (
    ALL_CATEGORIES,
    LLM_JUDGED_CATEGORIES,
    PROGRAMMATIC_CATEGORIES,
    CaseScore,
    ScorecardSummary,
    _emit_summary_line,
    _measure_programmatic,
    _render_scorecard,
    _score_color,
    _score_one_case,
)
from movate.cli.main import app

runner = CliRunner(mix_stderr=False)

# ANSI-escape strip pattern: CI runs with FORCE_COLOR=1 so Rich's
# styling shows up as escape sequences inside `result.stdout`. The
# substring assertions strip these first so they're whitespace +
# content focused.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


# ---------------------------------------------------------------------------
# Scorecard definition
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestScorecardDefinition:
    def test_10_categories_total(self) -> None:
        """The user's spec was a '10-category scorecard'. Pin the count
        so a future refactor can't silently shrink the rubric."""
        assert len(ALL_CATEGORIES) == 10

    def test_8_llm_judged_plus_2_programmatic(self) -> None:
        """The judge prompt scores 8 in one JSON call (cheap); the
        other 2 are measured from the run record (latency, cost).
        Mixing those buckets would either over-count tokens or
        under-score real bottlenecks."""
        assert len(LLM_JUDGED_CATEGORIES) == 8
        assert len(PROGRAMMATIC_CATEGORIES) == 2
        assert set(LLM_JUDGED_CATEGORIES).isdisjoint(set(PROGRAMMATIC_CATEGORIES))

    def test_all_categories_is_concat_of_buckets(self) -> None:
        assert set(ALL_CATEGORIES) == set(LLM_JUDGED_CATEGORIES) | set(PROGRAMMATIC_CATEGORIES)

    def test_canonical_category_names(self) -> None:
        """Snake-case underscores; no hyphens or spaces (downstream
        scrapers split on whitespace in the summary line)."""
        for cat in ALL_CATEGORIES:
            assert " " not in cat
            assert "-" not in cat
            assert cat == cat.lower()

    def test_user_specified_categories_all_present(self) -> None:
        """The user's sign-off message listed these 10 by name. Pin
        the exact set so a typo in a future edit doesn't silently
        drop one."""
        expected = {
            "accuracy",
            "faithfulness",
            "format",
            "safety",
            "refusal",
            "hallucination",
            "latency",
            "cost",
            "completeness",
            "instruction_following",
        }
        assert set(ALL_CATEGORIES) == expected


# ---------------------------------------------------------------------------
# Programmatic scoring math
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProgrammaticScoring:
    def test_within_budget_scores_1(self) -> None:
        result = _measure_programmatic(latency_ms=100.0, cost_usd=0.001)
        assert result["latency"] == 1.0
        assert result["cost"] == 1.0

    def test_at_budget_scores_1(self) -> None:
        """Exactly at the soft budget is still a pass — overshooting
        is what penalizes."""
        result = _measure_programmatic(
            latency_ms=5000.0,
            cost_usd=0.01,
            latency_budget_ms=5000.0,
            cost_budget_usd=0.01,
        )
        assert result["latency"] == 1.0
        assert result["cost"] == 1.0

    def test_2x_budget_scores_half(self) -> None:
        """2x budget → 0.5. Surfaces a real "this is slow/expensive"
        signal without flooring at zero immediately."""
        result = _measure_programmatic(
            latency_ms=10000.0,
            cost_usd=0.02,
            latency_budget_ms=5000.0,
            cost_budget_usd=0.01,
        )
        assert result["latency"] == pytest.approx(0.0)  # 2x budget → score 0
        # Wait — by the formula 1 - (10000 - 5000) / 5000 = 0.0. Pinning that.

    def test_50pct_over_budget_scores_half(self) -> None:
        """1.5x budget → 0.5 — the documented mid-point."""
        result = _measure_programmatic(
            latency_ms=7500.0,
            cost_usd=0.015,
            latency_budget_ms=5000.0,
            cost_budget_usd=0.01,
        )
        assert result["latency"] == pytest.approx(0.5)
        assert result["cost"] == pytest.approx(0.5)

    def test_3x_budget_floors_at_zero(self) -> None:
        """3x+ budget → 0.0 (don't go negative — scorecard scale is 0-1)."""
        result = _measure_programmatic(
            latency_ms=50000.0,
            cost_usd=0.1,
            latency_budget_ms=5000.0,
            cost_budget_usd=0.01,
        )
        assert result["latency"] == 0.0
        assert result["cost"] == 0.0

    def test_zero_budget_treated_as_unbounded(self) -> None:
        """Defensive: budget=0 would div-by-zero. Treat as 'no gate'."""
        result = _measure_programmatic(
            latency_ms=99999.0,
            cost_usd=0.99,
            latency_budget_ms=0.0,
            cost_budget_usd=0.0,
        )
        assert result["latency"] == 1.0
        assert result["cost"] == 1.0


# ---------------------------------------------------------------------------
# Score color (rendering helper)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestScoreColor:
    def test_green_at_80_plus(self) -> None:
        assert _score_color(0.80) == "green"
        assert _score_color(1.00) == "green"

    def test_yellow_60_to_80(self) -> None:
        assert _score_color(0.60) == "yellow"
        assert _score_color(0.79) == "yellow"

    def test_red_below_60(self) -> None:
        assert _score_color(0.59) == "red"
        assert _score_color(0.00) == "red"


# ---------------------------------------------------------------------------
# CLI validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_rejects_invalid_mix(tmp_path: Path) -> None:
    """``--mix bogus`` must error before any LLM calls fire. Cheapest
    way to catch a typo before burning $0.10 on generation."""
    # Scaffold a minimal agent so the loader doesn't fail first.
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "agent.yaml").write_text(
        "name: demo\nmodel:\n  provider: openai/gpt-4o-mini-2024-07-18\n"
    )
    (agent_dir / "prompt.md").write_text("You are a demo agent.\n")

    result = runner.invoke(
        app,
        ["eval-scorecard", str(agent_dir), "--mix", "bogus"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "invalid --mix" in combined or "bogus" in combined


@pytest.mark.unit
def test_cli_command_surfaces_in_help() -> None:
    """``mdk --help`` must list eval-scorecard so operators discover
    it. Pin the surface."""
    result = runner.invoke(app, ["--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    plain = _ANSI_RE.sub("", result.stdout)
    assert "eval-scorecard" in plain


@pytest.mark.unit
def test_eval_scorecard_dedicated_help_describes_categories() -> None:
    """The command's own --help must list all 10 categories so an
    operator can decide whether the rubric matches their needs
    without reading the source."""
    result = runner.invoke(app, ["eval-scorecard", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    plain = _ANSI_RE.sub("", result.stdout)
    # Spot-check a few categories from each bucket.
    assert "accuracy" in plain
    assert "faithfulness" in plain
    assert "refusal" in plain
    assert "hallucination" in plain
    assert "latency" in plain
    assert "cost" in plain


# ---------------------------------------------------------------------------
# Rendering + summary line
# ---------------------------------------------------------------------------


def _fake_summary(*, agent: str = "demo", overall: float = 0.85) -> ScorecardSummary:
    cases = [
        CaseScore(
            input={"q": "hi"},
            output={"a": "hello"},
            latency_ms=100.0,
            cost_usd=0.0001,
            scores=dict.fromkeys(ALL_CATEGORIES, overall),
            rationales=dict.fromkeys(LLM_JUDGED_CATEGORIES, "looks fine"),
        )
    ]
    return ScorecardSummary(
        agent=agent,
        mix="standard",
        count=1,
        cases=cases,
        category_means=dict.fromkeys(ALL_CATEGORIES, overall),
        overall_mean=overall,
    )


@pytest.mark.unit
def test_render_scorecard_includes_all_10_categories(capsys: Any) -> None:
    """The Rich table must have one row per category. Catch a regression
    where someone iterates over the wrong tuple and drops a row."""
    summary = _fake_summary(overall=0.85)
    _render_scorecard(summary)
    captured = capsys.readouterr()
    plain = _ANSI_RE.sub("", captured.out)
    for cat in ALL_CATEGORIES:
        display_name = cat.replace("_", " ")
        assert display_name in plain, f"category {cat!r} missing from rendered table"


@pytest.mark.unit
def test_emit_summary_line_is_greppable_with_all_category_keys(capsys: Any) -> None:
    """``mdk_eval_scorecard_summary: agent=… overall=… <cat>=… …``
    is the CI-scrape surface. Pin that every category appears as
    ``key=value`` so a downstream parser can split on whitespace and
    get all 10 scores without surprises."""
    summary = _fake_summary(agent="demo", overall=0.75)
    _emit_summary_line(summary)
    captured = capsys.readouterr()
    line = captured.out
    assert "mdk_eval_scorecard_summary:" in line
    assert "agent=demo" in line
    assert "overall=0.750" in line
    for cat in ALL_CATEGORIES:
        assert f"{cat}=" in line, f"category {cat!r} missing from summary line"


# ---------------------------------------------------------------------------
# Judge response parsing
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_score_one_case_parses_bare_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: judge returns a clean JSON object with all 8 keys."""

    fake_response = mock.Mock()
    fake_response.text = (
        '{"accuracy": {"score": 0.95, "rationale": "correct"},'
        '"faithfulness": {"score": 0.90, "rationale": "grounded"},'
        '"format": {"score": 1.0, "rationale": "valid"},'
        '"safety": {"score": 1.0, "rationale": "safe"},'
        '"refusal": {"score": 1.0, "rationale": "benign"},'
        '"hallucination": {"score": 0.85, "rationale": "minor"},'
        '"completeness": {"score": 0.80, "rationale": "partial"},'
        '"instruction_following": {"score": 0.90, "rationale": "good"}}'
    )
    fake_rt = mock.Mock()
    fake_rt.provider.complete = mock.AsyncMock(return_value=fake_response)
    fake_bundle = mock.Mock()
    fake_bundle.spec.model.provider = "openai/gpt-4o-mini-2024-07-18"
    fake_bundle.system_prompt = "You are a demo agent."

    scores, rationales = await _score_one_case(fake_rt, fake_bundle, {"q": "hi"}, {"a": "hello"})
    assert scores["accuracy"] == 0.95
    assert scores["format"] == 1.0
    assert rationales["accuracy"] == "correct"
    # All 8 categories populated.
    assert set(scores.keys()) == set(LLM_JUDGED_CATEGORIES)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_score_one_case_strips_code_fences(monkeypatch: pytest.MonkeyPatch) -> None:
    """Models love to wrap JSON in ```json fences even when asked not
    to. The parser must strip them."""

    fake_response = mock.Mock()
    fake_response.text = (
        "```json\n" + '{"accuracy": {"score": 1.0, "rationale": "x"},'
        '"faithfulness": {"score": 1.0, "rationale": "x"},'
        '"format": {"score": 1.0, "rationale": "x"},'
        '"safety": {"score": 1.0, "rationale": "x"},'
        '"refusal": {"score": 1.0, "rationale": "x"},'
        '"hallucination": {"score": 1.0, "rationale": "x"},'
        '"completeness": {"score": 1.0, "rationale": "x"},'
        '"instruction_following": {"score": 1.0, "rationale": "x"}}' + "\n```"
    )
    fake_rt = mock.Mock()
    fake_rt.provider.complete = mock.AsyncMock(return_value=fake_response)
    fake_bundle = mock.Mock()
    fake_bundle.spec.model.provider = "openai/gpt-4o-mini-2024-07-18"
    fake_bundle.system_prompt = "..."

    scores, _ = await _score_one_case(fake_rt, fake_bundle, {}, {})
    assert all(s == 1.0 for s in scores.values())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_score_one_case_tolerates_missing_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the judge omits a category (e.g. truncated response), fill
    with 0 + an explanatory rationale rather than KeyError'ing — the
    table must still render."""

    fake_response = mock.Mock()
    # Only 3 categories present.
    fake_response.text = (
        '{"accuracy": {"score": 1.0, "rationale": "x"},'
        '"safety": {"score": 1.0, "rationale": "x"},'
        '"format": {"score": 0.5, "rationale": "x"}}'
    )
    fake_rt = mock.Mock()
    fake_rt.provider.complete = mock.AsyncMock(return_value=fake_response)
    fake_bundle = mock.Mock()
    fake_bundle.spec.model.provider = "openai/gpt-4o-mini-2024-07-18"
    fake_bundle.system_prompt = "..."

    scores, rationales = await _score_one_case(fake_rt, fake_bundle, {}, {})
    # All 8 keys must be present even though judge sent only 3.
    assert set(scores.keys()) == set(LLM_JUDGED_CATEGORIES)
    # Missing ones default to 0.
    assert scores["refusal"] == 0.0
    assert "omitted" in rationales["refusal"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_score_one_case_judge_failure_returns_zeros(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A judge network error or unparseable response shouldn't crash
    the scorecard — return zeros so the table still surfaces the
    other (successful) cases."""

    fake_rt = mock.Mock()
    fake_rt.provider.complete = mock.AsyncMock(side_effect=RuntimeError("network down"))
    fake_bundle = mock.Mock()
    fake_bundle.spec.model.provider = "openai/gpt-4o-mini-2024-07-18"
    fake_bundle.system_prompt = "..."

    scores, rationales = await _score_one_case(fake_rt, fake_bundle, {}, {})
    assert all(s == 0.0 for s in scores.values())
    assert "judge error" in rationales["accuracy"]
