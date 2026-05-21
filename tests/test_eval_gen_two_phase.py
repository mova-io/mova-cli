"""Tests for the two-phase eval-gen preview flow.

Phase 1: _generate_inputs_only — returns inputs without calling the agent.
Phase 2: _execute_inputs — runs the agent to capture expected outputs.
Preview: _print_inputs_preview — renders inputs as a Rich table.

The CLI flow:
  1. Phase 1 progress bar + input generation
  2. _print_inputs_preview() table
  3. Optional Confirm.ask() gate (skipped when --yes or non-TTY)
  4. Phase 2 progress bar + agent execution
  5. _print_entries_preview() of full entries
  6. Write JSONL file
"""

from __future__ import annotations

import asyncio
import json
import shutil
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

import movate.cli.eval_gen_cmd as eval_gen_cmd_mod
from movate.cli.eval_gen_cmd import (
    _INPUTS_PREVIEW_MAX,
    _execute_inputs,
    _generate_inputs_only,
    _print_inputs_preview,
)
from movate.cli.main import app
from movate.core.loader import load_agent

runner = CliRunner(mix_stderr=False)

_TEMPLATE = Path(__file__).parent.parent / "src" / "movate" / "templates" / "agent_init"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _scaffold_agent(dst: Path, name: str = "demo") -> Path:
    shutil.copytree(_TEMPLATE, dst)
    yaml_path = dst / "agent.yaml"
    yaml_path.write_text(yaml_path.read_text().replace("__AGENT_NAME__", name))
    return dst


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "movate.yaml").write_text("api_version: movate/v1\nkind: Project\nname: t\n")
    _scaffold_agent(tmp_path / "agents" / "demo", name="demo")
    monkeypatch.setenv("MOVATE_DB", str(tmp_path / "test.db"))
    return tmp_path


# ---------------------------------------------------------------------------
# _generate_inputs_only — Phase 1 unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_generate_inputs_only_returns_inputs_without_entries(project: Path) -> None:
    """Phase 1 returns a list of input dicts, not full {input, expected} entries."""
    bundle = load_agent(project / "agents" / "demo")
    inputs = asyncio.run(
        _generate_inputs_only(bundle, num=3, sample_input=None, mock=True)
    )
    assert len(inputs) == 3
    for inp in inputs:
        assert isinstance(inp, dict)
        # Inputs are NOT entries — no "expected" key yet
        assert "expected" not in inp
        assert "generated" not in inp


@pytest.mark.unit
def test_generate_inputs_only_on_progress_callback(project: Path) -> None:
    """on_progress is called once per successful input."""
    bundle = load_agent(project / "agents" / "demo")
    calls: list[tuple[int, int]] = []

    def _cb(cur: int, tot: int) -> None:
        calls.append((cur, tot))

    asyncio.run(
        _generate_inputs_only(bundle, num=4, sample_input=None, mock=True, on_progress=_cb)
    )
    assert len(calls) == 4
    # Progress should be monotonically increasing
    for i, (cur, _tot) in enumerate(calls):
        assert cur == i + 1


# ---------------------------------------------------------------------------
# _execute_inputs — Phase 2 unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_execute_inputs_wraps_inputs_into_entries(project: Path) -> None:
    """Phase 2 augments each input with expected + generated flag."""
    bundle = load_agent(project / "agents" / "demo")
    inputs = asyncio.run(
        _generate_inputs_only(bundle, num=2, sample_input=None, mock=True)
    )
    entries = asyncio.run(
        _execute_inputs(bundle, inputs, mock=True, with_dimensions=False)
    )
    assert len(entries) == 2
    for entry in entries:
        assert "input" in entry
        assert "expected" in entry
        assert entry["generated"] is True


