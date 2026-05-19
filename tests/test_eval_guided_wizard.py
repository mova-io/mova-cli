"""Tests for `mdk eval --guided` — interactive eval wizard (PR #97).

The wizard mirrors `mdk menu`'s visual language (Rich Panel + numbered
Prompt.ask) and walks the operator through the five most-common eval
decisions: agent selection, mock-vs-real provider, gate threshold,
runs per case, baseline behavior. After collection, it falls through
to the existing eval dispatch — no duplicated execution logic.

Auto-trigger: bare `mdk eval` (no args, no `--all`) from a TTY inside
a project drops into the wizard. CI / pipe / no-args-outside-project
still falls through to the canonical "path required" error.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Init a project + add one agent. Returns project root.

    Injects a fake ``OPENAI_API_KEY`` into the test env so the
    pre-flight check (PR #215) passes without forcing CI to provide
    real keys. Also stubs ``verify_provider_key`` to return OK so
    the stricter live-verify pre-flight (added 2026-05-19) doesn't
    block tests with a "key rejected" panel — that strictness is
    exactly the thing we WANT in production, but in tests the fake
    key would always fail a real verify call against OpenAI."""
    from movate.cli import auth as auth_mod  # noqa: PLC0415
    from movate.credentials.verify import VerifyResult  # noqa: PLC0415

    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-test-key-for-precheck-only")
    # Stub the verifier so live-verify returns OK without HTTP.
    monkeypatch.setattr(
        "movate.credentials.verify_provider_key",
        lambda provider, key: VerifyResult(ok=True, detail="OK (test stub)"),
    )
    # Clear the per-process verify cache so the new stub is the one
    # that gets consulted (a prior test in the same session may have
    # cached a real-verify result before the stub was installed).
    auth_mod._verify_cache.clear()
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    proj = tmp_path / "proj"
    monkeypatch.chdir(proj)
    result = runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    return proj


