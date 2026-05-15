"""Bundle B — `mdk doctor` interactive fix handoff + `mdk doctor agent <name>`.

Two new behaviors:

1. **Interactive `mdk fix` handoff** — after the doctor table + summary
   line, if there are fixable findings AND stdin is a TTY, prompt to
   run `mdk fix --apply`. `--no-fix-prompt` skips. Non-TTY (CI) skips.
2. **`mdk doctor agent <name>`** — per-agent health check: loads,
   renders prompt against first dataset row, prices the model, resolves
   skills + contexts, counts dataset rows, checks baseline + last run.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.doctor import _resolve_agent_dir
from movate.cli.main import app
from movate.fixes.registry import FixResult, FixStatus

runner = CliRunner(mix_stderr=False)


def _bootstrap_project(tmp_path: Path) -> Path:
    """Minimal valid project — same fixture pattern as test_add_cmd.py."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "movate.yaml").write_text("agents_dir: ./agents\n")
    (proj / "agents").mkdir()
    return proj


def _scaffold_test_agent(proj: Path, name: str = "test-agent") -> Path:
    """Drop a minimal agent under proj/agents/<name>/."""
    agent_dir = proj / "agents" / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "schema").mkdir()
    (agent_dir / "evals").mkdir()
    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": name,
                "version": "0.1.0",
                "model": {"provider": "openai/gpt-4o-mini-2024-07-18"},
                "prompt": "./prompt.md",
                "schema": {
                    "input": "./schema/input.json",
                    "output": "./schema/output.json",
                },
                "evals": {"dataset": "./evals/dataset.jsonl"},
            }
        )
    )
    (agent_dir / "prompt.md").write_text("echo: {{ input.text }}")
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["text"],
                "properties": {"text": {"type": "string", "minLength": 1}},
            }
        )
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["message"],
                "properties": {"message": {"type": "string"}},
            }
        )
    )
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        json.dumps({"input": {"text": "hi"}, "expected": {"message": "ok"}}) + "\n"
    )
    return agent_dir


# ---------------------------------------------------------------------------
# Item 1: interactive `mdk fix` handoff
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFixHandoff:
    def test_no_fix_prompt_flag_suppresses_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With --no-fix-prompt, the interactive prompt path is skipped
        even when fixable findings exist. CI-friendly."""
        monkeypatch.chdir(tmp_path)
        # Stub diagnose_and_fix to return one fixable result.
        with patch(
            "movate.fixes.registry.diagnose_and_fix",
            return_value=[
                FixResult(
                    fix_id="ensure-gitignore",
                    status=FixStatus.WOULD_APPLY,
                    message="would create .gitignore",
                ),
            ],
        ) as mock_fix:
            result = runner.invoke(
                app, ["doctor", "--no-fix-prompt"], env={"COLUMNS": "200"}
            )
            # Doctor itself succeeds (exit 0 in normal modes).
            assert result.exit_code == 0, result.stdout + result.stderr
            # The handoff message should fire but the prompt is suppressed.
            # We saw the fixable list in stdout.
            assert "ensure-gitignore" in result.stdout
            # diagnose_and_fix was called once for the probe (no apply).
            assert mock_fix.call_count >= 1
            # And it was always in dry-run mode.
            for call in mock_fix.call_args_list:
                assert call.kwargs.get("dry_run") is True

    def test_no_fixable_findings_means_no_handoff(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When fix has nothing to do, the handoff path is silent."""
        monkeypatch.chdir(tmp_path)
        with patch(
            "movate.fixes.registry.diagnose_and_fix",
            return_value=[
                FixResult(fix_id="x", status=FixStatus.NOT_NEEDED, message=""),
            ],
        ):
            result = runner.invoke(
                app, ["doctor", "--no-fix-prompt"], env={"COLUMNS": "200"}
            )
            assert result.exit_code == 0, result.stdout + result.stderr
            # No "mdk fix can auto-resolve" line.
            assert "auto-resolve" not in result.stdout

    def test_handoff_skipped_under_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--target invokes the Azure preflight; the fix handoff is a
        local-project concern and shouldn't fire in that context."""
        monkeypatch.chdir(tmp_path)
        with patch("movate.fixes.registry.diagnose_and_fix") as mock_fix:
            # Use a bogus target — the preflight will fail gracefully.
            result = runner.invoke(
                app, ["doctor", "--target", "nonexistent-target"], env={"COLUMNS": "200"}
            )
            # Whatever the exit code, the fix probe should NOT have been
            # called — --target gates the handoff entirely.
            mock_fix.assert_not_called()
            assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Item 2: `mdk doctor agent <name>` — resolution paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAgentDirResolution:
    def test_resolves_via_explicit_path(self, tmp_path: Path) -> None:
        proj = _bootstrap_project(tmp_path)
        agent_dir = _scaffold_test_agent(proj)
        # Passing the literal path should resolve directly.
        assert _resolve_agent_dir(str(agent_dir), None) == agent_dir.resolve()

    def test_resolves_via_explicit_project_root(self, tmp_path: Path) -> None:
        proj = _bootstrap_project(tmp_path)
        _scaffold_test_agent(proj, name="test-agent")
        # Passing the project root with name should resolve.
        resolved = _resolve_agent_dir("test-agent", proj)
        assert resolved == (proj / "agents" / "test-agent").resolve()

    def test_resolves_via_walk_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap_project(tmp_path)
        _scaffold_test_agent(proj, name="test-agent")
        # cd into a subdirectory and resolve by name only.
        nested = proj / "agents" / "test-agent" / "schema"
        monkeypatch.chdir(nested)
        resolved = _resolve_agent_dir("test-agent", None)
        assert resolved == (proj / "agents" / "test-agent").resolve()

    def test_unresolvable_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert _resolve_agent_dir("does-not-exist", None) is None