@pytest.mark.unit
def test_execute_inputs_on_progress_callback(project: Path) -> None:
    """on_progress fires once per agent call in Phase 2."""
    bundle = load_agent(project / "agents" / "demo")
    inputs = asyncio.run(
        _generate_inputs_only(bundle, num=3, sample_input=None, mock=True)
    )
    calls: list[tuple[int, int]] = []

    def _cb(cur: int, tot: int) -> None:
        calls.append((cur, tot))

    asyncio.run(
        _execute_inputs(bundle, inputs, mock=True, with_dimensions=False, on_progress=_cb)
    )
    assert len(calls) == 3
    assert calls[-1] == (3, 3)


@pytest.mark.unit
def test_execute_inputs_refusal_mode_tags_entries(project: Path) -> None:
    """mode='refusal' sets refusal_expected: True on each entry."""
    bundle = load_agent(project / "agents" / "demo")
    inputs = asyncio.run(
        _generate_inputs_only(bundle, num=2, sample_input=None, mock=True, mode="refusal")
    )
    entries = asyncio.run(
        _execute_inputs(bundle, inputs, mock=True, with_dimensions=False, mode="refusal")
    )
    for entry in entries:
        assert entry.get("refusal_expected") is True
        assert entry.get("mode") == "refusal"


@pytest.mark.unit
def test_execute_inputs_non_standard_mode_tags_entries(project: Path) -> None:
    """mode other than 'standard' is written into each entry's 'mode' field."""
    bundle = load_agent(project / "agents" / "demo")
    inputs = asyncio.run(
        _generate_inputs_only(bundle, num=2, sample_input=None, mock=True, mode="edge")
    )
    entries = asyncio.run(
        _execute_inputs(bundle, inputs, mock=True, with_dimensions=False, mode="edge")
    )
    for entry in entries:
        assert entry.get("mode") == "edge"


# ---------------------------------------------------------------------------
# _print_inputs_preview — display tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPrintInputsPreview:
    def _make_inputs(self, n: int) -> list[dict[str, Any]]:
        return [{"text": f"query number {i}", "priority": i} for i in range(1, n + 1)]

    def test_renders_without_error_standard_mode(self) -> None:
        inputs = self._make_inputs(3)
        # Should not raise
        _print_inputs_preview(inputs, mode="standard", num_requested=3)

    def test_renders_without_error_adversarial_mode(self) -> None:
        inputs = self._make_inputs(2)
        _print_inputs_preview(inputs, mode="adversarial", num_requested=5)

    def test_mode_column_absent_for_standard(self) -> None:
        """Standard mode omits the Mode column — less visual noise."""
        buf = StringIO()
        con = Console(file=buf, highlight=False)
        with patch.object(eval_gen_cmd_mod, "console", con):
            inputs = self._make_inputs(2)
            _print_inputs_preview(inputs, mode="standard", num_requested=2)
        output = buf.getvalue()
        # "Mode" header should NOT appear for standard mode
        assert "Mode" not in output

    def test_mode_column_present_for_adversarial(self) -> None:
        buf = StringIO()
        con = Console(file=buf, highlight=False)
        with patch.object(eval_gen_cmd_mod, "console", con):
            inputs = self._make_inputs(2)
            _print_inputs_preview(inputs, mode="adversarial", num_requested=2)
        output = buf.getvalue()
        assert "Mode" in output

    def test_truncation_note_shown_when_over_max(self) -> None:
        buf = StringIO()
        con = Console(file=buf, highlight=False, width=200)
        with patch.object(eval_gen_cmd_mod, "console", con):
            inputs = self._make_inputs(_INPUTS_PREVIEW_MAX + 5)
            _print_inputs_preview(inputs, mode="standard", num_requested=_INPUTS_PREVIEW_MAX + 5)
        output = buf.getvalue()
        assert "more input" in output

    def test_no_truncation_note_at_or_below_max(self) -> None:
        buf = StringIO()
        con = Console(file=buf, highlight=False, width=200)
        with patch.object(eval_gen_cmd_mod, "console", con):
            inputs = self._make_inputs(_INPUTS_PREVIEW_MAX)
            _print_inputs_preview(inputs, mode="standard", num_requested=_INPUTS_PREVIEW_MAX)
        output = buf.getvalue()
        assert "more input" not in output

    def test_title_shows_generated_count_and_requested(self) -> None:
        buf = StringIO()
        con = Console(file=buf, highlight=False, width=200)
        with patch.object(eval_gen_cmd_mod, "console", con):
            # Request 10 but only 7 validated
            inputs = self._make_inputs(7)
            _print_inputs_preview(inputs, mode="standard", num_requested=10)
        output = buf.getvalue()
        assert "7" in output
        assert "10" in output


