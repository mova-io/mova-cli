"""``mdk init --project`` next-steps now surface ``mdk serve``.

Once a project has agents in it, the FastAPI runtime (``mdk serve``)
becomes a natural next move — host the agents over HTTP, hit them
from the Angular UI / a curl probe / Postman. Pre-bundle that wasn't
mentioned in the success Panel and operators had to dig through
``mdk --help`` to discover the command.

The hint fires only in the two paths where agents are already in
place:

* ``mdk init --project foo --with-agents X,Y,Z`` → combined Workspace
  Panel includes ``mdk serve``.
* ``mdk init --project foo`` followed by agents added later — the
  initial Panel runs the "no agents yet, go add some" branch, so we
  DON'T mention serve there (operators would hit empty /agents).

Tests cover both arms.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


@pytest.mark.unit
class TestInitSuggestsServe:
    def test_combined_workspace_panel_mentions_mdk_serve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk init --project foo --with-agents X` → combined Panel
        next-steps include `mdk serve`."""
        monkeypatch.chdir(tmp_path)
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
        # `mdk serve` appears as a next-step.
        assert "mdk serve" in result.stdout
        # And the comment explains WHY (HTTP runtime, not deploy).
        assert "POST /run" in result.stdout or "HTTP" in result.stdout

    def test_no_agents_panel_does_not_mention_serve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk init --project foo` (NO --with-agents) → Panel
        focuses on `mdk add --list`, NOT on serve. Serving with zero
        agents would give the operator an empty /agents endpoint
        and that's confusing."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "--project", "empty-bot", "--skip-snapshot"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # `mdk add --list` is the headline next-step.
        assert "mdk add --list" in result.stdout
        # `mdk serve` is NOT mentioned — agents aren't there yet.
        # (Operator can run `mdk add` then re-run init context to
        # see the with-agents Panel suggest serve, OR just discover
        # it via `mdk --help` once they're past the empty state.)
        assert "mdk serve" not in result.stdout

    def test_agent_mode_legacy_text_does_not_mention_serve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk init <agent>` (single-agent template scaffold, no
        project mode) doesn't suggest serve either — single-agent
        scaffolds are usually for quick iteration, not for hosting."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "my-agent", "-t", "default", "--target", str(tmp_path)],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # Legacy plain-text next-steps still show validate + run.
        assert "movate validate" in result.stdout
        # Serve is NOT in the suggestion.
        assert "mdk serve" not in result.stdout
        assert "movate serve" not in result.stdout
