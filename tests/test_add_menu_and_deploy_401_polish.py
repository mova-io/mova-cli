"""PR #102 — three small polish items from Saturday-afternoon smoke.

1. ``mdk add``'s "What next?" menu had `[s]` swallowed by Rich's
   strikethrough markup. Backslash-escape so it renders literal.
2. ``mdk deploy`` 401 from the runtime printed the raw JSON body —
   give a friendlier hint pointing at ``$MDK_DEV_KEY`` and the
   ``mdk auth save-runtime-key`` recovery path.
3. ``mdk menu``'s "Run <agent>" suggestion used literal ``'{}'`` for
   the input payload. Reuse the same dataset-example helper that
   ``mdk add``'s Panel uses so the menu shows a real input.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from typer.testing import CliRunner

from movate.cli.deploy import _upload_one_agent_bundle
from movate.cli.main import app
from movate.menu.actions import _first_agent_dataset_input

runner = CliRunner(mix_stderr=False)


def _bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    monkeypatch.chdir(tmp_path / "proj")
    return tmp_path / "proj"


# ---------------------------------------------------------------------------
# #3 — _first_agent_dataset_input pulls real example or falls back to '{}'
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFirstAgentDatasetInput:
    def test_returns_first_row_input_when_dataset_present(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "ag"
        (agent_dir / "evals").mkdir(parents=True)
        first_input = {"text": "hello"}
        (agent_dir / "evals" / "dataset.jsonl").write_text(
            json.dumps({"input": first_input, "expected": {}}) + "\n"
        )
        result = _first_agent_dataset_input(agent_dir)
        assert json.loads(result) == first_input

    def test_returns_empty_dict_string_when_no_dataset(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "no-dataset"
        agent_dir.mkdir()
        assert _first_agent_dataset_input(agent_dir) == "{}"

    def test_returns_empty_dict_string_on_malformed_jsonl(self, tmp_path: Path) -> None:
        """Malformed dataset → silent fallback (no exception)."""
        agent_dir = tmp_path / "bad"
        (agent_dir / "evals").mkdir(parents=True)
        (agent_dir / "evals" / "dataset.jsonl").write_text("not valid json\n")
        assert _first_agent_dataset_input(agent_dir) == "{}"

    def test_returns_empty_dict_string_when_row_lacks_input_key(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "incomplete"
        (agent_dir / "evals").mkdir(parents=True)
        (agent_dir / "evals" / "dataset.jsonl").write_text(
            json.dumps({"expected": {"x": 1}}) + "\n"
        )
        assert _first_agent_dataset_input(agent_dir) == "{}"


# ---------------------------------------------------------------------------
# #3 — `mdk menu` surfaces the real dataset input in its Run action
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_menu_run_action_uses_real_dataset_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: after `mdk add ticket-triager` the menu's "Run
    'ticket-triager'" suggestion should include real subject/body
    fields from dataset.jsonl, NOT the literal '{}'."""
    _bootstrap(tmp_path, monkeypatch)
    result = runner.invoke(app, ["add", "ticket-triager"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    # Send 'q' so the menu exits quickly. The action LABEL + command
    # render before the prompt.
    result = runner.invoke(app, ["menu"], input="q\n", env={"COLUMNS": "200"})
    assert "ticket-triager" in result.stdout
    # The command in the menu's Action.command field is rendered with
    # the real dataset[0].input from ticket-triager's evals — which
    # has `subject` + `body` keys. Rich's table renderer can split
    # label-column from command-column onto separate visual lines,
    # so check the entire stdout for the schema-field presence.
    assert "subject" in result.stdout
    # The legacy literal '{}' placeholder should NOT appear next to
    # the agent name (the original bug). Search the rendered command
    # specifically — i.e. any line containing `mdk run ticket-triager`.
    run_command_lines = [
        line for line in result.stdout.splitlines() if "mdk run ticket-triager" in line
    ]
    assert run_command_lines, "mdk run ticket-triager line not found in menu"
    for line in run_command_lines:
        assert "'{}'" not in line, f"legacy '{{}}' placeholder still in: {line!r}"


# ---------------------------------------------------------------------------
# #1 — `[s]` renders literal in the mdk add next-steps menu
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_add_menu_renders_literal_s_bracket() -> None:
    """When the menu's `[s]` Skip line is rendered with the
    backslash-escape, the literal text `[s]` makes it into the
    captured output instead of being eaten by Rich's strikethrough
    markup. We can't easily test the rendered Panel directly (TTY
    gating skips the menu under CliRunner), so verify the source
    string is the escaped form."""
    # Post-PR-#105 the menu rendering lives in the shared helper
    # (src/movate/cli/_next_steps.py) instead of inline in add_cmd.py.
    src = Path("src/movate/cli/_next_steps.py").read_text()
    # The literal source must contain the escaped bracket so Rich
    # doesn't strip it as a tag. Regression-guard: if someone
    # removes the escape, this test fails.
    assert r"\[s]" in src, (
        "menu's [s] Skip line must use a backslash-escaped bracket "
        "(else Rich treats it as a strikethrough tag and swallows it)"
    )


# ---------------------------------------------------------------------------
# #2 — deploy 401 renders friendlier guidance
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_deploy_401_returns_actionable_hint() -> None:
    """When the runtime returns 401, the per-agent error string should
    name `$MDK_DEV_KEY` and point at `mdk auth save-runtime-key` —
    NOT just dump the raw JSON body."""
    # Mock httpx.Client.post to return a 401 with the runtime's
    # canonical auth_required body.
    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.json.return_value = {
        "detail": {"error": {"code": "auth_required", "message": "authentication required"}}
    }
    mock_client = MagicMock()
    mock_client.post.return_value = mock_response

    # Need a minimal agent dir on disk for the function to read files.
    with tempfile.TemporaryDirectory() as tmpdir:
        agent_dir = Path(tmpdir) / "ag"
        (agent_dir / "schema").mkdir(parents=True)
        (agent_dir / "agent.yaml").write_text(
            "api_version: movate/v1\nname: ag\nversion: 0.1.0\n"
            "schema:\n  input: ./schema/input.json\n  output: ./schema/output.json\n"
        )
        (agent_dir / "prompt.md").write_text("Test prompt.\n")
        (agent_dir / "schema" / "input.json").write_text(
            '{"type":"object","properties":{},"additionalProperties":false}'
        )
        (agent_dir / "schema" / "output.json").write_text(
            '{"type":"object","properties":{},"additionalProperties":false}'
        )
        # Make the mock pass `isinstance(client, httpx.Client)` in deploy.py.
        mock_client.__class__ = httpx.Client

        result = _upload_one_agent_bundle(
            client=mock_client,
            base_url="https://fake.example.com",
            headers={"Authorization": "Bearer mvt_live_abcdef0123456789_DEADBEEF_secret"},
            agent_dir=agent_dir,
        )
    assert result is not None
    # Hint mentions the env var.
    assert "$MDK_DEV_KEY" in result or "MDK_DEV_KEY" in result
    # Hint mentions the recovery command.
    assert "save-runtime-key" in result
    # Hint shows a TRUNCATED prefix of the bearer (first 16 chars
    # after `Bearer `; not the whole thing — that would leak the
    # secret into logs).
    assert "mvt_live_abcdef0" in result  # `mvt_live_` (9) + `abcdef0` (7) = 16
    # Full secret NOT echoed.
    assert "DEADBEEF" not in result
    assert "_secret" not in result