# ---------------------------------------------------------------------------
# CLI two-phase flow — integration tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_two_phase_mock_skips_confirm(project: Path) -> None:
    """--mock + non-TTY bypasses the confirmation gate; file is written end-to-end."""
    result = runner.invoke(
        app,
        ["eval-gen", "demo", "--num", "3", "--mock", "--project-root", str(project)],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    out = project / "evals" / "demo" / "dataset.generated.jsonl"
    assert out.is_file()
    lines = [ln for ln in out.read_text().splitlines() if ln.strip()]
    assert len(lines) == 3
    for ln in lines:
        entry = json.loads(ln)
        assert "input" in entry
        assert "expected" in entry
        assert entry["generated"] is True


@pytest.mark.unit
def test_cli_yes_flag_skips_confirm(project: Path) -> None:
    """--yes suppresses the Phase 1→2 confirmation prompt."""
    result = runner.invoke(
        app,
        [
            "eval-gen",
            "demo",
            "--num",
            "2",
            "--mock",
            "--yes",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    out = project / "evals" / "demo" / "dataset.generated.jsonl"
    assert out.is_file()


@pytest.mark.unit
def test_cli_two_phase_outputs_phase_labels(project: Path) -> None:
    """Both 'Phase 1' and 'Phase 2' labels appear in stdout output."""
    result = runner.invoke(
        app,
        ["eval-gen", "demo", "--num", "2", "--mock", "--project-root", str(project)],
    )
    assert result.exit_code == 0
    assert "Phase 1" in result.output
    assert "Phase 2" in result.output


@pytest.mark.unit
def test_cli_abort_on_confirm_no(project: Path) -> None:
    """Answering 'n' at the confirmation gate exits cleanly with no file written.

    CliRunner doesn't expose a real TTY so we patch sys inside the module
    to simulate an interactive session, then mock Confirm.ask to return False.
    """
    mock_sys = MagicMock()
    mock_sys.stdin.isatty.return_value = True
    mock_sys.stdout.isatty.return_value = True

    with (
        patch.object(eval_gen_cmd_mod, "sys", mock_sys),
        patch.object(eval_gen_cmd_mod, "Confirm") as mock_confirm_cls,
    ):
        mock_confirm_cls.ask.return_value = False
        result = runner.invoke(
            app,
            ["eval-gen", "demo", "--num", "2", "--mock", "--project-root", str(project)],
        )
    # Exit 0 (user chose to abort, not an error)
    assert result.exit_code == 0
    out = project / "evals" / "demo" / "dataset.generated.jsonl"
    # No file written when aborted
    assert not out.exists()


@pytest.mark.unit
def test_cli_two_phase_inputs_preview_printed_before_entries_preview(
    project: Path,
) -> None:
    """_print_inputs_preview fires before _print_entries_preview in stdout."""
    call_order: list[str] = []

    orig_inputs_preview = _print_inputs_preview

    def _spy_inputs(inputs: list[Any], *, mode: str, num_requested: int) -> None:
        call_order.append("inputs_preview")
        orig_inputs_preview(inputs, mode=mode, num_requested=num_requested)

    with (
        patch.object(eval_gen_cmd_mod, "_print_inputs_preview", side_effect=_spy_inputs),
        patch.object(eval_gen_cmd_mod, "_print_entries_preview") as mock_entries,
    ):
        mock_entries.side_effect = lambda entries: call_order.append("entries_preview")
        result = runner.invoke(
            app,
            [
                "eval-gen",
                "demo",
                "--num",
                "2",
                "--mock",
                "--project-root",
                str(project),
            ],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert call_order == ["inputs_preview", "entries_preview"]
