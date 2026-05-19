"""PR #105 — shared interactive "what next?" menu across init / validate / eval.

PR #101 added the menu to ``mdk add``. PR #105 extracts the helper to
``src/movate/cli/_next_steps.py`` and wires it into ``mdk init`` /
``mdk validate --all`` / ``mdk eval --all`` so the same picker pattern
is used everywhere. Each command keeps a static `Next:` fallback line
for non-TTY callers (CI, pipes, pytest) so log-scrapers don't break.

Tested here:

1. The shared :class:`NextStep` + :func:`prompt_next_step` helper:
   no-op on non-TTY; renders + prompts on TTY (mocked).
2. Static fallback for non-TTY: ``mdk validate --all`` /
   ``mdk eval --all`` still print a `Next:` recommendation that
   log-scrapers can grep.
3. End-to-end: each command still works without the menu firing.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from movate.cli._next_steps import NextStep, mdk_bin_name, prompt_next_step
from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPromptNextStepHelper:
    def test_renders_list_under_non_tty(self) -> None:
        """Post-PR-#106 the helper renders the `Next:` list in BOTH
        TTY and non-TTY modes — only the prompt is gated. Non-TTY
        callers get the list as documentation; scripts can grep it."""
        out = Console(record=True)
        prompt_next_step(
            console=out,
            steps=[NextStep(label="Foo", command="mdk foo", argv=["mdk", "foo"])],
        )
        rendered = out.export_text()
        assert "Next:" in rendered
        assert "Foo" in rendered
        assert "mdk foo" in rendered

    def test_no_op_on_empty_steps(self) -> None:
        out = Console(record=True)
        prompt_next_step(console=out, steps=[])
        assert out.export_text() == ""


@pytest.mark.unit
def test_mdk_bin_name_defaults_to_mdk() -> None:
    """Without a `movate` invocation, the helper resolves to `mdk`."""
    name = mdk_bin_name()
    assert name in ("mdk", "movate")


# ---------------------------------------------------------------------------
# Static fallback (non-TTY behavior)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_all_renders_doctor_in_next_under_non_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under CliRunner (non-TTY), ``mdk validate --all`` still prints
    the ``Next:`` recommendation so CI log-scrapers / operators
    reading captured output see follow-ups.

    Post-2026-05-19 the menu is DOMAIN-SCOPED: only ``mdk doctor`` /
    ``mdk doctor agent <name>`` surface (diagnostic / autofix). The
    pre-scoping eval-suggestion is gone (operator feedback: out of
    domain). Companion regression in test_flow_polish_batch."""
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init", "p", "--skip-snapshot"], env={"COLUMNS": "200"})
    monkeypatch.chdir(tmp_path / "p")
    runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    result = runner.invoke(app, ["validate", "--all"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert "Next:" in result.stdout
    assert "mdk doctor" in result.stdout
    # eval is out of validate's domain — must not surface.
    assert "mdk eval --all" not in result.stdout


@pytest.mark.unit
def test_eval_all_does_not_render_next_step_menu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-2026-05-19, ``mdk eval --all`` does NOT render the
    Quick-run / Serve / Deploy next-step menu.

    Operators reported the menu was noise after a green eval — they
    already know what comes next (run, serve, deploy) and the extra
    prompt cluttered the scrollback right when the agents-table was
    the most interesting thing on screen. The greppable
    ``mdk_eval_all_summary`` line is the only post-eval output now;
    CI scripts that scrape it are unaffected.

    NB: ``mdk validate --all``'s next-step menu is unchanged — that
    one points at ``mdk eval --all`` which IS the next step, and
    surfaces the gate-threshold question for operators who haven't
    used eval before. The eval-itself menu was the one with no
    obvious follow-up to nudge toward.
    """
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init", "p", "--skip-snapshot"], env={"COLUMNS": "200"})
    monkeypatch.chdir(tmp_path / "p")
    runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    result = runner.invoke(
        app, ["eval", "--all", "--mock", "--gate", "0.7"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0
    # ``Next:`` block intentionally absent — the agents table + the
    # ``mdk_eval_all_summary`` line are the whole post-eval output.
    assert "Quick-run" not in result.stdout, (
        "Quick-run/Serve/Deploy menu should be suppressed after eval (operator-reported noise)"
    )
    assert "Serve runtime locally" not in result.stdout
    assert "Deploy agents to Azure dev" not in result.stdout
    # The summary line MUST still surface (CI scrapers depend on it).
    assert "mdk_eval_all_summary" in result.stdout


@pytest.mark.unit
def test_init_renders_next_steps_under_non_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-PR-#106 the helper renders the `Next:` list in both
    modes (only the prompt is TTY-gated). Under pytest (non-TTY)
    we should see the numbered list as documentation, but no
    interactive prompt fires."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "demo", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    # Helper's `Next:` list rendered.
    assert "Next:" in result.stdout
    # Numbered rows present.
    assert "[1]" in result.stdout
    # Skip marker too (proof the helper got past the early return).
    assert "[s]" in result.stdout
    # The legacy duplicate `Next steps:` block (inside Panel body)
    # is GONE. Asserting on the legacy string catches regressions.
    assert "Next steps:" not in result.stdout


# ---------------------------------------------------------------------------
# Wizard mode + Skip default for the eval --guided path (regression)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_add_menu_uses_shared_helper_no_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression check: PR #105 refactored `mdk add`'s inline menu
    to use the shared helper. Post-PR-#106 the helper's `Next:`
    list renders in both modes; the legacy in-Panel `Next steps:`
    block is gone."""
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init", "p", "--skip-snapshot"], env={"COLUMNS": "200"})
    monkeypatch.chdir(tmp_path / "p")
    result = runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    # Helper-rendered `Next:` surface present.
    assert "Next:" in result.stdout
    # Legacy in-Panel `Next steps:` block GONE (would be a regression).
    assert "Next steps:" not in result.stdout
    # Legacy `What next?` header from PR #101's inline version also
    # gone — the helper uses `Next:`.
    assert "What next?" not in result.stdout


# ---------------------------------------------------------------------------
# Interactive path (TTY simulated)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_prompt_next_step_renders_under_simulated_tty() -> None:
    """When stdin + stdout are TTYs and the operator picks `s`
    (Skip default), the helper renders the menu and returns
    without shelling out."""
    out = Console(record=True, force_terminal=True)

    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("sys.stdout.isatty", return_value=True),
        patch("rich.prompt.Prompt.ask", return_value="s"),
    ):
        prompt_next_step(
            console=out,
            steps=[
                NextStep(label="Foo", command="mdk foo", argv=["mdk", "foo"]),
                NextStep(label="Bar", command="mdk bar", argv=["mdk", "bar"]),
            ],
        )
    rendered = out.export_text()
    # All three rows + Skip marker visible.
    assert "Foo" in rendered
    assert "Bar" in rendered
    assert "[s]" in rendered
    assert "Next:" in rendered
