"""``mdk init`` shows a `code <path>` hint and `--open` launches VS Code.

Two operator-facing improvements after `mdk init`:

1. **Hint in the success Panel** — when VS Code's `code` shell command
   is on PATH, the success Panel includes a `code <path>` line so the
   operator can launch the editor without retyping the path. Silent
   when `code` isn't installed.

2. **`--open` flag** — `mdk init --project foo --open` (or
   `mdk init my-agent --open`) auto-launches `code <path>` after the
   scaffold succeeds. Warns on stderr (non-fatal) if `code` isn't on
   PATH.

The hint applies across all four scaffold paths: project mode (with
agents / without agents), single-agent template scaffold, and the
LLM-scaffold success panel.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from movate.cli.init import _editor_open_hint, _maybe_auto_open
from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# _editor_open_hint helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEditorOpenHint:
    def test_returns_none_when_code_not_on_path(self, tmp_path: Path) -> None:
        """`code` not installed → no hint."""
        with patch("movate.cli.init.shutil.which", return_value=None):
            assert _editor_open_hint(tmp_path) is None

    def test_returns_hint_when_code_is_on_path(self, tmp_path: Path) -> None:
        """`code` on PATH → returns `code <abs-path>`."""
        with patch("movate.cli.init.shutil.which", return_value="/usr/local/bin/code"):
            result = _editor_open_hint(tmp_path)
            assert result is not None
            assert result == f"code {tmp_path}"


# ---------------------------------------------------------------------------
# _maybe_auto_open helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMaybeAutoOpen:
    def test_does_nothing_when_not_requested(self, tmp_path: Path) -> None:
        """Without --open, the helper is a no-op (returns False)."""
        with patch("movate.cli.init.subprocess.Popen") as mock_popen:
            assert _maybe_auto_open(tmp_path, requested=False) is False
            mock_popen.assert_not_called()

    def test_launches_code_when_requested_and_on_path(self, tmp_path: Path) -> None:
        """With --open + `code` on PATH → fire-and-forget Popen, return True."""
        with (
            patch("movate.cli.init.shutil.which", return_value="/usr/local/bin/code"),
            patch("movate.cli.init.subprocess.Popen") as mock_popen,
        ):
            assert _maybe_auto_open(tmp_path, requested=True) is True
            mock_popen.assert_called_once()
            # First positional arg is the command list.
            args = mock_popen.call_args[0][0]
            assert args == ["/usr/local/bin/code", str(tmp_path)]

    def test_warns_and_returns_false_when_requested_but_no_code(self, tmp_path: Path) -> None:
        """With --open but `code` missing → stderr warning, return False
        (don't block init)."""
        with patch("movate.cli.init.shutil.which", return_value=None):
            assert _maybe_auto_open(tmp_path, requested=True) is False
        # No subprocess attempt — couldn't find the binary.

    def test_returns_false_on_oserror_during_popen(self, tmp_path: Path) -> None:
        """OSError from Popen (binary unexec'able etc.) → return False,
        don't crash init."""
        with (
            patch("movate.cli.init.shutil.which", return_value="/usr/local/bin/code"),
            patch("movate.cli.init.subprocess.Popen", side_effect=OSError("Permission denied")),
        ):
            assert _maybe_auto_open(tmp_path, requested=True) is False


# ---------------------------------------------------------------------------
# Success panels show the `code <path>` hint when `code` is on PATH
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSuccessPanelShowsCodeHint:
    def test_project_panel_shows_code_hint_when_code_on_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Project init Panel includes `code <path>` as a next-step
        when `code` is on PATH."""
        monkeypatch.chdir(tmp_path)
        # Mock `code` as available; everything else passes through.
        original_which = __import__("shutil").which

        def fake_which(name: str) -> str | None:
            if name == "code":
                return "/usr/local/bin/code"
            return original_which(name)

        with patch("movate.cli.init.shutil.which", side_effect=fake_which):
            result = runner.invoke(
                app,
                ["init", "--project", "my-proj", "--skip-snapshot"],
                env={"COLUMNS": "200"},
            )
        assert result.exit_code == 0, result.stdout + result.stderr
        # The hint appears in the next-steps block.
        assert "code my-proj" in result.stdout

    def test_project_panel_omits_code_hint_when_code_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No `code` on PATH → no hint line in the Panel (silent)."""
        monkeypatch.chdir(tmp_path)
        with patch("movate.cli.init.shutil.which", return_value=None):
            result = runner.invoke(
                app,
                ["init", "--project", "my-proj", "--skip-snapshot"],
                env={"COLUMNS": "200"},
            )
        assert result.exit_code == 0
        # No "open in VS Code" reference appears.
        assert "VS Code" not in result.stdout
        assert "code my-proj" not in result.stdout

    def test_agent_mode_legacy_text_shows_code_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk init <agent>` (no project mode) shows the legacy plain-
        text next-steps with `code <path>` appended when code is on PATH."""
        monkeypatch.chdir(tmp_path)
        original_which = __import__("shutil").which

        def fake_which(name: str) -> str | None:
            if name == "code":
                return "/usr/local/bin/code"
            return original_which(name)

        with patch("movate.cli.init.shutil.which", side_effect=fake_which):
            result = runner.invoke(
                app,
                ["init", "my-agent", "-t", "default", "--target", str(tmp_path)],
                env={"COLUMNS": "200"},
            )
        assert result.exit_code == 0, result.stdout + result.stderr
        # The hint appears in the plain-text output.
        expected = f"code {(tmp_path / 'my-agent').resolve()}"
        assert expected in result.stdout

    def test_combined_workspace_panel_shows_code_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--with-agents combined Workspace Panel also shows the hint."""
        monkeypatch.chdir(tmp_path)
        original_which = __import__("shutil").which

        def fake_which(name: str) -> str | None:
            if name == "code":
                return "/usr/local/bin/code"
            return original_which(name)

        with patch("movate.cli.init.shutil.which", side_effect=fake_which):
            result = runner.invoke(
                app,
                [
                    "init",
                    "--project",
                    "support-bot",
                    "--skip-snapshot",
                    "--with-agents",
                    "rag-qa",
                ],
                env={"COLUMNS": "200"},
            )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Workspace ready" in result.stdout
        assert "code support-bot" in result.stdout


# ---------------------------------------------------------------------------
# --open flag launches VS Code
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOpenFlagLaunchesCode:
    def test_open_flag_invokes_popen_for_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--open + project mode → Popen called with the project root."""
        monkeypatch.chdir(tmp_path)
        with (
            patch("movate.cli.init.shutil.which", return_value="/usr/local/bin/code"),
            patch("movate.cli.init.subprocess.Popen") as mock_popen,
        ):
            result = runner.invoke(
                app,
                ["init", "--project", "my-proj", "--skip-snapshot", "--open"],
                env={"COLUMNS": "200"},
            )
            assert result.exit_code == 0, result.stdout + result.stderr
            mock_popen.assert_called_once()
            args = mock_popen.call_args[0][0]
            assert args[0] == "/usr/local/bin/code"
            assert args[1] == str((tmp_path / "my-proj").resolve())

    def test_open_flag_invokes_popen_for_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--open + agent mode → Popen called with the agent dir."""
        monkeypatch.chdir(tmp_path)
        with (
            patch("movate.cli.init.shutil.which", return_value="/usr/local/bin/code"),
            patch("movate.cli.init.subprocess.Popen") as mock_popen,
        ):
            result = runner.invoke(
                app,
                ["init", "my-agent", "-t", "default", "--target", str(tmp_path), "--open"],
                env={"COLUMNS": "200"},
            )
            assert result.exit_code == 0, result.stdout + result.stderr
            mock_popen.assert_called_once()
            args = mock_popen.call_args[0][0]
            assert args[1] == str((tmp_path / "my-agent").resolve())

    def test_open_flag_warns_when_code_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--open but `code` not installed → stderr warning, NOT a hard
        fail. The init itself succeeds; only the auto-open is skipped."""
        monkeypatch.chdir(tmp_path)
        with patch("movate.cli.init.shutil.which", return_value=None):
            result = runner.invoke(
                app,
                ["init", "--project", "my-proj", "--skip-snapshot", "--open"],
                env={"COLUMNS": "200"},
            )
        # Project still created.
        assert result.exit_code == 0, result.stdout + result.stderr
        assert (tmp_path / "my-proj" / "movate.yaml").is_file()
        # Warning on stderr.
        assert "code" in result.stderr
        assert "isn't on PATH" in result.stderr

    def test_no_open_flag_does_not_invoke_popen(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default (no --open) never launches Popen, even when `code`
        is available."""
        monkeypatch.chdir(tmp_path)
        with (
            patch("movate.cli.init.shutil.which", return_value="/usr/local/bin/code"),
            patch("movate.cli.init.subprocess.Popen") as mock_popen,
        ):
            result = runner.invoke(
                app,
                ["init", "--project", "my-proj", "--skip-snapshot"],
                env={"COLUMNS": "200"},
            )
            assert result.exit_code == 0
            mock_popen.assert_not_called()
