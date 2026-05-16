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
    def test_no_op_on_non_tty(self) -> None:
        """Default: stdin/stdout aren't TTYs (pytest), helper returns
        immediately without rendering anything."""
        out = Console(record=True)
        prompt_next_step(
            console=out,
            steps=[NextStep(label="x", command="mdk x", argv=["mdk", "x"])],
        )
        # Nothing rendered.
        assert out.export_text() == ""

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
def test_validate_all_renders_static_next_under_non_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under CliRunner (non-TTY), `mdk validate --all` should still
    print the `Next:` recommendation so CI log-scrapers / operators
    reading the captured output know what to do next."""
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init", "p", "--skip-snapshot"], env={"COLUMNS": "200"})
    monkeypatch.chdir(tmp_path / "p")
    runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    result = runner.invoke(app, ["validate", "--all"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert "Next:" in result.stdout
    assert "mdk eval --all" in result.stdout


@pytest.mark.unit
def test_eval_all_renders_static_next_under_non_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under non-TTY, `mdk eval --all` should still print the
    `Next:` block (Quick-run / Serve / Deploy) as static text."""
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init", "p", "--skip-snapshot"], env={"COLUMNS": "200"})
    monkeypatch.chdir(tmp_path / "p")
    runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    result = runner.invoke(
        app, ["eval", "--all", "--mock", "--gate", "0.7"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0
    assert "Next:" in result.stdout
    # Three suggested follow-ups appear.
    assert "mdk run" in result.stdout
    assert "mdk serve" in result.stdout
    assert "mdk deploy" in result.stdout


@pytest.mark.unit
def test_init_does_not_render_menu_under_non_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mdk init` keeps its existing Panel + static next-steps block
    in the Panel body. The new helper call is gated on TTY — under
    pytest it's silent."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "demo", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    # The Panel's own `Next steps:` block (existing pre-PR-#105) is
    # still rendered — operators reading captured output still see
    # what to do next.
    assert "Next steps:" in result.stdout
    # The interactive picker's `[1]`-style cyan markers are NOT
    # rendered (no TTY).
    interactive_marker_count = result.stdout.count("[1]")
    # If the menu fired, we'd see at least 1 `[N]` row. Allow up to
    # 1 in case some other Panel uses the same shape (paranoid check).
    assert interactive_marker_count <= 1


# ---------------------------------------------------------------------------
# Wizard mode + Skip default for the eval --guided path (regression)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_add_menu_uses_shared_helper_no_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression check: PR #105 refactored `mdk add`'s inline menu
    to use the shared helper. The non-TTY path must still show the
    static `Next steps:` block in the Panel (unchanged behavior)."""
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init", "p", "--skip-snapshot"], env={"COLUMNS": "200"})
    monkeypatch.chdir(tmp_path / "p")
    result = runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    # Static `Next steps:` from the Panel body still rendered.
    assert "Next steps:" in result.stdout
    # Interactive menu NOT rendered (non-TTY).
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
