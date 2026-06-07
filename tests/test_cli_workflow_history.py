"""``mdk workflow history <run_id>`` — event-history export (ADR 054 D6).

The command fetches a Temporal run's durable event history and either prints
it (table or JSON) or exports it to a file via ``--output`` for offline replay
(``mdk workflow replay <run_id> --from-file ...``).

These tests patch ``_fetch_history`` so they exercise the command's
*presentation* logic (format resolution, file export, table rendering, the
unknown-format guard) without a live Temporal server.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli import workflow_cmd
from movate.cli.workflow_cmd import workflow_app

runner = CliRunner(mix_stderr=False)

_FAKE_HISTORY = {
    "events": [
        {
            "eventId": 1,
            "eventType": "WorkflowExecutionStarted",
            "eventTime": "2026-06-07T10:00:00Z",
        },
        {
            "eventId": 2,
            "eventType": "ActivityTaskScheduled",
            "eventTime": "2026-06-07T10:00:01Z",
        },
        {
            "eventId": 3,
            "eventType": "WorkflowExecutionCompleted",
            "eventTime": "2026-06-07T10:00:05Z",
        },
    ]
}


@pytest.fixture(autouse=True)
def _patch_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the live Temporal fetch with a canned history dict."""

    async def _fake_fetch(
        *, run_id: str, target: str | None = None, suppress: bool = False
    ) -> dict:
        return _FAKE_HISTORY

    monkeypatch.setattr(workflow_cmd, "_fetch_history", _fake_fetch)


@pytest.mark.unit
def test_history_export_to_file_writes_json(tmp_path: Path) -> None:
    """``--output`` writes the full history as JSON and confirms the count."""
    out = tmp_path / "history.json"
    result = runner.invoke(workflow_app, ["history", "1f3cabc", "--output", str(out)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert out.is_file()

    written = json.loads(out.read_text())
    assert len(written["events"]) == 3
    assert "exported 3 event(s)" in result.stdout
    # The file path is rendered (Rich may line-wrap it, so match the basename).
    assert out.name in result.stdout


@pytest.mark.unit
def test_history_export_creates_parent_dirs(tmp_path: Path) -> None:
    """``--output`` to a nested path creates intermediate directories."""
    out = tmp_path / "nested" / "deep" / "history.json"
    result = runner.invoke(workflow_app, ["history", "1f3cabc", "-o", str(out)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert out.is_file()


@pytest.mark.unit
def test_history_json_to_stdout() -> None:
    """``--format json`` prints machine-readable JSON to stdout."""
    result = runner.invoke(workflow_app, ["history", "1f3cabc", "--format", "json"])
    assert result.exit_code == 0, result.stdout + result.stderr

    parsed = json.loads(result.stdout)
    assert len(parsed["events"]) == 3


@pytest.mark.unit
def test_history_table_format_renders_event_rows() -> None:
    """``--format table`` renders one row per event with the event type."""
    result = runner.invoke(workflow_app, ["history", "1f3cabc", "--format", "table"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "WorkflowExecutionStarted" in result.stdout
    assert "WorkflowExecutionCompleted" in result.stdout
    assert "3 events" in result.stdout


@pytest.mark.unit
def test_history_table_with_output_also_exports(tmp_path: Path) -> None:
    """Table format + ``--output`` renders the table AND writes the JSON file."""
    out = tmp_path / "history.json"
    result = runner.invoke(
        workflow_app, ["history", "1f3cabc", "--format", "table", "--output", str(out)]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "WorkflowExecutionStarted" in result.stdout
    assert out.is_file()
    assert "also exported" in result.stdout


@pytest.mark.unit
def test_history_unknown_format_exits_2() -> None:
    """An unknown ``--format`` value is a clean exit 2, not a crash."""
    result = runner.invoke(workflow_app, ["history", "1f3cabc", "--format", "yaml"])
    assert result.exit_code == 2
