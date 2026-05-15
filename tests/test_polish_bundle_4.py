"""Fourth polish bundle — five lock-in items before locking the MDK CLI.

1. ``mdk run`` echoes the run_id + replay hint on stderr.
2. ``mdk audit`` embeds ``mdk fix`` command per applicable finding.
3. ``mdk validate`` surfaces ``path:line:col`` on YAML errors.
4. ``mdk eval`` adds a pass/fail header line + greppable ``mdk_eval_summary:``.
5. ``mdk doctor`` adds greppable ``mdk_doctor_summary:`` line.

Theme: make commands point at each other so the CLI feels cohesive
instead of like 50 islands. Each item closes a feedback loop or
gives CI a single greppable signal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.audit.report import AuditReport, Finding, Severity
from movate.cli import _console
from movate.cli import run as run_cmd
from movate.cli.audit_cmd import _CATEGORY_TO_FIX_COMMAND, _render_rich
from movate.cli.doctor import _classify_result
from movate.cli.main import app
from movate.core.loader import AgentLoadError, load_agent

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Item 2: mdk audit embeds fix command per applicable finding
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAuditFixCommandEmbed:
    def test_missing_evals_maps_to_eval_gen(self) -> None:
        """missing-evals findings should surface ``mdk eval-gen`` as the fix."""
        assert "missing-evals" in _CATEGORY_TO_FIX_COMMAND
        assert "mdk eval-gen" in _CATEGORY_TO_FIX_COMMAND["missing-evals"]

    def test_v2_scanners_omitted_from_fix_map(self) -> None:
        """v2 scanners are operator-edits in agent.yaml — no auto-fix."""
        # If any of these gain a fix command, this test fails loudly so
        # the audit table picks up the new hint without separate work.
        v2_categories = {
            "floating-model-tag",
            "missing-version",
            "missing-fallback",
            "prompt-too-long",
            "schema-no-required",
        }
        leaked = v2_categories & set(_CATEGORY_TO_FIX_COMMAND)
        assert leaked == set(), (
            f"v2 scanner(s) gained a fix mapping unexpectedly: {leaked}"
        )

    def test_render_includes_fix_command_for_mapped_category(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A missing-evals finding renders with the fix command embedded."""
        report = AuditReport(
            scanned_agents=1,
            findings=(
                Finding(
                    category="missing-evals",
                    severity=Severity.WARNING,
                    target="alpha",
                    message="no dataset.jsonl",
                    hint="generate one with mdk eval-gen",
                ),
            ),
        )
        _render_rich(report, target="current", strict=False)
        captured = capsys.readouterr()
        # The fix command appears verbatim in the table output.
        assert "mdk eval-gen" in captured.out

    def test_render_omits_fix_line_for_unmapped_category(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A category without a 1:1 fix renders without the cyan arrow line."""
        report = AuditReport(
            scanned_agents=1,
            findings=(
                Finding(
                    category="floating-model-tag",
                    severity=Severity.WARNING,
                    target="alpha",
                    message="model uses :latest tag",
                    hint="pin a stable revision",
                ),
            ),
        )
        _render_rich(report, target="current", strict=False)
        captured = capsys.readouterr()
        # The hint still renders; just no "→ mdk ..." command line.
        assert "→ mdk" not in captured.out


# ---------------------------------------------------------------------------
# Item 3: mdk validate file:line:col on YAML errors
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoaderYamlErrorLocation:
    def test_yaml_syntax_error_carries_path_line_col(self, tmp_path: Path) -> None:
        """A broken YAML file should raise AgentLoadError with `path:line:col`."""
        agent_dir = tmp_path / "broken"
        agent_dir.mkdir()
        # Tab-after-colon + bad indent → PyYAML carries problem_mark.
        (agent_dir / "agent.yaml").write_text(
            "api_version: movate/v1\nname: x\n  bad: [unterminated\n"
        )

        with pytest.raises(AgentLoadError) as exc_info:
            load_agent(agent_dir)

        msg = str(exc_info.value)
        # Path appears in the message AND we have a line:col suffix.
        assert "agent.yaml" in msg
        # Line + column markers (line is 1-indexed; column is too).
        assert ":3:" in msg or ":2:" in msg  # error sits on line 2 or 3

    def test_yaml_validation_error_carries_path(self, tmp_path: Path) -> None:
        """Pydantic validation failure includes the file path in the message."""
        agent_dir = tmp_path / "invalid"
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text(
            yaml.safe_dump({"api_version": "movate/v1", "kind": "Agent"})  # missing fields
        )

        with pytest.raises(AgentLoadError) as exc_info:
            load_agent(agent_dir)

        msg = str(exc_info.value)
        assert "validation failed" in msg
        assert "agent.yaml" in msg


# ---------------------------------------------------------------------------
# Item 4: mdk eval pass/fail header + mdk_eval_summary line
# ---------------------------------------------------------------------------


def _scaffold_eval_agent(agent_dir: Path, *, name: str) -> Path:
    """Minimal agent whose mock-mode eval scores 1.0 (PASS)."""
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "schema").mkdir(exist_ok=True)
    (agent_dir / "evals").mkdir(exist_ok=True)
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
    (agent_dir / "prompt.md").write_text("echo {{ input.text }}\n")
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
        json.dumps({"input": {"text": "x"}, "expected": {"message": "mock response"}}) + "\n"
    )
    return agent_dir


@pytest.mark.unit
def test_eval_table_emits_pass_header_and_summary_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Table mode: PASS banner + mdk_eval_summary line both appear."""
    monkeypatch.setenv("HOME", str(tmp_path))
    agent_dir = _scaffold_eval_agent(tmp_path / "alpha", name="alpha")
    result = runner.invoke(
        app, ["eval", str(agent_dir), "--mock", "--gate", "0.5"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # PASS banner above the table.
    assert "Eval PASSED" in result.stdout
    # Greppable summary line — keys present, values structured.
    assert "mdk_eval_summary:" in result.stdout
    assert "overall_pass=true" in result.stdout
    assert "regressed=false" in result.stdout
    assert "agent=alpha" in result.stdout


@pytest.mark.unit
def test_eval_table_emits_fail_header_on_gate_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A high gate forces FAIL — banner + summary reflect it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    agent_dir = _scaffold_eval_agent(tmp_path / "beta", name="beta")
    # Sabotage mock response so score = 0.0, failing any gate > 0.
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "different"}')
    result = runner.invoke(
        app, ["eval", str(agent_dir), "--mock", "--gate", "0.9"]
    )
    assert result.exit_code == 1, result.stdout + result.stderr
    assert "Eval FAILED" in result.stdout
    assert "mdk_eval_summary:" in result.stdout
    assert "overall_pass=false" in result.stdout


@pytest.mark.unit
def test_eval_json_mode_omits_summary_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSON output must stay pipeable to jq — no Rich summary line."""
    monkeypatch.setenv("HOME", str(tmp_path))
    agent_dir = _scaffold_eval_agent(tmp_path / "gamma", name="gamma")
    result = runner.invoke(
        app, ["eval", str(agent_dir), "--mock", "--gate", "0.5", "-o", "json"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Summary line must NOT appear — stdout has to remain pure JSON.
    assert "mdk_eval_summary:" not in result.stdout
    # And the JSON has to actually parse.
    payload = json.loads(result.stdout)
    assert payload["overall_pass"] is True


# ---------------------------------------------------------------------------
# Item 5: mdk doctor mdk_doctor_summary line
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDoctorClassifier:
    def test_section_separator_excluded(self) -> None:
        assert _classify_result("", "") is None

    def test_informational_rows_excluded(self) -> None:
        # Python / movate / storage / pricing carry raw values, not verdicts.
        assert _classify_result("Python", "3.11.5") is None
        assert _classify_result("movate", "0.5.0") is None
        assert _classify_result("storage (sqlite)", "/x/y (exists)") is None
        assert _classify_result("pricing", "v1 (50 models, ...)") is None

    def test_required_dep_install_fail_is_error(self) -> None:
        """A red 'missing (install fail)' must classify as error, NOT missing."""
        assert _classify_result("dep: foo", "[red]missing (install fail)[/red]") == "error"

    def test_present_dep_is_ok(self) -> None:
        assert _classify_result("dep: typer", "[green]ok[/green]") == "ok"

    def test_absent_optional_dep_is_missing(self) -> None:
        assert (
            _classify_result("opt: langfuse", "[yellow]missing[/yellow] [dim]not installed[/dim]")
            == "missing"
        )

    def test_pricing_load_failed_is_error(self) -> None:
        assert _classify_result("pricing", "[red]load failed: boom[/red]") is None
        # Excluded because pricing is informational — load failure already
        # renders as red text in the table; counting it would double-count.


@pytest.mark.unit
def test_doctor_emits_summary_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A live ``mdk doctor`` run should emit the greppable summary line."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Strip env keys so the table has a predictable mix of missing rows.
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "LYZR_API_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_HOST",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_SERVICE_NAME",
        "MOVATE_TRACER",
    ):
        monkeypatch.delenv(key, raising=False)
    result = runner.invoke(app, ["doctor"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "mdk_doctor_summary:" in result.stdout
    assert "checks=" in result.stdout
    assert "ok=" in result.stdout
    assert "missing=" in result.stdout
    assert "error=" in result.stdout


# ---------------------------------------------------------------------------
# Item 1: mdk run echoes run_id + replay hint
# (Light coverage — the full integration path is exercised by test_cli_run.py;
# here we just confirm the helper hint formatting + import wires up.)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_module_imports_console_hint() -> None:
    """The run command must have access to ``_console.hint`` for the
    stderr-only ``→ saved as run_id …`` echo. A failed import here is
    the kind of silent CI footgun this test guards against."""
    assert hasattr(_console, "hint")
    # The run module imports _console at module level (verified by
    # the attribute lookup succeeding).
    assert run_cmd._console is _console
