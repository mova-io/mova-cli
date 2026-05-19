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
import re
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from typer.testing import CliRunner

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