# ---------------------------------------------------------------------------
# Item 2: `mdk doctor agent <name>` — end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDoctorAgent:
    def test_agent_doctor_runs_all_checks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Healthy agent should produce a table with the expected check
        rows + a 0-error summary line."""
        proj = _bootstrap_project(tmp_path)
        _scaffold_test_agent(proj, name="happy-agent")
        monkeypatch.chdir(proj)
        result = runner.invoke(
            app, ["doctor", "agent", "happy-agent"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # Every check row label appears.
        for label in (
            "load",
            "prompt renders",
            "pricing",
            "skills resolve",
            "contexts",
            "dataset rows",
            "eval baseline",
        ):
            assert label in result.stdout, f"missing row: {label}"
        # Summary line — error count should be 0.
        assert "mdk_doctor_agent_summary:" in result.stdout
        assert "agent=happy-agent" in result.stdout
        assert "error=0" in result.stdout

    def test_unknown_agent_errors_with_pointer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app, ["doctor", "agent", "does-not-exist"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 2
        assert "could not resolve" in result.stderr.lower()

    def test_summary_line_counts_match_findings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Healthy agent → ok > 0, missing/error should be small. Use as
        a sanity check that the tally logic works."""
        proj = _bootstrap_project(tmp_path)
        _scaffold_test_agent(proj, name="counted-agent")
        monkeypatch.chdir(proj)
        result = runner.invoke(
            app, ["doctor", "agent", "counted-agent"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0
        # Pull the summary line and parse k=v pairs.
        line = next(
            line for line in result.stdout.splitlines() if "mdk_doctor_agent_summary:" in line
        )
        # Extract checks=N
        import re  # noqa: PLC0415

        m = re.search(r"checks=(\d+)", line)
        assert m is not None
        checks = int(m.group(1))
        # 8 distinct checks land in the table (load, prompt renders,
        # pricing, skills, contexts, dataset, eval baseline, last run).
        # Tolerate ± because last-run probe may be skipped on storage
        # init failure.
        assert checks >= 6
        assert checks <= 8

    def test_agent_doctor_flag_path_resolution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk doctor agent <path>` works with a literal path too."""
        proj = _bootstrap_project(tmp_path)
        agent_dir = _scaffold_test_agent(proj, name="path-agent")
        # Run from outside the project, pass the absolute path.
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["doctor", "agent", str(agent_dir)],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # Summary line uses the original arg, not the resolved path.
        assert f"agent={agent_dir}" in result.stdout


# ---------------------------------------------------------------------------
# Backwards compat: `mdk doctor` (no subcommand) still works
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_doctor_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calling `mdk doctor` with no subcommand must dispatch to the
    env-check callback — the existing flat-command behavior."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["doctor", "--no-fix-prompt"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # The env-check table renders.
    assert "movate doctor" in result.stdout
    # Greppable summary line still fires.
    assert "mdk_doctor_summary:" in result.stdout


@pytest.mark.unit
def test_default_doctor_with_existing_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-existing flags --explain and --licenses must work on the
    callback path."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["doctor", "--licenses"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # --licenses takes a different render path (license posture table).
    assert "license posture" in result.stdout.lower()
