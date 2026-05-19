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
        '"instruction_following": {"score": 1.0, "rationale": "x"}}' + "\n```"
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
    should land on the help, not a stack trace."""
    result = runner.invoke(
        app,
        ["eval-scorecard"],
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
    on a missing-dir traceback."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["eval-scorecard", "--all"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "./agents/" in combined or "agents/" in combined


@pytest.mark.unit
def test_all_empty_agents_dir_vacuous_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A project with ./agents/ but zero agents under it is a
    vacuous-pass (ok=true, agents=0), not an error. Mirrors how
    ``mdk eval --all`` handles the same edge case."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "agents").mkdir()
    result = runner.invoke(
        app,
        ["eval-scorecard", "--all"],
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
    accepts — same path the operator's project goes through."""
    monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
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

    # Title surfaces the reduced count.
    assert "8/10 categories" in flat
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
        capsys: Any,
    ) -> None:
        """``AuthError`` from the provider during probe → ``typer.Exit(2)``
        with the hint-rich message in stderr. The probe stops at the
        first auth failure rather than continuing through other models."""

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

        with pytest.raises(typer.Exit) as excinfo:
            await eval_scorecard_cmd._preflight_check_generator_auth(
                models={"anthropic/claude-haiku-4-5-20251001"}, mock=False
            )
        assert excinfo.value.exit_code == 2
        # Probe ran exactly once before bailing.
        assert probed == ["anthropic/claude-haiku-4-5-20251001"]

        # Hint-rich error message hit stderr.
        captured = capsys.readouterr()
        stderr_plain = _ANSI_RE.sub("", captured.err)
        assert "preflight auth check failed" in stderr_plain
        assert "anthropic/claude-haiku-4-5-20251001" in stderr_plain
        assert "ANTHROPIC_API_KEY" in stderr_plain
        assert "--generator-model" in stderr_plain
        assert "--mock" in stderr_plain
        assert "mdk doctor" in stderr_plain

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
        """``--mock`` propagates through to the preflight call — the
        preflight itself short-circuits, but we still verify the
        wiring carries ``mock=True``."""
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
        assert len(preflight_calls) == 1
        assert preflight_calls[0]["mock"] is True

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