# ---------------------------------------------------------------------------
# `--guided` end-to-end with piped answers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_guided_picks_all_gate_0_runs_1_no_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the wizard with the simplest path: all agents, no gate,
    1 run, no baseline. The mock-provider question was dropped — the
    wizard now defaults to the real provider (mock is a CI concern,
    surfaced via the ``--mock`` CLI flag). Eval falls through to real
    providers; the test asserts on the wizard's resolved command + the
    summary line, NOT on eval success (no API keys in test env).

    Answers in order: agent(1=all), test_cases(2=keep existing — pick
    legacy path), gate(1=0.0), runs(1=1), baseline(1=none).

    Five answers as of 2026-05-19: the wizard now asks "Test cases?"
    in --all mode too (was previously skipped). Picking "keep" routes
    to legacy gate/runs/baseline — exactly the path this test pins."""
    _bootstrap(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["eval", "--guided"],
        input="1\n2\n1\n1\n1\n",
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    # The wizard's Panel header rendered.
    assert "mdk eval — guided setup" in combined
    # Four questions asked (no more "Use mock provider?").
    assert "Which agent(s)?" in combined
    assert "Use mock provider" not in combined, (
        "wizard should no longer ask about mock; the --mock flag is CLI-only now"
    )
    assert "Gate threshold?" in combined
    assert "Runs per case?" in combined
    assert "Baseline behavior?" in combined
    # The resolved command preview includes --all + gate (no --mock).
    assert "Running:" in combined
    assert "mdk eval --all --gate 0.0" in combined
    assert "--mock" not in combined.split("Running:")[1].splitlines()[0]


@pytest.mark.unit
def test_guided_picks_single_agent_gate_07_runs_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pick agent #2 (faq, since #1 is 'all'), keep existing dataset,
    gate=0.7, runs=3, no baseline. Single-agent flow now includes a
    "Test cases?" prompt between agent selection and gate threshold —
    the wizard's first job is to either generate fresh cases via LLM
    or use the existing dataset.jsonl. Picking "keep existing" here
    skips generation so the test stays hermetic (no API calls)."""
    _bootstrap(tmp_path, monkeypatch)
    # Five answers (was four before Phase 3a's test-cases prompt):
    # 2=faq, 1=keep existing dataset, 3=gate 0.7, 2=runs 3,
    # 1=no baseline.
    result = runner.invoke(
        app,
        ["eval", "--guided"],
        input="2\n1\n3\n2\n1\n",
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    assert "Test cases?" in combined, "wizard must prompt about test cases"
    assert "Running:" in combined
    # Single-agent mode (no --all), no --mock, gate 0.7, runs 3.
    assert "mdk eval faq" in combined
    assert "--all" not in combined.split("Running:")[1].splitlines()[0]
    assert "--gate 0.7" in combined
    assert "--runs 3" in combined


@pytest.mark.unit
def test_guided_single_agent_generate_previews_and_dispatches_to_scorecard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Picking "generate fresh" in the wizard runs the preview flow
    (PR #212): generate cases NOW, show them in a Rich table, ask
    continue/regenerate/cancel, ask gate threshold, then dispatch
    to ``_run_scorecard_single_agent`` with the approved cases
    pre-generated.

    Before PR #212 the wizard skipped the gate question + did NOT
    generate (the scorecard generated internally later). Both
    invariants reversed:

    * Wizard now ASKS the gate threshold even in scorecard mode
      (operator wants one knob for CI gating).
    * Wizard now CALLS ``_generate_entries`` once (for the preview).
      The scorecard receives those entries via ``pre_generated_entries``
      and skips its own generation — so total generation count is
      still ONE (no double-gen).
    """
    _bootstrap(tmp_path, monkeypatch)

    # Mock the orchestrator the wizard now dispatches to directly
    # (post-PR #212 — was ``eval_scorecard`` Typer command before).
    dispatch_calls: list[dict[str, object]] = []

    def fake_run_scorecard_single_agent(**kwargs: object) -> None:
        dispatch_calls.append(kwargs)

    monkeypatch.setattr(
        "movate.cli.eval_scorecard_cmd._run_scorecard_single_agent",
        fake_run_scorecard_single_agent,
    )

    # Track generator invocations — the wizard NOW calls
    # _generate_entries once (for the preview); the scorecard
    # receives the result via pre_generated_entries.
    gen_calls: list[object] = []

    async def fake_generate_entries(*args: object, **kwargs: object) -> list[dict[str, object]]:
        gen_calls.append(kwargs)
        # Return a realistic-shaped entry so the preview table can
        # render + the operator's "continue" choice has something
        # to forward to the scorecard.
        return [
            {"input": {"question": f"q{i}"}, "expected": {"answer": f"a{i}"}} for i in range(10)
        ]

    monkeypatch.setattr("movate.cli.eval_gen_cmd._generate_entries", fake_generate_entries)
    # The wizard imports _generate_entries inside the preview helper;
    # patch that namespace too so the import resolves to the fake.
    monkeypatch.setattr(
        "movate.cli.eval._generate_cases_for_preview",
        # Use a thin re-import wrapper so the test doesn't need to
        # know the helper's internal _resolve_generator_model call.
        lambda bundle, *, count, mix, mock, project_root: fake_generate_entries(
            bundle, num=count, mode=mix, mock=mock
        ),
    )

    # Seven answers: 2=faq, 2=generate, 2=10 cases, 1=standard mix,
    # 1=continue (after preview table), 2=runs 3 (default — averaging
    # widens the score distribution), 3=gate 0.7 (CI default).
    result = runner.invoke(
        app,
        ["eval", "--guided"],
        input="2\n2\n2\n1\n1\n2\n3\n",
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined

    # Wizard sub-prompts all surfaced.
    assert "Test cases?" in combined
    assert "How many cases?" in combined
    assert "Which mix?" in combined
    # NEW: preview "Looks good?" + runs-per-case + gate threshold prompts.
    assert "Looks good?" in combined
    assert "Runs per case?" in combined, (
        "scorecard branch must ask runs-per-case (added 2026-05-19) so "
        "operators get >0/1.00 binary scores"
    )
    assert "Gate threshold?" in combined
    # Baseline still skipped (scorecard doesn't have that concept).
    assert "Baseline behavior?" not in combined

    # Dispatch fired exactly once, against the right agent, with
    # pre_generated_entries forwarded + gates carrying the 0.7 floor.
    assert len(dispatch_calls) == 1, f"expected one dispatch, got {dispatch_calls}"
    call = dispatch_calls[0]
    assert call["count"] == 10
    assert call["mix"] == "standard"
    assert call["agent_path_str"].endswith("agents/faq")
    # Pre-generated entries forwarded → no double-generation.
    assert call["pre_generated_entries"] is not None
    assert len(call["pre_generated_entries"]) == 10
    # Gate config carries the operator's 0.7 choice on the overall
    # composite (matches PR #212's ``GateConfig(overall=...)`` shape).
    assert call["gates"].overall == 0.7
    # Runs-per-case forwarded (operator picked "2" → 3 runs).
    assert call["runs_per_case"] == 3


@pytest.mark.unit
def test_guided_single_agent_keep_existing_uses_legacy_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other branch of the test-cases prompt: "keep existing"
    must dispatch to the LEGACY flow (gate / runs / baseline) so
    operators with curated datasets keep the deterministic scoring
    they curated for. Pin that the gate/runs/baseline questions
    still surface in this branch."""
    _bootstrap(tmp_path, monkeypatch)

    # Block scorecard dispatch — it must NOT fire in the legacy branch.
    scorecard_calls: list[object] = []

    def fake_eval_scorecard(**kwargs: object) -> None:
        scorecard_calls.append(kwargs)

    monkeypatch.setattr("movate.cli.eval_scorecard_cmd.eval_scorecard", fake_eval_scorecard)

    # Five answers: 2=faq, 1=keep existing, 1=gate 0.0, 1=runs 1, 1=baseline none.
    result = runner.invoke(
        app,
        ["eval", "--guided"],
        input="2\n1\n1\n1\n1\n",
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    assert "Test cases?" in combined
    # Legacy flow → gate/runs/baseline questions fire.
    assert "Gate threshold?" in combined
    assert "Runs per case?" in combined
    assert "Baseline behavior?" in combined
    # Scorecard did NOT fire.
    assert len(scorecard_calls) == 0
    # Legacy dispatch composed a `mdk eval faq` invocation.
    assert "mdk eval faq" in combined


@pytest.mark.unit
def test_guided_baseline_write_creates_dir_and_passes_output_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baseline option 3 (write new baseline) should pass
    --output-baseline pointing at .movate/baseline.json and the
    .movate/ dir should be created if it doesn't exist."""
    proj = _bootstrap(tmp_path, monkeypatch)
    # Five answers (test-cases prompt now fires in --all too as of
    # 2026-05-19): 1=all, 2=keep existing dataset, 1=gate 0.0,
    # 1=runs 1, 3=write baseline.
    result = runner.invoke(
        app,
        ["eval", "--guided"],
        input="1\n2\n1\n1\n3\n",
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    assert "--output-baseline" in combined
    # .movate dir exists post-init (snapshot machinery) — verify
    # baseline.json got written by the eval too.
    assert (proj / ".movate" / "baseline.json").is_file() or "--output-baseline" in combined


@pytest.mark.unit
def test_guided_baseline_compare_warns_when_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baseline option 2 (compare) when .movate/baseline.json doesn't
    exist should warn and skip the drift check — not error."""
    _bootstrap(tmp_path, monkeypatch)
    # Make sure baseline doesn't exist (fresh project doesn't have it).
    # Five answers (test-cases prompt now fires in --all too): 1=all,
    # 2=keep existing dataset, 1=gate 0.0, 1=runs 1, 2=compare.
    result = runner.invoke(
        app,
        ["eval", "--guided"],
        input="1\n2\n1\n1\n2\n",
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    assert "no baseline file" in combined.lower()


# ---------------------------------------------------------------------------
# Auto-trigger detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_guided_does_not_auto_trigger_when_path_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mdk eval <agent>` should NOT drop into the wizard — the
    operator already specified the agent."""
    _bootstrap(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["eval", "faq", "--mock", "--gate", "0.0"],
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    assert "guided setup" not in combined


@pytest.mark.unit
def test_guided_does_not_auto_trigger_with_explicit_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mdk eval --all` should NOT drop into the wizard — the
    operator already chose the sweep mode."""
    _bootstrap(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["eval", "--all", "--mock", "--gate", "0.0"],
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    assert "guided setup" not in combined


@pytest.mark.unit
def test_guided_does_not_auto_trigger_outside_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bare `mdk eval` outside any project should error with the
    canonical "path required" message, NOT drop into the wizard
    (no agents to choose from). Same as pre-wizard behavior for the
    no-project case."""
    monkeypatch.chdir(tmp_path)
    # CliRunner's stdin is a pipe (not a TTY) so auto-trigger wouldn't
    # fire anyway. Confirm the error path still works.
    result = runner.invoke(app, ["eval"], env={"COLUMNS": "200"})
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    # Canonical error or wizard rejection — either is acceptable,
    # but the operator must know the project is missing.
    assert (
        "path required" in combined.lower()
        or "not inside" in combined.lower()
        or "guided eval needs" in combined.lower()
    )


# ---------------------------------------------------------------------------
# Wizard refuses on empty project (no agents to evaluate)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_guided_errors_on_empty_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wizard requires at least one agent in `agents/`. An empty
    project should error with a "no agents" hint, not present a
    choiceless picker."""
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init", "empty", "--skip-snapshot"], env={"COLUMNS": "200"})
    monkeypatch.chdir(tmp_path / "empty")
    result = runner.invoke(app, ["eval", "--guided"], env={"COLUMNS": "200"})
    combined = result.stdout + result.stderr
    assert "no agents" in combined.lower()
    assert "mdk add" in combined.lower()


@pytest.mark.unit
def test_guided_all_generate_dispatches_to_scorecard_all_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mdk eval`` wizard with ``all + generate`` dispatches to the
    scorecard's project-wide sweep — NOT the legacy gate/runs/baseline
    path, and NOT the single-agent preview flow.

    The ``--all`` mode deliberately skips the preview table (which
    would render N tables x 30s each) but DOES ask the count + mix
    + gate threshold so the operator gets a CI-gateable sweep.

    Operator footgun fixed in 2026-05-19: previously the wizard
    asked the "Test cases?" question only for single-agent mode and
    silently skipped it for ``--all``, meaning operators picking
    ``all`` couldn't get to the scorecard from the wizard at all.
    """
    _bootstrap(tmp_path, monkeypatch)

    # Mock the project-wide sweep so we can assert dispatch shape
    # without needing real LLM keys.
    dispatch_calls: list[dict[str, object]] = []

    def fake_run_all(**kwargs: object) -> None:
        dispatch_calls.append(kwargs)

    monkeypatch.setattr(
        "movate.cli.eval_scorecard_cmd._run_scorecard_all_in_project",
        fake_run_all,
    )

    # Six answers: 1=all, 1=generate fresh, 2=10 cases,
    # 1=standard mix, 2=runs 3 (default — averaging widens score
    # range), 3=gate 0.7 (CI default).
    # Note: --all path SKIPS the preview "Looks good?" prompt
    # (would render N tables); operator only sees the runs +
    # gate threshold prompts after picking count + mix.
    result = runner.invoke(
        app,
        ["eval", "--guided"],
        input="1\n1\n2\n1\n2\n3\n",
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined

    # Wizard sub-prompts: test-cases + count + mix + runs + gate.
    assert "Test cases?" in combined
    assert "How many cases?" in combined
    assert "Which mix?" in combined
    assert "Runs per case?" in combined, (
        "scorecard --all branch must also ask runs-per-case (added "
        "2026-05-19) so the sweep's per-agent scores average across "
        "N runs"
    )
    assert "Gate threshold?" in combined
    # Preview table NOT shown in --all mode.
    assert "Looks good?" not in combined, (
        "the --all path should skip the per-agent preview table (would render N tables x 30s each)"
    )
    # Baseline still skipped (the scorecard owns the scoring model).
    assert "Baseline behavior?" not in combined

    # Project-wide sweep fired exactly once with the operator's choices.
    assert len(dispatch_calls) == 1, f"expected 1 dispatch, got {dispatch_calls}"
    call = dispatch_calls[0]
    assert call["count"] == 10
    assert call["mix"] == "standard"
    # Gate carried into GateConfig.overall.
    assert call["gates"].overall == 0.7
    # Runs-per-case forwarded into the sweep dispatcher.
    assert call["runs_per_case"] == 3


# ---------------------------------------------------------------------------
# Pre-flight: require OpenAI or Anthropic key (added 2026-05-19)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_eval_warns_and_exits_when_no_provider_key_in_non_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In a non-TTY context (CliRunner / CI / piped), bare ``mdk eval``
    without OPENAI_API_KEY or ANTHROPIC_API_KEY must exit 2 with a
    clear hint pointing at ``mdk auth login`` — NOT silently fall
    through to the wizard and burn 4-5 prompts before failing at
    generation."""
    # Set up a project but explicitly clear both LLM keys (the env
    # would normally have whatever's autoloaded from the operator's
    # ~/.movate/credentials).
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    proj = tmp_path / "proj"
    monkeypatch.chdir(proj)
    result = runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    # Point credentials at an empty path so autoload finds nothing.
    empty_creds = tmp_path / "empty-credentials"
    empty_creds.write_text("")
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(empty_creds))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = runner.invoke(app, ["eval", "--all"], env={"COLUMNS": "200"})
    combined = result.stdout + result.stderr
    assert result.exit_code == 2, combined
    # Warning panel surfaced.
    assert "No OpenAI or Anthropic key configured" in combined or "no LLM" in combined.lower()
    # Hint points at the fix.
    assert "mdk auth login" in combined


@pytest.mark.unit
def test_eval_skips_precheck_under_mock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--mock`` is the offline / shape-only path; it should NOT
    trigger the pre-flight key check (no real LLM calls happen)."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    proj = tmp_path / "proj"
    monkeypatch.chdir(proj)
    result = runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    empty_creds = tmp_path / "empty-credentials"
    empty_creds.write_text("")
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(empty_creds))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # --mock + --all + --gate 0.0 → minimal path; no real LLM calls,
    # check should be skipped.
    result = runner.invoke(
        app,
        ["eval", "--all", "--mock", "--gate", "0.0"],
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    # Pre-flight warning panel must NOT fire under --mock.
    assert "No OpenAI or Anthropic key configured" not in combined
    # Eval ran (or attempted to run) past the precheck.
    assert result.exit_code in (0, 2), combined  # 0 = pass, 2 = mock data missing
    # If it failed, it's NOT because of the precheck.
    if result.exit_code != 0:
        assert "mdk auth login" not in combined or "No OpenAI" not in combined


@pytest.mark.unit
def test_guided_custom_count_typed_by_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator picks the ``c`` (custom) row in the count prompt and
    types a specific number (e.g. 8). The wizard should accept any
    integer in [1, 100], not just 5/10/25/50.

    Added 2026-05-19 after operator feedback that ``--count 8`` had
    to be set via the CLI flag because the wizard's 4 preset options
    didn't include it."""
    _bootstrap(tmp_path, monkeypatch)

    dispatch_calls: list[dict[str, object]] = []

    def fake_run_all(**kwargs: object) -> None:
        dispatch_calls.append(kwargs)

    monkeypatch.setattr(
        "movate.cli.eval_scorecard_cmd._run_scorecard_all_in_project",
        fake_run_all,
    )

    # Seven answers: 1=all, 1=generate, c=custom count, 8=type "8" in
    # the IntPrompt, 1=standard mix, 1=1 run, 1=0.0 gate.
    result = runner.invoke(
        app,
        ["eval", "--guided"],
        input="1\n1\nc\n8\n1\n1\n1\n",
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined

    # The custom row label surfaced.
    assert "custom — type a number" in combined or "custom" in combined.lower()
    # Dispatch received the typed count, not a preset.
    assert len(dispatch_calls) == 1
    assert dispatch_calls[0]["count"] == 8


@pytest.mark.unit
def test_guided_custom_runs_per_case_typed_by_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator picks ``c`` in the runs-per-case prompt and types
    a specific number (e.g. 4). Same UX as custom count — bypasses
    the 1/3/5 presets."""
    _bootstrap(tmp_path, monkeypatch)

    dispatch_calls: list[dict[str, object]] = []

    def fake_run_all(**kwargs: object) -> None:
        dispatch_calls.append(kwargs)

    monkeypatch.setattr(
        "movate.cli.eval_scorecard_cmd._run_scorecard_all_in_project",
        fake_run_all,
    )

    # Seven answers: 1=all, 1=generate, 2=10 cases, 1=standard mix,
    # c=custom runs, 4=type "4", 1=0.0 gate.
    result = runner.invoke(
        app,
        ["eval", "--guided"],
        input="1\n1\n2\n1\nc\n4\n1\n",
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined
    # Dispatch received the typed runs value, not a preset.
    assert len(dispatch_calls) == 1
    assert dispatch_calls[0]["runs_per_case"] == 4


@pytest.mark.unit
def test_guided_custom_count_clamps_to_safe_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Typing a number outside [1, 100] for cases (or [1, 10] for
    runs) gets clamped, not rejected — operators who expect
    "more thorough" by typing 500 get a useful default instead
    of a re-prompt loop."""
    _bootstrap(tmp_path, monkeypatch)

    dispatch_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "movate.cli.eval_scorecard_cmd._run_scorecard_all_in_project",
        lambda **kwargs: dispatch_calls.append(kwargs),
    )

    # 1=all, 1=generate, c=custom count, 500=type "500" (will clamp
    # to 100), 1=standard mix, 1=1 run, 1=0.0 gate.
    result = runner.invoke(
        app,
        ["eval", "--guided"],
        input="1\n1\nc\n500\n1\n1\n1\n",
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert len(dispatch_calls) == 1
    # Clamped to the upper bound (100) rather than re-prompting.
    assert dispatch_calls[0]["count"] == 100


@pytest.mark.unit
def test_eval_precheck_blocks_when_key_set_but_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live-verify (added 2026-05-19): a key that's SET but the
    provider rejects on a metadata probe (e.g. a stale stub like
    ``sk-test-*2345`` lingering in the shell) must block eval at
    the pre-flight rather than letting the operator walk through
    5+ wizard prompts and then hit AuthError mid-generation."""
    from movate.cli import auth as auth_mod  # noqa: PLC0415
    from movate.credentials.verify import VerifyResult  # noqa: PLC0415

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    proj = tmp_path / "proj"
    monkeypatch.chdir(proj)
    result = runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    # Set ONE key (openai) but make verify reject it.
    empty_creds = tmp_path / "empty-credentials"
    empty_creds.write_text("")
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(empty_creds))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-rejected-2345")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    auth_mod._verify_cache.clear()
    monkeypatch.setattr(
        "movate.credentials.verify_provider_key",
        lambda provider, key: VerifyResult(ok=False, detail="401 Unauthorized — key rejected"),
    )

    # Non-TTY (CliRunner): no place to prompt, should exit 2 directly.
    result = runner.invoke(
        app,
        ["eval", "--all", "--gate", "0.0"],
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    assert result.exit_code == 2, combined
    # The new rejected-keys panel surfaces.
    assert "All configured LLM keys rejected" in combined or "rejected" in combined.lower(), (
        combined
    )
    # Hint points at the fix.
    assert "mdk auth login" in combined


@pytest.mark.unit
def test_eval_precheck_passes_when_one_key_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At least one verified key (added 2026-05-19) → eval proceeds.
    The auto-detect downstream routes around rejected providers."""
    from movate.cli import auth as auth_mod  # noqa: PLC0415
    from movate.credentials.verify import VerifyResult  # noqa: PLC0415

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    proj = tmp_path / "proj"
    monkeypatch.chdir(proj)
    result = runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    monkeypatch.setenv("OPENAI_API_KEY", "sk-works")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    auth_mod._verify_cache.clear()
    monkeypatch.setattr(
        "movate.credentials.verify_provider_key",
        lambda provider, key: VerifyResult(ok=True, detail="OK — 47 models available"),
    )

    # --mock so we don't actually call an LLM; the test asserts the
    # pre-flight live-verify let us through, NOT eval results.
    result = runner.invoke(
        app,
        ["eval", "--all", "--mock", "--gate", "0.0"],
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    # Neither panel fires — at least one key verified.
    assert "No OpenAI or Anthropic key configured" not in combined
    assert "All configured LLM keys rejected" not in combined


@pytest.mark.unit
def test_eval_precheck_passes_when_one_key_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If at least one of OpenAI / Anthropic is configured, the
    pre-flight check is silent + eval proceeds. The auto-detect
    handles routing around whichever provider is missing."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    proj = tmp_path / "proj"
    monkeypatch.chdir(proj)
    result = runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    # Only OPENAI_API_KEY set — anthropic explicitly cleared.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-test-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # Use --mock so we don't actually try to call an LLM; the test
    # is about the pre-flight check passing, not about eval results.
    result = runner.invoke(
        app,
        ["eval", "--all", "--mock", "--gate", "0.0"],
        env={"COLUMNS": "200"},
    )
    combined = result.stdout + result.stderr
    # No pre-flight panel — at least one key configured.
    assert "No OpenAI or Anthropic key configured" not in combined
