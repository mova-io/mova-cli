"""Tests for the flow-polish batch (post-MVP UX-between-commands).

PR #92 fixed per-command bugs; this batch closes the transitions
between commands so the demo flow tells the user what to do next at
every step.

Covers:

1. ``mdk menu`` recognizes ``project.yaml`` (canonical) — not just
   the legacy ``movate.yaml``.
2. ``mdk add`` no longer prints duplicate next-steps (legacy
   plain-text echo from ``_init_agent`` is suppressed when the add
   Panel renders).
3. ``mdk add`` next-steps include a real example payload from
   ``evals/dataset.jsonl[0].input``, not literal ``{...}``.
4. ``mdk validate --all`` (success path) ends with a "Next: mdk eval
   --all" suggestion.
5. ``mdk eval --all`` (success path) ends with run / serve / deploy
   suggestions.
6. ``mdk deploy`` (dry-run; real deploy needs Azure) success Panel
   shows curl smoke-test commands.
7. ``mdk init`` (project-mode, no agents) mentions ``mdk templates
   list`` as the discovery surface.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.add_cmd import _first_dataset_input
from movate.cli.deploy import _first_agent_name
from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _bootstrap_with_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, template: str = "faq"
) -> Path:
    """Standard fixture — init project + add one agent. Returns project root."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    proj = tmp_path / "proj"
    monkeypatch.chdir(proj)
    result = runner.invoke(app, ["add", template], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    return proj


# ---------------------------------------------------------------------------
# #1 — `mdk menu` recognizes project.yaml
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_menu_recognizes_project_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A project with `project.yaml` (canonical post-PR #85) should
    show as initialized in `mdk menu`, not as `✗ not initialized`.
    Pre-fix the menu only knew about `movate.yaml`/`mdk.yaml`."""
    proj = _bootstrap_with_agent(tmp_path, monkeypatch)
    assert (proj / "project.yaml").is_file()
    # `mdk menu` shows an interactive prompt; pipe an immediate quit.
    result = runner.invoke(app, ["menu"], input="\n", env={"COLUMNS": "200"})
    # The yaml status row should show the green ✓ + project.yaml label,
    # NOT the red ✗ "not initialized" hint.
    assert "project.yaml" in result.stdout
    assert "not initialized" not in result.stdout


# ---------------------------------------------------------------------------
# #2 — `mdk add` no longer prints duplicate next-steps
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_add_prints_next_steps_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`mdk add faq` should render ONE next-steps surface (the
    interactive helper's `Next:` block). Pre-PR-#101 the legacy
    plain-text echo from _init_agent rendered an extra block above
    the Panel; pre-PR-#106 the Panel itself had a static `Next
    steps:` block that duplicated the helper. PR #106 makes the
    helper's `Next:` block the sole surface."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    monkeypatch.chdir(tmp_path / "proj")
    result = runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    # Exactly ONE `Next:` block in stdout.
    assert result.stdout.count("Next:") == 1
    # The legacy `Next steps:` block must NOT appear (would mean we
    # accidentally re-introduced the duplication).
    assert "Next steps:" not in result.stdout


# ---------------------------------------------------------------------------
# #3 — `mdk add` next-steps use a real dataset.jsonl example
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_add_next_step_uses_real_dataset_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mdk add faq` next-step `mdk run` line should embed the actual
    first-row input from `evals/dataset.jsonl`, not literal `{...}`."""
    proj = _bootstrap_with_agent(tmp_path, monkeypatch, template="faq")
    # Re-add through CliRunner to capture stdout (bootstrap already ran).
    # Trigger a fresh add of a different template so we can re-check
    # the just-rendered output.
    result = runner.invoke(app, ["add", "summarizer"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    # Should NOT contain the literal '{...}' placeholder.
    assert "'{...}'" not in result.stdout
    # Should contain real JSON shape — keys from the summarizer dataset.
    # (`text` is the field summarizer's dataset.jsonl[0].input has.)
    assert '"text"' in result.stdout
    _ = proj  # silence unused


@pytest.mark.unit
def test_add_next_step_falls_back_when_no_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a hypothetical template ships without a dataset, the
    fallback `'{...}'` placeholder still renders — never raises."""
    # Direct unit test of the helper — saves wiring a no-dataset
    # template fixture just for this branch.
    assert _first_dataset_input(tmp_path / "nonexistent") == "{...}"
    # Empty dir = no dataset.jsonl either.
    (tmp_path / "empty-agent").mkdir()
    assert _first_dataset_input(tmp_path / "empty-agent") == "{...}"


# ---------------------------------------------------------------------------
# #4 — `mdk validate --all` suggests `mdk eval --all`
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_all_suggests_eval_next(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After an all-pass `mdk validate --all`, the operator should
    see a "Next:" hint pointing at `mdk eval --all`."""
    _bootstrap_with_agent(tmp_path, monkeypatch)
    result = runner.invoke(app, ["validate", "--all"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Next:" in result.stdout
    assert "mdk eval --all" in result.stdout


@pytest.mark.unit
def test_validate_all_silent_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On validation failure, the "Next: eval" hint should NOT fire
    — the operator should fix the failure first, not chain eval."""
    proj = _bootstrap_with_agent(tmp_path, monkeypatch)
    # Sabotage the agent — make agent.yaml unloadable.
    (proj / "agents" / "faq" / "agent.yaml").write_text("garbage: not_valid:")
    result = runner.invoke(app, ["validate", "--all"], env={"COLUMNS": "200"})
    assert result.exit_code != 0
    # The "Next: mdk eval" hint should be absent on the failure path.
    assert "Next:" not in result.stdout or "mdk eval --all" not in result.stdout


# ---------------------------------------------------------------------------
# #5 — `mdk eval --all` suggests run / serve / deploy
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_eval_all_suggests_run_serve_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After an all-pass `mdk eval --all`, the success block lists
    three natural follow-ups: run, serve, deploy."""
    _bootstrap_with_agent(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["eval", "--all", "--mock", "--gate", "0.0"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Next:" in result.stdout
    # All three follow-up paths appear.
    assert "mdk run" in result.stdout
    assert "mdk serve" in result.stdout
    assert "mdk deploy" in result.stdout


# ---------------------------------------------------------------------------
# #6 — `mdk deploy` shows curl smoke-test commands
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_deploy_dry_run_still_works_after_curl_addition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dry-run shouldn't render the curl block (real deploy renders it
    AFTER /healthz polls). Just confirm the dry-run path still emits
    the greppable summary cleanly. The curl block on the real-deploy
    path is exercised in the live-deploy smoke (not automated here —
    needs Azure)."""
    monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
    monkeypatch.chdir(tmp_path)
    # Post-PR-#94: deploy preflight requires a Dockerfile in cwd. Touch
    # one so the preflight passes for this summary-shape test.
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    runner.invoke(
        app,
        [
            "config",
            "add-target",
            "fake",
            "--url",
            "https://fake.example.com",
            "--key-env",
            "FAKE_KEY",
            "--azure-subscription",
            "00000000-0000-0000-0000-000000000000",
            "--azure-resource-group",
            "fake-rg",
            "--azure-acr",
            "fakeacr",
            "--azure-env",
            "dev",
        ],
        env={"COLUMNS": "200"},
    )
    result = runner.invoke(app, ["deploy", "--target", "fake", "--dry-run"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "mdk_deploy_summary:" in combined
    assert "dry_run=true" in combined


@pytest.mark.unit
def test_first_agent_name_helper() -> None:
    """The _first_agent_name() helper that the deploy success block
    uses to build the curl example. Pure filesystem; no Azure."""
    with tempfile.TemporaryDirectory() as tmpdir:
        prev = os.getcwd()
        try:
            os.chdir(tmpdir)
            assert _first_agent_name() is None
            # Empty agents/ dir → still None.
            Path(tmpdir, "agents").mkdir()
            assert _first_agent_name() is None
            # Agent with no agent.yaml → still None.
            Path(tmpdir, "agents", "fake").mkdir()
            assert _first_agent_name() is None
            # Properly-shaped agent → returned by name.
            (Path(tmpdir, "agents", "fake") / "agent.yaml").write_text("name: fake\n")
            assert _first_agent_name() == "fake"
        finally:
            os.chdir(prev)


# ---------------------------------------------------------------------------
# #7 — `mdk init` mentions `mdk templates list`
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_project_mentions_templates_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mdk init <name>` (project mode, no agents) Next-steps Panel
    should mention `mdk templates list` as the discovery surface
    — pre-fix it pointed at the legacy `mdk add --list` (or none)."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "myproj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "mdk templates list" in result.stdout
