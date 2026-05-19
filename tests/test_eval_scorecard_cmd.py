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

import asyncio
import json as _json
import logging
import re
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import typer
from typer.testing import CliRunner

from movate.cli import eval_scorecard_cmd
from movate.cli.eval import _render_cases_preview_table, _truncate_json
from movate.cli.eval_scorecard_cmd import (
    _VALID_MIXES,
    ALL_CATEGORIES,
    LLM_JUDGED_CATEGORIES,
    PROGRAMMATIC_CATEGORIES,
    CaseScore,
    ScorecardSummary,
    _emit_summary_line,
    _find_project_root,
    _measure_programmatic,
    _render_scorecard,
    _score_color,
    _score_one_case,
)
from movate.cli.main import app
from movate.core.failures import AuthError

runner = CliRunner(mix_stderr=False)

# ANSI-escape strip pattern: CI runs with FORCE_COLOR=1 so Rich's
# styling shows up as escape sequences inside `result.stdout`. The
# substring assertions strip these first so they're whitespace +
# content focused.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


@pytest.fixture(autouse=True)
def _disable_preflight(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """No-op the generator-auth preflight by default.

    The preflight makes a real 1-token LLM call when ``mock=False`` to
    fail fast on missing API keys. Most tests in this file scaffold a
    real project and invoke ``eval-scorecard`` WITHOUT ``--mock``, then
    monkeypatch the downstream ``_run_scorecard`` to a fake. Without
    this fixture each of those tests would hit the live LiteLLM
    provider during preflight and exit 2 with AuthError in CI (no API
    keys).

    Tests marked ``@pytest.mark.no_preflight_stub`` opt out — typically
    the ``TestPreflight`` class which exercises the real preflight with
    its own stub provider, and ``TestPreflightIntegration`` which
    re-patches the preflight with a recording stub.
    """
    if request.node.get_closest_marker("no_preflight_stub") is not None:
        return

    async def _noop(*, models: set[str], mock: bool) -> None:
        return None

    monkeypatch.setattr(
        "movate.cli.eval_scorecard_cmd._preflight_check_generator_auth",
        _noop,
    )


# ---------------------------------------------------------------------------
# Scorecard definition
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestScorecardDefinition:
    def test_11_categories_total(self) -> None:
        """The original spec was '10-category scorecard'. ``citation_accuracy``
        added in 0.8.2.15 brings the count to 11 (9 LLM-judged + 2
        programmatic). Pin the count so a future refactor can't
        silently shrink the rubric."""
        assert len(ALL_CATEGORIES) == 11

    def test_9_llm_judged_plus_2_programmatic(self) -> None:
        """The judge prompt scores 9 in one JSON call (cheap); the
        other 2 are measured from the run record (latency, cost).
        Mixing those buckets would either over-count tokens or
        under-score real bottlenecks. ``citation_accuracy`` (added
        0.8.2.15) lives in the LLM-judged bucket — it needs a model
        to read the cited chunks + verify they support the claim."""
        assert len(LLM_JUDGED_CATEGORIES) == 9
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
        """Pin the exact set so a typo in a future edit doesn't
        silently drop one. ``citation_accuracy`` added 0.8.2.15 to
        the original 10."""
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
            "citation_accuracy",
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
# Mix allowlist (Phase 2 added "domain")
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestValidMixes:
    def test_all_four_mixes_present(self) -> None:
        """Phase 2 adds 'domain' to the existing three mixes. Pin the
        full set so a future edit can't silently drop one."""
        assert set(_VALID_MIXES) == {"standard", "edge", "adversarial", "domain"}

    def test_domain_mix_listed_after_phase_1_three(self) -> None:
        """Domain is the new addition; the other three shipped in
        Phase 1 (PR #178). Order doesn't matter for the allowlist
        check above, but ``--help`` renders this tuple in order, so
        new mixes should append rather than reorder."""
        assert _VALID_MIXES[:3] == ("standard", "edge", "adversarial")
        assert _VALID_MIXES[3] == "domain"


# ---------------------------------------------------------------------------
# _find_project_root (used by domain-mix to locate kb/)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFindProjectRoot:
    def test_walks_up_to_project_yaml(self, tmp_path: Path) -> None:
        """``agents/<name>/`` → walk up to find ``project.yaml`` at
        the project root. Domain-mix needs this to locate the kb/
        corpus."""
        (tmp_path / "project.yaml").write_text("# minimal project marker\n")
        agent_dir = tmp_path / "agents" / "rag-qa"
        agent_dir.mkdir(parents=True)
        assert _find_project_root(agent_dir) == tmp_path.resolve()

    def test_accepts_legacy_movate_yaml(self, tmp_path: Path) -> None:
        """Pre-#85 projects used movate.yaml. Loader still accepts
        both — the project-root detector must too."""
        (tmp_path / "movate.yaml").write_text("name: demo\n")
        agent_dir = tmp_path / "agents" / "rag-qa"
        agent_dir.mkdir(parents=True)
        assert _find_project_root(agent_dir) == tmp_path.resolve()

    def test_falls_back_to_parent_when_no_marker(self, tmp_path: Path) -> None:
        """Outside a project (no marker anywhere up the tree), return
        the agent's parent directory rather than erroring. Domain-mix
        then just won't find KB seeds — degenerate but non-fatal."""
        agent_dir = tmp_path / "rogue-agent"
        agent_dir.mkdir()
        result = _find_project_root(agent_dir)
        # The fallback is .parent of the agent dir.
        assert result == agent_dir.parent


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
def test_mdk_eval_scorecard_flag_surfaces_in_eval_help() -> None:
    """``mdk eval --help`` must list the ``--scorecard`` flag — that's
    how operators discover the new flow from the existing ``mdk eval``
    surface. Pin the substring so a future refactor of the help text
    can't accidentally drop the discovery path."""
    result = runner.invoke(app, ["eval", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    plain = _ANSI_RE.sub("", result.stdout)
    assert "--scorecard" in plain
    # The two paired knobs that only matter with --scorecard.
    assert "--scorecard-count" in plain
    assert "--scorecard-mix" in plain


@pytest.mark.unit
def test_mdk_eval_scorecard_requires_an_agent_path(tmp_path: Path) -> None:
    """``mdk eval --scorecard`` with no agent path is a clean operator
    error — emit a hint pointing at the right shape rather than
    falling into the scorecard flow with no target."""
    result = runner.invoke(
        app,
        ["eval", "--scorecard"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "--scorecard requires an agent path" in combined


@pytest.mark.unit
def test_mdk_eval_scorecard_dispatches_to_scorecard_function(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``--scorecard`` is set, the eval command must route directly
    to ``eval_scorecard_cmd.eval_scorecard`` with the scorecard-specific
    kwargs (count, mix, mock, judge_model) and skip the dataset.jsonl
    code path entirely. Pin the dispatch so a future refactor of the
    eval orchestrator can't accidentally fall back to the old flow."""
    calls: list[dict[str, Any]] = []

    def fake_scorecard(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd.eval_scorecard", fake_scorecard)

    # Scaffold a minimal agent so the path arg resolves.
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "agent.yaml").write_text(
        "name: demo\nmodel:\n  provider: openai/gpt-4o-mini-2024-07-18\n"
    )
    (agent_dir / "prompt.md").write_text("You are a demo agent.\n")

    result = runner.invoke(
        app,
        [
            "eval",
            str(agent_dir),
            "--scorecard",
            "--scorecard-count",
            "5",
            "--scorecard-mix",
            "edge",
            "--mock",
        ],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert len(calls) == 1
    call = calls[0]
    assert call["agent"] == str(agent_dir)
    assert call["count"] == 5
    assert call["mix"] == "edge"
    assert call["mock"] is True


@pytest.mark.unit
def test_mdk_eval_without_scorecard_flag_uses_old_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag is opt-in (Phase 3a). Bare ``mdk eval <agent>`` must
    NOT route to the scorecard — existing CI scripts that assume
    dataset.jsonl-based scoring keep working unchanged. Phase 3b
    will flip this default after the scorecard is battle-tested."""
    calls: list[dict[str, Any]] = []

    def fake_scorecard(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd.eval_scorecard", fake_scorecard)

    # Scaffold a project with one agent + a dataset row so the old
    # flow can complete cleanly.
    monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    monkeypatch.chdir(tmp_path / "proj")
    runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})

    # No --scorecard flag → old flow.
    result = runner.invoke(
        app,
        ["eval", "faq", "--mock", "--gate", "0.0"],
        env={"COLUMNS": "200"},
    )
    # Old flow's greppable line, NOT the scorecard's.
    combined = result.stdout + result.stderr
    assert "mdk_eval_summary:" in combined
    # The scorecard dispatch must NOT have fired.
    assert len(calls) == 0


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
    fake_bundle.prompt_template = "You are a demo agent."

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
        '"instruction_following": {"score": 1.0, "rationale": "x"},'
        '"citation_accuracy": {"score": 1.0, "rationale": "x"}}' + "\n```"
    )
    fake_rt = mock.Mock()
    fake_rt.provider.complete = mock.AsyncMock(return_value=fake_response)
    fake_bundle = mock.Mock()
    fake_bundle.spec.model.provider = "openai/gpt-4o-mini-2024-07-18"
    fake_bundle.prompt_template = "..."

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
    fake_bundle.prompt_template = "..."

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
    fake_bundle.prompt_template = "..."

    scores, rationales = await _score_one_case(fake_rt, fake_bundle, {}, {})
    assert all(s == 0.0 for s in scores.values())
    assert "judge error" in rationales["accuracy"]


# ---------------------------------------------------------------------------
# --all mode: project-wide scorecard sweep
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_all_flag_and_positional_agent_are_mutex(tmp_path: Path) -> None:
    """``mdk eval-scorecard agents/x --all`` is ambiguous — pick one
    or the other. The error must surface before any LLM call fires."""
    # No agent dir needed — we error before loading anything.
    result = runner.invoke(
        app,
        ["eval-scorecard", "agents/anything", "--all"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "mutually exclusive" in combined


@pytest.mark.unit
def test_no_agent_and_no_all_errors_with_hint(tmp_path: Path) -> None:
    """Bare ``mdk eval-scorecard`` (no positional, no --all) must
    error with a hint, not crash. Operators new to the command
    should land on the help, not a stack trace.

    Uses --mock to skip the live-verify pre-flight — the test
    subject is the no-agent error path, not key verification."""
    result = runner.invoke(
        app,
        ["eval-scorecard", "--mock"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "agent path required" in combined or "--all" in combined


@pytest.mark.unit
def test_all_outside_project_errors_with_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--all`` from a directory with no ./agents/ subdir must
    error with a hint pointing at the project-init flow, not crash
    on a missing-dir traceback.

    Uses --mock to skip the live-verify pre-flight."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["eval-scorecard", "--all", "--mock"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "./agents/" in combined or "agents/" in combined


@pytest.mark.unit
def test_all_empty_agents_dir_vacuous_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A project with ./agents/ but zero agents under it is a
    vacuous-pass (ok=true, agents=0), not an error. Mirrors how
    ``mdk eval --all`` handles the same edge case.

    Uses --mock to skip the live-verify pre-flight."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "agents").mkdir()
    result = runner.invoke(
        app,
        ["eval-scorecard", "--all", "--mock"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0
    combined = result.stdout + result.stderr
    assert "agents=0" in combined
    assert "ok=true" in combined


def _scaffold_project_with_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *agent_templates: str
) -> Path:
    """Init a real project + add real agents via the CLI scaffolding.

    Hand-rolled agent.yaml fixtures hit validation failures (input
    schema, objectives, prompt path, etc. all required). Going through
    ``mdk init`` + ``mdk add`` produces bundles the loader actually
    accepts — same path the operator's project goes through.

    Also injects a fake ``OPENAI_API_KEY`` so the eval-scorecard
    pre-flight check passes without CI needing real keys. Stubs
    ``verify_provider_key`` so the stricter live-verify pre-flight
    (PR #223) doesn't reject the fake key — that strictness is what
    we want in production, but in tests the fake would always fail a
    real HTTP probe against OpenAI."""
    from movate.cli import auth as auth_mod  # noqa: PLC0415
    from movate.credentials.verify import VerifyResult  # noqa: PLC0415

    monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
    # Set BOTH keys so the stricter "BOTH required" pre-flight passes
    # (2026-05-19). The scorecard's cross-family judge enforcement
    # needs OpenAI + Anthropic both verified.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-test-key-for-precheck-only")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-test-key-for-precheck-only")
    monkeypatch.setattr(
        "movate.credentials.verify_provider_key",
        lambda provider, key: VerifyResult(ok=True, detail="OK (test stub)"),
    )
    # Clear the per-process verify cache so the stub wins over any
    # cached real-verify result from an earlier test in this session.
    auth_mod._verify_cache.clear()
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    proj = tmp_path / "proj"
    monkeypatch.chdir(proj)
    for template in agent_templates:
        result = runner.invoke(app, ["add", template], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.stdout + result.stderr
    return proj


@pytest.mark.unit
def test_all_runs_per_agent_and_renders_rollup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--all`` discovers every agent under ./agents/, runs the
    scorecard against each, and renders a per-agent table + a
    project-level rollup. Mock the scorecard internals so the test
    stays hermetic; verify the orchestration (per-agent invocation
    + rollup table + greppable summary)."""
    _scaffold_project_with_agents(tmp_path, monkeypatch, "faq", "summarizer")

    # Mock _run_scorecard to return a deterministic summary per agent.
    invocations: list[str] = []

    async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
        invocations.append(bundle.spec.name)
        return ScorecardSummary(
            agent=bundle.spec.name,
            mix=kwargs.get("mix", "standard"),
            count=kwargs.get("count", 10),
            cases=[],
            category_means=dict.fromkeys(ALL_CATEGORIES, 0.85),
            overall_mean=0.85,
        )

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

    result = runner.invoke(
        app,
        ["eval-scorecard", "--all", "--mix", "standard", "--count", "5"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    # Both agents were processed.
    assert sorted(invocations) == ["faq", "summarizer"]

    plain = _ANSI_RE.sub("", result.stdout)
    assert "── faq" in plain
    assert "── summarizer" in plain
    assert "Project scorecard" in plain
    assert "mdk_eval_scorecard_all_summary:" in plain
    assert "agents=2" in plain
    assert "succeeded=2" in plain
    assert "failed=0" in plain
    assert "ok=true" in plain


@pytest.mark.unit
def test_all_one_agent_failure_doesnt_abort_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If one agent's scorecard run blows up (network error, judge
    failure, etc.) the sweep must keep going for the OTHER agents.
    The rollup table surfaces the failure; the process exits 2 at
    the end."""
    _scaffold_project_with_agents(tmp_path, monkeypatch, "faq", "summarizer", "rag-qa")

    async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
        if bundle.spec.name == "summarizer":
            raise RuntimeError("simulated judge timeout")
        return ScorecardSummary(
            agent=bundle.spec.name,
            mix="standard",
            count=5,
            cases=[],
            category_means=dict.fromkeys(ALL_CATEGORIES, 0.85),
            overall_mean=0.85,
        )

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

    result = runner.invoke(
        app,
        ["eval-scorecard", "--all", "--mix", "standard"],
        env={"COLUMNS": "200"},
    )
    # One failure → exit 2, but the other two agents still ran +
    # their results surface in the rollup.
    assert result.exit_code == 2
    plain = _ANSI_RE.sub("", result.stdout + result.stderr)
    assert "── faq" in plain
    assert "── summarizer" in plain
    assert "── rag-qa" in plain
    # Per-agent failure annotation surfaces in the rollup.
    assert "RuntimeError" in plain or "scorecard failed" in plain
    # Summary line shows partial success.
    assert "succeeded=2" in plain
    assert "failed=1" in plain
    assert "ok=false" in plain


# ---------------------------------------------------------------------------
# --output json (Gap 3b)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_single_agent_json_output_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``mdk eval-scorecard <agent> --output json`` emits a single
    JSON document on stdout — no Rich table, no greppable summary
    line. Shape is stable + machine-readable for CI scrapers."""

    _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
    agent_dir = tmp_path / "proj" / "agents" / "faq"

    async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
        return ScorecardSummary(
            agent=bundle.spec.name,
            mix="standard",
            count=3,
            cases=[
                CaseScore(
                    input={"q": "test"},
                    output={"a": "ok"},
                    latency_ms=120.0,
                    cost_usd=0.0001,
                    scores=dict.fromkeys(ALL_CATEGORIES, 0.9),
                    rationales=dict.fromkeys(LLM_JUDGED_CATEGORIES, "looks fine"),
                )
            ],
            category_means=dict.fromkeys(ALL_CATEGORIES, 0.9),
            overall_mean=0.9,
        )

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

    result = runner.invoke(
        app,
        ["eval-scorecard", str(agent_dir), "--output", "json"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    # stdout should be parseable JSON, nothing else.
    parsed = _json.loads(result.stdout)
    assert parsed["agent"] == "faq"
    assert parsed["mix"] == "standard"
    assert parsed["count"] == 3
    assert parsed["overall_mean"] == 0.9
    assert set(parsed["category_means"].keys()) == set(ALL_CATEGORIES)
    assert len(parsed["cases"]) == 1
    case = parsed["cases"][0]
    assert case["input"] == {"q": "test"}
    assert case["output"] == {"a": "ok"}
    assert case["latency_ms"] == 120.0
    assert case["cost_usd"] == 0.0001
    assert set(case["scores"].keys()) == set(ALL_CATEGORIES)

    # JSON mode must NOT emit the Rich table or greppable line on
    # stdout (those are table-mode surfaces).
    assert "mdk_eval_scorecard_summary:" not in result.stdout
    assert "Generating" not in result.stdout  # status was suppressed


@pytest.mark.unit
def test_all_json_output_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``mdk eval-scorecard --all --output json`` emits a single
    project-level JSON document with per-agent summaries +
    project-level aggregates. The shape is stable for CI scrapers."""

    _scaffold_project_with_agents(tmp_path, monkeypatch, "faq", "summarizer")

    async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
        return ScorecardSummary(
            agent=bundle.spec.name,
            mix="standard",
            count=5,
            cases=[],
            category_means=dict.fromkeys(ALL_CATEGORIES, 0.8),
            overall_mean=0.8,
        )

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

    result = runner.invoke(
        app,
        ["eval-scorecard", "--all", "--output", "json"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    parsed = _json.loads(result.stdout)
    assert parsed["agents_total"] == 2
    assert parsed["succeeded"] == 2
    assert parsed["failed"] == 0
    assert parsed["mix"] == "standard"
    assert parsed["ok"] is True
    assert parsed["project_mean"] == 0.8
    assert len(parsed["summaries"]) == 2
    assert {s["agent"] for s in parsed["summaries"]} == {"faq", "summarizer"}
    assert parsed["failures"] == []

    # Table-mode surfaces must NOT appear on stdout in JSON mode.
    assert "Project scorecard" not in result.stdout
    assert "mdk_eval_scorecard_all_summary:" not in result.stdout


@pytest.mark.unit
def test_all_json_output_captures_per_agent_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When some agents fail in --all mode, the JSON document
    surfaces them under ``failures: [...]`` with the agent name +
    error type. ``ok: false`` and exit code 2 for CI gating."""

    _scaffold_project_with_agents(tmp_path, monkeypatch, "faq", "summarizer")

    async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
        if bundle.spec.name == "summarizer":
            raise RuntimeError("simulated judge timeout")
        return ScorecardSummary(
            agent=bundle.spec.name,
            mix="standard",
            count=5,
            cases=[],
            category_means=dict.fromkeys(ALL_CATEGORIES, 0.8),
            overall_mean=0.8,
        )

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

    result = runner.invoke(
        app,
        ["eval-scorecard", "--all", "--output", "json"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2

    parsed = _json.loads(result.stdout)
    assert parsed["succeeded"] == 1
    assert parsed["failed"] == 1
    assert parsed["ok"] is False
    assert len(parsed["failures"]) == 1
    assert parsed["failures"][0]["agent"] == "summarizer"
    assert parsed["failures"][0]["reason"] == "RuntimeError"


# ---------------------------------------------------------------------------
# Per-category gates (Gap 3c)
# ---------------------------------------------------------------------------

from movate.cli.eval_scorecard_cmd import GateConfig  # noqa: E402


@pytest.mark.unit
class TestGateConfig:
    def test_no_gates_set_means_has_any_gate_is_false(self) -> None:
        assert GateConfig().has_any_gate() is False

    def test_one_gate_set_means_has_any_gate_is_true(self) -> None:
        assert GateConfig(overall=0.7).has_any_gate() is True
        assert GateConfig(safety=1.0).has_any_gate() is True

    def test_check_returns_empty_when_no_gates_set(self) -> None:
        summary = ScorecardSummary(
            agent="x",
            mix="standard",
            count=1,
            cases=[],
            category_means=dict.fromkeys(ALL_CATEGORIES, 0.5),
            overall_mean=0.5,
        )
        assert GateConfig().check(summary) == []

    def test_check_returns_empty_when_all_gates_pass(self) -> None:
        summary = ScorecardSummary(
            agent="x",
            mix="standard",
            count=1,
            cases=[],
            category_means=dict.fromkeys(ALL_CATEGORIES, 0.95),
            overall_mean=0.95,
        )
        config = GateConfig(overall=0.7, accuracy=0.85, safety=0.9)
        assert config.check(summary) == []

    def test_check_returns_failures_for_categories_below_threshold(self) -> None:
        means = dict.fromkeys(ALL_CATEGORIES, 0.95)
        means["safety"] = 0.6  # well below the 0.9 floor
        means["accuracy"] = 0.7  # below the 0.85 floor
        summary = ScorecardSummary(
            agent="x",
            mix="standard",
            count=1,
            cases=[],
            category_means=means,
            overall_mean=0.9,
        )
        config = GateConfig(accuracy=0.85, safety=0.9, faithfulness=0.5)
        failures = config.check(summary)
        # Two failures (accuracy, safety); faithfulness=0.5 passes at 0.95.
        cats = {f[0] for f in failures}
        assert cats == {"accuracy", "safety"}

    def test_check_includes_overall_when_set(self) -> None:
        summary = ScorecardSummary(
            agent="x",
            mix="standard",
            count=1,
            cases=[],
            category_means=dict.fromkeys(ALL_CATEGORIES, 0.5),
            overall_mean=0.5,
        )
        config = GateConfig(overall=0.7)
        failures = config.check(summary)
        assert len(failures) == 1
        assert failures[0][0] == "overall"
        assert failures[0][1] == 0.5
        assert failures[0][2] == 0.7


@pytest.mark.unit
def test_gate_pass_keeps_exit_0(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A passing gate adds the green PASSED banner + exits 0. No
    state change vs running without gates beyond the new banner."""
    _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
    agent_dir = tmp_path / "proj" / "agents" / "faq"

    async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
        return ScorecardSummary(
            agent=bundle.spec.name,
            mix="standard",
            count=3,
            cases=[],
            category_means=dict.fromkeys(ALL_CATEGORIES, 0.95),
            overall_mean=0.95,
        )

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

    result = runner.invoke(
        app,
        ["eval-scorecard", str(agent_dir), "--gate-overall", "0.7", "--gate-safety", "0.9"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    plain = _ANSI_RE.sub("", result.stdout)
    assert "Gates PASSED" in plain
    assert "2 gate(s) set" in plain


@pytest.mark.unit
def test_gate_failure_exits_2_with_red_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing gate exits 2 + emits a red FAILED block listing each
    failing category with its actual score vs the threshold."""
    _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
    agent_dir = tmp_path / "proj" / "agents" / "faq"

    async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
        means = dict.fromkeys(ALL_CATEGORIES, 0.95)
        means["safety"] = 0.5  # < 0.9 gate
        return ScorecardSummary(
            agent=bundle.spec.name,
            mix="standard",
            count=3,
            cases=[],
            category_means=means,
            overall_mean=0.9,
        )

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

    result = runner.invoke(
        app,
        ["eval-scorecard", str(agent_dir), "--gate-safety", "0.9"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
    plain = _ANSI_RE.sub("", result.stdout)
    assert "Gates FAILED" in plain
    assert "safety:" in plain
    # The actual + threshold both appear in the failure line.
    assert "0.50" in plain
    assert "0.90" in plain


@pytest.mark.unit
def test_no_gates_set_skips_gate_block_entirely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no gates are set, the gate PASS/FAIL block must NOT
    render. Otherwise operators who don't care about gates would
    see a noisy "0 gates set, all passed" line on every run."""
    _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
    agent_dir = tmp_path / "proj" / "agents" / "faq"

    async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
        return ScorecardSummary(
            agent=bundle.spec.name,
            mix="standard",
            count=3,
            cases=[],
            category_means=dict.fromkeys(ALL_CATEGORIES, 0.5),
            overall_mean=0.5,
        )

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

    result = runner.invoke(
        app,
        ["eval-scorecard", str(agent_dir)],  # no gate flags
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0
    plain = _ANSI_RE.sub("", result.stdout)
    assert "Gates PASSED" not in plain
    assert "Gates FAILED" not in plain


@pytest.mark.unit
def test_json_output_includes_gate_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The JSON output must include the gate config, failures list,
    and gates_passed boolean — CI scrapers gate on these."""
    _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
    agent_dir = tmp_path / "proj" / "agents" / "faq"

    async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
        means = dict.fromkeys(ALL_CATEGORIES, 0.95)
        means["accuracy"] = 0.5  # < 0.8 gate
        return ScorecardSummary(
            agent=bundle.spec.name,
            mix="standard",
            count=3,
            cases=[],
            category_means=means,
            overall_mean=0.85,
        )

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

    result = runner.invoke(
        app,
        [
            "eval-scorecard",
            str(agent_dir),
            "--gate-accuracy",
            "0.8",
            "--output",
            "json",
        ],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2  # gate failed
    parsed = _json.loads(result.stdout)
    assert parsed["gates"]["accuracy"] == 0.8
    assert parsed["gates"]["safety"] is None  # unset
    assert parsed["gates_passed"] is False
    assert len(parsed["gate_failures"]) == 1
    failure = parsed["gate_failures"][0]
    assert failure["category"] == "accuracy"
    assert failure["actual"] == 0.5
    assert failure["threshold"] == 0.8


@pytest.mark.unit
def test_all_mode_per_agent_gates_aggregate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In --all mode, gates apply to each agent independently. If any
    agent fails any gate, exit 2. The rollup Gates column shows the
    pass/fail per agent."""
    _scaffold_project_with_agents(tmp_path, monkeypatch, "faq", "summarizer")

    async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
        means = dict.fromkeys(ALL_CATEGORIES, 0.95)
        if bundle.spec.name == "summarizer":
            means["safety"] = 0.4  # gate fails
        return ScorecardSummary(
            agent=bundle.spec.name,
            mix="standard",
            count=3,
            cases=[],
            category_means=means,
            overall_mean=0.9,
        )

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

    result = runner.invoke(
        app,
        ["eval-scorecard", "--all", "--gate-safety", "0.9"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2  # at least one agent fails a gate
    plain = _ANSI_RE.sub("", result.stdout)
    # Rollup includes a "Gates" column when gates are set.
    assert "Gates" in plain
    # summarizer failed; faq passed.
    assert "✓ passed" in plain or "passed" in plain
    assert "safety" in plain  # the failing category named in the row
    # Project-level summary reflects the gate failure.
    assert "gate_failures=1" in plain
    assert "ok=false" in plain


@pytest.mark.unit
def test_all_mode_json_includes_per_agent_gate_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--all -o json`` includes per-agent ``gate_failures`` +
    ``gates_passed`` inside each summary, plus a project-level
    ``agents_failing_gate`` list."""
    _scaffold_project_with_agents(tmp_path, monkeypatch, "faq", "summarizer")

    async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
        means = dict.fromkeys(ALL_CATEGORIES, 0.95)
        if bundle.spec.name == "summarizer":
            means["safety"] = 0.4
        return ScorecardSummary(
            agent=bundle.spec.name,
            mix="standard",
            count=3,
            cases=[],
            category_means=means,
            overall_mean=0.9,
        )

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

    result = runner.invoke(
        app,
        ["eval-scorecard", "--all", "--gate-safety", "0.9", "-o", "json"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
    parsed = _json.loads(result.stdout)
    assert parsed["ok"] is False
    assert parsed["agents_failing_gate"] == ["summarizer"]
    by_agent = {s["agent"]: s for s in parsed["summaries"]}
    assert by_agent["faq"]["gates_passed"] is True
    assert by_agent["faq"]["gate_failures"] == []
    assert by_agent["summarizer"]["gates_passed"] is False
    assert len(by_agent["summarizer"]["gate_failures"]) == 1
    assert by_agent["summarizer"]["gate_failures"][0]["category"] == "safety"


# ---------------------------------------------------------------------------
# Baseline + drift (Gap 3d)
# ---------------------------------------------------------------------------

from movate.cli.eval_scorecard_cmd import (  # noqa: E402
    _compute_drift,
    _load_baseline_means,
)


@pytest.mark.unit
class TestLoadBaselineMeans:
    def test_single_agent_shape(self, tmp_path: Path) -> None:
        """Reading a single-agent scorecard JSON returns the agent
        + its per-category means including ``overall``."""
        baseline = tmp_path / "baseline.json"
        baseline.write_text(
            _json.dumps(
                {
                    "agent": "faq",
                    "overall_mean": 0.9,
                    "category_means": {"accuracy": 0.95, "safety": 1.0},
                }
            )
        )
        result = _load_baseline_means(baseline)
        assert "faq" in result
        assert result["faq"]["accuracy"] == 0.95
        assert result["faq"]["safety"] == 1.0
        assert result["faq"]["overall"] == 0.9

    def test_all_mode_shape(self, tmp_path: Path) -> None:
        """``--all`` baselines have a ``summaries: [...]`` array of
        per-agent entries. Each agent's means are flattened into the
        return dict."""
        baseline = tmp_path / "baseline.json"
        baseline.write_text(
            _json.dumps(
                {
                    "summaries": [
                        {"agent": "alpha", "overall_mean": 0.85, "category_means": {"safety": 1.0}},
                        {"agent": "beta", "overall_mean": 0.7, "category_means": {"safety": 0.5}},
                    ],
                }
            )
        )
        result = _load_baseline_means(baseline)
        assert set(result.keys()) == {"alpha", "beta"}
        assert result["alpha"]["overall"] == 0.85
        assert result["beta"]["safety"] == 0.5

    def test_unreadable_returns_empty(self, tmp_path: Path) -> None:
        """Missing / unreadable baseline returns ``{}`` so the caller
        can warn + skip rather than crashing."""
        assert _load_baseline_means(tmp_path / "nonexistent.json") == {}

    def test_malformed_json_returns_empty(self, tmp_path: Path) -> None:
        """Corrupt JSON returns ``{}`` rather than raising."""
        baseline = tmp_path / "baseline.json"
        baseline.write_text("not valid json at all {")
        assert _load_baseline_means(baseline) == {}


@pytest.mark.unit
class TestComputeDrift:
    def _summary(self, **means: float) -> ScorecardSummary:
        full = {**dict.fromkeys(ALL_CATEGORIES, 0.8), **means}
        return ScorecardSummary(
            agent="x",
            mix="standard",
            count=1,
            cases=[],
            category_means=full,
            overall_mean=full.get("overall", 0.8),
        )

    def test_improvement_not_a_regression(self) -> None:
        """A score improvement is rendered green but never flags as a
        regression, even with tolerance=0."""
        current = self._summary(accuracy=0.95)
        baseline = {"accuracy": 0.7, "overall": 0.8}
        drifts = _compute_drift(current, baseline, tolerance=0.0)
        accuracy = next(d for d in drifts if d.category == "accuracy")
        assert accuracy.delta == pytest.approx(0.25)
        assert accuracy.is_regression is False

    def test_drop_within_tolerance_not_a_regression(self) -> None:
        """A drop SMALLER than the tolerance forgives the noise."""
        current = self._summary(accuracy=0.86)
        baseline = {"accuracy": 0.9, "overall": 0.8}
        drifts = _compute_drift(current, baseline, tolerance=0.05)
        accuracy = next(d for d in drifts if d.category == "accuracy")
        assert accuracy.delta == pytest.approx(-0.04)
        assert accuracy.is_regression is False  # |delta| < tolerance

    def test_drop_exceeding_tolerance_is_a_regression(self) -> None:
        """A drop LARGER than the tolerance is a regression."""
        current = self._summary(accuracy=0.7)
        baseline = {"accuracy": 0.9, "overall": 0.8}
        drifts = _compute_drift(current, baseline, tolerance=0.05)
        accuracy = next(d for d in drifts if d.category == "accuracy")
        assert accuracy.is_regression is True

    def test_categories_only_in_baseline_skipped(self) -> None:
        """The baseline might have categories the current run doesn't
        (e.g. if the rubric expanded). Those entries are skipped
        rather than producing phantom drift rows."""
        current = self._summary()
        baseline = {"made_up_category": 0.5, "overall": 0.8}
        drifts = _compute_drift(current, baseline, tolerance=0.0)
        # 'made_up_category' should NOT appear in the drift output.
        cats = {d.category for d in drifts}
        assert "made_up_category" not in cats
        # 'overall' should still be present (it's in the baseline).
        assert "overall" in cats


@pytest.mark.unit
def test_output_baseline_writes_json_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--output-baseline path`` writes the current scorecard JSON
    to that path so a future ``--baseline-file`` run can diff against it.
    Creates parent dirs as needed."""
    _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
    agent_dir = tmp_path / "proj" / "agents" / "faq"
    baseline_out = tmp_path / "proj" / ".movate" / "scorecards" / "faq.json"

    async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
        return ScorecardSummary(
            agent=bundle.spec.name,
            mix="standard",
            count=3,
            cases=[],
            category_means=dict.fromkeys(ALL_CATEGORIES, 0.9),
            overall_mean=0.9,
        )

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

    result = runner.invoke(
        app,
        ["eval-scorecard", str(agent_dir), "--output-baseline", str(baseline_out)],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert baseline_out.is_file()
    parsed = _json.loads(baseline_out.read_text())
    assert parsed["agent"] == "faq"
    assert parsed["overall_mean"] == 0.9
    assert set(parsed["category_means"].keys()) == set(ALL_CATEGORIES)


@pytest.mark.unit
def test_baseline_file_drift_no_regression_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When current scores match (or improve on) baseline, the drift
    table renders but exit code stays 0."""
    _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
    agent_dir = tmp_path / "proj" / "agents" / "faq"

    # Save a baseline with mean 0.7 across all categories.
    baseline_file = tmp_path / "baseline.json"
    baseline_file.write_text(
        _json.dumps(
            {
                "agent": "faq",
                "overall_mean": 0.7,
                "category_means": dict.fromkeys(ALL_CATEGORIES, 0.7),
            }
        )
    )

    async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
        # Current run scores HIGHER → improvement, not regression.
        return ScorecardSummary(
            agent=bundle.spec.name,
            mix="standard",
            count=3,
            cases=[],
            category_means=dict.fromkeys(ALL_CATEGORIES, 0.9),
            overall_mean=0.9,
        )

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

    result = runner.invoke(
        app,
        ["eval-scorecard", str(agent_dir), "--baseline-file", str(baseline_file)],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    plain = _ANSI_RE.sub("", result.stdout)
    assert "Drift vs baseline" in plain
    assert "No regressions" in plain


@pytest.mark.unit
def test_baseline_file_drift_with_regression_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A regression beyond tolerance exits 2 + renders the failure
    block. Default tolerance 0.0 means any drop counts."""
    _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
    agent_dir = tmp_path / "proj" / "agents" / "faq"

    baseline_file = tmp_path / "baseline.json"
    baseline_file.write_text(
        _json.dumps(
            {
                "agent": "faq",
                "overall_mean": 0.9,
                "category_means": dict.fromkeys(ALL_CATEGORIES, 0.9),
            }
        )
    )

    async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
        # Current is LOWER than baseline → regression.
        means = dict.fromkeys(ALL_CATEGORIES, 0.9)
        means["accuracy"] = 0.5  # 0.4 drop = regression
        return ScorecardSummary(
            agent=bundle.spec.name,
            mix="standard",
            count=3,
            cases=[],
            category_means=means,
            overall_mean=0.85,
        )

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

    result = runner.invoke(
        app,
        ["eval-scorecard", str(agent_dir), "--baseline-file", str(baseline_file)],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
    plain = _ANSI_RE.sub("", result.stdout)
    assert "Drift vs baseline" in plain
    assert "regression" in plain.lower()


@pytest.mark.unit
def test_baseline_file_tolerance_forgives_small_drops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--regression-tolerance 0.05`` forgives drops smaller than 5%
    so noisy LLM-judge sampling doesn't constantly trigger regressions."""
    _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
    agent_dir = tmp_path / "proj" / "agents" / "faq"

    baseline_file = tmp_path / "baseline.json"
    baseline_file.write_text(
        _json.dumps(
            {
                "agent": "faq",
                "overall_mean": 0.9,
                "category_means": dict.fromkeys(ALL_CATEGORIES, 0.9),
            }
        )
    )

    async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
        # Tiny 0.03 drop — within the 0.05 tolerance.
        means = dict.fromkeys(ALL_CATEGORIES, 0.87)
        return ScorecardSummary(
            agent=bundle.spec.name,
            mix="standard",
            count=3,
            cases=[],
            category_means=means,
            overall_mean=0.87,
        )

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

    result = runner.invoke(
        app,
        [
            "eval-scorecard",
            str(agent_dir),
            "--baseline-file",
            str(baseline_file),
            "--regression-tolerance",
            "0.05",
        ],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    plain = _ANSI_RE.sub("", result.stdout)
    assert "No regressions" in plain


@pytest.mark.unit
def test_baseline_missing_agent_warns_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the baseline file has no entry for the current agent
    (e.g. agent was added after baseline was committed), emit a
    yellow warning and proceed without drift comparison rather than
    erroring."""
    _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
    agent_dir = tmp_path / "proj" / "agents" / "faq"

    baseline_file = tmp_path / "baseline.json"
    baseline_file.write_text(
        _json.dumps(
            {
                "agent": "different-agent",  # baseline is for a different agent
                "overall_mean": 0.9,
                "category_means": dict.fromkeys(ALL_CATEGORIES, 0.9),
            }
        )
    )

    async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
        return ScorecardSummary(
            agent=bundle.spec.name,
            mix="standard",
            count=3,
            cases=[],
            category_means=dict.fromkeys(ALL_CATEGORIES, 0.5),
            overall_mean=0.5,
        )

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

    result = runner.invoke(
        app,
        ["eval-scorecard", str(agent_dir), "--baseline-file", str(baseline_file)],
        env={"COLUMNS": "200"},
    )
    # Exit 0 — no drift check happened, no regressions to flag.
    assert result.exit_code == 0
    combined = result.stdout + result.stderr
    assert "no entry for agent" in combined or "Skipping drift" in combined


@pytest.mark.unit
def test_baseline_round_trip_output_then_compare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: run with --output-baseline, then re-run with
    --baseline-file pointed at the same file. Same scores → no
    drift → exit 0."""
    _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
    agent_dir = tmp_path / "proj" / "agents" / "faq"
    baseline_path = tmp_path / "baseline.json"

    async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
        return ScorecardSummary(
            agent=bundle.spec.name,
            mix="standard",
            count=3,
            cases=[],
            category_means=dict.fromkeys(ALL_CATEGORIES, 0.8),
            overall_mean=0.8,
        )

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

    # Step 1: save baseline.
    result1 = runner.invoke(
        app,
        ["eval-scorecard", str(agent_dir), "--output-baseline", str(baseline_path)],
        env={"COLUMNS": "200"},
    )
    assert result1.exit_code == 0
    assert baseline_path.is_file()

    # Step 2: re-run with --baseline-file. Same scores → no regressions.
    result2 = runner.invoke(
        app,
        ["eval-scorecard", str(agent_dir), "--baseline-file", str(baseline_path)],
        env={"COLUMNS": "200"},
    )
    assert result2.exit_code == 0
    plain = _ANSI_RE.sub("", result2.stdout)
    assert "No regressions" in plain


@pytest.mark.unit
def test_all_mode_baseline_per_agent_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--all --baseline-file path`` does per-agent drift detection.
    Project exits 2 if any agent regresses."""
    _scaffold_project_with_agents(tmp_path, monkeypatch, "faq", "summarizer")

    baseline_file = tmp_path / "baseline.json"
    baseline_file.write_text(
        _json.dumps(
            {
                "summaries": [
                    {
                        "agent": "faq",
                        "overall_mean": 0.9,
                        "category_means": dict.fromkeys(ALL_CATEGORIES, 0.9),
                    },
                    {
                        "agent": "summarizer",
                        "overall_mean": 0.85,
                        "category_means": dict.fromkeys(ALL_CATEGORIES, 0.85),
                    },
                ],
            }
        )
    )

    async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
        if bundle.spec.name == "summarizer":
            # Regression for summarizer.
            return ScorecardSummary(
                agent=bundle.spec.name,
                mix="standard",
                count=3,
                cases=[],
                category_means=dict.fromkeys(ALL_CATEGORIES, 0.4),
                overall_mean=0.4,
            )
        # No regression for faq.
        return ScorecardSummary(
            agent=bundle.spec.name,
            mix="standard",
            count=3,
            cases=[],
            category_means=dict.fromkeys(ALL_CATEGORIES, 0.95),
            overall_mean=0.95,
        )

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

    result = runner.invoke(
        app,
        ["eval-scorecard", "--all", "--baseline-file", str(baseline_file)],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2  # summarizer regressed
    plain = _ANSI_RE.sub("", result.stdout)
    # Per-agent drift table for summarizer surfaces in the output.
    assert "drift for" in plain.lower() or "Drift vs baseline" in plain
    # Project summary reflects the regression count.
    assert "regressions=1" in plain
    assert "ok=false" in plain


# ---------------------------------------------------------------------------
# Per-project rubric overrides (Gap 3e)
# ---------------------------------------------------------------------------

from movate.cli.eval_scorecard_cmd import (  # noqa: E402
    EffectiveCategories,
    _build_judge_prompt,
    _resolve_effective_categories,
)


@pytest.mark.unit
class TestEffectiveCategories:
    def test_default_is_full_set(self) -> None:
        eff = _resolve_effective_categories()
        assert eff.llm_judged == LLM_JUDGED_CATEGORIES
        assert eff.programmatic == PROGRAMMATIC_CATEGORIES
        assert eff.all == ALL_CATEGORIES
        assert eff.is_default is True

    def test_disabling_an_llm_category_shrinks_llm_set(self) -> None:
        eff = _resolve_effective_categories(["refusal"])
        assert "refusal" not in eff.llm_judged
        assert "refusal" not in eff.all
        # Programmatic ones unaffected.
        assert eff.programmatic == PROGRAMMATIC_CATEGORIES
        assert eff.is_default is False

    def test_disabling_a_programmatic_category_shrinks_programmatic_set(self) -> None:
        eff = _resolve_effective_categories(["cost"])
        assert "cost" not in eff.programmatic
        assert "cost" not in eff.all
        assert eff.llm_judged == LLM_JUDGED_CATEGORIES
        assert eff.is_default is False

    def test_disabling_all_llm_categories_leaves_only_programmatic(self) -> None:
        eff = _resolve_effective_categories(list(LLM_JUDGED_CATEGORIES))
        assert eff.llm_judged == ()
        assert eff.programmatic == PROGRAMMATIC_CATEGORIES
        assert set(eff.all) == set(PROGRAMMATIC_CATEGORIES)

    def test_unknown_disabled_silently_ignored(self) -> None:
        """The CLI / project.yaml loader validates names — the helper
        is forgiving so a typo doesn't crash the run."""
        eff = _resolve_effective_categories(["not-a-real-category"])
        assert eff.is_default is True


@pytest.mark.unit
class TestBuildJudgePrompt:
    def test_full_set_matches_pre_built_default(self) -> None:
        """The dynamically-built prompt for the full default set must
        match the pre-built ``_JUDGE_SYSTEM_PROMPT`` constant."""
        from movate.cli.eval_scorecard_cmd import _JUDGE_SYSTEM_PROMPT  # noqa: PLC0415

        assert _build_judge_prompt(LLM_JUDGED_CATEGORIES) == _JUDGE_SYSTEM_PROMPT

    def test_disabled_categories_dont_appear_in_prompt(self) -> None:
        """When a category is disabled, the judge prompt must not
        mention it — otherwise the LLM might still try to score it
        (wasted tokens + confused parsing)."""
        prompt = _build_judge_prompt(
            tuple(c for c in LLM_JUDGED_CATEGORIES if c not in {"refusal", "hallucination"})
        )
        assert "refusal" not in prompt
        assert "hallucination" not in prompt
        # The remaining categories must still appear.
        assert "accuracy" in prompt
        assert "safety" in prompt

    def test_prompt_count_reflects_active_set(self) -> None:
        """Prompt says ``Score each of these N categories``; N should
        update with the active set."""
        prompt = _build_judge_prompt(("accuracy", "safety", "format"))
        assert "3 categories" in prompt

    def test_empty_active_returns_empty_prompt(self) -> None:
        """Edge case: every LLM category disabled. The prompt is empty
        and the caller is expected to skip the judge call."""
        assert _build_judge_prompt(()) == ""


@pytest.mark.unit
def test_cli_disable_category_filters_scorecard_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--disable-category refusal --disable-category hallucination``
    must produce a scorecard table that omits those categories +
    drops them from the summary line / JSON."""
    _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
    agent_dir = tmp_path / "proj" / "agents" / "faq"

    captured_effective: list[EffectiveCategories | None] = []

    async def fake_run_scorecard(
        bundle: Any, *, effective: EffectiveCategories | None = None, **kwargs: Any
    ) -> ScorecardSummary:
        captured_effective.append(effective)
        active = effective.all if effective else ALL_CATEGORIES
        return ScorecardSummary(
            agent=bundle.spec.name,
            mix="standard",
            count=3,
            cases=[],
            category_means=dict.fromkeys(active, 0.9),
            overall_mean=0.9,
        )

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

    result = runner.invoke(
        app,
        [
            "eval-scorecard",
            str(agent_dir),
            "--disable-category",
            "refusal",
            "--disable-category",
            "hallucination",
        ],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    plain = _ANSI_RE.sub("", result.stdout)
    # Flatten whitespace so Rich-wrapped title text doesn't trip the
    # substring check ("8/10\n    categories" → "8/10 categories").
    flat = " ".join(plain.split())

    # Title surfaces the reduced count. ``citation_accuracy`` added
    # 0.8.2.15 bumped the total from 10 to 11; this test disables
    # 2 (hallucination, refusal) so the rendered count is 9/11.
    assert "9/11 categories" in flat
    # Disabled categories don't render as table rows.
    body = plain.split("Category")[1].split("overall")[0] if "Category" in plain else plain
    assert "refusal" not in body
    assert "hallucination" not in body
    # The effective set passed to _run_scorecard reflects the disabled flags.
    assert len(captured_effective) == 1
    eff = captured_effective[0]
    assert eff is not None
    assert "refusal" not in eff.all
    assert "hallucination" not in eff.all


@pytest.mark.unit
def test_cli_rejects_unknown_disable_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo in ``--disable-category`` must error before any LLM
    call fires — operators learn the right name immediately."""
    _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
    agent_dir = tmp_path / "proj" / "agents" / "faq"

    result = runner.invoke(
        app,
        ["eval-scorecard", str(agent_dir), "--disable-category", "typo-not-real"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "unknown --disable-category" in combined or "typo-not-real" in combined


@pytest.mark.unit
def test_project_yaml_disabled_categories_take_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project that drops ``refusal`` in its project.yaml must have
    the scorecard run without scoring that category — no CLI flag
    needed."""
    proj = _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
    # Edit the scaffolded project.yaml to add scorecard overrides.
    project_yaml = proj / "project.yaml"
    existing = project_yaml.read_text()
    project_yaml.write_text(existing + "\nscorecard:\n  disabled_categories: [refusal]\n")

    agent_dir = proj / "agents" / "faq"
    captured_effective: list[EffectiveCategories | None] = []

    async def fake_run_scorecard(
        bundle: Any, *, effective: EffectiveCategories | None = None, **kwargs: Any
    ) -> ScorecardSummary:
        captured_effective.append(effective)
        active = effective.all if effective else ALL_CATEGORIES
        return ScorecardSummary(
            agent=bundle.spec.name,
            mix="standard",
            count=3,
            cases=[],
            category_means=dict.fromkeys(active, 0.9),
            overall_mean=0.9,
        )

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

    result = runner.invoke(
        app,
        ["eval-scorecard", str(agent_dir)],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert len(captured_effective) == 1
    eff = captured_effective[0]
    assert eff is not None
    assert "refusal" not in eff.all


@pytest.mark.unit
def test_cli_and_project_yaml_disabled_categories_union(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When both project.yaml and CLI disable categories, the union
    applies — neither overrides the other. Operators use the CLI
    flag for one-off skips on top of the project-level baseline."""
    proj = _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
    project_yaml = proj / "project.yaml"
    project_yaml.write_text(
        project_yaml.read_text() + "\nscorecard:\n  disabled_categories: [refusal]\n"
    )

    agent_dir = proj / "agents" / "faq"
    captured_effective: list[EffectiveCategories | None] = []

    async def fake_run_scorecard(
        bundle: Any, *, effective: EffectiveCategories | None = None, **kwargs: Any
    ) -> ScorecardSummary:
        captured_effective.append(effective)
        active = effective.all if effective else ALL_CATEGORIES
        return ScorecardSummary(
            agent=bundle.spec.name,
            mix="standard",
            count=3,
            cases=[],
            category_means=dict.fromkeys(active, 0.9),
            overall_mean=0.9,
        )

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

    result = runner.invoke(
        app,
        [
            "eval-scorecard",
            str(agent_dir),
            "--disable-category",
            "hallucination",  # CLI adds to project.yaml's [refusal]
        ],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    eff = captured_effective[0]
    assert eff is not None
    assert "refusal" not in eff.all  # from project.yaml
    assert "hallucination" not in eff.all  # from CLI


@pytest.mark.unit
def test_gates_for_disabled_categories_silently_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If an operator sets a gate on a category that's been disabled
    (e.g. ``--gate-refusal 0.9 --disable-category refusal``), the
    gate must silently skip rather than failing with 0 < 0.9. This
    is the practical case where a project.yaml disables a category
    but a stale CI invocation still has the gate flag set."""
    _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
    agent_dir = tmp_path / "proj" / "agents" / "faq"

    async def fake_run_scorecard(
        bundle: Any, *, effective: EffectiveCategories | None = None, **kwargs: Any
    ) -> ScorecardSummary:
        active = effective.all if effective else ALL_CATEGORIES
        return ScorecardSummary(
            agent=bundle.spec.name,
            mix="standard",
            count=3,
            cases=[],
            category_means=dict.fromkeys(active, 0.95),
            overall_mean=0.95,
        )

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

    result = runner.invoke(
        app,
        [
            "eval-scorecard",
            str(agent_dir),
            "--disable-category",
            "refusal",
            "--gate-refusal",
            "0.9",  # would fail if not skipped
        ],
        env={"COLUMNS": "200"},
    )
    # Should pass — the disabled category's gate is skipped.
    assert result.exit_code == 0, result.stdout + result.stderr


@pytest.mark.unit
class TestScorecardConfigValidation:
    def test_project_yaml_unknown_category_rejected_at_load_time(self) -> None:
        """A typo in ``scorecard.disabled_categories`` must error at
        project.yaml load time — not silently disable nothing at
        scorecard runtime. Catches misconfiguration early."""
        from movate.core.config import ProjectConfig  # noqa: PLC0415

        with pytest.raises(Exception, match="unknown scorecard categories"):
            ProjectConfig(scorecard={"disabled_categories": ["typo-not-real"]})

    def test_project_yaml_default_is_empty(self) -> None:
        """Absent ``scorecard:`` block = empty disabled list = full
        default rubric. Backwards-compatible with pre-Gap-3e projects."""
        from movate.core.config import ProjectConfig  # noqa: PLC0415

        assert ProjectConfig().scorecard.disabled_categories == []


# ---------------------------------------------------------------------------
# Pre-flight generator-auth probe
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.no_preflight_stub
class TestPreflight:
    """``_preflight_check_generator_auth`` makes one tiny LLM ping per
    unique model BEFORE the sweep — so an unset API key fails fast
    with a single hint, instead of flooding 200 duplicate warnings
    for ``10 agents x 10 cases x 2 retries``."""

    @pytest.mark.asyncio
    async def test_preflight_skipped_under_mock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``mock=True`` short-circuits — no runtime is built, no
        provider call. The mock provider doesn't need keys."""

        built: list[bool] = []

        async def fake_build(*, mock: bool) -> Any:
            built.append(mock)
            raise RuntimeError("should never be called under mock=True")

        monkeypatch.setattr(eval_scorecard_cmd, "build_local_runtime", fake_build)

        # Should return cleanly without touching the runtime builder.
        await eval_scorecard_cmd._preflight_check_generator_auth(
            models={"openai/gpt-4o-mini-2024-07-18"}, mock=True
        )
        assert built == []

    @pytest.mark.asyncio
    async def test_preflight_skipped_with_empty_models(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No models = nothing to probe. Common when ``--all`` runs on
        an agents/ dir where every bundle failed to load — we don't
        want to invent a probe model."""

        built: list[bool] = []

        async def fake_build(*, mock: bool) -> Any:
            built.append(mock)
            raise RuntimeError("should never be called for empty models")

        monkeypatch.setattr(eval_scorecard_cmd, "build_local_runtime", fake_build)

        await eval_scorecard_cmd._preflight_check_generator_auth(models=set(), mock=False)
        assert built == []

    @pytest.mark.asyncio
    async def test_preflight_fails_fast_on_auth_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``AuthError`` from the provider during probe → raises
        ``_PreflightAuthError`` (carrying model + message) so the
        caller can choose between retry and exit. The probe stops
        at the first auth failure rather than continuing through
        other models.

        Pre-2026-05-19, the preflight called ``typer.Exit(2)`` directly
        from inside; now the caller decides (the auto-retry path
        wraps this in ``_preflight_with_retry`` to handle stale
        keys like ``sk-test-*2345``)."""
        probed: list[str] = []

        class _StubProvider:
            async def complete(self, request: Any) -> Any:
                probed.append(request.provider)
                raise AuthError("litellm.AuthenticationError: Missing Anthropic API Key")

        class _StubStorage:
            async def close(self) -> None:
                pass

        class _StubTracer:
            pass

        class _StubRuntime:
            provider = _StubProvider()
            storage = _StubStorage()
            tracer = _StubTracer()

        async def fake_build(*, mock: bool) -> Any:
            return _StubRuntime()

        async def fake_shutdown(storage: Any, tracer: Any) -> None:
            return None

        monkeypatch.setattr(eval_scorecard_cmd, "build_local_runtime", fake_build)
        monkeypatch.setattr(eval_scorecard_cmd, "shutdown_runtime", fake_shutdown)

        with pytest.raises(eval_scorecard_cmd._PreflightAuthError) as excinfo:
            await eval_scorecard_cmd._preflight_check_generator_auth(
                models={"anthropic/claude-haiku-4-5-20251001"}, mock=False
            )
        # Exception carries the model + message so the retry wrapper
        # can decide whether to exclude and re-resolve.
        assert excinfo.value.model == "anthropic/claude-haiku-4-5-20251001"
        assert "Missing Anthropic API Key" in excinfo.value.message
        # Probe ran exactly once before bailing.
        assert probed == ["anthropic/claude-haiku-4-5-20251001"]

    @pytest.mark.asyncio
    async def test_preflight_tolerates_non_auth_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: Any,
    ) -> None:
        """Transient network error / rate limit during probe → warn
        and return, NOT exit. The real run may still succeed; we
        don't want a flaky probe blocking work that would have
        completed."""

        class _StubProvider:
            async def complete(self, request: Any) -> Any:
                raise TimeoutError("connection reset")

        class _StubStorage:
            async def close(self) -> None:
                pass

        class _StubRuntime:
            provider = _StubProvider()
            storage = _StubStorage()
            tracer = None

        async def fake_build(*, mock: bool) -> Any:
            return _StubRuntime()

        async def fake_shutdown(storage: Any, tracer: Any) -> None:
            return None

        monkeypatch.setattr(eval_scorecard_cmd, "build_local_runtime", fake_build)
        monkeypatch.setattr(eval_scorecard_cmd, "shutdown_runtime", fake_shutdown)

        with caplog.at_level(logging.WARNING, logger="movate.cli.eval_scorecard_cmd"):
            # Must NOT raise — non-auth failures are non-fatal.
            await eval_scorecard_cmd._preflight_check_generator_auth(
                models={"openai/gpt-4o-mini-2024-07-18"}, mock=False
            )
        joined = " ".join(r.message for r in caplog.records)
        assert "preflight probe for openai/gpt-4o-mini-2024-07-18" in joined
        assert "TimeoutError" in joined

    @pytest.mark.asyncio
    async def test_preflight_probes_each_unique_model_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--all`` collects unique providers across agents. If a
        project has 5 agents on openai/... and 5 on anthropic/...,
        the probe should hit each provider EXACTLY ONCE — not 10
        times."""

        probed: list[str] = []

        class _StubProvider:
            async def complete(self, request: Any) -> Any:
                probed.append(request.provider)

                class _Resp:
                    text = ""
                    tokens = None

                return _Resp()

        class _StubStorage:
            async def close(self) -> None:
                pass

        class _StubRuntime:
            provider = _StubProvider()
            storage = _StubStorage()
            tracer = None

        async def fake_build(*, mock: bool) -> Any:
            return _StubRuntime()

        async def fake_shutdown(storage: Any, tracer: Any) -> None:
            return None

        monkeypatch.setattr(eval_scorecard_cmd, "build_local_runtime", fake_build)
        monkeypatch.setattr(eval_scorecard_cmd, "shutdown_runtime", fake_shutdown)

        await eval_scorecard_cmd._preflight_check_generator_auth(
            models={
                "openai/gpt-4o-mini-2024-07-18",
                "anthropic/claude-haiku-4-5-20251001",
            },
            mock=False,
        )
        # Exactly one call per unique model; order is deterministic
        # (sorted) so the assertion is stable.
        assert probed == [
            "anthropic/claude-haiku-4-5-20251001",
            "openai/gpt-4o-mini-2024-07-18",
        ]

    @pytest.mark.asyncio
    async def test_preflight_probe_uses_max_tokens_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The probe must be MINIMAL — one token, low temperature —
        so it costs ~nothing and finishes in <1s. A heavier probe
        would defeat the "fail fast" goal."""

        seen_params: list[dict[str, Any]] = []
        seen_messages: list[list[Any]] = []

        class _StubProvider:
            async def complete(self, request: Any) -> Any:
                seen_params.append(request.params)
                seen_messages.append(request.messages)

                class _Resp:
                    text = ""
                    tokens = None

                return _Resp()

        class _StubStorage:
            async def close(self) -> None:
                pass

        class _StubRuntime:
            provider = _StubProvider()
            storage = _StubStorage()
            tracer = None

        async def fake_build(*, mock: bool) -> Any:
            return _StubRuntime()

        async def fake_shutdown(storage: Any, tracer: Any) -> None:
            return None

        monkeypatch.setattr(eval_scorecard_cmd, "build_local_runtime", fake_build)
        monkeypatch.setattr(eval_scorecard_cmd, "shutdown_runtime", fake_shutdown)

        await eval_scorecard_cmd._preflight_check_generator_auth(
            models={"openai/gpt-4o-mini-2024-07-18"}, mock=False
        )
        assert seen_params[0]["max_tokens"] == 1
        assert seen_params[0]["temperature"] == 0.0
        # Single short user message — no system prompt overhead.
        assert len(seen_messages[0]) == 1


@pytest.mark.unit
class TestPreflightIntegration:
    """Integration tests: preflight is wired into both orchestrators
    (single-agent + ``--all``) and fires BEFORE any scoring work."""

    def test_single_agent_runs_preflight_with_bundle_provider(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No ``--generator-model``: the preflight uses the agent's
        own declared provider."""
        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")

        preflight_calls: list[dict[str, Any]] = []

        async def fake_preflight(*, models: set[str], mock: bool) -> None:
            preflight_calls.append({"models": models, "mock": mock})

        async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
            return ScorecardSummary(
                agent=bundle.spec.name,
                mix="standard",
                count=3,
                cases=[],
                category_means=dict.fromkeys(ALL_CATEGORIES, 0.85),
                overall_mean=0.85,
            )

        monkeypatch.setattr(
            "movate.cli.eval_scorecard_cmd._preflight_check_generator_auth",
            fake_preflight,
        )
        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

        agent_dir = tmp_path / "proj" / "agents" / "faq"
        result = runner.invoke(
            app,
            ["eval-scorecard", str(agent_dir), "--count", "3"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert len(preflight_calls) == 1
        # The faq bundle has SOME declared provider — we don't care
        # which one (templates evolve), just that exactly one model
        # was probed and it's not ``None``.
        models = preflight_calls[0]["models"]
        assert len(models) == 1
        assert next(iter(models))  # non-empty string
        assert preflight_calls[0]["mock"] is False

    def test_single_agent_runs_preflight_with_generator_model_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--generator-model X`` makes the preflight probe X, NOT
        the agent's declared provider — since X is what generation
        will actually use."""
        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")

        preflight_calls: list[dict[str, Any]] = []

        async def fake_preflight(*, models: set[str], mock: bool) -> None:
            preflight_calls.append({"models": models, "mock": mock})

        async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
            return ScorecardSummary(
                agent=bundle.spec.name,
                mix="standard",
                count=3,
                cases=[],
                category_means=dict.fromkeys(ALL_CATEGORIES, 0.85),
                overall_mean=0.85,
            )

        monkeypatch.setattr(
            "movate.cli.eval_scorecard_cmd._preflight_check_generator_auth",
            fake_preflight,
        )
        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

        agent_dir = tmp_path / "proj" / "agents" / "faq"
        result = runner.invoke(
            app,
            [
                "eval-scorecard",
                str(agent_dir),
                "--count",
                "3",
                "--generator-model",
                "anthropic/claude-haiku-4-5-20251001",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert preflight_calls == [
            {"models": {"anthropic/claude-haiku-4-5-20251001"}, "mock": False}
        ]

    def test_single_agent_skips_preflight_under_mock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--mock`` short-circuits the entire resolution + preflight
        path before any LLM call would fire. Pin this: the inner
        ``_preflight_check_generator_auth`` is NEVER reached under
        mock (the auto-retry wrapper bails first), AND the run
        completes successfully."""
        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")

        preflight_calls: list[dict[str, Any]] = []

        async def fake_preflight(*, models: set[str], mock: bool) -> None:
            preflight_calls.append({"models": models, "mock": mock})

        async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
            return ScorecardSummary(
                agent=bundle.spec.name,
                mix="standard",
                count=3,
                cases=[],
                category_means=dict.fromkeys(ALL_CATEGORIES, 0.85),
                overall_mean=0.85,
            )

        monkeypatch.setattr(
            "movate.cli.eval_scorecard_cmd._preflight_check_generator_auth",
            fake_preflight,
        )
        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

        agent_dir = tmp_path / "proj" / "agents" / "faq"
        result = runner.invoke(
            app,
            ["eval-scorecard", str(agent_dir), "--count", "3", "--mock"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # Under --mock, ``_preflight_with_retry`` returns {} without
        # ever calling the inner preflight. No probe fires.
        assert preflight_calls == []

    def test_all_runs_preflight_with_generator_model_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In ``--all`` mode with ``--generator-model X``, the
        preflight probes ONLY X (uniform override) — not each agent's
        declared provider."""
        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq", "summarizer")

        preflight_calls: list[dict[str, Any]] = []

        async def fake_preflight(*, models: set[str], mock: bool) -> None:
            preflight_calls.append({"models": models, "mock": mock})

        async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
            return ScorecardSummary(
                agent=bundle.spec.name,
                mix="standard",
                count=3,
                cases=[],
                category_means=dict.fromkeys(ALL_CATEGORIES, 0.85),
                overall_mean=0.85,
            )

        monkeypatch.setattr(
            "movate.cli.eval_scorecard_cmd._preflight_check_generator_auth",
            fake_preflight,
        )
        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

        result = runner.invoke(
            app,
            [
                "eval-scorecard",
                "--all",
                "--count",
                "3",
                "--generator-model",
                "anthropic/claude-haiku-4-5-20251001",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert preflight_calls == [
            {"models": {"anthropic/claude-haiku-4-5-20251001"}, "mock": False}
        ]

    def test_all_preflight_failure_aborts_before_any_agent_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the preflight raises ``typer.Exit(2)`` (auth fail), the
        sweep MUST NOT proceed to any agent. Otherwise we'd still
        burn 10 x 10 calls before exiting - defeating the whole
        purpose of the preflight."""
        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq", "summarizer")

        run_scorecard_calls: list[str] = []

        async def fake_preflight(*, models: set[str], mock: bool) -> None:

            raise typer.Exit(code=2)

        async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
            run_scorecard_calls.append(bundle.spec.name)
            return ScorecardSummary(
                agent=bundle.spec.name,
                mix="standard",
                count=0,
                cases=[],
                category_means={},
                overall_mean=0.0,
            )

        monkeypatch.setattr(
            "movate.cli.eval_scorecard_cmd._preflight_check_generator_auth",
            fake_preflight,
        )
        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

        result = runner.invoke(
            app,
            ["eval-scorecard", "--all", "--count", "10"],
            env={"COLUMNS": "200"},
        )
        # Exited via the preflight, not via the rollup's fail path.
        assert result.exit_code == 2
        # No agent's scorecard ran — preflight aborted upfront.
        assert run_scorecard_calls == []


# ---------------------------------------------------------------------------
# Single-asyncio.run-per-invocation regression guard (2026-05-18 bug)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOneEventLoopPerInvocation:
    """LiteLLM's module-level ``LoggingWorker`` holds an ``asyncio.Queue``
    that binds to whichever event loop first touches it. Splitting the
    scorecard flow across multiple ``asyncio.run`` calls (preflight +
    one-per-agent in the old shape) closed that loop under LiteLLM's
    feet and surfaced as ``RuntimeError: Queue is bound to a different
    event loop`` on every subsequent ``acompletion``.

    These tests pin the invariant: ONE ``asyncio.run`` per top-level
    CLI invocation. If someone reintroduces a per-agent or
    preflight-then-loop pattern, these tests fail before LiteLLM
    crashes in production.
    """

    def test_all_sweep_uses_exactly_one_asyncio_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--all`` against N agents must call ``asyncio.run`` exactly
        once total (NOT once per agent + once for preflight)."""
        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq", "summarizer", "rag-qa")

        run_calls: list[Any] = []
        real_run = asyncio.run

        def counting_run(coro: Any, **kwargs: Any) -> Any:
            run_calls.append(coro)
            return real_run(coro, **kwargs)

        async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
            return ScorecardSummary(
                agent=bundle.spec.name,
                mix="standard",
                count=3,
                cases=[],
                category_means=dict.fromkeys(ALL_CATEGORIES, 0.85),
                overall_mean=0.85,
            )

        monkeypatch.setattr(eval_scorecard_cmd.asyncio, "run", counting_run)
        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

        result = runner.invoke(
            app,
            ["eval-scorecard", "--all", "--count", "3"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # 3 agents — would have been 4 calls in the pre-fix shape
        # (1 preflight + 3 per-agent). Now exactly 1.
        assert len(run_calls) == 1, (
            f"Expected exactly 1 asyncio.run call for --all sweep, "
            f"got {len(run_calls)}. Each extra call risks stranding "
            f"LiteLLM's LoggingWorker queue in a closed event loop."
        )

    def test_single_agent_uses_exactly_one_asyncio_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Single-agent path must call ``asyncio.run`` exactly once
        (NOT once for preflight + once for the scorecard run)."""
        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")

        run_calls: list[Any] = []
        real_run = asyncio.run

        def counting_run(coro: Any, **kwargs: Any) -> Any:
            run_calls.append(coro)
            return real_run(coro, **kwargs)

        async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
            return ScorecardSummary(
                agent=bundle.spec.name,
                mix="standard",
                count=3,
                cases=[],
                category_means=dict.fromkeys(ALL_CATEGORIES, 0.85),
                overall_mean=0.85,
            )

        monkeypatch.setattr(eval_scorecard_cmd.asyncio, "run", counting_run)
        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

        agent_dir = tmp_path / "proj" / "agents" / "faq"
        result = runner.invoke(
            app,
            ["eval-scorecard", str(agent_dir), "--count", "3"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # Was 2 in the pre-fix shape (preflight + scorecard). Now 1.
        assert len(run_calls) == 1, (
            f"Expected exactly 1 asyncio.run call for single-agent path, "
            f"got {len(run_calls)}. Each extra call risks stranding "
            f"LiteLLM's LoggingWorker queue in a closed event loop."
        )

    def test_sweep_preflight_and_scorecard_share_event_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The preflight probe and every per-agent scorecard run must
        execute in the SAME event loop. Captures the loop id at each
        await point and asserts they all match."""
        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq", "summarizer")

        loop_ids: list[int] = []

        async def fake_preflight(*, models: set[str], mock: bool) -> None:
            loop_ids.append(id(asyncio.get_running_loop()))

        async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
            loop_ids.append(id(asyncio.get_running_loop()))
            return ScorecardSummary(
                agent=bundle.spec.name,
                mix="standard",
                count=3,
                cases=[],
                category_means=dict.fromkeys(ALL_CATEGORIES, 0.85),
                overall_mean=0.85,
            )

        monkeypatch.setattr(
            "movate.cli.eval_scorecard_cmd._preflight_check_generator_auth",
            fake_preflight,
        )
        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

        result = runner.invoke(
            app,
            ["eval-scorecard", "--all", "--count", "3"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # 1 preflight + 2 agents = 3 calls, ALL on the same loop.
        assert len(loop_ids) == 3
        assert len(set(loop_ids)) == 1, (
            f"Preflight + per-agent runs landed in different event loops: "
            f"{loop_ids}. LiteLLM's LoggingWorker would crash on the "
            f"second call in production."
        )


# ---------------------------------------------------------------------------
# Generator-model auto-detect
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGeneratorModelAutoDetect:
    """``_resolve_generator_model`` picks the right model based on which
    keys the operator has set. Precedence (highest wins):

    1. ``--generator-model FLAG`` (explicit choice)
    2. Agent's declared provider IF its key is set
    3. First fallback provider with a key set
    4. Declared provider (preflight will fail with hint)
    """

    def test_explicit_flag_always_wins_even_with_missing_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the operator passed --generator-model, never override.
        Even if FLAG's own key is missing, that's the operator's
        choice — preflight will catch and report."""
        # Force every key to "unset" so auto-detect would otherwise
        # bail to precedence 4. Explicit flag must still survive.
        monkeypatch.setattr(eval_scorecard_cmd, "_provider_has_key", lambda p: False)

        model, note = eval_scorecard_cmd._resolve_generator_model(
            "openai/gpt-4o-mini-2024-07-18",
            "anthropic/claude-haiku-4-5-20251001",
        )
        assert model == "anthropic/claude-haiku-4-5-20251001"
        assert note is None  # No fallback happened.

    def test_declared_provider_used_when_its_key_is_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Happy path: operator's agent.yaml declares openai/... and
        they have OPENAI_API_KEY. Use the declared model verbatim."""
        monkeypatch.setattr(
            eval_scorecard_cmd,
            "_provider_has_key",
            lambda p: p == "openai",
        )

        model, note = eval_scorecard_cmd._resolve_generator_model(
            "openai/gpt-4o-mini-2024-07-18", None
        )
        assert model == "openai/gpt-4o-mini-2024-07-18"
        assert note is None

    def test_falls_back_to_anthropic_when_declared_openai_missing_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Agent declares openai/... but operator only has
        ANTHROPIC_API_KEY. Auto-detect routes through Anthropic and
        returns a note explaining the swap."""
        monkeypatch.setattr(
            eval_scorecard_cmd,
            "_provider_has_key",
            lambda p: p == "anthropic",
        )

        model, note = eval_scorecard_cmd._resolve_generator_model(
            "openai/gpt-4o-mini-2024-07-18", None
        )
        assert model == "anthropic/claude-haiku-4-5-20251001"
        assert note is not None
        # Note explains what happened + how to suppress the auto-route.
        plain = _ANSI_RE.sub("", note)
        assert "openai/gpt-4o-mini-2024-07-18" in plain
        assert "anthropic/claude-haiku-4-5-20251001" in plain
        assert "OPENAI_API_KEY" in plain
        assert "--generator-model" in plain

    def test_fallback_priority_anthropic_before_openai_before_gemini(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pin the priority order — Anthropic first, then OpenAI,
        then Gemini. Changing this is a UX change worth a deliberate
        decision, not an accidental refactor."""
        # Declared = azure (not in fallback list) so all 3 fallbacks
        # are candidates. Toggle keys one at a time + assert which
        # one wins.
        available: set[str] = set()
        monkeypatch.setattr(eval_scorecard_cmd, "_provider_has_key", lambda p: p in available)

        # Only Gemini available → use Gemini.
        available = {"gemini"}
        model, _ = eval_scorecard_cmd._resolve_generator_model("azure/gpt-4", None)
        assert model == "gemini/gemini-2.5-flash"

        # OpenAI + Gemini → OpenAI wins (higher priority).
        available = {"openai", "gemini"}
        model, _ = eval_scorecard_cmd._resolve_generator_model("azure/gpt-4", None)
        assert model == "openai/gpt-4o-mini-2024-07-18"

        # All 3 → Anthropic wins.
        available = {"anthropic", "openai", "gemini"}
        model, _ = eval_scorecard_cmd._resolve_generator_model("azure/gpt-4", None)
        assert model == "anthropic/claude-haiku-4-5-20251001"

    def test_no_keys_returns_declared_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Nothing configured anywhere → return the declared model
        so the preflight catches it with the hint message. We don't
        invent a fake fallback that would later fail anyway."""
        monkeypatch.setattr(eval_scorecard_cmd, "_provider_has_key", lambda p: False)

        model, note = eval_scorecard_cmd._resolve_generator_model(
            "openai/gpt-4o-mini-2024-07-18", None
        )
        assert model == "openai/gpt-4o-mini-2024-07-18"
        assert note is None

    def test_declared_provider_not_re_probed_as_its_own_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If declared is anthropic/... and the anthropic key is
        missing, auto-detect should skip anthropic in the fallback
        list and go straight to OpenAI / Gemini."""
        monkeypatch.setattr(eval_scorecard_cmd, "_provider_has_key", lambda p: p == "openai")

        model, note = eval_scorecard_cmd._resolve_generator_model("anthropic/claude-3-opus", None)
        assert model == "openai/gpt-4o-mini-2024-07-18"
        assert note is not None
        plain = _ANSI_RE.sub("", note)
        assert "anthropic/claude-3-opus" in plain  # declared, mentioned
        assert "openai/gpt-4o-mini-2024-07-18" in plain  # fallback

    def test_bare_provider_string_handled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Some agent.yaml files declare a bare runtime name without
        a slash. Don't crash on the missing slash."""
        monkeypatch.setattr(eval_scorecard_cmd, "_provider_has_key", lambda p: p == "openai")

        model, note = eval_scorecard_cmd._resolve_generator_model("openai", None)
        assert model == "openai"
        assert note is None

    def test_provider_has_key_returns_false_for_unknown_provider(self) -> None:
        """Unknown provider prefixes must return False, NOT raise."""
        # "made-up-provider" isn't in _PROVIDER_TO_ENV_VAR. Should
        # not raise KeyError, should return False.
        assert eval_scorecard_cmd._provider_has_key("not-a-real-provider") is False


@pytest.mark.unit
class TestGeneratorAutoDetectIntegration:
    """End-to-end: the orchestrators emit the fallback note + route
    through the resolved provider when the operator's key state
    triggers auto-detect."""

    def test_single_agent_emits_fallback_note_when_auto_routing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stub ``_provider_has_key`` so only anthropic returns True;
        invoke single-agent scorecard with no --generator-model;
        verify the fallback note hits stderr + the resolved model
        propagates to ``_run_scorecard``."""
        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")

        monkeypatch.setattr(eval_scorecard_cmd, "_provider_has_key", lambda p: p == "anthropic")

        captured_models: list[str | None] = []

        async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
            captured_models.append(kwargs.get("generator_model"))
            return ScorecardSummary(
                agent=bundle.spec.name,
                mix="standard",
                count=3,
                cases=[],
                category_means=dict.fromkeys(ALL_CATEGORIES, 0.85),
                overall_mean=0.85,
            )

        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

        agent_dir = tmp_path / "proj" / "agents" / "faq"
        result = runner.invoke(
            app,
            ["eval-scorecard", str(agent_dir), "--count", "3"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr

        # The resolved generator_model passed to _run_scorecard should
        # be the anthropic fallback (the faq template declares
        # openai/... and only anthropic has a key per our stub).
        assert captured_models == ["anthropic/claude-haiku-4-5-20251001"]

        # Fallback note hit stderr.
        stderr_plain = _ANSI_RE.sub("", result.stderr)
        assert "routing generation through" in stderr_plain
        assert "anthropic/claude-haiku-4-5-20251001" in stderr_plain

    def test_single_agent_no_fallback_note_when_explicit_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--generator-model X`` bypasses auto-detect entirely —
        no fallback note printed, X used verbatim."""
        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")

        # No keys set anywhere — would normally trigger auto-detect.
        monkeypatch.setattr(eval_scorecard_cmd, "_provider_has_key", lambda p: False)

        captured_models: list[str | None] = []

        async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
            captured_models.append(kwargs.get("generator_model"))
            return ScorecardSummary(
                agent=bundle.spec.name,
                mix="standard",
                count=3,
                cases=[],
                category_means=dict.fromkeys(ALL_CATEGORIES, 0.85),
                overall_mean=0.85,
            )

        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

        agent_dir = tmp_path / "proj" / "agents" / "faq"
        result = runner.invoke(
            app,
            [
                "eval-scorecard",
                str(agent_dir),
                "--count",
                "3",
                "--generator-model",
                "openai/gpt-4o-mini-2024-07-18",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert captured_models == ["openai/gpt-4o-mini-2024-07-18"]
        # No "routing generation through" line — explicit flag wins.
        stderr_plain = _ANSI_RE.sub("", result.stderr)
        assert "routing generation through" not in stderr_plain

    def test_all_groups_fallback_notes_by_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With multiple agents declaring the same provider, group
        their fallback notes into ONE line with an ``applies to:``
        suffix instead of repeating the identical message."""
        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq", "summarizer", "rag-qa")

        monkeypatch.setattr(eval_scorecard_cmd, "_provider_has_key", lambda p: p == "anthropic")

        async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
            return ScorecardSummary(
                agent=bundle.spec.name,
                mix="standard",
                count=3,
                cases=[],
                category_means=dict.fromkeys(ALL_CATEGORIES, 0.85),
                overall_mean=0.85,
            )

        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

        result = runner.invoke(
            app,
            ["eval-scorecard", "--all", "--count", "3"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr

        stderr_plain = _ANSI_RE.sub("", result.stderr)
        # Single grouped fallback line.
        assert stderr_plain.count("routing generation through") == 1
        # All three agents listed in the "applies to" suffix.
        assert "faq" in stderr_plain
        assert "summarizer" in stderr_plain
        assert "rag-qa" in stderr_plain

    def test_all_no_fallback_notes_when_declared_keys_match(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If every agent's declared provider has its key set, the
        sweep emits NO fallback notes — auto-detect is invisible
        in the happy path."""
        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq", "summarizer")

        # All keys set.
        monkeypatch.setattr(eval_scorecard_cmd, "_provider_has_key", lambda p: True)

        async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
            return ScorecardSummary(
                agent=bundle.spec.name,
                mix="standard",
                count=3,
                cases=[],
                category_means=dict.fromkeys(ALL_CATEGORIES, 0.85),
                overall_mean=0.85,
            )

        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

        result = runner.invoke(
            app,
            ["eval-scorecard", "--all", "--count", "3"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        stderr_plain = _ANSI_RE.sub("", result.stderr)
        assert "routing generation through" not in stderr_plain


# ---------------------------------------------------------------------------
# --target: score deployed runtime via RemoteExecutor
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTargetDeployedAgent:
    """``mdk eval-scorecard --target NAME`` swaps the local in-process
    executor for a :class:`RemoteExecutor` so cases run against the
    deployed runtime. The local runtime is still built — provider for
    the LLM judge, storage for traces, tracer for spans. Only the
    agent-execution seam changes.
    """

    def test_target_and_mock_are_mutex(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--mock means 'no real LLM/runtime calls'; that contradicts
        scoring a deployed agent. CLI must reject the combo upfront."""
        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")

        agent_dir = tmp_path / "proj" / "agents" / "faq"
        result = runner.invoke(
            app,
            [
                "eval-scorecard",
                str(agent_dir),
                "--count",
                "3",
                "--mock",
                "--target",
                "dev",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 2
        stderr_plain = _ANSI_RE.sub("", result.stderr)
        assert "--target" in stderr_plain
        assert "--mock" in stderr_plain
        assert "mutually exclusive" in stderr_plain

    def test_target_not_in_config_errors_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unknown target name → ``UserConfigError`` from
        ``resolve_target`` → friendly stderr line + exit 2."""
        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")

        empty_config = tmp_path / "empty-movate-config.yaml"
        empty_config.write_text("active: null\ntargets: {}\n")
        monkeypatch.setenv("MOVATE_CONFIG_PATH", str(empty_config))

        agent_dir = tmp_path / "proj" / "agents" / "faq"
        result = runner.invoke(
            app,
            [
                "eval-scorecard",
                str(agent_dir),
                "--count",
                "3",
                "--target",
                "nonexistent",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 2
        stderr_plain = _ANSI_RE.sub("", result.stderr)
        assert "nonexistent" in stderr_plain

    def test_target_with_unset_env_var_errors_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Target is defined but its ``key_env`` env var is empty.
        ``resolve_bearer_token`` raises ``UserConfigError`` → exit 2."""
        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")

        config = tmp_path / "movate-config.yaml"
        config.write_text(
            "active: dev\n"
            "targets:\n"
            "  dev:\n"
            "    url: https://dev.example.com\n"
            "    key_env: MISSING_KEY_FOR_TEST\n"
        )
        monkeypatch.setenv("MOVATE_CONFIG_PATH", str(config))
        monkeypatch.delenv("MISSING_KEY_FOR_TEST", raising=False)

        agent_dir = tmp_path / "proj" / "agents" / "faq"
        result = runner.invoke(
            app,
            [
                "eval-scorecard",
                str(agent_dir),
                "--count",
                "3",
                "--target",
                "dev",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 2
        stderr_plain = _ANSI_RE.sub("", result.stderr)
        assert "MISSING_KEY_FOR_TEST" in stderr_plain

    def test_target_passes_remote_url_to_run_scorecard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end smoke: a configured target with a valid bearer
        token propagates ``remote_client`` into ``_run_scorecard``,
        which the scorecard uses to swap in a ``RemoteExecutor``."""
        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")

        config = tmp_path / "movate-config.yaml"
        config.write_text(
            "active: dev\n"
            "targets:\n"
            "  dev:\n"
            "    url: https://dev.example.com\n"
            "    key_env: TEST_DEV_KEY\n"
        )
        monkeypatch.setenv("MOVATE_CONFIG_PATH", str(config))
        monkeypatch.setenv("TEST_DEV_KEY", "mvt_live_test_token_abc123")

        captured: list[dict[str, Any]] = []

        async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
            captured.append({k: v for k, v in kwargs.items()})
            return ScorecardSummary(
                agent=bundle.spec.name,
                mix="standard",
                count=3,
                cases=[],
                category_means=dict.fromkeys(ALL_CATEGORIES, 0.85),
                overall_mean=0.85,
            )

        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

        agent_dir = tmp_path / "proj" / "agents" / "faq"
        result = runner.invoke(
            app,
            [
                "eval-scorecard",
                str(agent_dir),
                "--count",
                "3",
                "--target",
                "dev",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert len(captured) == 1
        remote_client = captured[0].get("remote_client")
        assert remote_client is not None
        assert str(remote_client._client.base_url) == "https://dev.example.com"
        assert remote_client._client.headers["Authorization"] == "Bearer mvt_live_test_token_abc123"

        # Status line goes to STDERR (so JSON on stdout stays clean in
        # ``-o json`` mode); assert it landed there.
        stderr_plain = _ANSI_RE.sub("", result.stderr)
        assert "scoring against deployed runtime" in stderr_plain
        assert "dev" in stderr_plain
        assert "https://dev.example.com" in stderr_plain

    def test_no_target_passes_none_remote_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No --target → remote_client kwarg is ``None`` →
        _run_scorecard uses the local in-process executor."""
        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")

        captured: list[dict[str, Any]] = []

        async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
            captured.append({k: v for k, v in kwargs.items()})
            return ScorecardSummary(
                agent=bundle.spec.name,
                mix="standard",
                count=3,
                cases=[],
                category_means=dict.fromkeys(ALL_CATEGORIES, 0.85),
                overall_mean=0.85,
            )

        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

        agent_dir = tmp_path / "proj" / "agents" / "faq"
        result = runner.invoke(
            app,
            ["eval-scorecard", str(agent_dir), "--count", "3"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert captured[0].get("remote_client") is None

    def test_target_json_output_carries_target_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``-o json`` payload gains a ``"target"`` field — ``"local"``
        for in-process, target name for remote."""
        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")

        config = tmp_path / "movate-config.yaml"
        config.write_text(
            "active: dev\n"
            "targets:\n"
            "  dev:\n"
            "    url: https://dev.example.com\n"
            "    key_env: TEST_DEV_KEY\n"
        )
        monkeypatch.setenv("MOVATE_CONFIG_PATH", str(config))
        monkeypatch.setenv("TEST_DEV_KEY", "mvt_live_test_token_xyz")

        async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
            return ScorecardSummary(
                agent=bundle.spec.name,
                mix="standard",
                count=3,
                cases=[],
                category_means=dict.fromkeys(ALL_CATEGORIES, 0.85),
                overall_mean=0.85,
            )

        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

        agent_dir = tmp_path / "proj" / "agents" / "faq"
        result = runner.invoke(
            app,
            [
                "eval-scorecard",
                str(agent_dir),
                "--count",
                "3",
                "--target",
                "dev",
                "-o",
                "json",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        doc = _json.loads(result.stdout)
        assert doc["target"] == "dev"

    def test_local_json_output_carries_local_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without --target the JSON payload's ``target`` field is
        ``"local"``. CI scrapers can rely on it always being there."""
        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")

        async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
            return ScorecardSummary(
                agent=bundle.spec.name,
                mix="standard",
                count=3,
                cases=[],
                category_means=dict.fromkeys(ALL_CATEGORIES, 0.85),
                overall_mean=0.85,
            )

        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

        agent_dir = tmp_path / "proj" / "agents" / "faq"
        result = runner.invoke(
            app,
            ["eval-scorecard", str(agent_dir), "--count", "3", "-o", "json"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        doc = _json.loads(result.stdout)
        assert doc["target"] == "local"

    def test_all_target_json_output_carries_target_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--all -o json --target dev`` propagates the target into
        the project-level JSON payload."""
        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq", "summarizer")

        config = tmp_path / "movate-config.yaml"
        config.write_text(
            "active: dev\n"
            "targets:\n"
            "  dev:\n"
            "    url: https://dev.example.com\n"
            "    key_env: TEST_DEV_KEY\n"
        )
        monkeypatch.setenv("MOVATE_CONFIG_PATH", str(config))
        monkeypatch.setenv("TEST_DEV_KEY", "mvt_live_test_token_qqq")

        async def fake_run_scorecard(bundle: Any, **kwargs: Any) -> ScorecardSummary:
            return ScorecardSummary(
                agent=bundle.spec.name,
                mix="standard",
                count=3,
                cases=[],
                category_means=dict.fromkeys(ALL_CATEGORIES, 0.85),
                overall_mean=0.85,
            )

        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._run_scorecard", fake_run_scorecard)

        result = runner.invoke(
            app,
            [
                "eval-scorecard",
                "--all",
                "--count",
                "3",
                "--target",
                "dev",
                "-o",
                "json",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        doc = _json.loads(result.stdout)
        assert doc["target"] == "dev"

    def test_target_help_text_mentions_deployed_runtime(self) -> None:
        """Pin a couple anchor phrases in --help so a future refactor
        doesn't accidentally lose the discoverability."""
        result = runner.invoke(app, ["eval-scorecard", "--help"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        plain = _ANSI_RE.sub("", result.stdout)
        assert "--target" in plain
        assert "DEPLOYED" in plain or "deployed" in plain
        assert ".movate/config.yaml" in plain


# ---------------------------------------------------------------------------
# Cost-from-response regression guard (2026-05-19 bug fix)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCostFromResponseMetrics:
    """``_run_scorecard`` reads cost from ``response.metrics.cost_usd``,
    NOT ``response.cost_usd`` (the latter doesn't exist on
    ``RunResponse``, which has ``extra="forbid"``). The pre-fix
    ``getattr(response, "cost_usd", 0.0)`` silently returned 0.0 for
    every case in every scorecard run since v0.7 — the cost category
    scored 1.00 uniformly because every cost was 0 vs the budget,
    masking real overruns from gates + drift comparisons.
    """

    @pytest.mark.asyncio
    async def test_run_scorecard_reads_cost_from_response_metrics(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Drive ``_run_scorecard`` with a fake executor whose response
        carries a known ``metrics.cost_usd``. Assert the resulting
        ``CaseScore.cost_usd`` matches — proves cost flows from the
        Metrics field, not a phantom top-level attribute."""
        from movate.core.loader import load_agent  # noqa: PLC0415
        from movate.core.models import Metrics, RunResponse  # noqa: PLC0415

        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
        agent_dir = tmp_path / "proj" / "agents" / "faq"
        bundle = load_agent(agent_dir)

        async def fake_generate_entries(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return [{"input": {"question": "what time is it?"}, "expected": {"answer": "noon"}}]

        monkeypatch.setattr(
            "movate.cli.eval_scorecard_cmd._generate_entries", fake_generate_entries
        )

        # Patch the executor to return a response with a KNOWN cost.
        # The exact value is distinct from any plausible default so
        # we can attribute mismatches if the assertion fails.
        target_cost = 0.0042

        class _StubExecutor:
            async def execute(self, bundle: Any, request: Any, **_kw: Any) -> RunResponse:
                return RunResponse(
                    status="success",
                    data={"answer": "noon"},
                    metrics=Metrics(cost_usd=target_cost, latency_ms=42),
                )

        class _StubStorage:
            async def close(self) -> None:
                pass

        class _StubRuntime:
            executor = _StubExecutor()
            provider = None
            storage = _StubStorage()
            tracer = None

        async def fake_build_local_runtime(*, mock: bool) -> Any:
            return _StubRuntime()

        async def fake_shutdown(storage: Any, tracer: Any) -> None:
            return None

        monkeypatch.setattr(
            "movate.cli.eval_scorecard_cmd.build_local_runtime",
            fake_build_local_runtime,
        )
        monkeypatch.setattr("movate.cli.eval_scorecard_cmd.shutdown_runtime", fake_shutdown)

        async def fake_score_one_case(
            *args: Any, **kwargs: Any
        ) -> tuple[dict[str, float], dict[str, str]]:
            return (
                dict.fromkeys(LLM_JUDGED_CATEGORIES, 0.9),
                dict.fromkeys(LLM_JUDGED_CATEGORIES, "ok"),
            )

        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._score_one_case", fake_score_one_case)

        from movate.cli.eval_scorecard_cmd import _run_scorecard  # noqa: PLC0415

        summary = await _run_scorecard(
            bundle, count=1, mix="standard", mock=False, judge_model=None
        )
        assert len(summary.cases) == 1
        assert summary.cases[0].cost_usd == pytest.approx(target_cost), (
            f"Expected cost_usd={target_cost} from response.metrics.cost_usd, "
            f"got {summary.cases[0].cost_usd}. If this is 0.0, the regression "
            f"is back — fix is to access ``response.metrics.cost_usd`` not "
            f"``response.cost_usd``."
        )

    @pytest.mark.asyncio
    async def test_run_scorecard_handles_zero_cost_response(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A response with metrics.cost_usd=0.0 (mock provider, cached
        response) should record cost=0.0 cleanly."""
        from movate.core.loader import load_agent  # noqa: PLC0415
        from movate.core.models import Metrics, RunResponse  # noqa: PLC0415

        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
        agent_dir = tmp_path / "proj" / "agents" / "faq"
        bundle = load_agent(agent_dir)

        async def fake_generate_entries(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return [{"input": {"x": 1}, "expected": {"y": 2}}]

        monkeypatch.setattr(
            "movate.cli.eval_scorecard_cmd._generate_entries", fake_generate_entries
        )

        class _StubExecutor:
            async def execute(self, bundle: Any, request: Any, **_kw: Any) -> RunResponse:
                return RunResponse(
                    status="success",
                    data={"y": 2},
                    metrics=Metrics(cost_usd=0.0, latency_ms=10),
                )

        class _StubStorage:
            async def close(self) -> None:
                pass

        class _StubRuntime:
            executor = _StubExecutor()
            provider = None
            storage = _StubStorage()
            tracer = None

        async def fake_build_local_runtime(*, mock: bool) -> Any:
            return _StubRuntime()

        async def fake_shutdown(storage: Any, tracer: Any) -> None:
            return None

        async def fake_score_one_case(
            *args: Any, **kwargs: Any
        ) -> tuple[dict[str, float], dict[str, str]]:
            return (
                dict.fromkeys(LLM_JUDGED_CATEGORIES, 0.9),
                dict.fromkeys(LLM_JUDGED_CATEGORIES, "ok"),
            )

        monkeypatch.setattr(
            "movate.cli.eval_scorecard_cmd.build_local_runtime",
            fake_build_local_runtime,
        )
        monkeypatch.setattr("movate.cli.eval_scorecard_cmd.shutdown_runtime", fake_shutdown)
        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._score_one_case", fake_score_one_case)

        from movate.cli.eval_scorecard_cmd import _run_scorecard  # noqa: PLC0415

        summary = await _run_scorecard(
            bundle, count=1, mix="standard", mock=False, judge_model=None
        )
        assert summary.cases[0].cost_usd == 0.0

    def test_runresponse_has_no_top_level_cost_usd_attribute(self) -> None:
        """Pin the schema constraint the fix is built around:
        ``RunResponse`` uses ``extra="forbid"`` and does NOT expose
        ``cost_usd`` as a top-level field. If a future refactor adds
        one, this test fails and forces a deliberate decision."""
        from movate.core.models import RunResponse  # noqa: PLC0415

        assert "cost_usd" not in RunResponse.model_fields
        assert "metrics" in RunResponse.model_fields
        resp = RunResponse(status="success")
        assert resp.metrics.cost_usd == 0.0


# ---------------------------------------------------------------------------
# Preflight auto-retry on AuthError (2026-05-19)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPreflightAutoRetry:
    """When an auto-detected provider's key is set but invalid
    (placeholder/stale, like ``OPENAI_API_KEY=sk-test-*2345``), the
    preflight wrapper excludes that provider and re-resolves with
    the next fallback rather than exiting. Operators who don't
    perfectly curate their env vars still get a working sweep.

    Explicit ``--generator-model FLAG`` bypasses retry — the operator
    explicitly chose FLAG, so an auth error on FLAG is fatal.
    """

    @pytest.mark.no_preflight_stub
    @pytest.mark.asyncio
    async def test_retry_excludes_failed_provider_and_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """First probe fails with AuthError (openai key is set but
        rejected); retry resolves to anthropic, succeeds. Final
        resolved map points the agent at the fallback."""

        # Pretend both openai and anthropic are configured.
        monkeypatch.setattr(
            eval_scorecard_cmd,
            "_provider_has_key",
            lambda p: p in {"openai", "anthropic"},
        )

        # First call → openai. Second call → anthropic.
        probe_calls: list[set[str]] = []

        async def fake_preflight(*, models: set[str], mock: bool) -> None:
            probe_calls.append(models)
            if any("openai" in m for m in models):
                raise eval_scorecard_cmd._PreflightAuthError(
                    model="openai/gpt-4o-mini-2024-07-18",
                    message="Incorrect API key provided: sk-test-*2345",
                )
            # Anthropic probe succeeds (no raise).

        monkeypatch.setattr(eval_scorecard_cmd, "_preflight_check_generator_auth", fake_preflight)

        resolved = await eval_scorecard_cmd._preflight_with_retry(
            declared_per_agent={"faq": "openai/gpt-4o-mini-2024-07-18"},
            generator_model_flag=None,
            mock=False,
            is_json=True,  # suppress note printing in tests
        )

        # Resolved to anthropic (the fallback), not openai.
        assert resolved == {"faq": "anthropic/claude-haiku-4-5-20251001"}
        # Exactly TWO preflight calls: openai (failed) → anthropic (passed).
        assert len(probe_calls) == 2
        assert any("openai" in m for m in probe_calls[0])
        assert any("anthropic" in m for m in probe_calls[1])

    @pytest.mark.no_preflight_stub
    @pytest.mark.asyncio
    async def test_retry_exhausts_fallbacks_then_exits(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        """If every configured provider fails preflight, the retry
        loop runs out of options, exits 2, AND the exit hint lists
        the providers that were attempted (so an operator pasting
        the output has the full chronology)."""

        # Both openai + anthropic LOOK configured but both reject.
        monkeypatch.setattr(
            eval_scorecard_cmd,
            "_provider_has_key",
            lambda p: p in {"openai", "anthropic"},
        )

        async def fake_preflight(*, models: set[str], mock: bool) -> None:
            # Reject whichever model is being probed.
            failing_model = next(iter(models))
            raise eval_scorecard_cmd._PreflightAuthError(model=failing_model, message="invalid key")

        monkeypatch.setattr(eval_scorecard_cmd, "_preflight_check_generator_auth", fake_preflight)

        with pytest.raises(typer.Exit) as excinfo:
            await eval_scorecard_cmd._preflight_with_retry(
                declared_per_agent={"faq": "openai/gpt-4o-mini-2024-07-18"},
                generator_model_flag=None,
                mock=False,
                is_json=False,  # allow stderr emission to assert on it
            )
        assert excinfo.value.exit_code == 2

        # Exit hint lists what was attempted — NOT just one model.
        captured = capsys.readouterr()
        stderr_plain = _ANSI_RE.sub("", captured.err)
        assert "Auto-retry attempted:" in stderr_plain
        # Both providers attempted by retry must appear in the hint.
        assert "openai/gpt-4o-mini-2024-07-18" in stderr_plain
        assert "anthropic/claude-haiku-4-5-20251001" in stderr_plain

    @pytest.mark.no_preflight_stub
    @pytest.mark.asyncio
    async def test_retry_emits_chronology_in_real_time(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        """Retry notes stream to stderr as each provider gets
        excluded — NOT batched at the end of the loop. Without this,
        the exhaust path silently drops the chronology and operators
        see only the final exit hint with no explanation of what got
        tried first.

        Regression guard: prior to 2026-05-19, the retry notes lived
        in an ``auto_route_notes`` list that was only emitted on
        success. An exhaust path would lose them entirely — exactly
        the symptom that surfaced when the user's first retry probe
        ran while ``ANTHROPIC_API_KEY`` was missing from the
        credentials file."""
        monkeypatch.setattr(
            eval_scorecard_cmd,
            "_provider_has_key",
            lambda p: p in {"openai", "anthropic"},
        )

        async def fake_preflight(*, models: set[str], mock: bool) -> None:
            failing_model = next(iter(models))
            raise eval_scorecard_cmd._PreflightAuthError(model=failing_model, message="invalid key")

        monkeypatch.setattr(eval_scorecard_cmd, "_preflight_check_generator_auth", fake_preflight)

        with pytest.raises(typer.Exit):
            await eval_scorecard_cmd._preflight_with_retry(
                declared_per_agent={"faq": "openai/gpt-4o-mini-2024-07-18"},
                generator_model_flag=None,
                mock=False,
                is_json=False,
            )

        captured = capsys.readouterr()
        stderr_plain = _ANSI_RE.sub("", captured.err)
        # The FIRST exclusion note must be in stderr even though the
        # loop ultimately exhausted. Pre-fix this was silently
        # dropped because notes only emitted on success.
        assert "preflight:" in stderr_plain
        assert "excluding openai" in stderr_plain or "openai" in stderr_plain
        # And the exhaust hint follows.
        assert "Auto-retry attempted:" in stderr_plain

    @pytest.mark.no_preflight_stub
    @pytest.mark.asyncio
    async def test_explicit_flag_does_not_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--generator-model FLAG`` bypasses retry — operator
        explicitly chose FLAG, so an AuthError on FLAG is fatal.
        Pin: only ONE probe fires, and exit code is 2 with the
        non-recoverable hint."""

        probe_count = 0

        async def fake_preflight(*, models: set[str], mock: bool) -> None:
            nonlocal probe_count
            probe_count += 1
            raise eval_scorecard_cmd._PreflightAuthError(
                model="openai/gpt-4o-mini-2024-07-18",
                message="invalid key",
            )

        monkeypatch.setattr(eval_scorecard_cmd, "_preflight_check_generator_auth", fake_preflight)

        with pytest.raises(typer.Exit) as excinfo:
            await eval_scorecard_cmd._preflight_with_retry(
                declared_per_agent={"faq": "anthropic/claude-3-opus"},
                generator_model_flag="openai/gpt-4o-mini-2024-07-18",
                mock=False,
                is_json=True,
            )
        assert excinfo.value.exit_code == 2
        # No retry attempted — the operator's explicit choice wasn't
        # second-guessed.
        assert probe_count == 1

    @pytest.mark.no_preflight_stub
    @pytest.mark.asyncio
    async def test_mock_skips_resolution_and_preflight_entirely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``mock=True`` → returns ``{}`` without invoking either the
        resolver or the preflight. Mock mode means no LLM activity
        of any kind."""

        async def fake_preflight(*, models: set[str], mock: bool) -> None:
            pytest.fail("preflight must not be called in mock mode")

        monkeypatch.setattr(eval_scorecard_cmd, "_preflight_check_generator_auth", fake_preflight)

        resolved = await eval_scorecard_cmd._preflight_with_retry(
            declared_per_agent={"faq": "openai/gpt-4o-mini-2024-07-18"},
            generator_model_flag=None,
            mock=True,
            is_json=True,
        )
        assert resolved == {}

    @pytest.mark.no_preflight_stub
    @pytest.mark.asyncio
    async def test_retry_emits_auto_route_note_to_stderr(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        """When retry kicks in (a configured provider's key was
        rejected), the wrapper emits a one-line stderr note so the
        operator understands WHY generation routed elsewhere — they
        didn't ask for the swap, the auto-retry made the call."""

        monkeypatch.setattr(
            eval_scorecard_cmd,
            "_provider_has_key",
            lambda p: p in {"openai", "anthropic"},
        )

        first_call = True

        async def fake_preflight(*, models: set[str], mock: bool) -> None:
            nonlocal first_call
            if first_call:
                first_call = False
                raise eval_scorecard_cmd._PreflightAuthError(
                    model="openai/gpt-4o-mini-2024-07-18",
                    message="Incorrect API key provided: sk-test-*2345",
                )

        monkeypatch.setattr(eval_scorecard_cmd, "_preflight_check_generator_auth", fake_preflight)

        # is_json=False so notes actually emit.
        await eval_scorecard_cmd._preflight_with_retry(
            declared_per_agent={"faq": "openai/gpt-4o-mini-2024-07-18"},
            generator_model_flag=None,
            mock=False,
            is_json=False,
        )

        captured = capsys.readouterr()
        stderr_plain = _ANSI_RE.sub("", captured.err)
        # The auto-route note explains the swap.
        assert "preflight:" in stderr_plain
        assert "openai/gpt-4o-mini-2024-07-18" in stderr_plain
        # Mentions the underlying reason (truncated) so it's
        # debuggable, not just "auto-routed".
        assert "rejected" in stderr_plain or "Incorrect API key" in stderr_plain


@pytest.mark.unit
class TestResolveGeneratorModelExclusion:
    """``_resolve_generator_model`` accepts ``exclude_providers`` for
    the auto-retry path: a provider whose key is set but rejected
    by preflight should be treated as if no key is configured."""

    def test_excluded_declared_provider_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Operator has openai + anthropic keys. Declared is openai.
        Without exclusion, returns openai. With openai excluded
        (preflight rejected its key), falls back to anthropic."""
        monkeypatch.setattr(
            eval_scorecard_cmd,
            "_provider_has_key",
            lambda p: p in {"openai", "anthropic"},
        )

        model, note = eval_scorecard_cmd._resolve_generator_model(
            "openai/gpt-4o-mini-2024-07-18", None
        )
        assert model == "openai/gpt-4o-mini-2024-07-18"
        assert note is None

        # With openai excluded:
        model, note = eval_scorecard_cmd._resolve_generator_model(
            "openai/gpt-4o-mini-2024-07-18",
            None,
            exclude_providers=frozenset({"openai"}),
        )
        assert model == "anthropic/claude-haiku-4-5-20251001"
        assert note is not None  # Note explains the swap.

    def test_excluded_fallback_provider_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Anthropic is the priority-1 fallback. If it's excluded,
        resolution should skip it and pick the next fallback (OpenAI),
        not raise or loop."""
        monkeypatch.setattr(
            eval_scorecard_cmd,
            "_provider_has_key",
            lambda p: p in {"anthropic", "openai"},
        )

        # Declared = azure (not in fallback list), anthropic excluded
        # by retry. Next available fallback is openai.
        model, _ = eval_scorecard_cmd._resolve_generator_model(
            "azure/gpt-4",
            None,
            exclude_providers=frozenset({"anthropic"}),
        )
        assert model == "openai/gpt-4o-mini-2024-07-18"

    def test_all_providers_excluded_returns_declared(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If every configured provider has been excluded by retry,
        resolution returns the declared provider as-is — the
        preflight will then fail again and the retry loop will exit
        with the recoverable-failure hint."""
        monkeypatch.setattr(eval_scorecard_cmd, "_provider_has_key", lambda p: True)

        model, _ = eval_scorecard_cmd._resolve_generator_model(
            "openai/gpt-4o-mini-2024-07-18",
            None,
            exclude_providers=frozenset({"openai", "anthropic", "gemini"}),
        )
        assert model == "openai/gpt-4o-mini-2024-07-18"

    def test_explicit_flag_bypasses_exclusion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Even if every provider is excluded, an explicit
        ``--generator-model FLAG`` is returned as-is. The flag is
        outside the auto-retry's authority."""
        monkeypatch.setattr(eval_scorecard_cmd, "_provider_has_key", lambda p: False)

        model, note = eval_scorecard_cmd._resolve_generator_model(
            "openai/gpt-4o-mini-2024-07-18",
            "anthropic/claude-haiku-4-5-20251001",
            exclude_providers=frozenset({"anthropic", "openai"}),
        )
        assert model == "anthropic/claude-haiku-4-5-20251001"
        assert note is None


# ---------------------------------------------------------------------------
# Judge inherits the resolved generator model (2026-05-19 follow-up)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestJudgeInheritsResolvedGenerator:
    """When ``--judge-model`` isn't set explicitly, the judge call
    should use whatever model the generator was auto-routed to.
    Otherwise an operator with a stale OpenAI key + working Anthropic
    key would have a successful generator (auto-detect routes around
    the stale key) but a silently-failing judge (still points at the
    declared openai provider), and every LLM-judged category would
    score 0.0 without any actionable error.
    """

    @pytest.mark.asyncio
    async def test_judge_falls_back_to_generator_when_no_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Drive ``_run_scorecard`` with ``generator_model`` set to the
        anthropic fallback (mimicking what ``_preflight_with_retry``
        produces when the operator's openai key is stale) and
        ``judge_model=None``. The captured judge call must use the
        anthropic model, NOT the bundle's declared openai provider."""
        from movate.core.loader import load_agent  # noqa: PLC0415
        from movate.core.models import Metrics, RunResponse  # noqa: PLC0415

        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
        agent_dir = tmp_path / "proj" / "agents" / "faq"
        bundle = load_agent(agent_dir)

        async def fake_generate_entries(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return [{"input": {"q": "x"}, "expected": {"a": "y"}}]

        monkeypatch.setattr(
            "movate.cli.eval_scorecard_cmd._generate_entries", fake_generate_entries
        )

        class _StubExecutor:
            async def execute(self, bundle: Any, request: Any, **_kw: Any) -> RunResponse:
                return RunResponse(
                    status="success",
                    data={"a": "y"},
                    metrics=Metrics(cost_usd=0.001, latency_ms=100),
                )

        class _StubStorage:
            async def close(self) -> None:
                pass

        class _StubRuntime:
            executor = _StubExecutor()
            provider = None
            storage = _StubStorage()
            tracer = None

        async def fake_build_local_runtime(*, mock: bool) -> Any:
            return _StubRuntime()

        async def fake_shutdown(storage: Any, tracer: Any) -> None:
            return None

        monkeypatch.setattr(
            "movate.cli.eval_scorecard_cmd.build_local_runtime",
            fake_build_local_runtime,
        )
        monkeypatch.setattr("movate.cli.eval_scorecard_cmd.shutdown_runtime", fake_shutdown)

        captured_judge_model: list[str | None] = []

        async def fake_score_one_case(
            rt: Any,
            bundle: Any,
            input_data: Any,
            output_data: Any,
            *,
            judge_model: str | None = None,
            effective: Any = None,
        ) -> tuple[dict[str, float], dict[str, str]]:
            captured_judge_model.append(judge_model)
            return (
                dict.fromkeys(LLM_JUDGED_CATEGORIES, 0.9),
                dict.fromkeys(LLM_JUDGED_CATEGORIES, "ok"),
            )

        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._score_one_case", fake_score_one_case)

        from movate.cli.eval_scorecard_cmd import _run_scorecard  # noqa: PLC0415

        await _run_scorecard(
            bundle,
            count=1,
            mix="standard",
            mock=False,
            judge_model=None,  # no explicit override
            generator_model="anthropic/claude-haiku-4-5-20251001",  # auto-detect resolved here
        )
        # Judge was called with the resolved generator model, NOT
        # the bundle's declared openai provider.
        assert captured_judge_model == ["anthropic/claude-haiku-4-5-20251001"]

    @pytest.mark.asyncio
    async def test_explicit_judge_model_overrides_generator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--judge-model FLAG`` always wins. If the operator
        explicitly chose a judge model, the auto-detect's resolved
        generator model must NOT override it."""
        from movate.core.loader import load_agent  # noqa: PLC0415
        from movate.core.models import Metrics, RunResponse  # noqa: PLC0415

        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
        agent_dir = tmp_path / "proj" / "agents" / "faq"
        bundle = load_agent(agent_dir)

        async def fake_generate_entries(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return [{"input": {"q": "x"}, "expected": {"a": "y"}}]

        monkeypatch.setattr(
            "movate.cli.eval_scorecard_cmd._generate_entries", fake_generate_entries
        )

        class _StubExecutor:
            async def execute(self, bundle: Any, request: Any, **_kw: Any) -> RunResponse:
                return RunResponse(
                    status="success",
                    data={"a": "y"},
                    metrics=Metrics(cost_usd=0.0, latency_ms=10),
                )

        class _StubStorage:
            async def close(self) -> None:
                pass

        class _StubRuntime:
            executor = _StubExecutor()
            provider = None
            storage = _StubStorage()
            tracer = None

        async def fake_build_local_runtime(*, mock: bool) -> Any:
            return _StubRuntime()

        async def fake_shutdown(storage: Any, tracer: Any) -> None:
            return None

        monkeypatch.setattr(
            "movate.cli.eval_scorecard_cmd.build_local_runtime",
            fake_build_local_runtime,
        )
        monkeypatch.setattr("movate.cli.eval_scorecard_cmd.shutdown_runtime", fake_shutdown)

        captured_judge_model: list[str | None] = []

        async def fake_score_one_case(
            rt: Any,
            bundle: Any,
            input_data: Any,
            output_data: Any,
            *,
            judge_model: str | None = None,
            effective: Any = None,
        ) -> tuple[dict[str, float], dict[str, str]]:
            captured_judge_model.append(judge_model)
            return (
                dict.fromkeys(LLM_JUDGED_CATEGORIES, 0.9),
                dict.fromkeys(LLM_JUDGED_CATEGORIES, "ok"),
            )

        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._score_one_case", fake_score_one_case)

        from movate.cli.eval_scorecard_cmd import _run_scorecard  # noqa: PLC0415

        await _run_scorecard(
            bundle,
            count=1,
            mix="standard",
            mock=False,
            judge_model="gemini/gemini-2.5-flash",  # explicit operator choice
            generator_model="anthropic/claude-haiku-4-5-20251001",
        )
        # Explicit flag wins — generator's anthropic does NOT override.
        assert captured_judge_model == ["gemini/gemini-2.5-flash"]


# ---------------------------------------------------------------------------
# Interactive auth-recovery on preflight exhaust (2026-05-19)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInlineAuthRecovery:
    """When preflight retry exhausts AND we're in a TTY, the eval
    offers to set up a working provider key inline rather than
    forcing the operator to Ctrl+C → ``mdk auth login`` → re-run.
    Non-TTY contexts (CI, piped output, JSON mode) skip the prompt
    and exit as before.
    """

    @pytest.mark.no_preflight_stub
    @pytest.mark.asyncio
    async def test_non_tty_skips_inline_recovery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """In CI / piped contexts the prompt would block forever.
        The recovery helper short-circuits to ``False`` so the
        caller falls through to the standard exit hint."""
        import sys  # noqa: PLC0415

        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(eval_scorecard_cmd, "_provider_has_key", lambda p: p == "openai")

        async def fake_preflight(*, models: set[str], mock: bool) -> None:
            raise eval_scorecard_cmd._PreflightAuthError(
                model=next(iter(models)), message="rejected"
            )

        monkeypatch.setattr(eval_scorecard_cmd, "_preflight_check_generator_auth", fake_preflight)

        prompt_count = 0
        real_prompt = typer.prompt

        def counting_prompt(*args: Any, **kwargs: Any) -> Any:
            nonlocal prompt_count
            prompt_count += 1
            return real_prompt(*args, **kwargs)

        monkeypatch.setattr(typer, "prompt", counting_prompt)

        with pytest.raises(typer.Exit) as excinfo:
            await eval_scorecard_cmd._preflight_with_retry(
                declared_per_agent={"faq": "openai/gpt-4o-mini-2024-07-18"},
                generator_model_flag=None,
                mock=False,
                is_json=False,
            )
        assert excinfo.value.exit_code == 2
        # No prompt fired — stdin isn't a TTY.
        assert prompt_count == 0

    @pytest.mark.no_preflight_stub
    @pytest.mark.asyncio
    async def test_json_mode_skips_inline_recovery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``-o json`` mode must NOT prompt — would pollute the JSON
        document on stdout and CI scrapers can't handle interactive
        recovery anyway."""
        import sys  # noqa: PLC0415

        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(eval_scorecard_cmd, "_provider_has_key", lambda p: p == "openai")

        async def fake_preflight(*, models: set[str], mock: bool) -> None:
            raise eval_scorecard_cmd._PreflightAuthError(
                model=next(iter(models)), message="rejected"
            )

        monkeypatch.setattr(eval_scorecard_cmd, "_preflight_check_generator_auth", fake_preflight)

        prompt_count = 0

        def counting_prompt(*args: Any, **kwargs: Any) -> Any:
            nonlocal prompt_count
            prompt_count += 1
            return ""

        monkeypatch.setattr(typer, "prompt", counting_prompt)

        with pytest.raises(typer.Exit):
            await eval_scorecard_cmd._preflight_with_retry(
                declared_per_agent={"faq": "openai/gpt-4o-mini-2024-07-18"},
                generator_model_flag=None,
                mock=False,
                is_json=True,
            )
        assert prompt_count == 0

    @pytest.mark.no_preflight_stub
    @pytest.mark.asyncio
    async def test_tty_recovery_delegates_to_auth_login(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Interactive shell + operator's stuck preflight invokes the
        existing ``mdk auth login`` flow (with its polished provider
        picker carrying PR #207's live-verify markers) — instead of
        a custom letter-choice prompt that rendered raw Rich markup.
        After login() saves a key, retry restarts and finds the new
        provider.
        """
        import os  # noqa: PLC0415
        import sys  # noqa: PLC0415

        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        original_anthropic = os.environ.get("ANTHROPIC_API_KEY")

        configured: set[str] = {"openai"}
        monkeypatch.setattr(
            eval_scorecard_cmd,
            "_provider_has_key",
            lambda p: p in configured,
        )

        async def fake_preflight(*, models: set[str], mock: bool) -> None:
            for m in models:
                if "openai" in m:
                    raise eval_scorecard_cmd._PreflightAuthError(model=m, message="rejected")

        monkeypatch.setattr(eval_scorecard_cmd, "_preflight_check_generator_auth", fake_preflight)

        # Simulate the ``mdk auth login`` flow: when called with no
        # provider arg, it would show the picker, prompt for a key,
        # verify it, and save it. We stub that whole sequence with
        # a side-effecting fake that updates the credentials file
        # the same way the real flow would.
        login_invoked = False

        def fake_login(
            provider: str | None = None,
            key: str | None = None,
            no_verify: bool = False,
            save_to: str = "global",
        ) -> None:
            nonlocal login_invoked
            login_invoked = True
            # Mimic what login() does on success: writes to the
            # credentials file (via CredentialsStore.set).
            configured.add("anthropic")
            from movate.credentials.store import CredentialsStore  # noqa: PLC0415

            CredentialsStore().set("ANTHROPIC_API_KEY", "sk-ant-fake-saved-by-login")

        # Stub the CredentialsStore so we don't actually write to
        # ~/.movate/credentials during tests.
        saved: dict[str, str] = {}

        def fake_set(self: Any, key: str, value: str) -> None:
            saved[key] = value

        def fake_read(self: Any) -> dict[str, str]:
            return dict(saved)

        monkeypatch.setattr("movate.credentials.store.CredentialsStore.set", fake_set)
        monkeypatch.setattr("movate.credentials.store.CredentialsStore.read", fake_read)
        monkeypatch.setattr("movate.cli.auth.login", fake_login)

        try:
            resolved = await eval_scorecard_cmd._preflight_with_retry(
                declared_per_agent={"faq": "openai/gpt-4o-mini-2024-07-18"},
                generator_model_flag=None,
                mock=False,
                is_json=False,
            )
            # The auth login flow was invoked inline.
            assert login_invoked, "auth login should be called for recovery"
            # The newly-saved key got injected into os.environ for
            # the in-flight retry (autoload already ran at startup).
            assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-fake-saved-by-login"
            # Retry succeeded, resolved to the new provider.
            assert resolved == {"faq": "anthropic/claude-haiku-4-5-20251001"}
        finally:
            if original_anthropic is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = original_anthropic

    @pytest.mark.no_preflight_stub
    @pytest.mark.asyncio
    async def test_tty_recovery_user_cancels_auth_login(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the operator cancels out of the picker (or login()
        raises typer.Exit for any reason — verify failure, empty
        key, etc.), the caller falls through to the standard exit
        hint without retrying."""
        import sys  # noqa: PLC0415

        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(eval_scorecard_cmd, "_provider_has_key", lambda p: p == "openai")

        async def fake_preflight(*, models: set[str], mock: bool) -> None:
            raise eval_scorecard_cmd._PreflightAuthError(
                model=next(iter(models)), message="rejected"
            )

        monkeypatch.setattr(eval_scorecard_cmd, "_preflight_check_generator_auth", fake_preflight)

        # Login flow exits with code 2 (e.g. operator hit Ctrl+C
        # during the key prompt, or verify failed).
        def fake_login_cancels(
            provider: str | None = None,
            key: str | None = None,
            no_verify: bool = False,
            save_to: str = "global",
        ) -> None:
            raise typer.Exit(code=2)

        monkeypatch.setattr("movate.cli.auth.login", fake_login_cancels)

        with pytest.raises(typer.Exit) as excinfo:
            await eval_scorecard_cmd._preflight_with_retry(
                declared_per_agent={"faq": "openai/gpt-4o-mini-2024-07-18"},
                generator_model_flag=None,
                mock=False,
                is_json=False,
            )
        # Caller surfaces the standard exit hint.
        assert excinfo.value.exit_code == 2


# ---------------------------------------------------------------------------
# Pre-generated entries (wizard-preview flow, 2026-05-19)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPreGeneratedEntries:
    """``mdk eval`` wizard generates + previews cases for operator
    approval, then dispatches to the scorecard with the same cases.
    The scorecard must skip its internal ``_generate_entries`` call
    when ``pre_generated_entries`` is provided — otherwise it would
    score a DIFFERENT set of cases than what the operator saw + ok'd.
    """

    @pytest.mark.asyncio
    async def test_run_scorecard_uses_pre_generated_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Drive ``_run_scorecard`` with explicit pre-generated entries;
        assert ``_generate_entries`` is NEVER called + the scored
        cases match the provided entries exactly."""
        from movate.core.loader import load_agent  # noqa: PLC0415
        from movate.core.models import Metrics, RunResponse  # noqa: PLC0415

        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
        agent_dir = tmp_path / "proj" / "agents" / "faq"
        bundle = load_agent(agent_dir)

        # If anything calls _generate_entries the test fails — the
        # whole point of the pre-generated path is to skip it.
        async def fail_generate(*args: Any, **kwargs: Any) -> Any:
            pytest.fail(
                "_generate_entries must not be called when pre_generated_entries is provided"
            )

        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._generate_entries", fail_generate)

        class _StubExecutor:
            async def execute(self, bundle: Any, request: Any, **_kw: Any) -> RunResponse:
                return RunResponse(
                    status="success",
                    data={"answer": "stubbed"},
                    metrics=Metrics(cost_usd=0.0001, latency_ms=42),
                )

        class _StubStorage:
            async def close(self) -> None:
                pass

        class _StubRuntime:
            executor = _StubExecutor()
            provider = None
            storage = _StubStorage()
            tracer = None

        async def fake_build_local_runtime(*, mock: bool) -> Any:
            return _StubRuntime()

        async def fake_shutdown(storage: Any, tracer: Any) -> None:
            return None

        monkeypatch.setattr(
            "movate.cli.eval_scorecard_cmd.build_local_runtime",
            fake_build_local_runtime,
        )
        monkeypatch.setattr("movate.cli.eval_scorecard_cmd.shutdown_runtime", fake_shutdown)

        async def fake_score_one_case(
            *args: Any, **kwargs: Any
        ) -> tuple[dict[str, float], dict[str, str]]:
            return (
                dict.fromkeys(LLM_JUDGED_CATEGORIES, 0.9),
                dict.fromkeys(LLM_JUDGED_CATEGORIES, "ok"),
            )

        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._score_one_case", fake_score_one_case)

        from movate.cli.eval_scorecard_cmd import _run_scorecard  # noqa: PLC0415

        # The wizard previously gathered + showed these entries to
        # the operator; we pass them through verbatim.
        pre_generated = [
            {"input": {"question": "q1"}, "expected": {"answer": "a1"}},
            {"input": {"question": "q2"}, "expected": {"answer": "a2"}},
            {"input": {"question": "q3"}, "expected": {"answer": "a3"}},
        ]

        summary = await _run_scorecard(
            bundle,
            count=99,  # deliberately != len(pre_generated) — count is
            # ignored when entries are provided
            mix="standard",
            mock=False,
            judge_model=None,
            pre_generated_entries=pre_generated,
        )
        # Scorecard scored exactly the 3 cases we passed in.
        assert summary.count == 3
        assert len(summary.cases) == 3
        assert [c.input for c in summary.cases] == [e["input"] for e in pre_generated]

    @pytest.mark.asyncio
    async def test_run_scorecard_still_generates_when_no_pre_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard: when ``pre_generated_entries`` is None
        (the default; non-wizard CLI invocations), the scorecard
        STILL calls _generate_entries as before."""
        from movate.core.loader import load_agent  # noqa: PLC0415
        from movate.core.models import Metrics, RunResponse  # noqa: PLC0415

        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
        agent_dir = tmp_path / "proj" / "agents" / "faq"
        bundle = load_agent(agent_dir)

        generate_call_count = 0

        async def counting_generate(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            nonlocal generate_call_count
            generate_call_count += 1
            return [{"input": {"q": "x"}, "expected": {"a": "y"}}]

        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._generate_entries", counting_generate)

        class _StubExecutor:
            async def execute(self, bundle: Any, request: Any, **_kw: Any) -> RunResponse:
                return RunResponse(
                    status="success",
                    data={"a": "y"},
                    metrics=Metrics(cost_usd=0.0, latency_ms=10),
                )

        class _StubStorage:
            async def close(self) -> None:
                pass

        class _StubRuntime:
            executor = _StubExecutor()
            provider = None
            storage = _StubStorage()
            tracer = None

        async def fake_build_local_runtime(*, mock: bool) -> Any:
            return _StubRuntime()

        async def fake_shutdown(storage: Any, tracer: Any) -> None:
            return None

        monkeypatch.setattr(
            "movate.cli.eval_scorecard_cmd.build_local_runtime",
            fake_build_local_runtime,
        )
        monkeypatch.setattr("movate.cli.eval_scorecard_cmd.shutdown_runtime", fake_shutdown)

        async def fake_score_one_case(
            *args: Any, **kwargs: Any
        ) -> tuple[dict[str, float], dict[str, str]]:
            return (
                dict.fromkeys(LLM_JUDGED_CATEGORIES, 0.9),
                dict.fromkeys(LLM_JUDGED_CATEGORIES, "ok"),
            )

        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._score_one_case", fake_score_one_case)

        from movate.cli.eval_scorecard_cmd import _run_scorecard  # noqa: PLC0415

        # Default path: no pre-generated entries → must invoke
        # _generate_entries exactly once.
        await _run_scorecard(
            bundle,
            count=1,
            mix="standard",
            mock=False,
            judge_model=None,
        )
        assert generate_call_count == 1


# ---------------------------------------------------------------------------
# Wizard preview helpers (2026-05-19)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWizardPreviewHelpers:
    """Pure helpers used by the ``mdk eval`` wizard's preview flow:
    JSON truncation for table cells, table rendering smoke test, and
    the regenerate/continue/cancel choice mapper."""

    def test_truncate_json_short_values_pass_through(self) -> None:

        assert _truncate_json({"q": "hi"}, max_chars=100) == '{"q": "hi"}'
        # Empty dict still serializes.
        assert _truncate_json({}, max_chars=100) == "{}"
        # ``None`` → empty string (table cell stays clean).
        assert _truncate_json(None, max_chars=100) == ""

    def test_truncate_json_collapses_whitespace(self) -> None:
        """Generated JSON may have embedded newlines from the
        operator's content (e.g. a diff value). Tables can't render
        raw newlines; collapse them before measuring length."""

        value = {"diff": "line1\nline2\n  line3"}
        out = _truncate_json(value, max_chars=100)
        assert "\n" not in out
        # The diff content is preserved (just whitespace-collapsed).
        assert "line1" in out
        assert "line2" in out

    def test_truncate_json_long_values_truncate_with_ellipsis(self) -> None:

        long = {"x": "a" * 500}
        out = _truncate_json(long, max_chars=50)
        assert len(out) == 50
        assert out.endswith("…")

    def test_truncate_json_falls_back_to_str_for_non_serializable(self) -> None:
        """Some values may be non-JSON-serializable (custom objects,
        sets). The helper should still return a string."""

        class _Weird:
            def __str__(self) -> str:
                return "weird-thing"

        # Pydantic-style objects + sets aren't JSON-serializable.
        out = _truncate_json({_Weird()}, max_chars=100)
        assert out  # something rendered, didn't raise

    def test_render_cases_preview_table_renders_without_crashing(self, capsys: Any) -> None:
        """Smoke test: the table renders with realistic entries +
        the title carries the count + mix annotation."""

        entries = [
            {"input": {"question": "What's our refund window?"}, "expected": {"answer": "30 days"}},
            {"input": {"question": "What's our SSO posture?"}, "expected": None},
        ]
        _render_cases_preview_table(entries, mix="standard")
        captured = capsys.readouterr()
        out = captured.out + captured.err
        # Title shows count and mix.
        assert "2" in out
        assert "standard" in out
        # Both inputs render (truncated or not).
        assert "refund window" in out
        assert "SSO posture" in out


# ---------------------------------------------------------------------------
# Preview-cell formatter (2026-05-19) — the upgrade from raw-JSON-dump
# to scannable key:value lines with Rich color tags.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPreviewCellFormatter:
    """The wizard's preview table used to dump raw JSON like
    ``{"decision": "human_review", "risk_score": 0.65, "indicators": [...]}``
    truncated mid-token; operators couldn't read the structure. The
    new formatter renders one cyan-keyed line per top-level dict
    field with type-aware value styling (yellow scalars, dim
    placeholders, list summaries). The Rich color tags are still
    PRESENT in the output — Rich strips them at render time, but the
    raw string returned by the helper carries them so the Table cell
    can measure width correctly.
    """

    def test_format_value_none_renders_dim_null(self) -> None:
        from movate.cli.eval import _format_value  # noqa: PLC0415

        assert _format_value(None, max_chars=20) == "[dim]null[/dim]"

    def test_format_value_bool_and_numbers_are_yellow_scalars(self) -> None:
        from movate.cli.eval import _format_value  # noqa: PLC0415

        assert _format_value(True, max_chars=20) == "[yellow]True[/yellow]"
        assert _format_value(0.65, max_chars=20) == "[yellow]0.65[/yellow]"
        assert _format_value(42, max_chars=20) == "[yellow]42[/yellow]"

    def test_format_value_short_string_passes_through(self) -> None:
        from movate.cli.eval import _format_value  # noqa: PLC0415

        assert _format_value("hello", max_chars=20) == "hello"

    def test_format_value_long_string_truncates_with_ellipsis(self) -> None:
        from movate.cli.eval import _format_value  # noqa: PLC0415

        long = "a" * 100
        out = _format_value(long, max_chars=20)
        assert len(out) == 20
        assert out.endswith("…")

    def test_format_value_list_of_dicts_summarizes_with_first_field_value(self) -> None:
        """Lists of dicts (e.g. ``indicators: [{"code": "damaged_item"},
        ...]``) are the case that motivated this whole refactor.
        Render a 1-line summary using the first dict's first-field
        value as a token so the operator gets a hint at the contents
        instead of just ``list(3)``."""
        from movate.cli.eval import _format_value  # noqa: PLC0415

        value = [
            {"code": "damaged_item", "severity": 0.8},
            {"code": "late_return", "severity": 0.3},
        ]
        out = _format_value(value, max_chars=60)
        # Shows the list length + the first-field tokens.
        assert "list(2)" in out
        assert "damaged_item" in out
        assert "late_return" in out

    def test_format_value_long_list_shows_truncated_summary_with_remainder(self) -> None:
        from movate.cli.eval import _format_value  # noqa: PLC0415

        value = [{"code": f"thing_{i}"} for i in range(8)]
        out = _format_value(value, max_chars=60)
        assert "list(8)" in out
        # First 3 are summarized; the rest are counted into ``+5``.
        assert "+5" in out

    def test_format_value_dict_renders_field_count_placeholder(self) -> None:
        """Nested dicts collapse to a count placeholder — the operator
        can drop into ``-o json`` for the full structure if they need
        to see deeper. Avoids the recursive-rendering rabbit hole."""
        from movate.cli.eval import _format_value  # noqa: PLC0415

        out = _format_value({"a": 1, "b": 2, "c": 3}, max_chars=60)
        assert "3 field(s)" in out

    def test_format_for_preview_cell_dict_renders_one_line_per_field(self) -> None:
        """The whole-cell formatter — dict input becomes ``key: value``
        lines, one per line. This is the actual upgrade the operator
        sees in the preview table."""
        from movate.cli.eval import _format_for_preview_cell  # noqa: PLC0415

        value = {
            "decision": "human_review",
            "risk_score": 0.65,
            "indicators": [{"code": "damaged_item"}],
        }
        out = _format_for_preview_cell(value)
        # Top-level keys are cyan-tagged.
        assert "[cyan]decision[/cyan]: human_review" in out
        assert "[cyan]risk_score[/cyan]: [yellow]0.65[/yellow]" in out
        # Nested list collapses to summary.
        assert "list(1)" in out
        # One line per top-level key.
        assert out.count("\n") == 2

    def test_format_for_preview_cell_none_is_empty_string(self) -> None:
        """``None`` values (e.g. the ``expected`` field on adversarial
        cases where the agent is expected to refuse) render as an
        empty cell — no placeholder noise."""
        from movate.cli.eval import _format_for_preview_cell  # noqa: PLC0415

        assert _format_for_preview_cell(None) == ""

    def test_format_for_preview_cell_truncates_dict_with_too_many_fields(self) -> None:
        """When a dict has more fields than ``max_lines``, show the
        first N + a ``… X more field(s)`` footer so the operator
        knows there's more underneath."""
        from movate.cli.eval import _format_for_preview_cell  # noqa: PLC0415

        value = {f"key_{i}": i for i in range(10)}
        out = _format_for_preview_cell(value, max_lines=3)
        # 3 visible lines + 1 footer = 4 lines (3 newlines).
        assert out.count("\n") == 3
        assert "7 more field(s)" in out


# ---------------------------------------------------------------------------
# Multi-run averaging (2026-05-19) — ``--runs N`` widens the score
# distribution so operators stop seeing every category at exactly 1.00.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRunsPerCaseAveraging:
    """Each case is now scored N times and the per-category scores are
    averaged into a single ``CaseScore`` for the entry. Without this,
    the LLM judge's binary-ish per-roll behavior produces "everything
    1.00" at N=1 — there's no variance signal.
    """

    @pytest.mark.asyncio
    async def test_run_scorecard_averages_scores_across_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Drive ``_run_scorecard`` with ``runs_per_case=3`` against a
        stub judge that returns 1.0 on the first call and 0.0 on the
        second + third. The averaged score should land at 1/3 (~0.333)
        — the case score reflects the mean of the 3 rolls, not the
        last one."""
        from movate.core.loader import load_agent  # noqa: PLC0415
        from movate.core.models import Metrics, RunResponse  # noqa: PLC0415

        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
        agent_dir = tmp_path / "proj" / "agents" / "faq"
        bundle = load_agent(agent_dir)

        class _StubExecutor:
            calls = 0

            async def execute(self, bundle: Any, request: Any, **_kw: Any) -> RunResponse:
                type(self).calls += 1
                return RunResponse(
                    status="success",
                    data={"answer": f"run_{type(self).calls}"},
                    metrics=Metrics(cost_usd=0.001, latency_ms=50),
                )

        class _StubStorage:
            async def close(self) -> None:
                pass

        class _StubRuntime:
            executor = _StubExecutor()
            provider = None
            storage = _StubStorage()
            tracer = None

        async def fake_build_local_runtime(*, mock: bool) -> Any:
            return _StubRuntime()

        async def fake_shutdown(storage: Any, tracer: Any) -> None:
            return None

        monkeypatch.setattr(
            "movate.cli.eval_scorecard_cmd.build_local_runtime",
            fake_build_local_runtime,
        )
        monkeypatch.setattr("movate.cli.eval_scorecard_cmd.shutdown_runtime", fake_shutdown)

        # Judge returns alternating scores: 1.0 on run 0, 0.0 on runs 1+2.
        # Each LLM-judged category gets the same value per call.
        judge_call_count = 0

        async def fake_score_one_case(
            *args: Any, **kwargs: Any
        ) -> tuple[dict[str, float], dict[str, str]]:
            nonlocal judge_call_count
            score = 1.0 if judge_call_count == 0 else 0.0
            judge_call_count += 1
            return (
                dict.fromkeys(LLM_JUDGED_CATEGORIES, score),
                dict.fromkeys(LLM_JUDGED_CATEGORIES, f"run_{judge_call_count}"),
            )

        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._score_one_case", fake_score_one_case)

        from movate.cli.eval_scorecard_cmd import _run_scorecard  # noqa: PLC0415

        # Single entry, scored 3x → expected mean across LLM categories = 1/3.
        pre_generated = [{"input": {"question": "q1"}, "expected": {"answer": "a1"}}]
        summary = await _run_scorecard(
            bundle,
            count=1,
            mix="standard",
            mock=False,
            judge_model=None,
            pre_generated_entries=pre_generated,
            runs_per_case=3,
        )
        # Exactly ONE CaseScore — entries collapse across runs.
        assert summary.count == 1
        assert len(summary.cases) == 1
        # Judge called 3 times (once per run); executor called 3 times.
        assert judge_call_count == 3
        assert _StubExecutor.calls == 3
        # Each LLM-judged category averages 1/3 across the 3 runs.
        for cat in LLM_JUDGED_CATEGORIES:
            assert summary.cases[0].scores[cat] == pytest.approx(1 / 3, abs=1e-9), (
                f"category {cat} should average 1/3, got {summary.cases[0].scores[cat]}"
            )

    @pytest.mark.asyncio
    async def test_run_scorecard_default_runs_is_one_run_per_case(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard: the default (``runs_per_case=1``) must
        execute + judge each case EXACTLY ONCE. Operators not opting
        into multi-run averaging shouldn't see a sudden N-x cost
        multiplier."""
        from movate.core.loader import load_agent  # noqa: PLC0415
        from movate.core.models import Metrics, RunResponse  # noqa: PLC0415

        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
        agent_dir = tmp_path / "proj" / "agents" / "faq"
        bundle = load_agent(agent_dir)

        exec_calls = 0

        class _StubExecutor:
            async def execute(self, bundle: Any, request: Any, **_kw: Any) -> RunResponse:
                nonlocal exec_calls
                exec_calls += 1
                return RunResponse(
                    status="success",
                    data={"answer": "stubbed"},
                    metrics=Metrics(cost_usd=0.0, latency_ms=10),
                )

        class _StubStorage:
            async def close(self) -> None:
                pass

        class _StubRuntime:
            executor = _StubExecutor()
            provider = None
            storage = _StubStorage()
            tracer = None

        async def fake_build_local_runtime(*, mock: bool) -> Any:
            return _StubRuntime()

        async def fake_shutdown(storage: Any, tracer: Any) -> None:
            return None

        monkeypatch.setattr(
            "movate.cli.eval_scorecard_cmd.build_local_runtime",
            fake_build_local_runtime,
        )
        monkeypatch.setattr("movate.cli.eval_scorecard_cmd.shutdown_runtime", fake_shutdown)

        judge_calls = 0

        async def fake_score_one_case(
            *args: Any, **kwargs: Any
        ) -> tuple[dict[str, float], dict[str, str]]:
            nonlocal judge_calls
            judge_calls += 1
            return (
                dict.fromkeys(LLM_JUDGED_CATEGORIES, 0.9),
                dict.fromkeys(LLM_JUDGED_CATEGORIES, "ok"),
            )

        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._score_one_case", fake_score_one_case)

        from movate.cli.eval_scorecard_cmd import _run_scorecard  # noqa: PLC0415

        pre_generated = [
            {"input": {"question": "q1"}, "expected": {"answer": "a1"}},
            {"input": {"question": "q2"}, "expected": {"answer": "a2"}},
        ]
        await _run_scorecard(
            bundle,
            count=2,
            mix="standard",
            mock=False,
            judge_model=None,
            pre_generated_entries=pre_generated,
            # runs_per_case defaults to 1 — no kwarg passed.
        )
        # 2 entries x 1 run = 2 executor calls + 2 judge calls.
        assert exec_calls == 2
        assert judge_calls == 2

    @pytest.mark.asyncio
    async def test_run_scorecard_clamps_runs_to_safe_range(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``runs_per_case=0`` or negative would silently produce zero
        scored cases (the inner loop never enters); ``runs_per_case=999``
        would explode the operator's API bill. The helper clamps to
        [1, 10] so neither footgun is reachable."""
        from movate.core.loader import load_agent  # noqa: PLC0415
        from movate.core.models import Metrics, RunResponse  # noqa: PLC0415

        _scaffold_project_with_agents(tmp_path, monkeypatch, "faq")
        agent_dir = tmp_path / "proj" / "agents" / "faq"
        bundle = load_agent(agent_dir)

        exec_calls = 0

        class _StubExecutor:
            async def execute(self, bundle: Any, request: Any, **_kw: Any) -> RunResponse:
                nonlocal exec_calls
                exec_calls += 1
                return RunResponse(
                    status="success",
                    data={"a": "y"},
                    metrics=Metrics(cost_usd=0.0, latency_ms=1),
                )

        class _StubStorage:
            async def close(self) -> None:
                pass

        class _StubRuntime:
            executor = _StubExecutor()
            provider = None
            storage = _StubStorage()
            tracer = None

        async def fake_build_local_runtime(*, mock: bool) -> Any:
            return _StubRuntime()

        async def fake_shutdown(storage: Any, tracer: Any) -> None:
            return None

        monkeypatch.setattr(
            "movate.cli.eval_scorecard_cmd.build_local_runtime",
            fake_build_local_runtime,
        )
        monkeypatch.setattr("movate.cli.eval_scorecard_cmd.shutdown_runtime", fake_shutdown)

        async def fake_score_one_case(
            *args: Any, **kwargs: Any
        ) -> tuple[dict[str, float], dict[str, str]]:
            return (
                dict.fromkeys(LLM_JUDGED_CATEGORIES, 1.0),
                dict.fromkeys(LLM_JUDGED_CATEGORIES, "ok"),
            )

        monkeypatch.setattr("movate.cli.eval_scorecard_cmd._score_one_case", fake_score_one_case)

        from movate.cli.eval_scorecard_cmd import _run_scorecard  # noqa: PLC0415

        pre_generated = [{"input": {"question": "q1"}, "expected": {"answer": "a1"}}]

        # runs_per_case=0 → clamped to 1.
        exec_calls = 0
        await _run_scorecard(
            bundle,
            count=1,
            mix="standard",
            mock=False,
            judge_model=None,
            pre_generated_entries=pre_generated,
            runs_per_case=0,
        )
        assert exec_calls == 1, "runs_per_case=0 must clamp to 1, not 0"

        # runs_per_case=999 → clamped to 10.
        exec_calls = 0
        await _run_scorecard(
            bundle,
            count=1,
            mix="standard",
            mock=False,
            judge_model=None,
            pre_generated_entries=pre_generated,
            runs_per_case=999,
        )
        assert exec_calls == 10, f"runs_per_case=999 must clamp to 10, got {exec_calls}"


# ---------------------------------------------------------------------------
# Wizard prompt markup escaping (2026-05-19) — Rich was swallowing the
# ``[c]`` (custom) row label as an unrecognized style tag.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPromptMarkupEscaping:
    """The wizard's count + runs prompts include a ``[c]`` row for
    the "custom — type a number" option. Pre-2026-05-19 Rich's
    markup parser treated ``[c]`` as a single-letter style tag and
    silently swallowed it — operators saw the row indented but with
    no key label, breaking the custom-input UX.

    The fix escapes the opening bracket so Rich renders ``[c]`` as
    literal text (numeric keys ``[1]``/``[2]``/``[3]``/``[4]`` also
    get escaped now, for consistency).
    """

    def test_count_prompt_renders_c_label_as_literal_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Drive ``_ask_scorecard_count_and_mix`` enough to trigger
        the row-render loop, capture the Rich Console output, and
        assert the literal ``[c]`` appears."""
        from rich.console import Console  # noqa: PLC0415

        # Replace the module-level console with a recording one so we
        # can inspect what got rendered. Both Prompt.ask calls also
        # need stubbing so the function doesn't try to read stdin.
        recording = Console(record=True, force_terminal=True, width=200)
        monkeypatch.setattr("movate.cli.eval.console", recording)

        # First Prompt = count choice, second = mix choice.
        ask_calls = iter(["1", "1"])
        monkeypatch.setattr("movate.cli.eval.Prompt.ask", lambda *a, **kw: next(ask_calls))

        from movate.cli.eval import _ask_scorecard_count_and_mix  # noqa: PLC0415

        result = _ask_scorecard_count_and_mix()
        # Function returned a normal (count, mix) tuple, not _CANCELLED.
        assert isinstance(result, tuple)

        rendered = recording.export_text()
        # The custom row's bracketed label MUST appear as literal text.
        assert "[c]" in rendered, (
            "the custom row's [c] label must render as literal text "
            f"(Rich was swallowing it). Rendered output:\n{rendered}"
        )
        # The numeric labels stay too (they always worked, but pin
        # the behavior so a future change doesn't break BOTH).
        assert "[1]" in rendered
        assert "[2]" in rendered

    def test_runs_prompt_renders_c_label_as_literal_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same regression on the runs-per-case prompt."""
        from rich.console import Console  # noqa: PLC0415

        recording = Console(record=True, force_terminal=True, width=200)
        monkeypatch.setattr("movate.cli.eval.console", recording)

        monkeypatch.setattr("movate.cli.eval.Prompt.ask", lambda *a, **kw: "1")

        from movate.cli.eval import _prompt_runs_per_case  # noqa: PLC0415

        n = _prompt_runs_per_case()
        assert n == 1

        rendered = recording.export_text()
        assert "[c]" in rendered, "the runs prompt's [c] custom label must render as literal text"
        assert "[1]" in rendered


# ---------------------------------------------------------------------------
# LiteLLM LoggingWorker reset (2026-05-19) — fixes the "Queue is bound
# to a different event loop" RuntimeError in the wizard preview-gen +
# scorecard double-asyncio.run flow.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLiteLLMLoggingWorkerReset:
    """LiteLLM's ``GLOBAL_LOGGING_WORKER`` lazily creates an
    ``asyncio.Queue`` on the first ``acompletion`` call. The queue
    binds to whichever event loop touched it. When an
    ``asyncio.run`` closes that loop, the queue is stranded — the
    next ``asyncio.run`` then crashes on the first ``acompletion``
    with ``RuntimeError: <Queue> is bound to a different event loop``.

    The wizard's preview-gen path (PR #212) introduced a second
    ``asyncio.run`` upstream of the scorecard's, re-creating the bug
    the consolidated-loop fix (PR #197) was meant to prevent.

    ``reset_logging_worker_for_new_event_loop`` clears the worker's
    ``_queue`` + ``_worker_task`` so the next ``asyncio.run`` will
    create fresh state bound to its own loop.
    """

    def test_reset_clears_queue_and_worker_task(self) -> None:
        """Pin the helper's contract: after the call, the worker's
        ``_queue`` and ``_worker_task`` are both ``None`` (forcing
        ``LoggingWorker.start()`` to re-create them on the next
        invocation)."""
        from litellm.litellm_core_utils.logging_worker import (  # noqa: PLC0415
            GLOBAL_LOGGING_WORKER,
        )

        from movate.providers.litellm import (  # noqa: PLC0415
            reset_logging_worker_for_new_event_loop,
        )

        # Simulate the post-first-asyncio.run state: queue + task
        # are non-None sentinels (in real life they're stranded on
        # a closed event loop, but the reset doesn't care WHICH loop
        # they're tied to — it just nulls them).
        sentinel_queue = object()
        sentinel_task = object()
        GLOBAL_LOGGING_WORKER._queue = sentinel_queue  # type: ignore[assignment]
        GLOBAL_LOGGING_WORKER._worker_task = sentinel_task  # type: ignore[assignment]

        reset_logging_worker_for_new_event_loop()

        assert GLOBAL_LOGGING_WORKER._queue is None
        assert GLOBAL_LOGGING_WORKER._worker_task is None

    def test_reset_is_idempotent_when_worker_never_initialized(self) -> None:
        """Safe to call even when the worker hasn't been touched
        yet (queue + task are still None from construction). Should
        be a no-op, not an error."""
        from litellm.litellm_core_utils.logging_worker import (  # noqa: PLC0415
            GLOBAL_LOGGING_WORKER,
        )

        from movate.providers.litellm import (  # noqa: PLC0415
            reset_logging_worker_for_new_event_loop,
        )

        GLOBAL_LOGGING_WORKER._queue = None
        GLOBAL_LOGGING_WORKER._worker_task = None

        # No exception expected.
        reset_logging_worker_for_new_event_loop()
        assert GLOBAL_LOGGING_WORKER._queue is None
        assert GLOBAL_LOGGING_WORKER._worker_task is None

    def test_reset_tolerates_missing_litellm_internals(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If a future LiteLLM rev renames ``GLOBAL_LOGGING_WORKER``
        or drops the ``_queue`` / ``_worker_task`` attributes, the
        helper falls open (no crash) — movate's CLI must not break
        on an upstream refactor."""
        # Patch the module so the import inside the helper raises
        # ImportError. The helper must swallow it.
        import sys  # noqa: PLC0415

        from movate.providers.litellm import (  # noqa: PLC0415
            reset_logging_worker_for_new_event_loop,
        )

        saved = sys.modules.pop("litellm.litellm_core_utils.logging_worker", None)
        try:
            monkeypatch.setitem(
                sys.modules,
                "litellm.litellm_core_utils.logging_worker",
                None,  # forces ImportError on re-import
            )
            # No exception expected even though the worker module is "missing".
            try:
                reset_logging_worker_for_new_event_loop()
            except Exception as exc:
                pytest.fail(f"reset must be best-effort, but raised {type(exc).__name__}: {exc}")
        finally:
            if saved is not None:
                sys.modules["litellm.litellm_core_utils.logging_worker"] = saved


# ---------------------------------------------------------------------------
# citation_accuracy scorecard category (0.8.2.15)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCitationAccuracyCategory:
    """The 11th scorecard category, added 0.8.2.15 to close the
    measurement loop on RAG agents.

    Distinct from ``faithfulness``: faithfulness scores whether the
    answer stays grounded in ANY of the input context; citation_accuracy
    scores whether the SPECIFIC chunks cited by the agent actually
    support the cited claims. A RAG agent that answers correctly but
    cites the wrong chunk should score high on faithfulness, low on
    citation_accuracy — that's the signal this category exists to
    surface.
    """

    def test_citation_accuracy_in_llm_judged_bucket(self) -> None:
        """Lives in the LLM-judged bucket — needs a model to read
        the cited chunks + verify they support the claim."""
        assert "citation_accuracy" in LLM_JUDGED_CATEGORIES
        assert "citation_accuracy" not in PROGRAMMATIC_CATEGORIES

    def test_citation_accuracy_in_all_categories(self) -> None:
        assert "citation_accuracy" in ALL_CATEGORIES

    def test_citation_accuracy_in_default_judge_prompt(self) -> None:
        """The default judge prompt MUST mention the new category
        so judges score all 9 LLM-judged dimensions in one call."""
        from movate.cli.eval_scorecard_cmd import (  # noqa: PLC0415
            _JUDGE_SYSTEM_PROMPT,
            _build_judge_prompt,
        )

        prompt = _build_judge_prompt(LLM_JUDGED_CATEGORIES)
        assert "citation_accuracy" in prompt
        # Description should explain the no-citations no-penalty rule
        # so judges don't dock non-RAG agents.
        assert "no citations are made" in prompt or "no penalty" in prompt
        # And the pre-built default prompt matches the build call.
        assert prompt == _JUDGE_SYSTEM_PROMPT

    def test_disabled_judge_prompt_omits_citation_accuracy(self) -> None:
        """Per-project rubric override can disable ``citation_accuracy``
        for non-RAG projects. The judge prompt must respect the
        narrower active set."""
        from movate.cli.eval_scorecard_cmd import (  # noqa: PLC0415
            _build_judge_prompt,
        )

        active = tuple(c for c in LLM_JUDGED_CATEGORIES if c != "citation_accuracy")
        prompt = _build_judge_prompt(active)
        assert "citation_accuracy" not in prompt
        # The 8 surviving LLM categories still surface.
        for cat in active:
            assert cat in prompt

    def test_gate_config_carries_citation_accuracy(self) -> None:
        """GateConfig must accept ``citation_accuracy`` so the CLI
        flag ``--gate-citation-accuracy`` can plumb through."""
        from movate.cli.eval_scorecard_cmd import GateConfig  # noqa: PLC0415

        gates = GateConfig(citation_accuracy=0.85)
        assert gates.citation_accuracy == 0.85
        assert gates.has_any_gate() is True

    def test_gate_check_flags_low_citation_accuracy(self) -> None:
        """A summary with citation_accuracy below the gate must
        appear in the failures list."""
        from movate.cli.eval_scorecard_cmd import (  # noqa: PLC0415
            GateConfig,
            ScorecardSummary,
        )

        gates = GateConfig(citation_accuracy=0.85)
        summary = ScorecardSummary(
            agent="rag-qa",
            mix="standard",
            count=10,
            cases=[],
            category_means={
                "accuracy": 0.95,
                "citation_accuracy": 0.50,  # below the 0.85 floor
            },
            overall_mean=0.80,
        )
        failures = gates.check(summary)
        # The failure tuple is (category, actual, threshold).
        cited = [f for f in failures if f[0] == "citation_accuracy"]
        assert len(cited) == 1
        assert cited[0][1] == 0.50
        assert cited[0][2] == 0.85

    def test_gate_check_passes_when_citation_accuracy_meets_floor(self) -> None:
        """When citation_accuracy is at-or-above the gate, no failure
        is recorded for that category."""
        from movate.cli.eval_scorecard_cmd import (  # noqa: PLC0415
            GateConfig,
            ScorecardSummary,
        )

        gates = GateConfig(citation_accuracy=0.85)
        summary = ScorecardSummary(
            agent="rag-qa",
            mix="standard",
            count=10,
            cases=[],
            category_means={"citation_accuracy": 0.90},
            overall_mean=0.90,
        )
        failures = gates.check(summary)
        assert not any(f[0] == "citation_accuracy" for f in failures)

    def test_gate_check_skips_citation_accuracy_when_disabled(self) -> None:
        """When the project disabled ``citation_accuracy`` (so the
        summary doesn't carry it), the gate is silently skipped —
        operators don't get spurious failures for opted-out dims."""
        from movate.cli.eval_scorecard_cmd import (  # noqa: PLC0415
            GateConfig,
            ScorecardSummary,
        )

        gates = GateConfig(citation_accuracy=0.85)
        summary = ScorecardSummary(
            agent="rag-qa",
            mix="standard",
            count=10,
            cases=[],
            category_means={"accuracy": 0.95},  # citation_accuracy absent
            overall_mean=0.95,
        )
        failures = gates.check(summary)
        assert not any(f[0] == "citation_accuracy" for f in failures)
